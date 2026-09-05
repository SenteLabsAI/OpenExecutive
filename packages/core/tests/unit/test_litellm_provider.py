"""Tests for the LiteLLM-backed provider and its registry routing.

The ``litellm`` package is an optional extra, so these tests never import it —
``LiteLLMProvider._import_litellm`` is patched with a fake module whose
``acompletion`` is an ``AsyncMock``. That keeps the suite runnable in the base
environment while still pinning the request-translation + response contract.
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-used")

import pytest  # noqa: E402

from openexecutive.providers import registry as registry_mod  # noqa: E402
from openexecutive.providers.litellm_provider import (  # noqa: E402
    LiteLLMProvider,
    _LiteLLMStream,
)
from openexecutive.providers.provider import LLMProvider  # noqa: E402


def _response(**usage: int) -> Any:
    """A stubbed OpenAI-shape ModelResponse (only ``.model_dump()`` is read)."""
    return SimpleNamespace(
        model_dump=lambda: {
            "id": "cmpl-x",
            "model": "gemini/gemini-2.5-pro",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": usage.get("prompt", 5),
                "completion_tokens": usage.get("completion", 2),
            },
        }
    )


def _fake_litellm(response: Any) -> MagicMock:
    fake = MagicMock(name="litellm")
    fake.acompletion = AsyncMock(return_value=response)
    return fake


def _run_create(provider: LiteLLMProvider, fake: MagicMock, **kwargs: Any) -> Any:
    with patch(
        "openexecutive.providers.litellm_provider._import_litellm",
        return_value=fake,
    ):
        return asyncio.run(
            provider.messages_create(
                model=kwargs.pop("model", "gemini/gemini-2.5-pro"),
                max_tokens=kwargs.pop("max_tokens", 16),
                messages=kwargs.pop("messages", [{"role": "user", "content": "hi"}]),
                **kwargs,
            )
        )


class TestLiteLLMProvider:
    def test_messages_create_translates_and_dispatches(self) -> None:
        fake = _fake_litellm(_response())
        provider = LiteLLMProvider()
        result = _run_create(provider, fake)

        fake.acompletion.assert_awaited_once()
        sent = fake.acompletion.call_args.kwargs
        assert sent["model"] == "gemini/gemini-2.5-pro"
        assert sent["messages"][-1] == {"role": "user", "content": "hi"}
        assert sent["max_tokens"] == 16
        # drop_params defaults on for cross-provider kwarg compatibility.
        assert sent["drop_params"] is True
        # OpenRouter-only fields must be stripped before hitting LiteLLM.
        assert "usage" not in sent
        assert "plugins" not in sent
        # Blank credentials are omitted so LiteLLM uses each provider's env var.
        assert "api_key" not in sent
        assert "api_base" not in sent
        # Response is translated back to an Anthropic-shape Message.
        assert result.content[0].type == "text"
        assert result.content[0].text == "ok"
        assert result.stop_reason == "end_turn"
        assert result.usage.input_tokens == 5
        assert result.usage.output_tokens == 2

    def test_credentials_and_base_url_forwarded_when_set(self) -> None:
        fake = _fake_litellm(_response())
        provider = LiteLLMProvider(
            api_key="sk-proxy", base_url="http://localhost:4000", timeout_s=42.0
        )
        _run_create(provider, fake)
        sent = fake.acompletion.call_args.kwargs
        assert sent["api_key"] == "sk-proxy"
        assert sent["api_base"] == "http://localhost:4000"
        assert sent["timeout"] == 42.0

    def test_drop_params_opt_out(self) -> None:
        fake = _fake_litellm(_response())
        provider = LiteLLMProvider(drop_params=False)
        _run_create(provider, fake)
        assert fake.acompletion.call_args.kwargs["drop_params"] is False

    def test_satisfies_provider_protocol(self) -> None:
        assert isinstance(LiteLLMProvider(), LLMProvider)

    def test_stream_translates_chunks_to_anthropic_events(self) -> None:
        chunks = [
            SimpleNamespace(
                model_dump=lambda: {
                    "id": "c1",
                    "model": "gemini/gemini-2.5-pro",
                    "choices": [{"delta": {"content": "Hel"}, "finish_reason": None}],
                }
            ),
            SimpleNamespace(
                model_dump=lambda: {
                    "choices": [{"delta": {"content": "lo"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 1},
                }
            ),
        ]

        class _AsyncChunks:
            def __aiter__(self) -> Any:
                async def _gen() -> Any:
                    for c in chunks:
                        yield c

                return _gen()

        fake = MagicMock(name="litellm")
        fake.acompletion = AsyncMock(return_value=_AsyncChunks())

        async def _drive() -> tuple[str, Any]:
            stream = _LiteLLMStream(
                params={"model": "gemini/gemini-2.5-pro", "messages": []}
            )
            with patch(
                "openexecutive.providers.litellm_provider._import_litellm",
                return_value=fake,
            ):
                text = ""
                async with stream as s:
                    async for event in s:
                        if (
                            event.type == "content_block_delta"
                            and event.delta.type == "text_delta"
                        ):
                            text += event.delta.text
                    final = await s.get_final_message()
                return text, final

        text, final = asyncio.run(_drive())
        assert text == "Hello"
        assert final.content[0].text == "Hello"
        assert final.stop_reason == "end_turn"
        assert final.usage.output_tokens == 1


class TestLiteLLMRegistryRouting:
    @pytest.fixture(autouse=True)
    def _reset(self) -> Any:
        registry_mod._reset_for_tests()
        yield
        registry_mod._reset_for_tests()

    def _settings(self, **over: Any) -> SimpleNamespace:
        base = {
            "anthropic_api_key": None,
            "openrouter_enabled": False,
            "local_models_enabled": False,
            "litellm_enabled": True,
            "litellm_models": ["gemini/gemini-2.5-pro"],
            "litellm_api_key": None,
            "litellm_base_url": None,
            "litellm_timeout_s": 180.0,
        }
        base.update(over)
        return SimpleNamespace(**base)

    def test_get_provider_routes_litellm_slug(self) -> None:
        with patch.object(registry_mod, "get_settings", return_value=self._settings()):
            provider = registry_mod.get_provider("gemini/gemini-2.5-pro")
        assert isinstance(provider, LiteLLMProvider)

    def test_allowed_models_includes_litellm_when_enabled(self) -> None:
        with patch.object(registry_mod, "get_settings", return_value=self._settings()):
            assert "gemini/gemini-2.5-pro" in registry_mod.allowed_models()

    def test_litellm_models_hidden_when_disabled(self) -> None:
        settings = self._settings(litellm_enabled=False)
        with patch.object(registry_mod, "get_settings", return_value=settings):
            assert "gemini/gemini-2.5-pro" not in registry_mod.allowed_models()
