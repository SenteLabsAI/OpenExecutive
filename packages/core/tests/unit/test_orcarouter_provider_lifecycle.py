"""Lifecycle and error-path tests for the OrcaRouter HTTP provider.

Mirrors ``test_openrouter_provider_lifecycle.py`` — OrcaRouter is a thin
specialization of the generic OpenAI-compatible provider, so this file pins
the OrcaRouter-specific bits: default base URL, verbatim ``orcarouter/*``
slug passthrough, Claude → ``anthropic/*`` slug translation, and the
Anthropic-only feature stripping that applies to the non-Claude native
models.
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from openexecutive.providers.feature_gate import FeatureSpec
from openexecutive.providers.orcarouter_provider import OrcaRouterProvider


def _provider() -> OrcaRouterProvider:
    return OrcaRouterProvider(
        api_key="sk-orca-test",
        base_url="https://api.orcarouter.ai/v1",
        slug_lookup={"claude-sonnet-4-6": "anthropic/claude-sonnet-4.6"},
        spec_lookup={"claude-sonnet-4-6": FeatureSpec()},
    )


def test_default_base_url_is_orcarouter() -> None:
    provider = _provider()
    assert provider._base_url == "https://api.orcarouter.ai/v1"


def test_native_orcarouter_slug_passes_through_verbatim() -> None:
    """The ``orcarouter/*`` namespace isn't translated — slugs are sent as-is
    so they match what OrcaRouter actually serves."""
    provider = _provider()
    captured: dict[str, Any] = {}

    async def _fake_post(url: str, **kwargs: Any) -> Any:
        captured["url"] = url
        captured["headers"] = kwargs.get("headers", {})
        captured["json"] = kwargs.get("json", {})
        fake = MagicMock()
        fake.json.return_value = {
            "id": "chatcmpl-x",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "Hello"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        fake.raise_for_status = MagicMock()
        return fake

    provider._client.post = AsyncMock(side_effect=_fake_post)  # type: ignore[method-assign]
    asyncio.run(
        provider.messages_create(
            model="orcarouter/fusion-mini",
            max_tokens=64,
            messages=[{"role": "user", "content": "Hi"}],
        )
    )
    assert captured["url"] == "/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer sk-orca-test"
    assert captured["json"]["model"] == "orcarouter/fusion-mini"


def test_claude_model_translates_to_orcarouter_anthropic_slug() -> None:
    """A Claude model requested through OrcaRouter maps onto its ``anthropic/*``
    namespace, exactly like the OpenRouter path."""
    provider = _provider()
    captured: dict[str, Any] = {}

    async def _fake_post(url: str, **kwargs: Any) -> Any:
        captured["json"] = kwargs.get("json", {})
        fake = MagicMock()
        fake.json.return_value = {
            "id": "x",
            "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        }
        fake.raise_for_status = MagicMock()
        return fake

    provider._client.post = AsyncMock(side_effect=_fake_post)  # type: ignore[method-assign]
    asyncio.run(
        provider.messages_create(
            model="claude-sonnet-4-6",
            max_tokens=64,
            messages=[{"role": "user", "content": "Hi"}],
        )
    )
    assert captured["json"]["model"] == "anthropic/claude-sonnet-4.6"


def test_messages_create_strips_anthropic_only_fields_for_native_model() -> None:
    """A request bound for an ``orcarouter/*`` native model must not carry
    ``thinking`` or ``cache_control`` — the feature gate strips them before
    translation."""
    provider = OrcaRouterProvider(
        api_key="sk-orca-test",
        slug_lookup={},
        spec_lookup={
            "orcarouter/fusion-mini": FeatureSpec(
                supports_cache_control=False,
                supports_thinking=False,
                supports_web_search=False,
                supports_tool_use=True,
            )
        },
    )
    captured: dict[str, Any] = {}

    async def _fake_post(url: str, **kwargs: Any) -> Any:
        captured["json"] = kwargs.get("json", {})
        fake = MagicMock()
        fake.json.return_value = {
            "id": "x",
            "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        }
        fake.raise_for_status = MagicMock()
        return fake

    provider._client.post = AsyncMock(side_effect=_fake_post)  # type: ignore[method-assign]

    asyncio.run(
        provider.messages_create(
            model="orcarouter/fusion-mini",
            max_tokens=64,
            thinking={"type": "adaptive"},
            output_config={"effort": "low"},
            system=[
                {"type": "text", "text": "P", "cache_control": {"type": "ephemeral"}}
            ],
            messages=[{"role": "user", "content": "hi"}],
        )
    )

    body = captured["json"]
    # cache_control vanished with the system block flatten.
    assert isinstance(body["messages"][0]["content"], str)
    # thinking / output_config never reach the wire.
    assert "thinking" not in body
    assert "output_config" not in body
