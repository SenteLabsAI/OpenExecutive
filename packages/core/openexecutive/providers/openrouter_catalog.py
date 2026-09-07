"""Live OpenRouter model catalog for the Council UI dropdown.

OpenRouter publishes its full model list at ``GET /api/v1/models`` (no API
key required). Instead of hand-maintaining the non-Anthropic slug list in
``registry.OPENROUTER_MODELS`` — which goes stale every time a vendor ships
a model — the API fetches that catalog once at startup (and re-fetches on a
slow timer), filters it down to a dropdown-sized set, and caches the result
in this module. ``registry.openrouter_models()`` reads the cache and falls
back to the hardcoded list whenever nothing has been loaded, so a failed or
disabled fetch degrades to exactly the pre-existing behaviour.

Selection rules (``select_models``), all env-tunable via ``Settings``:

* provider allowlist — ``id`` prefix before the ``/`` must be listed;
* tool calling required — every agent path issues tool_use, so a model
  without ``"tools"`` in ``supported_parameters`` would fail on first turn;
* text output only — image / audio generators are not chat models;
* slug must match ``<vendor>/<name>`` in ``[A-Za-z0-9._-]`` — which also
  excludes every ``:variant`` suffix (``:free`` 429s under load, ``:batch``
  is async, ``:thinking`` / ``:online`` change request semantics);
* paid only — a zero-priced entry is the rate-limited free tier by another
  name;
* newest ``per_provider`` entries per provider by ``created``, so the
  dropdown tracks the current generation instead of listing 50 GPT slugs.

Nothing here imports ``registry`` (which imports this module) — the cache
is plain module state so the two can't form an import cycle.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Iterable, Mapping
from typing import Any

import httpx

from openexecutive.config import _DEFAULT_OPENROUTER_CATALOG_PROVIDERS

logger = logging.getLogger(__name__)

# Providers surfaced when OPENROUTER_CATALOG_PROVIDERS is unset. Matches the
# vendor set the original hardcoded list covered.
DEFAULT_CATALOG_PROVIDERS: tuple[str, ...] = _DEFAULT_OPENROUTER_CATALOG_PROVIDERS
DEFAULT_PER_PROVIDER = 6

# Hard cap on the /models response body. The real payload is well under
# 1 MB; anything near this is not a catalog. Bounds memory at boot.
MAX_CATALOG_BYTES = 16 * 1024 * 1024
# Floor on the background re-fetch cadence so a mis-set env var can't turn
# into a tight loop against OpenRouter's public endpoint.
MIN_REFRESH_INTERVAL_S = 60.0
# Slugs are used as identifiers downstream (allowlist, SQLite override
# column, outbound request body). Accept only the shape OpenRouter actually
# emits; anything else from the remote catalog is dropped, not sanitised.
_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}/[A-Za-z0-9._-]{1,96}$")

# Module-level cache. ``None`` means "never loaded" — the registry then
# serves its hardcoded fallback. An empty list is a *loaded* result (the
# filter matched nothing) and is served as-is so a misconfigured allowlist
# is visible rather than silently masked by the fallback.
_loaded_models: list[str] | None = None
# Subset of ``_loaded_models`` whose catalog entry advertises "reasoning" in
# ``supported_parameters`` — i.e. OpenRouter will honour a ``reasoning``
# request field for them. Drives the Council "Deep reasoning" checkbox for
# non-Claude models.
_loaded_reasoning: frozenset[str] = frozenset()
_loaded_at: float | None = None


def loaded_models() -> list[str] | None:
    """Slugs from the last successful fetch, or ``None`` if none succeeded."""
    return None if _loaded_models is None else list(_loaded_models)


def supports_reasoning(model_id: str) -> bool | None:
    """Whether the loaded catalog says ``model_id`` accepts ``reasoning``.

    ``None`` when no catalog is loaded or the slug isn't in it — callers
    treat that as "unknown" and keep their conservative default.
    """
    if _loaded_models is None or model_id not in _loaded_models:
        return None
    return model_id in _loaded_reasoning


def reasoning_capable_ids(entries: Iterable[Mapping[str, Any]]) -> frozenset[str]:
    """Ids of catalog entries that list "reasoning" in ``supported_parameters``."""
    out: set[str] = set()
    for entry in entries:
        model_id = entry.get("id")
        if isinstance(model_id, str) and _advertises(entry, "reasoning"):
            out.add(model_id)
    return frozenset(out)


def loaded_at() -> float | None:
    """``time.time()`` of the last successful fetch, for diagnostics."""
    return _loaded_at


def _advertises(entry: Mapping[str, Any], parameter: str) -> bool:
    """True iff ``supported_parameters`` is a list containing ``parameter``.

    Strict on type: a string value would make ``in`` a substring test and
    a dict a key test — remote data must not flip a capability that way.
    """
    params = entry.get("supported_parameters")
    return isinstance(params, list) and parameter in params


def _price(entry: Mapping[str, Any], key: str) -> float:
    pricing = entry.get("pricing")
    if not isinstance(pricing, Mapping):
        return 0.0
    try:
        return float(pricing.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _is_candidate(entry: Mapping[str, Any], providers: frozenset[str]) -> bool:
    model_id = entry.get("id")
    if not isinstance(model_id, str) or _SLUG_RE.match(model_id) is None:
        return False
    provider, _, _rest = model_id.partition("/")
    if provider not in providers:
        return False
    if not _advertises(entry, "tools"):
        return False
    arch = entry.get("architecture")
    outputs = arch.get("output_modalities") if isinstance(arch, Mapping) else None
    if list(outputs or []) != ["text"]:
        return False
    # Paid only. OpenRouter reports the router pseudo-models with -1.
    return _price(entry, "prompt") > 0 or _price(entry, "completion") > 0


def select_models(
    entries: Iterable[Mapping[str, Any]],
    *,
    providers: Iterable[str] = DEFAULT_CATALOG_PROVIDERS,
    per_provider: int = DEFAULT_PER_PROVIDER,
) -> list[str]:
    """Pure filter over raw catalog entries → ordered dropdown slugs.

    Output is grouped in ``providers`` order, newest-first within each
    group, capped at ``per_provider`` per group. ``per_provider <= 0`` means
    no cap.
    """
    # Ordered + de-duplicated: a repeated vendor in OPENROUTER_CATALOG_PROVIDERS
    # must not append that vendor's models twice.
    ordered_providers = list(dict.fromkeys(p.strip() for p in providers if p and p.strip()))
    allow = frozenset(ordered_providers)
    by_provider: dict[str, list[tuple[float, str]]] = {p: [] for p in ordered_providers}
    seen: set[str] = set()
    for entry in entries:
        if not _is_candidate(entry, allow):
            continue
        model_id = str(entry["id"])
        if model_id in seen:
            continue
        seen.add(model_id)
        created = entry.get("created") or 0
        try:
            created_f = float(created)
        except (TypeError, ValueError):
            created_f = 0.0
        by_provider[model_id.partition("/")[0]].append((created_f, model_id))

    selected: list[str] = []
    for provider in ordered_providers:
        rows = sorted(by_provider[provider], key=lambda r: (-r[0], r[1]))
        if per_provider > 0:
            rows = rows[:per_provider]
        selected.extend(model_id for _, model_id in rows)
    return selected


async def fetch_catalog(
    *,
    base_url: str,
    timeout_s: float,
    client: httpx.AsyncClient | None = None,
    max_bytes: int = MAX_CATALOG_BYTES,
) -> list[dict[str, Any]]:
    """GET ``{base_url}/models`` and return the raw ``data`` list.

    Bounded two ways: ``timeout_s`` is a *total* deadline (httpx's own
    timeout is per-read, so a trickling server would otherwise hold the
    caller open indefinitely), and the body is streamed with a ``max_bytes``
    cap. Raises on any transport / HTTP / size / shape error — callers
    decide whether that means "keep the previous cache" or "use the
    fallback".
    """
    url = base_url.rstrip("/") + "/models"
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=timeout_s)

    async def _get() -> bytes:
        buf = bytearray()
        async with http.stream("GET", url) as resp:
            resp.raise_for_status()
            async for chunk in resp.aiter_bytes():
                buf += chunk
                if len(buf) > max_bytes:
                    raise ValueError(
                        f"OpenRouter /models response exceeds {max_bytes} bytes"
                    )
        return bytes(buf)

    try:
        raw = await asyncio.wait_for(_get(), timeout=timeout_s)
    finally:
        if owns_client:
            await http.aclose()
    payload = json.loads(raw)
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise ValueError("OpenRouter /models response has no 'data' list")
    return [d for d in data if isinstance(d, dict)]


async def refresh_openrouter_catalog(settings: Any) -> bool:
    """Fetch + filter + cache. Returns True on success.

    Never raises: a failure logs a warning and leaves the previous cache
    (or the never-loaded state → registry fallback) untouched.
    """
    global _loaded_models, _loaded_reasoning, _loaded_at
    try:
        entries = await fetch_catalog(
            base_url=settings.openrouter_base_url,
            timeout_s=settings.openrouter_catalog_timeout_s,
        )
        models = select_models(
            entries,
            providers=settings.openrouter_catalog_providers,
            per_provider=settings.openrouter_catalog_per_provider,
        )
    except Exception as exc:  # noqa: BLE001 — degrade, never crash startup
        logger.warning(
            "OpenRouter catalog fetch failed (%s: %s); Council dropdown keeps "
            "%s",
            type(exc).__name__,
            exc,
            "the previous catalog" if _loaded_models is not None else "the built-in fallback list",
        )
        return False
    _loaded_models = models
    _loaded_reasoning = reasoning_capable_ids(entries) & frozenset(models)
    _loaded_at = time.time()
    logger.info(
        "OpenRouter catalog loaded: %d model(s) from %d catalog entries, "
        "%d with reasoning support (providers=%s, per_provider=%d)",
        len(models),
        len(entries),
        len(_loaded_reasoning),
        ",".join(settings.openrouter_catalog_providers),
        settings.openrouter_catalog_per_provider,
    )
    return True


async def run_catalog_refresher(settings: Any) -> None:
    """Background loop: re-fetch every ``openrouter_catalog_refresh_s``.

    Started by the API lifespan after the initial fetch so a long-lived
    process picks up newly released models without a restart. Cancelled
    on shutdown.
    """
    interval = float(settings.openrouter_catalog_refresh_s)
    if interval <= 0:
        return
    interval = max(interval, MIN_REFRESH_INTERVAL_S)
    while True:
        await asyncio.sleep(interval)
        # refresh_openrouter_catalog swallows Exception but not
        # CancelledError (a BaseException), so shutdown still cancels us
        # cleanly mid-fetch.
        await refresh_openrouter_catalog(settings)


def _reset_for_tests() -> None:
    global _loaded_models, _loaded_reasoning, _loaded_at
    _loaded_models = None
    _loaded_reasoning = frozenset()
    _loaded_at = None
