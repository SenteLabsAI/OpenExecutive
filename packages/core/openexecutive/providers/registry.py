"""Model registry + per-call provider routing.

This module is the single source of truth for: what we ship to the
Council UI (``allowed_models``), how a Claude model name maps to its
OpenRouter slug (``openrouter_slug_for_claude``), and which Anthropic-only
features each model tolerates. ``get_provider(model)`` picks the backend
per call so the user can flip an agent's model in the Council UI and have
requests for that agent — and only that agent — route differently.

The non-Anthropic OpenRouter set is no longer a hand-maintained constant:
``openrouter_models()`` serves the live catalog cached by
``providers.openrouter_catalog`` (fetched at API startup) and falls back to
the hardcoded ``OPENROUTER_MODELS`` snapshot when nothing has been loaded.
"""
from __future__ import annotations

import re
from typing import Any

from fastapi import HTTPException

from openexecutive.config import get_settings
from openexecutive.providers import openrouter_catalog
from openexecutive.providers.anthropic_provider import AnthropicProvider
from openexecutive.providers.feature_gate import FeatureSpec
from openexecutive.providers.openai_compatible import OpenAICompatibleProvider
from openexecutive.providers.openrouter_provider import OpenRouterProvider
from openexecutive.providers.provider import LLMProvider

# Anthropic-direct slugs — used as canonical model names everywhere in
# the codebase (config defaults, agent class defaults, override DB).
ANTHROPIC_DIRECT_MODELS: list[str] = [
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-haiku-4-5",
]

# Hardcoded FALLBACK for the non-Anthropic OpenRouter set. The live list
# comes from ``providers.openrouter_catalog`` (fetched from OpenRouter's
# public /models endpoint at API startup); this snapshot is served only
# when that fetch has not succeeded (disabled, offline, CLI paths that skip
# the lifespan). These strings ARE OpenRouter slugs — we don't translate
# them on the way through. Curated PAID models only: the rate-limited
# ``:free`` tier was removed because its 429s surfaced as user-visible
# errors. Refresh this snapshot occasionally from a successful catalog
# load; it is not the primary source anymore.
OPENROUTER_MODELS: list[str] = [
    "openai/gpt-6-astra",
    "openai/gpt-6-astra-pro",
    "openai/gpt-5.6-terra",
    "openai/gpt-5.6-luna",
    "google/gemini-3.8-flash",
    "google/gemini-3.5-flash-lite",
    "meta-llama/llama-4-maverick",
    "meta-llama/llama-4-scout",
    "deepseek/deepseek-v4-pro",
    "deepseek/deepseek-v4-flash",
    "x-ai/grok-4.6",
    "x-ai/grok-4.5",
]


# Anthropic model ids look like ``claude-<family>-<major>[-<minor>][-<yyyymmdd>]``
# (``claude-opus-5``, ``claude-haiku-4-5``, or a dated pin such as
# ``claude-haiku-4-5-20251001`` / ``claude-opus-5-20260315``).
# OpenRouter's slug for the same model is ``anthropic/claude-<family>-<major>[.<minor>]``
# with the date snapshot dropped. Deriving the slug mechanically means any
# Claude id — including older ones still stored in agent overrides, and
# newer ones set via env before this file is touched — routes correctly.
# ``minor`` is capped at 3 digits so an 8-digit date snapshot following a
# major-only id (``claude-opus-5-20260315``) can't be swallowed as the minor
# version — that would derive ``anthropic/claude-opus-5.20260315``.
_CLAUDE_ID_RE = re.compile(
    r"^claude-(?P<family>[a-z]+)-(?P<major>\d{1,3})(?:-(?P<minor>\d{1,3}))?(?:-\d{8})?$"
)
# Slugs on the OpenRouter side that are Claude models (either derived from an
# Anthropic id above, or listed verbatim by the live catalog).
_OPENROUTER_CLAUDE_PREFIX = "anthropic/claude-"
_HAIKU_FAMILY = "haiku"


