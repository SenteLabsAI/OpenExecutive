"""LLM provider abstraction.

All Anthropic-shaped LLM calls go through ``get_provider(model)`` rather
than instantiating ``anthropic.AsyncAnthropic`` directly. Today the only
backend is Anthropic itself; the layer exists so a Claude slug can be
routed via OpenRouter or OrcaRouter (and non-Claude OpenRouter slugs can be
selected per-agent) without rewriting every call site's Anthropic-shaped
request.
"""
from openexecutive.providers.provider import LLMProvider
from openexecutive.providers.registry import (
    ANTHROPIC_DIRECT_MODELS,
    OPENROUTER_MODELS,
    ORCAROUTER_MODELS,
    allowed_models,
    allowed_models_for,
    get_provider,
)

__all__ = [
    "ANTHROPIC_DIRECT_MODELS",
    "LLMProvider",
    "OPENROUTER_MODELS",
    "ORCAROUTER_MODELS",
    "allowed_models",
    "allowed_models_for",
    "get_provider",
]
