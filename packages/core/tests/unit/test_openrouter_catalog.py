"""Live OpenRouter catalog → Council dropdown, with hardcoded fallback.

Pins:

1. ``select_models`` filtering: provider allowlist, tool-calling required,
   text output only, no ``:variant`` suffixes, paid only, newest-N per
   provider in allowlist order.
2. ``registry.openrouter_models()`` serves the fallback until a catalog is
   loaded, then the catalog — minus slugs that duplicate the Anthropic-direct
   trio.
3. ``refresh_openrouter_catalog`` never raises: transport / HTTP / shape
   errors keep the previous state and return False.
4. Claude id → OpenRouter slug derivation, and the routing rule that any
   Claude id (not just the current trio) still resolves to the Anthropic
   backend.
5. The OpenRouter provider hands catalog Claude slugs the Claude feature
   spec and everything else the non-Claude default.
"""
from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-used")

import httpx  # noqa: E402
import pytest  # noqa: E402

from openexecutive.providers import openrouter_catalog as catalog  # noqa: E402
from openexecutive.providers import registry as registry_mod  # noqa: E402
from openexecutive.providers.registry import (  # noqa: E402
    ANTHROPIC_DIRECT_MODELS,
    OPENROUTER_MODELS,
    get_provider,
    openrouter_models,
    openrouter_slug_for_claude,
)


@pytest.fixture(autouse=True)
def _reset() -> Any:
    catalog._reset_for_tests()
    registry_mod._reset_for_tests()
    yield
    catalog._reset_for_tests()
    registry_mod._reset_for_tests()


def _entry(
    model_id: str,
    *,
    created: int = 1_700_000_000,
    tools: bool = True,
    prompt_price: str = "0.000001",
    completion_price: str = "0.000002",
    outputs: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": model_id,
        "created": created,
        "supported_parameters": ["tools", "temperature"] if tools else ["temperature"],
        "pricing": {"prompt": prompt_price, "completion": completion_price},
        "architecture": {"output_modalities": outputs if outputs is not None else ["text"]},
    }


