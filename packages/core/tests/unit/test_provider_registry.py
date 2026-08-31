"""Provider abstraction smoke tests.

Commit 1 wiring: every call site that used to call ``client.messages.create``
or ``client.messages.stream`` now resolves the backend via
``providers.get_provider(model)``. The registry currently always returns
the Anthropic provider; OpenRouter routing will be added in the next commit.

These tests pin the contract:

1. ``get_provider`` returns the same singleton across calls (cheap to call
   inside per-request paths).
2. The returned object satisfies the ``LLMProvider`` Protocol — it exposes
   ``messages_create`` and ``messages_stream``.
3. ``messages_create`` proxies kwargs through to the underlying SDK with
   no rewriting.
4. ``messages_stream`` returns the SDK's async context manager unchanged
   (so the streaming dispatch in ``executive.py`` keeps working).
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-used")

import pytest  # noqa: E402

from openexecutive.providers import LLMProvider, get_provider  # noqa: E402
from openexecutive.providers import registry as registry_mod  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_singleton() -> Any:
    registry_mod._reset_for_tests()
    yield
    registry_mod._reset_for_tests()


def test_get_provider_returns_same_singleton_across_claude_calls() -> None:
    a = get_provider("claude-sonnet-4-6")
    b = get_provider("claude-opus-4-7")
    # Different Claude model args resolve to the same Anthropic-direct
    # singleton — reconstructing the SDK client per request would burn
    # ~10 ms on every specialist call.
    assert a is b


def test_returned_provider_satisfies_protocol() -> None:
    provider = get_provider("claude-sonnet-4-6")
    assert isinstance(provider, LLMProvider)
    assert hasattr(provider, "messages_create")
    assert hasattr(provider, "messages_stream")


def test_messages_create_proxies_kwargs_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Anthropic-shape request body must reach the SDK verbatim — adding
    or rewriting fields here would silently break prompt caching."""
    captured: dict[str, Any] = {}
    fake_message = SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")])

    async def _fake_create(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return fake_message

    monkeypatch.setattr(
        "openexecutive.providers.anthropic_provider.anthropic.AsyncAnthropic",
        lambda **_kw: SimpleNamespace(messages=SimpleNamespace(create=_fake_create)),
    )
    registry_mod._reset_for_tests()

    provider = get_provider("claude-sonnet-4-6")
    payload = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 1024,
        "system": [
            {
                "type": "text",
                "text": "S",
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": [{"role": "user", "content": "hi"}],
    }
    asyncio.run(provider.messages_create(**payload))
    assert captured == payload


def test_messages_stream_returns_sdk_context_manager_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = MagicMock(name="anthropic-stream-cm")

    monkeypatch.setattr(
        "openexecutive.providers.anthropic_provider.anthropic.AsyncAnthropic",
        lambda **_kw: SimpleNamespace(
            messages=SimpleNamespace(stream=lambda **_kw: sentinel)
        ),
    )
    registry_mod._reset_for_tests()

    provider = get_provider("claude-sonnet-4-6")
    # Note: messages_stream is sync; the SDK returns the CM and the caller
    # uses it in ``async with``. The provider must not wrap it in another
    # layer that would change ``stream.get_final_message`` semantics.
    assert provider.messages_stream(model="claude-sonnet-4-6") is sentinel


def test_messages_create_accepts_per_request_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-request timeout keyword is what carries through with the singleton
    pattern; the previous per-client timeout setup is gone."""
    captured: dict[str, Any] = {}

    async def _fake_create(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return SimpleNamespace(content=[])

    monkeypatch.setattr(
        "openexecutive.providers.anthropic_provider.anthropic.AsyncAnthropic",
        lambda **_kw: SimpleNamespace(messages=SimpleNamespace(create=_fake_create)),
    )
    registry_mod._reset_for_tests()

    provider = get_provider("claude-sonnet-4-6")
    asyncio.run(
        provider.messages_create(
            model="claude-sonnet-4-6", max_tokens=10, messages=[], timeout=42.0
        )
    )
    assert captured["timeout"] == 42.0


def test_anthropic_provider_constructs_lazy_only_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    construction_count = 0

    def _ctor(**_kw: Any) -> Any:
        nonlocal construction_count
        construction_count += 1
        return SimpleNamespace(messages=SimpleNamespace(create=AsyncMock()))

    monkeypatch.setattr(
        "openexecutive.providers.anthropic_provider.anthropic.AsyncAnthropic", _ctor
    )
    registry_mod._reset_for_tests()
    for _ in range(5):
        get_provider("claude-sonnet-4-6")
    assert construction_count == 1


# --------------------------------------------------------------------------
# OpenRouter routing matrix
# --------------------------------------------------------------------------


def _settings_stub(
    *,
    enabled: bool = False,
    key: str | None = "sk-or-v1-xxx",
    anthropic_key: str | None = "sk-test",
    local_enabled: bool = False,
    local_base_url: str | None = None,
    local_models: list[str] | None = None,
    local_api_key: str | None = None,
    local_timeout_s: float = 300.0,
    orcarouter_enabled: bool = False,
    orcarouter_key: str | None = "sk-orca-test",
    orcarouter_base_url: str = "https://api.orcarouter.ai/v1",
    orcarouter_timeout_s: float = 180.0,
) -> Any:
    return SimpleNamespace(
        anthropic_api_key=anthropic_key,
        openrouter_enabled=enabled,
        openrouter_api_key=key,
        openrouter_base_url="https://openrouter.ai/api/v1",
        openrouter_app_title="Open Executive",
        openrouter_referer=None,
        openrouter_timeout_s=180.0,
        orcarouter_enabled=orcarouter_enabled,
        orcarouter_api_key=orcarouter_key,
        orcarouter_base_url=orcarouter_base_url,
        orcarouter_timeout_s=orcarouter_timeout_s,
        local_models_enabled=local_enabled,
        local_base_url=local_base_url,
        local_models=local_models or [],
        local_api_key=local_api_key,
        local_timeout_s=local_timeout_s,
    )


def test_claude_model_with_openrouter_off_routes_to_anthropic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "openexecutive.providers.registry.get_settings",
        lambda: _settings_stub(enabled=False),
    )
    registry_mod._reset_for_tests()
    from openexecutive.providers.anthropic_provider import AnthropicProvider

    provider = get_provider("claude-sonnet-4-6")
    assert isinstance(provider, AnthropicProvider)


def test_claude_model_with_openrouter_on_routes_to_openrouter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "openexecutive.providers.registry.get_settings",
        lambda: _settings_stub(enabled=True),
    )
    registry_mod._reset_for_tests()
    from openexecutive.providers.openrouter_provider import OpenRouterProvider

    provider = get_provider("claude-sonnet-4-6")
    assert isinstance(provider, OpenRouterProvider)


def test_non_claude_model_with_openrouter_on_routes_to_openrouter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "openexecutive.providers.registry.get_settings",
        lambda: _settings_stub(enabled=True),
    )
    registry_mod._reset_for_tests()
    from openexecutive.providers.openrouter_provider import OpenRouterProvider

    provider = get_provider("openai/gpt-5")
    assert isinstance(provider, OpenRouterProvider)


def test_non_claude_model_with_openrouter_off_raises_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No fallback path — non-Claude slugs require OpenRouter to be on."""
    from fastapi import HTTPException

    monkeypatch.setattr(
        "openexecutive.providers.registry.get_settings",
        lambda: _settings_stub(enabled=False),
    )
    registry_mod._reset_for_tests()
    with pytest.raises(HTTPException) as exc_info:
        get_provider("openai/gpt-5")
    assert exc_info.value.status_code == 400
    assert "OPENROUTER_ENABLED" in exc_info.value.detail


def test_anthropic_singleton_distinct_from_openrouter_singleton(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Different model families resolve to different singleton instances —
    not the same object — so misconfiguration can't silently send a
    non-Claude slug to the Anthropic SDK."""
    monkeypatch.setattr(
        "openexecutive.providers.registry.get_settings",
        lambda: _settings_stub(enabled=True),
    )
    registry_mod._reset_for_tests()
    claude_provider = get_provider("claude-sonnet-4-6")
    or_provider = get_provider("openai/gpt-5")
    # Both happen to be the OpenRouter provider when enabled=True, so they
    # ARE the same. But when toggled off, the Claude path returns Anthropic.
    # Verify the swap.
    monkeypatch.setattr(
        "openexecutive.providers.registry.get_settings",
        lambda: _settings_stub(enabled=False),
    )
    registry_mod._reset_for_tests()
    direct_claude = get_provider("claude-sonnet-4-6")
    assert direct_claude is not or_provider
    assert direct_claude is not claude_provider


def test_allowed_models_includes_openrouter_set_only_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.providers import allowed_models
    from openexecutive.providers.registry import (
        ANTHROPIC_DIRECT_MODELS,
        OPENROUTER_MODELS,
    )

    monkeypatch.setattr(
        "openexecutive.providers.registry.get_settings",
        lambda: _settings_stub(enabled=False),
    )
    off = allowed_models()
    assert off == ANTHROPIC_DIRECT_MODELS

    monkeypatch.setattr(
        "openexecutive.providers.registry.get_settings",
        lambda: _settings_stub(enabled=True),
    )
    on = allowed_models()
    assert on == [*ANTHROPIC_DIRECT_MODELS, *OPENROUTER_MODELS]
    # Both sets contain entries (catch the case where one list was emptied).
    assert len(on) > len(off)


# --------------------------------------------------------------------------
# OrcaRouter routing matrix
# --------------------------------------------------------------------------


def test_orca_native_model_with_orca_off_raises_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No fallback path — OrcaRouter-native slugs require the toggle to be on."""
    from fastapi import HTTPException

    monkeypatch.setattr(
        "openexecutive.providers.registry.get_settings",
        lambda: _settings_stub(enabled=False, orcarouter_enabled=False),
    )
    registry_mod._reset_for_tests()
    with pytest.raises(HTTPException) as exc_info:
        get_provider("orcarouter/fusion-mini")
    assert exc_info.value.status_code == 400
    assert "ORCAROUTER_ENABLED" in exc_info.value.detail


def test_orca_native_model_with_orca_on_routes_to_orcarouter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "openexecutive.providers.registry.get_settings",
        lambda: _settings_stub(enabled=False, orcarouter_enabled=True),
    )
    registry_mod._reset_for_tests()
    from openexecutive.providers.orcarouter_provider import OrcaRouterProvider

    provider = get_provider("orcarouter/fusion-mini")
    assert isinstance(provider, OrcaRouterProvider)


def test_claude_model_with_orca_on_and_openrouter_off_routes_to_orcarouter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "openexecutive.providers.registry.get_settings",
        lambda: _settings_stub(enabled=False, orcarouter_enabled=True),
    )
    registry_mod._reset_for_tests()
    from openexecutive.providers.orcarouter_provider import OrcaRouterProvider

    provider = get_provider("claude-sonnet-4-6")
    assert isinstance(provider, OrcaRouterProvider)


def test_openrouter_takes_precedence_over_orcarouter_for_claude(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both toggles on: OpenRouter wins for Claude — existing deployments are
    unchanged when they enable OrcaRouter later."""
    monkeypatch.setattr(
        "openexecutive.providers.registry.get_settings",
        lambda: _settings_stub(enabled=True, orcarouter_enabled=True),
    )
    registry_mod._reset_for_tests()
    from openexecutive.providers.openrouter_provider import OpenRouterProvider

    provider = get_provider("claude-sonnet-4-6")
    assert isinstance(provider, OpenRouterProvider)


def test_orcarouter_provider_distinct_from_openrouter_singleton(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A native OrcaRouter slug and an OpenRouter slug resolve to different
    provider instances — an OrcaRouter request can't leak to OpenRouter."""
    monkeypatch.setattr(
        "openexecutive.providers.registry.get_settings",
        lambda: _settings_stub(enabled=True, orcarouter_enabled=True),
    )
    registry_mod._reset_for_tests()
    orca = get_provider("orcarouter/fusion-mini")
    openrouter = get_provider("openai/gpt-5")
    assert orca is not openrouter


def test_allowed_models_includes_orcarouter_set_only_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.providers import allowed_models
    from openexecutive.providers.registry import (
        ANTHROPIC_DIRECT_MODELS,
        OPENROUTER_MODELS,
        ORCAROUTER_MODELS,
    )

    monkeypatch.setattr(
        "openexecutive.providers.registry.get_settings",
        lambda: _settings_stub(enabled=False, orcarouter_enabled=False),
    )
    off = allowed_models()
    assert off == ANTHROPIC_DIRECT_MODELS

    monkeypatch.setattr(
        "openexecutive.providers.registry.get_settings",
        lambda: _settings_stub(enabled=False, orcarouter_enabled=True),
    )
    on = allowed_models()
    assert on == [*ANTHROPIC_DIRECT_MODELS, *ORCAROUTER_MODELS]


# --------------------------------------------------------------------------
# Local / self-hosted model routing
# --------------------------------------------------------------------------


def _local_stub(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> None:
    monkeypatch.setattr(
        "openexecutive.providers.registry.get_settings",
        lambda: _settings_stub(
            local_enabled=True,
            local_base_url="http://localhost:11434/v1",
            local_models=["llama3.3", "qwen2.5"],
            **kwargs,
        ),
    )
    registry_mod._reset_for_tests()


def test_local_model_routes_to_local_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.providers.openai_compatible import OpenAICompatibleProvider
    from openexecutive.providers.openrouter_provider import OpenRouterProvider

    _local_stub(monkeypatch, enabled=False)
    provider = get_provider("llama3.3")
    assert isinstance(provider, OpenAICompatibleProvider)
    # The local backend is the plain base class, never the OpenRouter subclass.
    assert not isinstance(provider, OpenRouterProvider)
    # Cached singleton — same object across calls.
    assert get_provider("qwen2.5") is provider


def test_local_provider_distinct_from_openrouter_singleton(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With OpenRouter also on, a local slug and an OpenRouter slug resolve to
    different provider instances — a local request can't leak to OpenRouter."""
    _local_stub(monkeypatch, enabled=True)
    local = get_provider("llama3.3")
    openrouter = get_provider("openai/gpt-5")
    assert local is not openrouter


def test_unlisted_model_does_not_route_local(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local routing claims ONLY its configured slugs; an unlisted non-Claude
    slug with OpenRouter off is still a 400."""
    from fastapi import HTTPException

    _local_stub(monkeypatch, enabled=False)
    with pytest.raises(HTTPException) as exc_info:
        get_provider("some-unlisted-model")
    assert exc_info.value.status_code == 400


def test_allowed_models_includes_local_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.providers import allowed_models
    from openexecutive.providers.registry import ANTHROPIC_DIRECT_MODELS

    _local_stub(monkeypatch, enabled=False)
    assert allowed_models() == [*ANTHROPIC_DIRECT_MODELS, "llama3.3", "qwen2.5"]


def test_anthropic_free_deployment_hides_claude_and_serves_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No Anthropic key + OpenRouter off + local on: the Council UI must not
    offer Claude (unreachable), only the local slugs — which route locally."""
    from openexecutive.providers import allowed_models
    from openexecutive.providers.openai_compatible import OpenAICompatibleProvider

    _local_stub(monkeypatch, enabled=False, anthropic_key=None)
    assert allowed_models() == ["llama3.3", "qwen2.5"]
    assert isinstance(get_provider("llama3.3"), OpenAICompatibleProvider)


def test_claude_without_key_or_openrouter_raises_actionable_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An Anthropic-free deployment that left a model setting pointed at Claude
    gets a clear 400 — not a None-key SDK crash deep in a request."""
    from fastapi import HTTPException

    _local_stub(monkeypatch, enabled=False, anthropic_key=None)
    with pytest.raises(HTTPException) as exc_info:
        get_provider("claude-sonnet-4-6")
    assert exc_info.value.status_code == 400
    assert "ANTHROPIC_API_KEY" in exc_info.value.detail
