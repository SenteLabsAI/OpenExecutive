"""LiteLLM-backed LLMProvider — one gateway, 100+ model providers.

LiteLLM (https://github.com/BerriAI/litellm) exposes every provider through
the OpenAI ``chat.completions`` shape and routes by *model prefix*
(``anthropic/claude-...``, ``gemini/gemini-2.5-pro``, ``bedrock/...``,
``azure/...``, ``groq/...``, ``ollama/...``, ...). That makes it a drop-in
transport for this app's OpenAI-compatible seam: the request/response/stream
translation lives entirely in ``translator.py`` (shared with
``OpenAICompatibleProvider``); this class only swaps the httpx call for
``litellm.acompletion``.

Why this over pointing ``OpenAICompatibleProvider`` at a LiteLLM *proxy*
base URL: the LiteLLM Python SDK routes to each provider's native endpoint
with that provider's *native auth* — AWS SigV4 (Bedrock), Google ADC
(Vertex), Azure AD — none of which a bare OpenAI ``base_url`` + bearer token
can express. Set ``LITELLM_BASE_URL`` only if you want to front a running
LiteLLM proxy instead; leave it unset to route natively by model prefix.

``litellm`` is an optional extra (``pip install 'openexecutive[litellm]'``)
and is imported lazily so a base install without it is unaffected.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable
from contextlib import AbstractAsyncContextManager
from types import SimpleNamespace
from typing import Any

from openexecutive.providers.feature_gate import FeatureSpec, apply_feature_gates
from openexecutive.providers.openai_compatible import OpenAICompatibleProvider
from openexecutive.providers.translator import (
    StreamAccumulator,
    from_openai_response,
    to_openai_request,
)

logger = logging.getLogger(__name__)


def _import_litellm() -> Any:
    try:
        import litellm
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise RuntimeError(
            "The LiteLLM provider requires the optional 'litellm' package. "
            "Install it with: pip install 'openexecutive[litellm]'"
        ) from exc
    return litellm


class LiteLLMProvider(OpenAICompatibleProvider):
    """LLMProvider backed by the LiteLLM SDK.

    Reuses the shared Anthropic<->OpenAI translation (via
    ``OpenAICompatibleProvider``'s ``_resolve`` + the translator helpers) but
    dispatches through ``litellm.acompletion`` instead of a raw HTTP client,
    so a single instance reaches any LiteLLM-supported provider by model
    prefix with that provider's native auth.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_s: float = 180.0,
        drop_params: bool = True,
        slug_lookup: dict[str, str] | None = None,
        spec_lookup: dict[str, FeatureSpec] | None = None,
    ) -> None:
        # Intentionally do NOT call super().__init__ — the base builds an
        # httpx.AsyncClient this transport never uses (and we override every
        # method that would touch it). We keep ``_api_key`` / ``_slug_lookup``
        # / ``_spec_lookup`` so the inherited ``_resolve`` helper works, and
        # use ``_api_base`` (optional) rather than the base's required
        # ``_base_url`` since LiteLLM routes by model prefix without one.
        self._api_key = api_key
        self._api_base = base_url.rstrip("/") if base_url else None
        self._timeout_s = timeout_s
        self._drop_params = drop_params
        self._slug_lookup = slug_lookup or {}
        self._spec_lookup = spec_lookup or {}

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    def _build_params(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Translate Anthropic-shape kwargs into ``litellm.acompletion`` params."""
        request_timeout = kwargs.pop("timeout", None)
        model = kwargs.pop("model", "")
        slug, spec = self._resolve(model)
        gated = apply_feature_gates(spec, kwargs)
        body = to_openai_request(slug, gated)
        # ``to_openai_request`` emits two OpenRouter-only fields: the
        # ``usage: {include: true}`` cost-accounting flag and the ``plugins``
        # web-search shim. Neither is a standard chat param, so drop them
        # before handing the body to LiteLLM / a native provider endpoint.
        body.pop("usage", None)
        body.pop("plugins", None)
        # Silently drop any per-provider-unsupported kwargs instead of 400ing
        # (Anthropic rejects OpenAI-only fields and vice-versa).
        body["drop_params"] = self._drop_params
        # Forward credentials only when set; blank ones let LiteLLM fall back
        # to each provider's own env var (ANTHROPIC_API_KEY, GEMINI_API_KEY,
        # AWS creds, ...).
        if self._api_key:
            body["api_key"] = self._api_key
        if self._api_base:
            body["api_base"] = self._api_base
        timeout = request_timeout if request_timeout is not None else self._timeout_s
        if timeout is not None:
            body["timeout"] = timeout
        return body

    # ------------------------------------------------------------------
    # LLMProvider surface
    # ------------------------------------------------------------------

    def messages_create(self, **kwargs: Any) -> Awaitable[Any]:
        return self._messages_create(kwargs)

    async def _messages_create(self, kwargs: dict[str, Any]) -> Any:
        litellm = _import_litellm()
        params = self._build_params(kwargs)
        try:
            response = await litellm.acompletion(**params)
        except Exception as exc:
            logger.error(
                "LiteLLM backend %s failed: %s", params.get("model", "?"), exc
            )
            raise
        # LiteLLM returns an OpenAI-shape ModelResponse (pydantic) — dump to a
        # plain dict so the shared translator can build the Anthropic Message.
        return from_openai_response(response.model_dump())

    def messages_stream(self, **kwargs: Any) -> AbstractAsyncContextManager[Any]:
        params = self._build_params(kwargs)
        params["stream"] = True
        # Ask for a final usage chunk; dropped by drop_params for providers
        # that don't support it.
        params.setdefault("stream_options", {"include_usage": True})
        return _LiteLLMStream(params=params)

    async def aclose(self) -> None:  # no HTTP client to close on this path
        return None


class _LiteLLMStream:
    """Wraps ``litellm.acompletion(stream=True)`` so it quacks like
    ``anthropic.AsyncMessageStreamManager`` — the same contract
    ``_OpenAICompatibleStream`` provides, driven by the LiteLLM SDK.
    """

    def __init__(self, *, params: dict[str, Any]) -> None:
        self._params = params
        self._accumulator = StreamAccumulator()
        self._finalized: SimpleNamespace | None = None
        self._stream: Any = None

    async def __aenter__(self) -> _LiteLLMStream:
        litellm = _import_litellm()
        try:
            self._stream = await litellm.acompletion(**self._params)
        except Exception as exc:
            logger.error(
                "LiteLLM stream %s failed: %s", self._params.get("model", "?"), exc
            )
            raise
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        aclose = getattr(self._stream, "aclose", None)
        if aclose is not None:
            await aclose()
        self._stream = None

    def __aiter__(self) -> AsyncIterator[Any]:
        return self._iter_events()

    async def _iter_events(self) -> AsyncIterator[Any]:
        if self._stream is None:
            return
        async for chunk in self._stream:
            for event in self._accumulator.feed(chunk.model_dump()):
                yield event
        self._finalized = self._accumulator.finalize()

    async def get_final_message(self) -> Any:
        if self._finalized is None:
            async for _ in self._iter_events():
                pass
        if self._finalized is None:
            self._finalized = self._accumulator.finalize()
        return self._finalized