def model_supports_deep_reasoning(model: str) -> bool:
    """Whether the "Deep reasoning" toggle may add thinking fields for ``model``.

    Haiku is the one Claude family that rejects adaptive thinking and the
    ``output_config.effort`` field outright (HTTP 400 from Anthropic), so it
    is excluded whether addressed by its Anthropic id or its OpenRouter slug,
    in current or legacy naming. The check is scoped to Claude names, so an
    unrelated slug that merely contains "haiku" keeps deep reasoning. Everything else is allowed
    here; the provider feature gate then drops the fields for models that
    can't actually reason, so a wrong guess costs nothing. Mirrors the
    Council UI guard.
    """
    lowered = model.lower()
    is_claude_name = lowered.startswith("claude-") or lowered.startswith(
        _OPENROUTER_CLAUDE_PREFIX
    )
    # Scoped substring: only a Claude id / slug can be Haiku, but within that
    # namespace the family may sit first (``claude-haiku-4-5``,
    # ``anthropic/claude-haiku-4.5``) or last (legacy
    # ``claude-3-5-haiku-20241022``, ``anthropic/claude-3.5-haiku``).
    return not (is_claude_name and _HAIKU_FAMILY in lowered)


def openrouter_slug_for_claude(model: str) -> str | None:
    """``claude-sonnet-4-6`` → ``anthropic/claude-sonnet-4.6``; None if not a Claude id."""
    m = _CLAUDE_ID_RE.match(model)
    if m is None:
        return None
    version = m.group("major")
    if m.group("minor") is not None:
        version = f"{version}.{m.group('minor')}"
    return f"anthropic/claude-{m.group('family')}-{version}"


# Retained as a derived view for readers of the old constant. The registry
# itself no longer consults it — ``openrouter_slug_for_claude`` is the rule.
_CLAUDE_OPENROUTER_SLUGS: dict[str, str] = {
    m: slug
    for m in ANTHROPIC_DIRECT_MODELS
    if (slug := openrouter_slug_for_claude(m)) is not None
}


_CLAUDE_FEATURE_SPEC = FeatureSpec(
    supports_cache_control=True,
    supports_thinking=True,
    supports_web_search=True,
    supports_tool_use=True,
)


# Per-non-Claude model spec. ``supports_tool_use`` is universal across
# the curated set; the other three are off — Anthropic-specific server
# tools and prompt-caching annotations have no OpenAI-format equivalent.
_DEFAULT_NON_CLAUDE_SPEC = FeatureSpec(
    supports_cache_control=False,
    supports_thinking=False,
    supports_web_search=False,
    supports_tool_use=True,
)

# Non-Claude model whose live catalog entry advertises ``reasoning``
# support: keep the Anthropic ``thinking`` / ``output_config`` fields so the
# translator can turn them into OpenRouter's ``reasoning`` parameter. Cache
# and web-search stay off — those really are Anthropic-only.
_NON_CLAUDE_REASONING_SPEC = FeatureSpec(
    supports_cache_control=False,
    supports_thinking=True,
    supports_web_search=False,
    supports_tool_use=True,
)


def _local_models(settings: Any) -> list[str]:
    """Configured local model slugs, or ``[]`` when local routing is off.

    Read defensively: lightweight test settings stubs may omit the field.
    """
    if not getattr(settings, "local_models_enabled", False):
        return []
    return list(getattr(settings, "local_models", []) or [])


def openrouter_models() -> list[str]:
    """Non-Anthropic-direct slugs offered when ``OPENROUTER_ENABLED`` is on.

    Live catalog when one has been loaded, else the hardcoded fallback.
    Catalog entries that duplicate an Anthropic-direct model (the same
    Claude model under its OpenRouter slug) are dropped so the dropdown
    doesn't list ``claude-opus-5`` twice; Claude models that exist ONLY on
    OpenRouter (e.g. an older or newer generation) stay in.
    """
    loaded = openrouter_catalog.loaded_models()
    if loaded is None:
        return list(OPENROUTER_MODELS)
    direct_slugs = set(_CLAUDE_OPENROUTER_SLUGS.values())
    return [m for m in loaded if m not in direct_slugs]