def _catalog_settings(**overrides: Any) -> Any:
    base = dict(
        openrouter_base_url="https://openrouter.example/api/v1",
        openrouter_catalog_timeout_s=1.0,
        openrouter_catalog_providers=["openai", "anthropic"],
        openrouter_catalog_per_provider=6,
        openrouter_catalog_refresh_s=0.0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _registry_settings(*, enabled: bool) -> Any:
    return SimpleNamespace(
        anthropic_api_key="sk-test",
        openrouter_enabled=enabled,
        openrouter_api_key="sk-or-test",
        openrouter_base_url="https://openrouter.example/api/v1",
        openrouter_app_title="Open Executive",
        openrouter_referer=None,
        openrouter_timeout_s=180.0,
        local_models_enabled=False,
        local_base_url=None,
        local_models=[],
        local_api_key=None,
        local_timeout_s=300.0,
    )


# --------------------------------------------------------------------------
# select_models
# --------------------------------------------------------------------------


def test_select_models_applies_every_filter() -> None:
    entries = [
        _entry("openai/gpt-6", created=10),
        _entry("openai/gpt-6:free", created=11, prompt_price="0", completion_price="0"),
        _entry("openai/gpt-6:batch", created=12),
        _entry("openai/gpt-6-notools", created=13, tools=False),
        _entry("openai/gpt-image", created=14, outputs=["image", "text"]),
        _entry("openai/gpt-zero-priced", created=15, prompt_price="0", completion_price="0"),
        _entry("openrouter/auto", created=16, prompt_price="-1", completion_price="-1"),
        _entry("mistralai/mistral-large", created=17),  # provider not allowlisted
        _entry("bare-slug-no-provider", created=18),
        _entry("anthropic/claude-opus-5", created=19),
        # Remote text that isn't a well-formed slug is dropped, not sanitised.
        _entry("openai/gpt with space", created=20),
        _entry("openai/gpt-6\nX-Injected: 1", created=21),
        _entry("openai/../gpt-6", created=22),
        _entry("openai/" + "x" * 97, created=23),
    ]
    assert catalog.select_models(entries, providers=["openai", "anthropic"]) == [
        "openai/gpt-6",
        "anthropic/claude-opus-5",
    ]


def test_select_models_orders_by_provider_then_newest_and_caps() -> None:
    entries = [
        _entry("google/gemini-old", created=1),
        _entry("openai/gpt-a", created=5),
        _entry("openai/gpt-c", created=7),
        _entry("openai/gpt-b", created=6),
        _entry("google/gemini-new", created=9),
    ]
    out = catalog.select_models(entries, providers=["openai", "google"], per_provider=2)
    # openai group first (allowlist order), newest-first within it, capped at 2.
    assert out == ["openai/gpt-c", "openai/gpt-b", "google/gemini-new", "google/gemini-old"]
    # 0 disables the cap.
    assert len(catalog.select_models(entries, providers=["openai"], per_provider=0)) == 3
    # A repeated vendor in the allowlist doesn't duplicate its models.
    assert catalog.select_models(entries, providers=["openai", "openai"], per_provider=0) == [
        "openai/gpt-c", "openai/gpt-b", "openai/gpt-a",
    ]


def test_select_models_dedups_and_tolerates_bad_entries() -> None:
    entries: list[Any] = [
        _entry("openai/gpt-6", created=1),
        _entry("openai/gpt-6", created=2),
        {"id": None},
        {"id": "openai/", "supported_parameters": ["tools"]},
        {"id": "openai/gpt-broken", "supported_parameters": ["tools"], "pricing": "n/a",
         "architecture": {"output_modalities": ["text"]}},
    ]
    assert catalog.select_models(entries, providers=["openai"]) == ["openai/gpt-6"]


# --------------------------------------------------------------------------
# registry.openrouter_models — fallback vs. loaded catalog
# --------------------------------------------------------------------------


def test_openrouter_models_serves_fallback_until_loaded() -> None:
    assert catalog.loaded_models() is None
    assert openrouter_models() == OPENROUTER_MODELS
    # A copy — callers can't mutate the module constant through it.
    assert openrouter_models() is not OPENROUTER_MODELS


def test_openrouter_models_serves_catalog_minus_direct_claude_duplicates() -> None:
    catalog._loaded_models = [
        "openai/gpt-6",
        "anthropic/claude-opus-5",  # same model as claude-opus-5 → dropped
        "anthropic/claude-opus-4.8",  # OpenRouter-only generation → kept
    ]
    assert openrouter_models() == ["openai/gpt-6", "anthropic/claude-opus-4.8"]


def test_loaded_empty_catalog_is_served_not_masked_by_fallback() -> None:
    """An allowlist that matches nothing is a visible misconfiguration, not
    silently replaced by the hardcoded list."""
    catalog._loaded_models = []
    assert openrouter_models() == []


def test_allowed_models_folds_catalog_in_only_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.providers import allowed_models

    catalog._loaded_models = ["openai/gpt-6"]
    monkeypatch.setattr(
        "openexecutive.providers.registry.get_settings",
        lambda: _registry_settings(enabled=False),
    )
    assert allowed_models() == ANTHROPIC_DIRECT_MODELS
    monkeypatch.setattr(
        "openexecutive.providers.registry.get_settings",
        lambda: _registry_settings(enabled=True),
    )
    assert allowed_models() == [*ANTHROPIC_DIRECT_MODELS, "openai/gpt-6"]


# --------------------------------------------------------------------------
# refresh_openrouter_catalog — fetch + degrade
# --------------------------------------------------------------------------


def _mock_transport(handler: Any) -> None:
    """Route every httpx.AsyncClient the module builds through ``handler``."""
    real_client = httpx.AsyncClient

    def _factory(**kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    catalog.httpx.AsyncClient = _factory  # type: ignore[misc]


@pytest.fixture
def restore_httpx() -> Any:
    real = catalog.httpx.AsyncClient
    yield
    catalog.httpx.AsyncClient = real  # type: ignore[misc]


@pytest.mark.usefixtures("restore_httpx")
async def test_refresh_loads_filtered_catalog() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(
            200,
            json={"data": [
                _entry("openai/gpt-6", created=2),
                _entry("openai/gpt-5:free", prompt_price="0", completion_price="0"),
                _entry("anthropic/claude-sonnet-5", created=1),
                _entry("qwen/qwen-x", created=3),  # not allowlisted
            ]},
        )

    _mock_transport(handler)
    ok = await catalog.refresh_openrouter_catalog(_catalog_settings())
    assert ok is True
    assert seen["url"] == "https://openrouter.example/api/v1/models"
    assert catalog.loaded_models() == ["openai/gpt-6", "anthropic/claude-sonnet-5"]
    assert catalog.loaded_at() is not None


@pytest.mark.usefixtures("restore_httpx")
@pytest.mark.parametrize(
    "handler",
    [
        lambda _r: httpx.Response(503, text="down"),
        lambda _r: httpx.Response(200, json={"unexpected": True}),
        lambda _r: httpx.Response(200, text="not json"),
        lambda _r: (_ for _ in ()).throw(httpx.ConnectError("refused")),
    ],
    ids=["http-5xx", "no-data-key", "not-json", "connect-error"],
)
async def test_refresh_failure_keeps_previous_state(handler: Any) -> None:
    _mock_transport(handler)
    # Never loaded → still None → registry serves the fallback.
    assert await catalog.refresh_openrouter_catalog(_catalog_settings()) is False
    assert catalog.loaded_models() is None
    assert openrouter_models() == OPENROUTER_MODELS
    # Previously loaded → the old catalog survives a failed refresh.
    catalog._loaded_models = ["openai/gpt-6"]
    assert await catalog.refresh_openrouter_catalog(_catalog_settings()) is False
    assert catalog.loaded_models() == ["openai/gpt-6"]


@pytest.mark.usefixtures("restore_httpx")
async def test_refresh_is_bounded_by_a_total_deadline() -> None:
    """httpx's timeout is per-read; a server that trickles bytes forever must
    still be cut off at ``openrouter_catalog_timeout_s`` so startup can't hang."""
    import asyncio
    import time

    async def _trickle() -> Any:
        while True:
            yield b" "
            await asyncio.sleep(0.05)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_trickle())

    _mock_transport(handler)
    t0 = time.monotonic()
    ok = await catalog.refresh_openrouter_catalog(
        _catalog_settings(openrouter_catalog_timeout_s=0.3)
    )
    elapsed = time.monotonic() - t0
    assert ok is False
    assert elapsed < 2.0, f"deadline not enforced: {elapsed:.2f}s"
    assert catalog.loaded_models() is None


@pytest.mark.usefixtures("restore_httpx")
async def test_refresh_rejects_oversized_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog, "MAX_CATALOG_BYTES", 64)
    # fetch_catalog's default binds at def time — pass the cap explicitly via
    # a wrapper so the module constant patch is honoured.
    real_fetch = catalog.fetch_catalog

    async def _capped(**kw: Any) -> Any:
        return await real_fetch(max_bytes=catalog.MAX_CATALOG_BYTES, **kw)

    monkeypatch.setattr(catalog, "fetch_catalog", _capped)
    big = {"data": [_entry(f"openai/gpt-{i}") for i in range(50)]}
    _mock_transport(lambda _r: httpx.Response(200, json=big))
    assert await catalog.refresh_openrouter_catalog(_catalog_settings()) is False
    assert catalog.loaded_models() is None


