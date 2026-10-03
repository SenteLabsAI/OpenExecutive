"""Messages sent while the Executive works are folded into the running turn.

Covers the inbox itself (orchestrator/turn_inbox.py), the agent loop taking
it at a step boundary, the history merge that keeps stored rows alternating,
POST /chat/add, and the one-turn-at-a-time lock on a web conversation.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import chat as chat_route
from openexecutive.orchestrator.executive import Executive
from openexecutive.orchestrator.session import Session
from openexecutive.orchestrator.turn_inbox import (
    MAX_PENDING_MESSAGES,
    TurnInbox,
    render_added_messages,
    with_added,
)

from ._agent_loop_fakes import FakeStream, FinalMsg, ScriptedProvider, TextBlock, ToolUseBlock

# ---------------------------------------------------------------- inbox --


def test_take_hands_over_pending_once_in_order() -> None:
    inbox = TurnInbox()
    assert inbox.add("m-000001", "make it Friday")
    assert inbox.add("m-000002", "and cc Dana")
    taken = inbox.take()
    assert [m.text for m in taken] == ["make it Friday", "and cc Dana"]
    assert inbox.take() == []
    assert inbox.taken_texts() == ["make it Friday", "and cc Dana"]


def test_closed_inbox_refuses_and_drops_what_was_never_taken() -> None:
    inbox = TurnInbox()
    inbox.add("m-000001", "first")
    inbox.take()
    inbox.add("m-000002", "too late")
    inbox.close()
    assert not inbox.add("m-000003", "after close")
    assert inbox.take() == []
    # Only what the model saw counts as part of the turn.
    assert inbox.taken_texts() == ["first"]


def test_duplicate_blank_and_overflow_are_refused() -> None:
    inbox = TurnInbox()
    assert inbox.add("m-000001", "one")
    assert not inbox.add("m-000001", "same id again")
    assert not inbox.add("m-000002", "   ")
    for i in range(2, MAX_PENDING_MESSAGES + 1):
        assert inbox.add(f"m-{i:06d}", f"msg {i}")
    assert not inbox.add("m-999999", "one too many")


def test_with_added_appends_taken_words_only() -> None:
    inbox = TurnInbox()
    inbox.add("m-000001", "make it Friday")
    assert with_added("book the room", inbox) == "book the room"
    inbox.take()
    assert with_added("book the room", inbox) == "book the room\n\nmake it Friday"
    # A turn with nothing to learn from (Act as me) stays empty.
    assert with_added("", inbox) == ""
    assert with_added("x", None) == "x"


# ----------------------------------------------------------- agent loop --


class _StubGateway:
    async def call_tool(self, _input: dict[str, Any]) -> str:
        return '{"status": "ok"}'

    async def search_tools(self, _input: dict[str, Any]) -> str:
        return '{"tools": []}'

    async def load_mcp_server(self, _input: dict[str, Any]) -> str:
        return '{"status": "ok"}'


class _ArrivingProvider(ScriptedProvider):
    """Delivers a message into the inbox while each model call runs."""

    def __init__(self, final_msgs: list[FinalMsg], inbox: TurnInbox, arrivals: list[str]) -> None:
        super().__init__(final_msgs)
        self._inbox = inbox
        self._arrivals = list(arrivals)

    def messages_stream(self, **kwargs: Any) -> FakeStream:
        if self._arrivals:
            n = len(self.calls) + 1
            self._inbox.add(f"msg-{n:06d}", self._arrivals.pop(0))
        return super().messages_stream(**kwargs)


@pytest.fixture(autouse=True)
def _no_audit(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        "openexecutive.orchestrator.executive.audit_log",
        lambda *a, **k: rows.append((a, k)),
    )
    return rows


def _run(provider: ScriptedProvider, inbox: TurnInbox) -> list[Any]:
    executive = Executive()
    executive._mcp_gateway = _StubGateway()  # type: ignore[assignment]

    async def _go() -> list[Any]:
        items: list[Any] = []
        with patch("openexecutive.orchestrator.executive.get_provider", return_value=provider):
            async for item in executive._stream_agent_loop(
                system_blocks=[],
                messages=[{"role": "user", "content": "book the room"}],
                model="claude-test",
                inbox=inbox,
            ):
                items.append(item)
        return items

    return asyncio.run(_go())


def _tool_round() -> FinalMsg:
    return FinalMsg(
        [ToolUseBlock("toolu_1", "call_tool", {"name": "calendar__find_room", "arguments": {}})],
        "tool_use",
    )


def test_message_sent_during_a_tool_round_reaches_the_next_call() -> None:
    inbox = TurnInbox()
    provider = _ArrivingProvider(
        [_tool_round(), FinalMsg([TextBlock("Booked for Friday.")], "end_turn")],
        inbox,
        ["actually make it Friday"],
    )
    items = _run(provider, inbox)

    last_user = provider.calls[1]["messages"][-1]
    assert last_user["role"] == "user"
    blocks = last_user["content"]
    # Tool results first (the API requires it), then the added message.
    assert blocks[0]["type"] == "tool_result"
    assert blocks[-1]["type"] == "text"
    assert "actually make it Friday" in blocks[-1]["text"]
    assert {"type": "message_added", "ids": ["msg-000001"]} in items
    assert inbox.taken_texts() == ["actually make it Friday"]


def test_message_sent_during_the_final_answer_is_left_for_the_next_turn() -> None:
    inbox = TurnInbox()
    provider = _ArrivingProvider(
        [_tool_round(), FinalMsg([TextBlock("Booked.")], "end_turn")],
        inbox,
        ["first", "while it writes the answer"],
    )
    items = _run(provider, inbox)
    inbox.close()

    added = [i for i in items if isinstance(i, dict) and i.get("type") == "message_added"]
    assert added == [{"type": "message_added", "ids": ["msg-000001"]}]
    assert inbox.taken_texts() == ["first"]


def test_render_names_the_newer_message_as_winning() -> None:
    inbox = TurnInbox()
    inbox.add("m-000001", "make it Friday")
    text = render_added_messages(inbox.take())
    assert "<added_message>\nmake it Friday\n</added_message>" in text
    assert "follow the newer message" in text


def test_history_with_two_user_rows_is_sent_as_one_user_turn() -> None:
    session = Session(session_id="s-merge")
    session.conversation_history = [
        {"role": "user", "content": "book the room"},
        {"role": "user", "content": "make it Friday"},
        {"role": "assistant", "content": "Booked for Friday."},
    ]
    messages = Executive()._build_messages(session, "thanks")
    assert messages[0] == {"role": "user", "content": "book the room\n\nmake it Friday"}
    assert messages[1]["role"] == "assistant"
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    # The session's own rows are untouched.
    assert session.conversation_history[0]["content"] == "book the room"


# ------------------------------------------------------------ /chat/add --


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    chat_route._active_stops.clear()
    monkeypatch.setattr(chat_route, "_resolve_caller_person_id", lambda _r: 7)
    app = FastAPI()
    app.include_router(chat_route.router)
    yield TestClient(app)
    chat_route._active_stops.clear()


def _seed(client_turn_id: str, owner: str) -> chat_route._StopEntry:
    entry = chat_route._StopEntry(
        asyncio.Event(), owner, "t-abc123456789", time.monotonic(), TurnInbox()
    )
    chat_route._active_stops[client_turn_id] = entry
    return entry


def _add(client: TestClient, turn: str, msg_id: str = "msg-000001", text: str = "make it Friday") -> Any:
    return client.post(
        "/chat/add", json={"client_turn_id": turn, "message_id": msg_id, "message": text}
    )


def test_add_queues_for_the_owner(client: TestClient) -> None:
    entry = _seed("live-turn-0001", "person:7")
    resp = _add(client, "live-turn-0001")
    assert resp.status_code == 200
    assert resp.json() == {"status": "added", "turn_id": "t-abc123456789"}
    assert entry.inbox is not None
    assert [m.text for m in entry.inbox.take()] == ["make it Friday"]


def test_add_to_someone_elses_or_unknown_turn_is_the_same_404(client: TestClient) -> None:
    entry = _seed("someone-elses-1", "person:99")
    theirs = _add(client, "someone-elses-1")
    unknown = _add(client, "no-such-turn-1")
    assert theirs.status_code == unknown.status_code == 404
    assert theirs.json() == unknown.json()
    assert entry.inbox is not None and entry.inbox.take() == []


def test_add_to_a_stopped_or_closed_turn_is_404(client: TestClient) -> None:
    stopped = _seed("stopped-turn-1", "person:7")
    stopped.event.set()
    assert _add(client, "stopped-turn-1").status_code == 404
    closed = _seed("closed-turn-01", "person:7")
    assert closed.inbox is not None
    closed.inbox.close()
    assert _add(client, "closed-turn-01").status_code == 404


def test_add_rejects_a_malformed_message_id(client: TestClient) -> None:
    _seed("live-turn-0002", "person:7")
    assert _add(client, "live-turn-0002", msg_id="bad id\n").status_code == 422


def test_registered_turns_get_an_inbox() -> None:
    chat_route._active_stops.clear()
    try:
        assert chat_route._register_stop("new-turn-00001", "person:7", "t-new00000000")
        assert isinstance(chat_route._active_stops["new-turn-00001"].inbox, TurnInbox)
    finally:
        chat_route._active_stops.clear()


# ------------------------------------------------------- session lock --


def test_second_turn_on_a_conversation_waits_for_the_first() -> None:
    async def _go() -> list[str]:
        order: list[str] = []
        first = await chat_route._acquire_session_turn("conv-1", 5)
        assert first is not None

        async def second() -> None:
            lock = await chat_route._acquire_session_turn("conv-1", 5)
            order.append("second")
            chat_route._release_session_turn("conv-1", lock)

        task = asyncio.create_task(second())
        await asyncio.sleep(0.01)
        order.append("first done")
        chat_route._release_session_turn("conv-1", first)
        await task
        return order

    assert asyncio.run(_go()) == ["first done", "second"]
    assert "conv-1" not in chat_route._session_turn_locks


def test_a_turn_arriving_as_the_lock_passes_on_still_waits() -> None:
    """A releases with B queued; C arrives before B has run. C must queue on
    the same lock, not find the map empty and run beside B."""

    async def _go() -> list[str]:
        events: list[str] = []
        a = await chat_route._acquire_session_turn("conv-3", 5)

        async def turn(name: str) -> None:
            lock = await chat_route._acquire_session_turn("conv-3", 5)
            events.append(f"{name} start")
            await asyncio.sleep(0.01)
            events.append(f"{name} end")
            chat_route._release_session_turn("conv-3", lock)

        b = asyncio.create_task(turn("B"))
        await asyncio.sleep(0)
        chat_route._release_session_turn("conv-3", a)
        c = asyncio.create_task(turn("C"))
        await asyncio.gather(b, c)
        return events

    assert asyncio.run(_go()) == ["B start", "B end", "C start", "C end"]
    assert "conv-3" not in chat_route._session_turn_locks
    assert "conv-3" not in chat_route._session_turn_users


def test_lock_wait_is_bounded() -> None:
    async def _go() -> Any:
        held = await chat_route._acquire_session_turn("conv-2", 5)
        late = await chat_route._acquire_session_turn("conv-2", 0.01)
        chat_route._release_session_turn("conv-2", held)
        return late

    assert asyncio.run(_go()) is None
    assert "conv-2" not in chat_route._session_turn_locks
    assert "conv-2" not in chat_route._session_turn_users