def allowed_models() -> list[str]:
    """Flat allowlist the Council UI's dropdown reads.

    The dropdown can't offer a model the runtime won't actually serve, so
    each family is folded in only when it's reachable:

    * Anthropic-direct trio — when an ``ANTHROPIC_API_KEY`` is set, OR when
      ``OPENROUTER_ENABLED`` is on (Claude is then reachable via OpenRouter).
    * OpenRouter set (live catalog or fallback) — when ``OPENROUTER_ENABLED``
      is on.
    * Local models — when ``LOCAL_MODELS_ENABLED`` is on.
    """
    settings = get_settings()
    models: list[str] = []
    if getattr(settings, "anthropic_api_key", None) or settings.openrouter_enabled:
        models.extend(ANTHROPIC_DIRECT_MODELS)
    if settings.openrouter_enabled:
        models.extend(openrouter_models())
    models.extend(_local_models(settings))
    return models


def allowed_models_for(agent_id: str | None) -> list[str]:
    """Per-agent allowlist for the Council UI dropdown and PATCH validator.

    Every agent — specialists, the Executive, Quality Judge, and the
    ``utility_fast`` virtual agent — gets the same ``allowed_models()``
    list. (The ``utility_fast``-only free/cheap OpenRouter matrix was
    removed; ``agent_id`` is retained for call-site stability and any
    future per-agent rules.)
    """
    return allowed_models()


def _is_claude(model: str) -> bool:
    """Any Anthropic-direct Claude id, not just the current trio.

    Agent overrides persisted in SQLite may still name a previous
    generation (``claude-opus-4-7``); those must keep routing to the
    Anthropic SDK rather than falling through to the non-Claude path.
    """
    return _CLAUDE_ID_RE.match(model) is not None


def _openrouter_model_resolver(model: str) -> tuple[str, FeatureSpec] | None:
    """Slug + feature spec for a model on the OpenRouter path.

    * Anthropic id (``claude-sonnet-5``) → derived OpenRouter slug, Claude spec.
    * Verbatim Claude slug from the catalog (``anthropic/claude-opus-4.8``) →
      unchanged, Claude spec — OpenRouter forwards cache_control / thinking
      to Anthropic for these, so stripping them would only cost money.
    * Non-Claude slug the catalog marks reasoning-capable → unchanged,
      thinking kept (translated to OpenRouter ``reasoning``), rest stripped.
    * Anything else → None; the provider applies the non-Claude default.
    """
    slug = openrouter_slug_for_claude(model)
    if slug is not None:
        return slug, _CLAUDE_FEATURE_SPEC
    if model.startswith(_OPENROUTER_CLAUDE_PREFIX):
        return model, _CLAUDE_FEATURE_SPEC
    # Non-Claude: the live catalog knows whether OpenRouter will honour a
    # ``reasoning`` request for this slug. Unknown (fallback list, or a slug
    # outside the catalog) keeps the conservative thinking-off default.
    if openrouter_catalog.supports_reasoning(model):
        return model, _NON_CLAUDE_REASONING_SPEC
    return None


# Module-level singletons — providers pool their own HTTP connections and
# are async-safe. Recreating them per call burns ~10 ms each.
_anthropic_provider: AnthropicProvider | None = None
_openrouter_provider: OpenRouterProvider | None = None
_local_provider: OpenAICompatibleProvider | None = None


def _anthropic() -> AnthropicProvider:
    global _anthropic_provider
    if _anthropic_provider is None:
        settings = get_settings()
        api_key = settings.anthropic_api_key
        if not api_key:
            # Reachable only when a Claude model is requested with no key and
            # OpenRouter off — e.g. an Anthropic-free deployment that left a
            # model setting pointed at Claude. Fail with actionable guidance.
            raise HTTPException(
                status_code=400,
                detail=(
                    "A Claude model was requested but ANTHROPIC_API_KEY is not "
                    "set. Set it, enable OpenRouter, or point the model setting "
                    "at a configured local model."
                ),
            )
        _anthropic_provider = AnthropicProvider(api_key=api_key)
    return _anthropic_provider


