"""OrcaRouter backend — a thin specialization of ``OpenAICompatibleProvider``.

OrcaRouter speaks the OpenAI ``/chat/completions`` format, so the entire
request/response/stream machinery lives in the generic
``OpenAICompatibleProvider`` base — the same engine that backs
``OpenRouterProvider`` and the local/self-hosted backends. The only
OrcaRouter-specific bit is the default base URL; the model slugs are passed
through verbatim, matching OrcaRouter's provider/model namespace.

``_OrcaRouterStream`` is re-exported as an alias of the generic stream class
for parity with the OpenRouter module.
"""
from __future__ import annotations

from openexecutive.providers.feature_gate import FeatureSpec
from openexecutive.providers.openai_compatible import (
    OpenAICompatibleProvider,
    _OpenAICompatibleStream,
)

# Backward-compatible alias — the stream class is fully generic.
_OrcaRouterStream = _OpenAICompatibleStream


class OrcaRouterProvider(OpenAICompatibleProvider):
    """LLMProvider implementation backed by OrcaRouter."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.orcarouter.ai/v1",
        timeout_s: float = 180.0,
        slug_lookup: dict[str, str] | None = None,
        spec_lookup: dict[str, FeatureSpec] | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            timeout_s=timeout_s,
            slug_lookup=slug_lookup,
            spec_lookup=spec_lookup,
        )