async def test_refresher_loop_is_a_noop_when_interval_is_zero() -> None:
    # Returns immediately rather than sleeping forever / fetching.
    await catalog.run_catalog_refresher(_catalog_settings(openrouter_catalog_refresh_s=0))
    assert catalog.loaded_models() is None


# --------------------------------------------------------------------------
# Claude id ↔ OpenRouter slug derivation + routing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "slug"),
    [
        ("claude-opus-5", "anthropic/claude-opus-5"),
        ("claude-sonnet-5", "anthropic/claude-sonnet-5"),
        ("claude-haiku-4-5", "anthropic/claude-haiku-4.5"),
        ("claude-haiku-4-5-20251001", "anthropic/claude-haiku-4.5"),
        ("claude-opus-4-7", "anthropic/claude-opus-4.7"),
        ("claude-sonnet-4-6", "anthropic/claude-sonnet-4.6"),
        ("claude-fable-5-1", "anthropic/claude-fable-5.1"),
        # Dated pin of a major-only id: the date must not be read as a minor.
        ("claude-opus-5-20260315", "anthropic/claude-opus-5"),
        ("claude-sonnet-5-20260101", "anthropic/claude-sonnet-5"),
    ],
)
def test_openrouter_slug_for_claude(model: str, slug: str) -> None:
    assert openrouter_slug_for_claude(model) == slug


