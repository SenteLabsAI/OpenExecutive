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

from openexecutive.providers.feature_gate import FeatureSpec
from openexecutive.providers.openai_compatible import (
    OpenAICompatibleProvider,
    _OpenAICompatibleStream,
)

# Backward-compatible alias — the stream class is fully generic.
_OpenRouterStream = _OpenAICompatibleStream


class OpenRouterProvider(OpenAICompatibleProvider):
    """LLMProvider implementation backed by OpenRouter."""

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
