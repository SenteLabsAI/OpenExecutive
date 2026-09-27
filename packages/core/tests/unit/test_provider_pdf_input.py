"""PDFs through each provider's own file support.

An Anthropic ``document`` block is what ``knowledge.pdf_reader`` sends. On
the OpenAI-format path it used to vanish in translation; it now becomes an
OpenAI ``file`` part, OpenRouter adds its ``file-parser`` plugin, and a model
that cannot take a PDF gets a note in its place instead of silence.
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from openexecutive.providers import openrouter_catalog as catalog
from openexecutive.providers import registry
from openexecutive.providers.feature_gate import (
    PDF_OMITTED_NOTE,
    FeatureSpec,
    apply_feature_gates,
)
from openexecutive.providers.openai_compatible import OpenAICompatibleProvider
from openexecutive.providers.openrouter_provider import OpenRouterProvider
from openexecutive.providers.translator import to_openai_request

_PDF = {
    "type": "document",
    "source": {"type": "base64", "media_type": "application/pdf", "data": "JVBERi0x"},
}


@pytest.fixture(autouse=True)
def _reset() -> Any:
    catalog._reset_for_tests()
    registry._reset_for_tests()
    yield
    catalog._reset_for_tests()
    registry._reset_for_tests()


def _user(*blocks: dict[str, Any]) -> dict[str, Any]:
    return {"model": "x", "max_tokens": 8, "messages": [{"role": "user", "content": list(blocks)}]}


# ── Translator ───────────────────────────────────────────────────────────────


def test_a_pdf_becomes_an_openai_file_part_in_order() -> None:
    body = to_openai_request(
        "openai/gpt-6",
        _user({"type": "text", "text": "Before"}, dict(_PDF, title="deck.pdf"),
              {"type": "text", "text": "Transcribe it."}),
    )

    assert body["messages"] == [{
        "role": "user",
        "content": [
            {"type": "text", "text": "Before"},
            {"type": "file", "file": {
                "filename": "deck.pdf", "file_data": "data:application/pdf;base64,JVBERi0x",
            }},
            {"type": "text", "text": "Transcribe it."},
        ],
    }]


def test_without_a_pdf_the_user_turn_is_unchanged() -> None:
    body = to_openai_request("openai/gpt-6", _user({"type": "text", "text": "hi"}))

    assert body["messages"] == [{"role": "user", "content": "hi"}]


def test_a_cache_marker_survives_next_to_a_pdf() -> None:
    cc = {"type": "ephemeral"}
    body = to_openai_request(
        "anthropic/claude-sonnet-5", _user(_PDF, {"type": "text", "text": "q", "cache_control": cc}),
    )

    content = body["messages"][0]["content"]
    assert content[0]["type"] == "file"
    assert content[1] == {"type": "text", "text": "q", "cache_control": cc}


@pytest.mark.parametrize(
    "source",
    [
        {"type": "url", "url": "https://example.com/a.pdf"},
        {"type": "base64", "media_type": "text/plain", "data": "aGk="},
    ],
    ids=["url-source", "not-a-pdf"],
)
def test_a_document_with_no_faithful_openai_shape_is_left_out(source: dict[str, Any]) -> None:
    body = to_openai_request(
        "openai/gpt-6", _user({"type": "document", "source": source}, {"type": "text", "text": "q"}),
    )

    assert body["messages"] == [{"role": "user", "content": "q"}]


# ── Feature gate ─────────────────────────────────────────────────────────────


def test_a_model_without_pdf_input_is_told_a_pdf_was_there() -> None:
    kwargs = _user(_PDF, {"type": "text", "text": "q"})

    out = apply_feature_gates(FeatureSpec(supports_pdf_input=False), kwargs)

    assert out["messages"][0]["content"] == [
        {"type": "text", "text": PDF_OMITTED_NOTE},
        {"type": "text", "text": "q"},
    ]
    assert kwargs["messages"][0]["content"][0] is _PDF  # the caller's dict is untouched


# ── OpenRouter's file-parser plugin ──────────────────────────────────────────


def _ok() -> MagicMock:
    fake = MagicMock()
    fake.json.return_value = {
        "id": "x",
        "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
    }
    fake.raise_for_status = MagicMock()
    return fake


def _sent_body(provider: OpenAICompatibleProvider, model: str, kwargs: dict[str, Any]) -> dict:
    captured: dict[str, Any] = {}

    async def fake_post(url: str, **post_kwargs: Any) -> Any:
        captured.update(post_kwargs.get("json", {}))
        return _ok()

    provider._client.post = AsyncMock(side_effect=fake_post)  # type: ignore[method-assign]
    asyncio.run(provider.messages_create(**dict(kwargs, model=model)))
    return captured


def _openrouter() -> OpenRouterProvider:
    return OpenRouterProvider(
        api_key="sk-or-test", model_resolver=registry._openrouter_model_resolver
    )


def test_claude_on_openrouter_reads_the_pdf_natively() -> None:
    body = _sent_body(_openrouter(), "claude-sonnet-5", _user(_PDF))

    assert body["plugins"] == [{"id": "file-parser", "pdf": {"engine": "native"}}]
    assert body["messages"][-1]["content"][0]["type"] == "file"


def test_a_catalog_file_model_reads_natively_and_others_get_the_parser(monkeypatch) -> None:
    monkeypatch.setattr(catalog, "_loaded_models", ["openai/gpt-6", "meta-llama/llama-4-scout"])
    monkeypatch.setattr(catalog, "_loaded_file_input", frozenset({"openai/gpt-6"}))
    monkeypatch.setattr(catalog, "_known_ids", frozenset({"openai/gpt-6", "meta-llama/llama-4-scout"}))

    native = _sent_body(_openrouter(), "openai/gpt-6", _user(_PDF))
    parsed = _sent_body(_openrouter(), "meta-llama/llama-4-scout", _user(_PDF))
    monkeypatch.setenv("PDF_OPENROUTER_ENGINE", "cloudflare-ai")
    configured = _sent_body(_openrouter(), "meta-llama/llama-4-scout", _user(_PDF))

    assert native["plugins"][0]["pdf"]["engine"] == "native"
    assert parsed["plugins"][0]["pdf"]["engine"] == "mistral-ocr"
    assert configured["plugins"][0]["pdf"]["engine"] == "cloudflare-ai"


def test_no_plugin_without_a_pdf_or_on_a_local_server() -> None:
    plain = _sent_body(_openrouter(), "openai/gpt-6", _user({"type": "text", "text": "hi"}))
    local = OpenAICompatibleProvider(
        base_url="http://localhost:11434/v1",
        spec_lookup={"llama3.3": FeatureSpec(supports_pdf_input=True)},
    )
    local_body = _sent_body(local, "llama3.3", _user(_PDF))

    assert "plugins" not in plain
    assert "plugins" not in local_body
    assert local_body["messages"][-1]["content"][0]["type"] == "file"


# ── Catalog ──────────────────────────────────────────────────────────────────


async def test_the_catalog_records_which_models_take_files(monkeypatch) -> None:
    def entry(model_id: str, inputs: list[str]) -> dict[str, Any]:
        return {
            "id": model_id,
            "created": 1,
            "supported_parameters": ["tools"],
            "pricing": {"prompt": "0.000001", "completion": "0.000002"},
            "architecture": {"input_modalities": inputs, "output_modalities": ["text"]},
        }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [
            entry("openai/gpt-6", ["text", "image", "file"]),
            entry("meta-llama/llama-4-scout", ["text"]),
        ]})

    real = catalog.httpx.AsyncClient

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(**kwargs)

    monkeypatch.setattr(catalog.httpx, "AsyncClient", factory)
    settings = MagicMock(
        openrouter_base_url="https://openrouter.example/api/v1",
        openrouter_catalog_timeout_s=5.0,
        openrouter_catalog_providers=frozenset({"openai", "meta-llama"}),
        openrouter_catalog_per_provider=5,
    )

    assert catalog.accepts_files("openai/gpt-6") is None  # nothing loaded yet
    assert await catalog.refresh_openrouter_catalog(settings) is True
    assert catalog.accepts_files("openai/gpt-6") is True
    assert catalog.accepts_files("meta-llama/llama-4-scout") is False
    assert catalog.accepts_files("unknown/model") is None


# ── Registry ─────────────────────────────────────────────────────────────────


def test_pdf_input_follows_the_route(monkeypatch) -> None:
    assert registry.pdf_input_supported("claude-sonnet-5") is True  # Anthropic direct

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_ENABLED", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    assert registry.pdf_input_supported("claude-sonnet-5") is True  # via OpenRouter
    assert registry.pdf_input_supported("openai/gpt-6") is True

    monkeypatch.setenv("OPENROUTER_ENABLED", "false")
    monkeypatch.setenv("LOCAL_MODELS_ENABLED", "true")
    monkeypatch.setenv("LOCAL_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("LOCAL_MODELS", "llama3.3")
    assert registry.pdf_input_supported("llama3.3") is False
    assert registry.pdf_input_supported("claude-sonnet-5") is False  # no key, no OpenRouter
    monkeypatch.setenv("LOCAL_PDF_INPUT", "true")
    assert registry.pdf_input_supported("llama3.3") is True


def test_a_local_server_without_pdf_input_gets_the_note(monkeypatch) -> None:
    monkeypatch.setenv("LOCAL_MODELS_ENABLED", "true")
    monkeypatch.setenv("LOCAL_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("LOCAL_MODELS", "llama3.3")

    body = _sent_body(registry.get_provider("llama3.3"), "llama3.3", _user(_PDF))

    assert body["messages"][-1]["content"] == PDF_OMITTED_NOTE
