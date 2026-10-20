from __future__ import annotations

from collections.abc import Awaitable
from contextlib import AbstractAsyncContextManager
from typing import Any

import anthropic

from openexecutive.providers.feature_gate import (
    fit_claude_generation,
    relax_forced_tool_choice,
)


def _client_kwargs(
    *, api_key: str | None, timeout: float | None, workspace_id: str | None
) -> dict[str, Any]:
    """The one place the Anthropic client's construction is defined.

    Shared so that the header an organisation-scoped key needs cannot be
    attached in one caller and forgotten in another — which is exactly how
    #128 would have half-shipped, with chat fixed and `make eval` still 400ing.

    An omitted key is left out entirely rather than passed as ``None``, so the
    SDK's own ``ANTHROPIC_API_KEY`` lookup still applies.
    """
    kwargs: dict[str, Any] = {}
    if api_key:
        kwargs["api_key"] = api_key
    if timeout is not None:
        kwargs["timeout"] = timeout
    if workspace_id:
        # An organisation-scoped key carries no workspace of its own, so
        # Anthropic 400s every call without this header. Set as a default
        # header rather than per-request: it applies to create and stream alike.
        kwargs["default_headers"] = {"anthropic-workspace-id": workspace_id}
    return kwargs


def configured_async_client(
    *,
    api_key: str | None = None,
    timeout: float | None = None,
) -> anthropic.AsyncAnthropic:
    """An ``AsyncAnthropic`` carrying this install's workspace configuration.

    For callers outside the request path — the eval runner and its judges —
    which build their own client rather than going through the registry.
    Anything omitted is resolved from ``Settings``.
    """
    from openexecutive.config import get_settings

    settings = get_settings()
    return anthropic.AsyncAnthropic(
        **_client_kwargs(
            api_key=api_key or settings.anthropic_api_key,
            timeout=timeout,
            workspace_id=getattr(settings, "anthropic_workspace_id", None),
        )
    )


class AnthropicProvider:
    """Thin delegate over ``anthropic.AsyncAnthropic``.

    Holds one ``AsyncAnthropic`` for the lifetime of the process; the SDK
    is async-safe and pools its own httpx connections.
    """

    def __init__(
        self,
        *,
        api_key: str,
        timeout: float | None = None,
        workspace_id: str | None = None,
    ) -> None:
        # Explicit, never settings-resolving: the registry passes what it
        # read, and a test constructing this directly must not pick up an
        # ambient workspace id from the environment.
        self._client = anthropic.AsyncAnthropic(
            **_client_kwargs(api_key=api_key, timeout=timeout, workspace_id=workspace_id)
        )

    def messages_create(self, **kwargs: Any) -> Awaitable[Any]:
        model = str(kwargs.get("model", ""))
        kwargs = fit_claude_generation(model, relax_forced_tool_choice(model, kwargs))
        return self._client.messages.create(**kwargs)

    def messages_stream(self, **kwargs: Any) -> AbstractAsyncContextManager[Any]:
        model = str(kwargs.get("model", ""))
        kwargs = fit_claude_generation(model, relax_forced_tool_choice(model, kwargs))
        return self._client.messages.stream(**kwargs)
