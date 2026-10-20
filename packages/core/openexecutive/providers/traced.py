"""``LLMProvider`` decorator that spans each model call for Logfire tracing.

Lives with the providers because it implements the call surface rather than
making a call of its own: usage is recorded by the caller of
``get_provider(...)``, exactly as for the bare backend. Built only when
``openexecutive.observability`` has tracing on; see that module for what a
trace may and may not carry.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

from openexecutive.providers.provider import LLMProvider

# OpenTelemetry GenAI semantic-convention keys, so Logfire's LLM views pick
# the usage up without its content-recording instrumentation.
_USAGE_ATTRIBUTES = {
    "input_tokens": "gen_ai.usage.input_tokens",
    "output_tokens": "gen_ai.usage.output_tokens",
    "cache_read_input_tokens": "gen_ai.usage.cache_read.input_tokens",
    "cache_creation_input_tokens": "gen_ai.usage.cache_creation.input_tokens",
}


def _record_usage(span: Any, message: Any) -> None:
    if message is None:
        return
    usage = getattr(message, "usage", None)
    for field, key in _USAGE_ATTRIBUTES.items():
        value = getattr(usage, field, None)
        if isinstance(value, int):
            span.set_attribute(key, value)
    stop_reason = getattr(message, "stop_reason", None)
    if isinstance(stop_reason, str):
        span.set_attribute("gen_ai.response.finish_reasons", (stop_reason,))


class TracedProvider:
    """``LLMProvider`` decorator that spans each call with model and usage only.

    The span is started without attaching it to the current context: a
    streamed turn is driven step by step from fresh tasks (``/chat`` pumps the
    executive generator with ``ensure_future``), so a context attached in one
    task cannot be detached in another. Model calls open no child spans, so
    nothing needs it as a parent.
    """

    def __init__(self, inner: LLMProvider, backend: str) -> None:
        self._inner = inner
        self._backend = backend

    @asynccontextmanager
    async def _span(self, operation: str, kwargs: dict[str, Any]) -> AsyncIterator[Any]:
        from opentelemetry import trace

        model = kwargs.get("model")
        attributes: dict[str, Any] = {
            "gen_ai.request.model": str(model),
            "gen_ai.system": self._backend,
        }
        max_tokens = kwargs.get("max_tokens")
        if isinstance(max_tokens, int):
            attributes["gen_ai.request.max_tokens"] = max_tokens
        span = trace.get_tracer(__name__).start_span(f"{operation} {model}", attributes=attributes)
        try:
            yield span
        except BaseException as exc:
            span.set_attribute("exception.type", type(exc).__name__)
            span.set_status(trace.Status(trace.StatusCode.ERROR))
            raise
        finally:
            span.end()

    async def messages_create(self, **kwargs: Any) -> Any:
        async with self._span("messages.create", kwargs) as span:
            message = await self._inner.messages_create(**kwargs)
            _record_usage(span, message)
            return message

    @asynccontextmanager
    async def _stream(self, kwargs: dict[str, Any]) -> AsyncIterator[Any]:
        async with (
            self._span("messages.stream", kwargs) as span,
            self._inner.messages_stream(**kwargs) as stream,
        ):
            yield stream
            _record_usage(span, _final_snapshot(stream))

    def messages_stream(self, **kwargs: Any) -> AbstractAsyncContextManager[Any]:
        return self._stream(kwargs)


def _final_snapshot(stream: Any) -> Any:
    # Anthropic's MessageStream exposes the accumulated message here; a
    # translated backend may not, and a stream abandoned before message_start
    # raises. Usage is best-effort — it must never fail the caller's turn.
    try:
        return stream.current_message_snapshot
    except Exception:
        return None
