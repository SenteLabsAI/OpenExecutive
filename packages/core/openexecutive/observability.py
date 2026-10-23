"""Opt-in request tracing via Pydantic Logfire.

Off unless ``LOGFIRE_TOKEN`` is set; with it unset every helper here is a
no-op and ``get_provider`` hands back the bare provider. When on, traces carry
timings, model names and token / prompt-cache usage — never prompt or reply
text, request bodies or query strings, since those carry company data and
Logfire's own LLM instrumentation records message bodies unconditionally
(they sit on its scrubber's safe-key list). That is also why the Anthropic SDK
and httpx are not auto-instrumented: model calls are spanned at the provider
seam instead, and httpx spans would record full URLs — a Telegram bot token
lives in the request path.

Exceptions are recorded by type only: an SDK error body can echo request
content, and a validation error carries its input. Request URLs are reduced
to their route template so path parameters (document filenames, watchlist
slugs) never leave, and client address / host / user agent are blanked.
Incoming trace context and baggage are ignored, since unauthenticated callers
could otherwise write attributes into the project.
"""
from __future__ import annotations

import functools
import logging
from collections.abc import Callable, Iterator
from contextlib import (
    contextmanager,
)
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

if TYPE_CHECKING:
    from fastapi import FastAPI

    from openexecutive.providers.provider import LLMProvider

logger = logging.getLogger(__name__)

SERVICE_NAME = "openexecutive-api"

# Request-span attributes ASGI instrumentation sets at span start that can
# carry path parameters or a query string; ``_redact_request`` replaces them
# with the matched route template.
_URL_ATTRIBUTES = ("http.target", "http.url", "url.path", "url.full")
# Identify the caller or deployment rather than describe the request's timing.
_BLANKED_ATTRIBUTES = (
    "url.query",
    "http.host",
    "http.server_name",
    "server.address",
    "net.host.name",
    "net.peer.ip",
    "client.address",
    "http.client_ip",
    "http.user_agent",
    "user_agent.original",
)

P = ParamSpec("P")
R = TypeVar("R")

_enabled = False


def tracing_enabled() -> bool:
    return _enabled


def configure_tracing(app: FastAPI) -> None:
    """Start exporting traces for ``app`` if ``LOGFIRE_TOKEN`` is configured."""
    global _enabled
    token = _logfire_token()
    if not token:
        return

    import logfire

    # Re-running configure is safe: create_app() runs once per process in
    # production and once per TestClient in the suite.
    logfire.configure(
        token=token,
        service_name=SERVICE_NAME,
        service_version=app.version,
        console=False,
        distributed_tracing=False,
        add_baggage_to_attributes=False,
        advanced=logfire.AdvancedOptions(exception_callback=_record_type_only),
    )
    logfire.instrument_fastapi(
        app,
        # The default mapper records every endpoint argument, chat bodies included.
        request_attributes_mapper=lambda _request, _attributes: None,
        server_request_hook=_redact_request,
    )
    _enabled = True
    logger.info("Logfire tracing enabled (service=%s)", SERVICE_NAME)


def _logfire_token() -> str | None:
    from openexecutive.config import get_settings

    return get_settings().logfire_token


def _record_type_only(helper: Any) -> None:
    exception = helper.exception
    helper.no_record_exception()
    helper.span.set_attribute("exception.type", type(exception).__name__)


def _redact_request(span: Any, scope: dict[str, Any]) -> None:
    if not span.is_recording():
        return
    attributes = getattr(span, "attributes", None) or {}
    route = _route_template(scope)
    for key in _URL_ATTRIBUTES:
        if key in attributes:
            span.set_attribute(key, route)
    for key in _BLANKED_ATTRIBUTES:
        if key in attributes:
            span.set_attribute(key, "")


def _route_template(scope: dict[str, Any]) -> str:
    """The matched route's path template, e.g. ``/documents/{filename}``."""
    from starlette.routing import Match

    app = scope.get("app")
    for route in getattr(app, "routes", ()):
        match, _ = route.matches(scope)
        if match != Match.NONE:
            return str(getattr(route, "path", ""))
    return ""


@contextmanager
def trace_span(msg_template: str, **attributes: Any) -> Iterator[Any]:
    """A Logfire span when tracing is on; otherwise a no-op yielding ``None``."""
    if not _enabled:
        yield None
        return
    import logfire
    from opentelemetry.trace import Status, StatusCode, get_current_span

    # The exception is caught inside the span and re-raised after it closes:
    # logfire.span's own exit writes "Type: message" into the status
    # description before exception_callback runs, and the message can echo
    # request content (an SDK 4xx escaping a specialist, say).
    error: BaseException | None = None
    with logfire.span(msg_template, **attributes) as span:
        try:
            yield span
        except BaseException as exc:
            error = exc
            span.set_attribute("exception.type", type(exc).__name__)
            # LogfireSpan has no typed set_status; this span is the current one.
            get_current_span().set_status(Status(StatusCode.ERROR))
    if error is not None:
        raise error


def traced(msg_template: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorate a sync function to run inside ``trace_span(msg_template)``.

    Arguments are deliberately not recorded — they are usually user text.
    """

    def decorator(fn: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            with trace_span(msg_template):
                return fn(*args, **kwargs)

        return wrapper

    return decorator


def wrap_provider(provider: LLMProvider, backend: str) -> LLMProvider:
    if not _enabled:
        return provider
    from openexecutive.providers.traced import TracedProvider

    return TracedProvider(provider, backend)