@pytest.mark.parametrize(
    "model",
    [
        "openai/gpt-6",
        "anthropic/claude-opus-5",
        "llama3.3",
        "claude",
        "claude-opus",
        "claude-opus-5-2026",  # malformed date
        "claude-3-5-sonnet-20241022",  # legacy shape, family not first
        "",
    ],
)
def test_openrouter_slug_for_claude_rejects_non_claude_ids(model: str) -> None:
    assert openrouter_slug_for_claude(model) is None


def test_every_direct_model_has_a_derived_slug() -> None:
    for m in ANTHROPIC_DIRECT_MODELS:
        assert openrouter_slug_for_claude(m) is not None


def test_previous_generation_claude_id_still_routes_to_anthropic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent override persisted before the trio was refreshed must not
    fall through to the non-Claude path (which 400s with OpenRouter off)."""
    from openexecutive.providers.anthropic_provider import AnthropicProvider

    monkeypatch.setattr(
        "openexecutive.providers.registry.get_settings",
        lambda: _registry_settings(enabled=False),
    )
    monkeypatch.setattr(
        "openexecutive.providers.anthropic_provider.anthropic.AsyncAnthropic",
        lambda **_kw: SimpleNamespace(),
    )
    assert isinstance(get_provider("claude-opus-4-7"), AnthropicProvider)
    assert isinstance(get_provider("claude-haiku-4-5-20251001"), AnthropicProvider)


def test_local_slug_that_looks_like_claude_stays_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slug the operator listed in LOCAL_MODELS must never be re-routed
    to a hosted vendor just because it matches the Claude id shape."""
    from openexecutive.providers.openai_compatible import OpenAICompatibleProvider
    from openexecutive.providers.openrouter_provider import OpenRouterProvider

    settings = _registry_settings(enabled=True)
    settings.local_models_enabled = True
    settings.local_base_url = "http://localhost:11434/v1"
    settings.local_models = ["claude-proxy-1", "llama3.3"]
    monkeypatch.setattr("openexecutive.providers.registry.get_settings", lambda: settings)

    provider = get_provider("claude-proxy-1")
    assert isinstance(provider, OpenAICompatibleProvider)
    assert not isinstance(provider, OpenRouterProvider)
    # A real Claude id NOT in LOCAL_MODELS still takes the Claude path.
    assert isinstance(get_provider("claude-sonnet-5"), OpenRouterProvider)


def test_every_direct_model_keeps_cache_spec_on_openrouter_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Losing cache_control on a Claude model is a ~10x cost regression that
    would otherwise fail silently — pin it for every Anthropic-direct id."""
    monkeypatch.setattr(
        "openexecutive.providers.registry.get_settings",
        lambda: _registry_settings(enabled=True),
    )
    provider = get_provider(ANTHROPIC_DIRECT_MODELS[0])
    for model in ANTHROPIC_DIRECT_MODELS:
        slug, spec = provider._resolve(model)
        assert slug.startswith("anthropic/claude-"), model
        assert spec.supports_cache_control and spec.supports_thinking, model


def test_openrouter_provider_resolves_specs_by_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.providers.openrouter_provider import OpenRouterProvider

    monkeypatch.setattr(
        "openexecutive.providers.registry.get_settings",
        lambda: _registry_settings(enabled=True),
    )
    provider = get_provider("openai/gpt-6")
    assert isinstance(provider, OpenRouterProvider)

    slug, spec = provider._resolve("claude-sonnet-5")
    assert slug == "anthropic/claude-sonnet-5"
    assert spec.supports_cache_control and spec.supports_thinking

    # Verbatim catalog Claude slug: unchanged, but still the Claude spec.
    slug, spec = provider._resolve("anthropic/claude-opus-4.8")
    assert slug == "anthropic/claude-opus-4.8"
    assert spec.supports_cache_control and spec.supports_thinking

    # Anything else: passthrough slug, non-Claude default (Anthropic-only
    # fields stripped).
    slug, spec = provider._resolve("openai/gpt-6")
    assert slug == "openai/gpt-6"
    assert not spec.supports_cache_control
    assert not spec.supports_thinking
    assert not spec.supports_web_search
    assert spec.supports_tool_use
