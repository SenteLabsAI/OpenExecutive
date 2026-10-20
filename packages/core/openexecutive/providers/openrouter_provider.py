"""OpenRouter backend — a thin specialization of ``OpenAICompatibleProvider``.

OpenRouter speaks the OpenAI ``/chat/completions`` format, so the entire
request/response/stream machinery lives in the generic
``OpenAICompatibleProvider`` base. The only OpenRouter-specific bits are the
default base URL and the attribution headers (``HTTP-Referer`` / ``X-Title``)
that surface this app in your OpenRouter dashboard alongside the cost data.

``_OpenRouterStream`` is re-exported as an alias of the generic stream class
for backward compatibility with existing imports.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from openexecutive.providers.feature_gate import FeatureSpec
from openexecutive.providers.openai_compatible import (
    OpenAICompatibleProvider,
    _OpenAICompatibleStream,
)

# Claude on OpenRouter reads PDFs natively, whatever the catalog says.
_CLAUDE_SLUG_PREFIX = "anthropic/claude-"

# Backward-compatible alias — the stream class is fully generic.
_OpenRouterStream = _OpenAICompatibleStream


class OpenRouterProvider(OpenAICompatibleProvider):
    """LLMProvider implementation backed by OpenRouter."""

    def _extend_body(self, slug: str, body: dict[str, Any]) -> None:
        """A request carrying a PDF (an OpenAI ``file`` part, from an
        Anthropic ``document`` block) names OpenRouter's file-parser engine:
        ``native`` for a model that reads files itself — Claude, or any the
        catalog lists with "file" input — so the PDF goes to it untouched;
        otherwise ``PDF_OPENROUTER_ENGINE`` (default ``mistral-ocr``, the
        engine for scans). A request with no file part is left byte-identical.
        """
        if not _has_file_part(body):
            return
        body["plugins"] = [{"id": "file-parser", "pdf": {"engine": _pdf_engine(slug)}}]

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://openrouter.ai/api/v1",
        app_title: str = "Open Executive",
        referer: str | None = None,
        timeout_s: float = 180.0,
        slug_lookup: dict[str, str] | None = None,
        spec_lookup: dict[str, FeatureSpec] | None = None,
        model_resolver: Callable[[str], tuple[str, FeatureSpec] | None] | None = None,
    ) -> None:
        # OpenRouter attribution headers — surfaced in your dashboard alongside
        # the cost data so you can attribute usage back to this app.
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            default_headers={
                "HTTP-Referer": referer or "https://github.com/",
                "X-Title": app_title,
            },
            timeout_s=timeout_s,
            slug_lookup=slug_lookup,
            spec_lookup=spec_lookup,
            model_resolver=model_resolver,
            # OpenRouter-format request extension (see
            # translator.to_openai_request). Also correct for OPENROUTER_BASE_URL
            # pointed at a third-party gateway, as long as it actually speaks
            # OpenRouter's request format — that's the documented contract for
            # this class, unlike the generic OpenAICompatibleProvider base
            # (used for LOCAL_MODELS), which defaults this off because it may
            # front a plain OpenAI-compatible server or a strict pass-through
            # to real Anthropic that rejects the field outright.
            include_usage_accounting=True,
        )


def _has_file_part(body: dict[str, Any]) -> bool:
    for message in body.get("messages") or []:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list) and any(
            isinstance(part, dict) and part.get("type") == "file" for part in content
        ):
            return True
    return False


def _pdf_engine(slug: str) -> str:
    from openexecutive.config import get_settings
    from openexecutive.providers import openrouter_catalog

    if slug.startswith(_CLAUDE_SLUG_PREFIX) or openrouter_catalog.accepts_files(slug):
        return "native"
    return str(get_settings().pdf_openrouter_engine)
