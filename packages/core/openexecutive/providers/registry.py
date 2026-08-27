"""Model registry + per-call provider routing.

``MODEL_SPECS`` is the single source of truth for: what we ship to the
Council UI, which OpenRouter slug each Claude model maps to, and which
Anthropic-only features each model tolerates. ``get_provider(model)``
picks the backend per call so the user can flip an agent's model in the
Council UI and have requests for that agent — and only that agent —
route differently.
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from openexecutive.config import get_settings
from openexecutive.providers.anthropic_provider import AnthropicProvider
from openexecutive.providers.feature_gate import FeatureSpec
from openexecutive.providers.openai_compatible import OpenAICompatibleProvider
from openexecutive.providers.openrouter_provider import OpenRouterProvider
from openexecutive.providers.provider import LLMProvider

# Anthropic-direct slugs — used as canonical model names everywhere in
# the codebase (config defaults, agent class defaults, override DB).
ANTHROPIC_DIRECT_MODELS: list[str] = [
    "claude-opus-4-7",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
]

# Models reachable only through OpenRouter. These strings ARE the
# OpenRouter slug — we don't translate them on the way through. Curated
# PAID models only: the rate-limited ``:free`` tier (and the utility_fast-
# only free-model matrix) was removed — its 429s surfaced as user-visible
# errors and the per-agent free-model surface was more than it earned.
# BYO-model routing through OpenRouter is unchanged — add a slug here to
# surface it in the Council UI dropdown.
OPENROUTER_MODELS: list[str] = [
    "openai/gpt-5",
    "openai/gpt-5-mini",
    "openai/gpt-5-nano",
    "google/gemini-2.5-pro",
    "google/gemini-2.5-flash",
    "google/gemini-2.5-flash-lite",
    "meta-llama/llama-3.3-70b-instruct",
    "deepseek/deepseek-r1",
    "x-ai/grok-4",
]


# Per-Claude OpenRouter slug. The Anthropic-direct name is the registry
# key; the value is what we send when OPENROUTER_ENABLED is on.
_CLAUDE_OPENROUTER_SLUGS: dict[str, str] = {
    "claude-opus-4-7": "anthropic/claude-opus-4.7",
    "claude-sonnet-5": "anthropic/claude-sonnet-5",
    "claude-sonnet-4-6": "anthropic/claude-sonnet-4.6",
    "claude-haiku-4-5-20251001": "anthropic/claude-haiku-4.5",
}


_CLAUDE_FEATURE_SPEC = FeatureSpec(
    supports_cache_control=True,
    supports_thinking=True,
    supports_web_search=True,
    supports_tool_use=True,
)


# Per-non-Claude model spec. ``supports_tool_use`` is universal across
# the curated set; the other three are off — Anthropic-specific server
# tools and prompt-caching annotations have no OpenAI-format equivalent.
_DEFAULT_NON_CLAUDE_SPEC = FeatureSpec(
    supports_cache_control=False,
    supports_thinking=False,
    supports_web_search=False,
    supports_tool_use=True,
)


def _local_models(settings: Any) -> list[str]:
    """Configured local model slugs, or ``[]`` when local routing is off.

    Read defensively: lightweight test settings stubs may omit the field.
    """
    if not getattr(settings, "local_models_enabled", False):
        return []
    return list(getattr(settings, "local_models", []) or [])


def allowed_models() -> list[str]:
    """Flat allowlist the Council UI's dropdown reads.

    The dropdown can't offer a model the runtime won't actually serve, so
    each family is folded in only when it's reachable:

    * Anthropic-direct trio — when an ``ANTHROPIC_API_KEY`` is set, OR when
      ``OPENROUTER_ENABLED`` is on (Claude is then reachable via OpenRouter).
    * OpenRouter set — when ``OPENROUTER_ENABLED`` is on.
    * Local models — when ``LOCAL_MODELS_ENABLED`` is on.
    """
    settings = get_settings()
    models: list[str] = []
    if getattr(settings, "anthropic_api_key", None) or settings.openrouter_enabled:
        models.extend(ANTHROPIC_DIRECT_MODELS)
    if settings.openrouter_enabled:
        models.extend(OPENROUTER_MODELS)
    models.extend(_local_models(settings))
    return models


def allowed_models_for(agent_id: str | None) -> list[str]:
    """Per-agent allowlist for the Council UI dropdown and PATCH validator.

    Every agent — specialists, the Executive, Quality Judge, and the
    ``utility_fast`` virtual agent — gets the same ``allowed_models()``
    list. (The ``utility_fast``-only free/cheap OpenRouter matrix was
    removed; ``agent_id`` is retained for call-site stability and any
    future per-agent rules.)
    """
    return allowed_models()


def _is_claude(model: str) -> bool:
    return model in _CLAUDE_OPENROUTER_SLUGS


# Module-level singletons — providers pool their own HTTP connections and
# are async-safe. Recreating them per call burns ~10 ms each.
_anthropic_provider: AnthropicProvider | None = None
_openrouter_provider: OpenRouterProvider | None = None
_local_provider: OpenAICompatibleProvider | None = None


def _anthropic() -> AnthropicProvider:
    global _anthropic_provider
    if _anthropic_provider is None:
        settings = get_settings()
        api_key = settings.anthropic_api_key
        if not api_key:
            # Reachable only when a Claude model is requested with no key and
            # OpenRouter off — e.g. an Anthropic-free deployment that left a
            # model setting pointed at Claude. Fail with actionable guidance.
            raise HTTPException(
                status_code=400,
                detail=(
                    "A Claude model was requested but ANTHROPIC_API_KEY is not "
                    "set. Set it, enable OpenRouter, or point the model setting "
                    "at a configured local model."
                ),
            )
        _anthropic_provider = AnthropicProvider(api_key=api_key)
    return _anthropic_provider


def _local() -> OpenAICompatibleProvider:
    global _local_provider
    if _local_provider is None:
        settings = get_settings()
        base_url = getattr(settings, "local_base_url", None)
        if not base_url:
            # The Settings model_validator already prevents LOCAL_MODELS_ENABLED
            # without a base URL, but defense in depth — a misconfigured env
            # could otherwise produce a request against an empty host.
            raise HTTPException(
                status_code=400,
                detail="Local model routing requires LOCAL_BASE_URL",
            )
        # Local models get the non-Claude feature spec: no cache_control,
        # thinking, or server-side web_search — those are Anthropic-only and
        # would 400 (or be silently ignored) on an OpenAI-compatible server.
        spec_lookup: dict[str, FeatureSpec] = {
            m: _DEFAULT_NON_CLAUDE_SPEC for m in _local_models(settings)
        }
        _local_provider = OpenAICompatibleProvider(
            base_url=base_url,
            api_key=getattr(settings, "local_api_key", None),
            timeout_s=getattr(settings, "local_timeout_s", 300.0),
            spec_lookup=spec_lookup,
        )
    return _local_provider


def _openrouter() -> OpenRouterProvider:
    global _openrouter_provider
    if _openrouter_provider is None:
        settings = get_settings()
        if not settings.openrouter_api_key:
            # The Settings model_validator already prevents OPENROUTER_ENABLED
            # without a key, but defense in depth — a misconfigured env could
            # otherwise produce a None-token request.
            raise HTTPException(
                status_code=400,
                detail="OpenRouter routing requires OPENROUTER_API_KEY",
            )
        # Pre-build the slug + spec lookup so the provider can resolve a
        # Claude model to its OpenRouter slug + feature spec on each call.
        slug_lookup = dict(_CLAUDE_OPENROUTER_SLUGS)
        spec_lookup: dict[str, FeatureSpec] = {
            m: _CLAUDE_FEATURE_SPEC for m in ANTHROPIC_DIRECT_MODELS
        }
        for m in OPENROUTER_MODELS:
            spec_lookup[m] = _DEFAULT_NON_CLAUDE_SPEC
        _openrouter_provider = OpenRouterProvider(
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
            app_title=settings.openrouter_app_title,
            referer=settings.openrouter_referer,
            timeout_s=settings.openrouter_timeout_s,
            slug_lookup=slug_lookup,
            spec_lookup=spec_lookup,
        )
    return _openrouter_provider


def get_provider(model: str) -> LLMProvider:
    """Return the provider that should serve calls for ``model``.

    Routing rules:

    * Claude family — Anthropic direct by default; OpenRouter when
      ``OPENROUTER_ENABLED`` is on.
    * Local models (slugs listed in ``LOCAL_MODELS`` with
      ``LOCAL_MODELS_ENABLED`` on) — the self-hosted OpenAI-compatible
      backend at ``LOCAL_BASE_URL``.
    * Other non-Claude (anything in ``OPENROUTER_MODELS``, or any unknown
      slug) — OpenRouter only. Raises HTTP 400 when ``OPENROUTER_ENABLED``
      is off, since we have no other backend that speaks those models.
    """
    settings = get_settings()
    if _is_claude(model):
        if settings.openrouter_enabled:
            return _openrouter()
        return _anthropic()
    # Local models take precedence for their configured slugs — a local
    # endpoint can serve them with no external dependency.
    if model in _local_models(settings):
        return _local()
    # Other non-Claude slugs require OpenRouter to be enabled.
    if not settings.openrouter_enabled:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Model {model!r} requires OPENROUTER_ENABLED=true (set "
                f"OPENROUTER_API_KEY and toggle the flag), or list it in "
                f"LOCAL_MODELS with LOCAL_MODELS_ENABLED=true to serve it "
                f"from a local OpenAI-compatible backend."
            ),
        )
    return _openrouter()


def _reset_for_tests() -> None:
    """Drop cached provider singletons. Test-only — pytest fixtures call this."""
    global _anthropic_provider, _openrouter_provider, _local_provider
    _anthropic_provider = None
    _openrouter_provider = None
    _local_provider = None
