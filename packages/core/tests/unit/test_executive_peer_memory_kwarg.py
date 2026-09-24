"""When the caller pre-resolves peer_memory_context (e.g. parallel fetch in
the route), Executive must NOT call its own _honcho_prefetch — otherwise we
pay the Honcho roundtrip twice. Tests both streaming entrypoints and the
non-streaming chat() wrapper.
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _no_audit_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep this module's `memory_extraction` rows out of the default
    `./episodic_memory.db`.

    Driving a turn schedules episodic extraction fire-and-forget, and its
    audit row goes through `audit.log_event`, which targets the default DB
    whatever `db_path` the extraction was handed. Those rows surface as real
    data in other modules' assertions during a full run only — the audit-log
    pollution trap in CLAUDE.md.
    """
    monkeypatch.setattr(
        "openexecutive.audit.log_event", lambda *a, **kw: None, raising=False
    )


os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-used")


def _make_stream_cm(final_msg: object) -> MagicMock:
    """Async-context-manager stub mimicking AsyncAnthropic.messages.stream()."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=cm)
    cm.__aexit__ = AsyncMock(return_value=None)

    async def _aiter():
        if False:
            yield None  # pragma: no cover

    cm.__aiter__ = lambda self=cm: _aiter()
    cm.get_final_message = AsyncMock(return_value=final_msg)
    return cm


def _end_turn_msg() -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text="done")],
        stop_reason="end_turn",
    )


def test_stream_chat_skips_prefetch_when_peer_memory_context_provided() -> None:
    from openexecutive.orchestrator.executive import Executive
    from openexecutive.orchestrator.session import Session

    prefetch_calls = 0

    async def fake_prefetch(*_args, **_kwargs) -> str:
        nonlocal prefetch_calls
        prefetch_calls += 1
        return "should-not-see-this"

    fake_provider = SimpleNamespace(
        messages_stream=lambda **_kw: _make_stream_cm(_end_turn_msg())
    )

    exec_ = Executive()

    async def _run() -> None:
        session = Session(session_id="t-kwarg-1")
        async for _ in exec_.stream_chat(
            "hello",
            session,
            person_id=42,
            peer_memory_context="PRE-RESOLVED",
        ):
            pass

    with patch(
        "openexecutive.orchestrator.executive.get_provider",
        return_value=fake_provider,
    ), patch(
        "openexecutive.memory.honcho_client.prefetch", new=fake_prefetch
    ):
        asyncio.run(_run())

    assert prefetch_calls == 0, "Executive ran its own prefetch despite caller providing one"


def test_stream_chat_still_prefetches_when_context_not_provided() -> None:
    """Backward-compat: callers that don't pass the kwarg still get the
    internal prefetch (so the email poller, Discord etc. continue working)."""
    from openexecutive.orchestrator.executive import Executive
    from openexecutive.orchestrator.session import Session

    prefetch_calls = 0

    async def fake_prefetch(*_args, **_kwargs) -> str:
        nonlocal prefetch_calls
        prefetch_calls += 1
        return ""

    fake_provider = SimpleNamespace(
        messages_stream=lambda **_kw: _make_stream_cm(_end_turn_msg())
    )

    exec_ = Executive()

    async def _run() -> None:
        session = Session(session_id="t-kwarg-2")
        async for _ in exec_.stream_chat("hello", session, person_id=42):
            pass

    with patch(
        "openexecutive.orchestrator.executive.get_provider",
        return_value=fake_provider,
    ), patch(
        "openexecutive.memory.honcho_client.prefetch", new=fake_prefetch
    ):
        asyncio.run(_run())

    assert prefetch_calls == 1


def test_stream_chat_committee_skips_prefetch_when_provided() -> None:
    from openexecutive.orchestrator.executive import Executive
    from openexecutive.orchestrator.session import Session

    prefetch_calls = 0

    async def fake_prefetch(*_args, **_kwargs) -> str:
        nonlocal prefetch_calls
        prefetch_calls += 1
        return "x"

    # Committee path fans out reviewers; mock the SDK so any streamed call
    # returns end_turn immediately and we don't actually drive the loop.
    fake_provider = SimpleNamespace(
        messages_stream=lambda **_kw: _make_stream_cm(_end_turn_msg()),
        messages_create=AsyncMock(
            return_value=SimpleNamespace(
                content=[SimpleNamespace(type="text", text="ok")],
                stop_reason="end_turn",
            )
        ),
    )

    exec_ = Executive()

    async def _run() -> None:
        session = Session(session_id="t-kwarg-3")
        async for _ in exec_.stream_chat_with_committee(
            "hello",
            session,
            person_id=42,
            peer_memory_context="PRE-RESOLVED-COMMITTEE",
        ):
            pass

    with patch(
        "openexecutive.orchestrator.executive.get_provider",
        return_value=fake_provider,
    ), patch(
        "openexecutive.memory.honcho_client.prefetch", new=fake_prefetch
    ):
        asyncio.run(_run())

    assert prefetch_calls == 0, "stream_chat_with_committee ran its own prefetch despite caller providing one"


def test_chat_wrapper_forwards_peer_memory_context_kwarg() -> None:
    """The non-streaming chat() wrapper must forward the kwarg, not drop it."""
    from openexecutive.orchestrator.executive import Executive
    from openexecutive.orchestrator.session import Session

    prefetch_calls = 0

    async def fake_prefetch(*_args, **_kwargs) -> str:
        nonlocal prefetch_calls
        prefetch_calls += 1
        return "x"

    fake_provider = SimpleNamespace(
        messages_stream=lambda **_kw: _make_stream_cm(_end_turn_msg())
    )

    exec_ = Executive()

    async def _run() -> str:
        session = Session(session_id="t-kwarg-4")
        return await exec_.chat(
            "hello",
            session,
            person_id=42,
            peer_memory_context="WRAPPED-CONTEXT",
        )

    with patch(
        "openexecutive.orchestrator.executive.get_provider",
        return_value=fake_provider,
    ), patch(
        "openexecutive.memory.honcho_client.prefetch", new=fake_prefetch
    ):
        result = asyncio.run(_run())

    assert prefetch_calls == 0
    assert isinstance(result, str)  # didn't crash
