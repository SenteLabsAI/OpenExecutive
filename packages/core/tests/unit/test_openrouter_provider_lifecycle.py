"""Lifecycle and error-path tests for the OpenRouter HTTP provider.

Translator/registry contracts are pinned elsewhere — this file focuses
on the parts of ``OpenRouterProvider`` that touch the network: stream
context manager close on HTTP error (connection-leak), request body
assembly, and Authorization header presence.
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from openexecutive.providers.feature_gate import FeatureSpec
from openexecutive.providers.openrouter_provider import (
    OpenRouterProvider,
    _OpenRouterStream,
)


def _provider() -> OpenRouterProvider:
    return OpenRouterProvider(
        api_key="sk-or-test",
        base_url="https://openrouter.example/api/v1",
        slug_lookup={"claude-sonnet-4-6": "anthropic/claude-sonnet-4.6"},
        spec_lookup={"claude-sonnet-4-6": FeatureSpec()},
    )


def _async_iter(lines: list[str]):
    async def _gen() -> Any:
        for line in lines:
            yield line
    return _gen()


# --------------------------------------------------------------------------
# Streaming lifecycle: connection MUST be released on HTTP error
# --------------------------------------------------------------------------


def test_stream_closes_response_cm_on_http_error_status() -> None:
    """Regression: when OpenRouter returns 401 inside ``__aenter__``, the
    httpx response context must be exited so the connection is returned
    to the pool. The outer ``async with`` does NOT call __aexit__ when
    __aenter__ raises — the stream class has to clean up itself."""
    provider = _provider()

    fake_response = MagicMock(spec=httpx.Response)
    fake_response.status_code = 401
    fake_response.aread = AsyncMock(return_value=b'{"error":{"message":"bad key"}}')
    fake_response.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("401", request=MagicMock(), response=fake_response)
    )

    fake_cm = MagicMock()
    fake_cm.__aenter__ = AsyncMock(return_value=fake_response)
    fake_cm.__aexit__ = AsyncMock(return_value=None)

    stream = _OpenRouterStream(
        client=provider._client,
        body={"model": "x", "messages": []},
        headers={"Authorization": "Bearer sk-or-test"},
        timeout=None,
    )
    # Substitute the would-be httpx stream CM.
    provider._client.stream = MagicMock(return_value=fake_cm)  # type: ignore[method-assign]

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(stream.__aenter__())

    # The bug we fixed: __aexit__ must have been called even though
    # __aenter__ raised — otherwise the response stays open and leaks.
    fake_cm.__aexit__.assert_awaited_once()


def test_stream_skips_keepalive_and_done_terminator() -> None:
    """OpenRouter's SSE includes ``: keepalive`` comments between data lines
    and a final ``data: [DONE]`` terminator. The iterator must ignore both
    rather than logging an undecodable-payload warning."""
    provider = _provider()

    fake_response = MagicMock(spec=httpx.Response)
    fake_response.status_code = 200
    fake_response.aiter_lines = MagicMock(
        return_value=_async_iter(
            [
                ": keepalive",
                'data: {"choices":[{"delta":{"content":"Hi"}}]}',
                "",
                'data: {"choices":[{"delta":{"content":"!"},"finish_reason":"stop"}]}',
                "data: [DONE]",
            ]
        )
    )

    fake_cm = MagicMock()
    fake_cm.__aenter__ = AsyncMock(return_value=fake_response)
    fake_cm.__aexit__ = AsyncMock(return_value=None)

    provider._client.stream = MagicMock(return_value=fake_cm)  # type: ignore[method-assign]

    async def _drive() -> list:
        events: list = []
        async with provider.messages_stream(
            model="claude-sonnet-4-6", max_tokens=1, messages=[]
        ) as stream:
            async for event in stream:
                events.append(event)
            final = await stream.get_final_message()
        return [events, final]

    events, final = asyncio.run(_drive())
    text_deltas = [e for e in events if getattr(e, "type", "") == "content_block_delta"]
    assert [d.delta.text for d in text_deltas] == ["Hi", "!"]
    assert final.content[0].text == "Hi!"
    assert final.stop_reason == "end_turn"


def test_messages_create_sends_authorization_header_and_translates_body() -> None:
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
            model="claude-sonnet-4-6",
            max_tokens=64,
            system="You are a helpful assistant.",
            messages=[{"role": "user", "content": "Hi"}],
        )
    )
    assert captured["url"] == "/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer sk-or-test"
    # Translator should have swapped to the OpenRouter slug.
    assert captured["json"]["model"] == "anthropic/claude-sonnet-4.6"
    # system flattened into a system message at position 0.
    assert captured["json"]["messages"][0] == {
        "role": "system",
        "content": "You are a helpful assistant.",
    }
    # OpenRouterProvider opts into the cost-accounting request field — this
    # is really OpenRouter, unlike a generic local/gateway backend.
    assert captured["json"]["usage"] == {"include": True}


def test_local_provider_omits_usage_accounting_field() -> None:
    """Regression: a generic OpenAICompatibleProvider (e.g. a self-hosted
    server, or a LiteLLM gateway aliased under a Claude-family name) must
    NOT send the OpenRouter-only `usage` request field. A gateway that
    forwards nearly verbatim to real Anthropic rejects an unrecognized
    top-level field outright, breaking every call for that model."""
    from openexecutive.providers.openai_compatible import OpenAICompatibleProvider

    provider = OpenAICompatibleProvider(
        base_url="https://litellm.example.com/v1",
        slug_lookup={},
        spec_lookup={"claude-sonnet-4-6": FeatureSpec()},
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
            model="claude-sonnet-4-6",
            max_tokens=64,
            messages=[{"role": "user", "content": "hi"}],
        )
    )
    assert "usage" not in captured["json"]


def test_local_provider_omits_usage_accounting_field_on_stream_path() -> None:
    """Same regression as test_local_provider_omits_usage_accounting_field,
    but for messages_stream — a separate call site into to_openai_request
    that the non-streaming test does not exercise. _OpenAICompatibleStream
    stores the assembled body on ``._body``, readable without ever entering
    the context manager or opening a connection."""
    from openexecutive.providers.openai_compatible import OpenAICompatibleProvider

    provider = OpenAICompatibleProvider(
        base_url="https://litellm.example.com/v1",
        slug_lookup={},
        spec_lookup={"claude-sonnet-4-6": FeatureSpec()},
    )
    stream = provider.messages_stream(
        model="claude-sonnet-4-6", max_tokens=64, messages=[{"role": "user", "content": "hi"}]
    )
    assert "usage" not in stream._body  # type: ignore[attr-defined]


def test_openrouter_provider_includes_usage_accounting_on_stream_path() -> None:
    """OpenRouterProvider must keep opting in on the streaming path too —
    the non-streaming test above only covers messages_create."""
    provider = _provider()
    stream = provider.messages_stream(
        model="claude-sonnet-4-6", max_tokens=64, messages=[{"role": "user", "content": "hi"}]
    )
    assert stream._body["usage"] == {"include": True}  # type: ignore[attr-defined]


def test_messages_create_strips_anthropic_only_fields_for_non_claude() -> None:
    """A request bound for openai/gpt-5 must not carry ``thinking`` or
    ``cache_control`` — the feature gate strips them before translation."""
    provider = OpenRouterProvider(
        api_key="sk-or-test",
        slug_lookup={"openai/gpt-5": "openai/gpt-5"},
        spec_lookup={
            "openai/gpt-5": FeatureSpec(
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
            model="openai/gpt-5",
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
