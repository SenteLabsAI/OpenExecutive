"""Tests for the generic OpenAI-compatible provider used by local backends.

OpenRouter's request/response/stream behavior is pinned in
``test_openrouter_provider_lifecycle.py`` (OpenRouter now subclasses this
provider). This file covers the behavior that's specific to the local /
self-hosted path: optional auth, verbatim model passthrough, and the
Anthropic-only feature stripping that a plain OpenAI-compatible server needs.
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from openexecutive.providers import openai_compatible
from openexecutive.providers.feature_gate import FeatureSpec
from openexecutive.providers.openai_compatible import OpenAICompatibleProvider

_LOCAL_SPEC = FeatureSpec(
    supports_cache_control=False,
    supports_thinking=False,
    supports_web_search=False,
    supports_tool_use=True,
)


def _local_provider(api_key: str | None = None) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        base_url="http://localhost:11434/v1",
        api_key=api_key,
        spec_lookup={"llama3.3": _LOCAL_SPEC},
    )


def _ok_response() -> MagicMock:
    fake = MagicMock()
    fake.json.return_value = {
        "id": "x",
        "choices": [
            {
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }
        ],
    }
    fake.raise_for_status = MagicMock()
    return fake


def _run_create(provider: OpenAICompatibleProvider, **kwargs: Any) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    async def _fake_post(url: str, **post_kwargs: Any) -> Any:
        captured["url"] = url
        captured["headers"] = post_kwargs.get("headers", {})
        captured["json"] = post_kwargs.get("json", {})
        return _ok_response()

    provider._client.post = AsyncMock(side_effect=_fake_post)  # type: ignore[method-assign]
    asyncio.run(
        provider.messages_create(
            model=kwargs.pop("model", "llama3.3"),
            max_tokens=kwargs.pop("max_tokens", 8),
            messages=kwargs.pop(
                "messages", [{"role": "user", "content": "hi"}]
            ),
            **kwargs,
        )
    )
    return captured


def test_no_auth_header_when_api_key_absent() -> None:
    """Local servers (Ollama, LM Studio) need no auth — we must NOT send a
    bogus ``Authorization: Bearer None`` that a strict server could reject."""
    captured = _run_create(_local_provider(api_key=None))
    assert "Authorization" not in captured["headers"]


def test_auth_header_present_when_api_key_given() -> None:
    """vLLM or a gateway in front of a local server may require a token."""
    captured = _run_create(_local_provider(api_key="vllm-secret"))
    assert captured["headers"]["Authorization"] == "Bearer vllm-secret"


def test_unknown_model_passes_through_verbatim() -> None:
    """Local model names aren't translated — they're sent as-is so they match
    what the server actually serves."""
    captured = _run_create(_local_provider(), model="llama3.3")
    assert captured["json"]["model"] == "llama3.3"


def test_anthropic_only_fields_stripped_for_local_model() -> None:
    """thinking / output_config / cache_control have no OpenAI-format
    equivalent and would 400 a plain server — the feature gate drops them."""
    captured = _run_create(
        _local_provider(),
        thinking={"type": "adaptive"},
        output_config={"effort": "low"},
        system=[
            {"type": "text", "text": "P", "cache_control": {"type": "ephemeral"}}
        ],
    )
    body = captured["json"]
    assert "thinking" not in body
    assert "output_config" not in body
    # system flattened into a plain string message — no cache_control survives.
    assert isinstance(body["messages"][0]["content"], str)


def _effort_provider(effort: str | None) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        base_url="https://api.fireworks.ai/inference/v1",
        spec_lookup={"llama3.3": _LOCAL_SPEC},
        reasoning_effort=effort,
    )


def test_reasoning_effort_omitted_by_default() -> None:
    """Most OpenAI-compatible servers don't know the field; unset = not sent."""
    captured = _run_create(_local_provider())
    assert "reasoning_effort" not in captured["json"]


def test_reasoning_effort_sent_when_configured() -> None:
    """Thinking-only models (GLM on Fireworks) otherwise burn the whole
    max_tokens budget reasoning and return no tool call."""
    captured = _run_create(_effort_provider("low"))
    assert captured["json"]["reasoning_effort"] == "low"


def test_reasoning_effort_sent_on_stream() -> None:
    provider = _effort_provider("low")
    stream = provider.messages_stream(
        model="llama3.3",
        max_tokens=8,
        messages=[{"role": "user", "content": "hi"}],
    )
    assert stream._body["reasoning_effort"] == "low"  # type: ignore[attr-defined]


def test_reasoning_effort_logged_once_per_slug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The effort in play is what decides whether a thinking-only model
    answers at all, so it's logged, but once per model, not per call."""
    monkeypatch.setattr(openai_compatible, "_effort_announced", set())
    fake_logger = MagicMock()
    monkeypatch.setattr(openai_compatible, "logger", fake_logger)
    provider = _effort_provider("low")
    _run_create(provider)
    _run_create(provider)
    assert fake_logger.info.call_count == 1
    assert fake_logger.info.call_args.args[1:] == ("low", "llama3.3")


def test_haiku_5_5_slug_gets_thinking_off_and_no_temperature() -> None:
    """Through a gateway, Haiku 5.5 is told not to think unless asked and
    never sent a temperature it would reject."""
    provider = OpenAICompatibleProvider(
        base_url="https://gateway.example/v1",
        api_key="k",
        slug_lookup={"claude-haiku-5-5": "anthropic/claude-haiku-5.5"},
        spec_lookup={"claude-haiku-5-5": FeatureSpec()},
    )
    captured = _run_create(provider, model="claude-haiku-5-5", temperature=0)
    body = captured["json"]
    assert body["model"] == "anthropic/claude-haiku-5.5"
    assert body["reasoning"] == {"enabled": False}
    assert "temperature" not in body


def test_backend_without_thinking_gets_no_reasoning_field_for_haiku_5_5() -> None:
    """A local gateway serving a Claude slug takes no thinking fields, so the
    turned-off thinking added for Haiku 5.5 must not reach it as ``reasoning``."""
    provider = OpenAICompatibleProvider(
        base_url="http://localhost:4000/v1",
        api_key=None,
        spec_lookup={"claude-haiku-5-5": _LOCAL_SPEC},
    )
    body = _run_create(provider, model="claude-haiku-5-5", temperature=0)["json"]
    assert "reasoning" not in body
    assert "temperature" not in body