def _local() -> OpenAICompatibleProvider:
    global _local_provider
    if _local_provider is None:
        settings = get_settings()
        base_url = getattr(settings, "local_base_url", None)
        if not base_url:
            # The Settings model_validator already prevents LOCAL_MODELS_ENABLED
            # without a base URL, but defense in depth — a misconfigured env
            # could otherwise produce a request against an empty host.
            raise HTTPException(
                status_code=400,
                detail="Local model routing requires LOCAL_BASE_URL",
            )
        # Local models get the non-Claude feature spec: no cache_control,
        # thinking, or server-side web_search — those are Anthropic-only and
        # would 400 (or be silently ignored) on an OpenAI-compatible server.
        spec_lookup: dict[str, FeatureSpec] = {
            m: _DEFAULT_NON_CLAUDE_SPEC for m in _local_models(settings)
        }
        _local_provider = OpenAICompatibleProvider(
            base_url=base_url,
            api_key=getattr(settings, "local_api_key", None),
            timeout_s=getattr(settings, "local_timeout_s", 300.0),
            spec_lookup=spec_lookup,
        )
    return _local_provider


def _openrouter() -> OpenRouterProvider:
    global _openrouter_provider
    if _openrouter_provider is None:
        settings = get_settings()
        if not settings.openrouter_api_key:
            # The Settings model_validator already prevents OPENROUTER_ENABLED
            # without a key, but defense in depth — a misconfigured env could
            # otherwise produce a None-token request.
            raise HTTPException(
                status_code=400,
                detail="OpenRouter routing requires OPENROUTER_API_KEY",
            )
        # Claude ids and catalog Claude slugs resolve through the derivation
        # rule; every other slug (curated fallback or live catalog) gets the
        # provider's non-Claude default spec, so a catalog refresh after
        # construction needs no rebuild.
        _openrouter_provider = OpenRouterProvider(
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
            app_title=settings.openrouter_app_title,
            referer=settings.openrouter_referer,
            timeout_s=settings.openrouter_timeout_s,
            model_resolver=_openrouter_model_resolver,
        )
    return _openrouter_provider


def get_provider(model: str) -> LLMProvider:
    """Return the provider that should serve calls for ``model``.

    Routing rules (in precedence order):

    * Local models (slugs listed in ``LOCAL_MODELS`` with
      ``LOCAL_MODELS_ENABLED`` on) — the self-hosted OpenAI-compatible
      backend at ``LOCAL_BASE_URL``. Always wins for its configured slugs.
    * Claude family (any ``claude-<family>-<version>`` id) — Anthropic
      direct by default; OpenRouter when ``OPENROUTER_ENABLED`` is on.
    * Other non-Claude (anything from ``openrouter_models()``, or any
      unknown slug) — OpenRouter only. Raises HTTP 400 when ``OPENROUTER_ENABLED``
      is off, since we have no other backend that speaks those models.
    """
    settings = get_settings()
    # Explicit operator config wins: a slug listed in LOCAL_MODELS never
    # leaves the local backend, even if it happens to look like a Claude id
    # (``claude-proxy-1`` on an on-prem gateway). Checked BEFORE the Claude
    # regex so widening ``_is_claude`` can't silently re-route local traffic
    # to a hosted vendor.
    if model in _local_models(settings):
        return _local()
    if _is_claude(model):
        if settings.openrouter_enabled:
            return _openrouter()
        return _anthropic()
    # Other non-Claude slugs require OpenRouter to be enabled.
    if not settings.openrouter_enabled:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Model {model!r} requires OPENROUTER_ENABLED=true (set "
                f"OPENROUTER_API_KEY and toggle the flag), or list it in "
                f"LOCAL_MODELS with LOCAL_MODELS_ENABLED=true to serve it "
                f"from a local OpenAI-compatible backend."
            ),
        )
    return _openrouter()


def _reset_for_tests() -> None:
    """Drop cached provider singletons. Test-only — pytest fixtures call this."""
    global _anthropic_provider, _openrouter_provider, _local_provider
    _anthropic_provider = None
    _openrouter_provider = None
    _local_provider = None
