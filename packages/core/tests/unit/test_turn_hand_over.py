"""I'll finish it: the person stops a long turn and takes over the work.

Covers the inbox's one-shot hand-over flag (orchestrator/turn_inbox.py), the
agent loop writing up what it has without running another tool, and POST
/chat/hand-over.
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
from openexecutive.orchestrator.turn_inbox import (
    HAND_OVER_EMPTY_REPLY,
    HAND_OVER_NOTE,
    TurnInbox,
)

from ._agent_loop_fakes import FakeStream, FinalMsg, ScriptedProvider, TextBlock, ToolUseBlock

# ---------------------------------------------------------------- inbox --


def test_hand_over_is_taken_once() -> None:
    inbox = TurnInbox()
    assert not inbox.take_hand_over()
    assert inbox.request_hand_over()
    assert inbox.request_hand_over()  # asking twice is harmless
    assert inbox.take_hand_over()
    assert not inbox.take_hand_over()


def test_closed_inbox_refuses_a_hand_over() -> None:
    inbox = TurnInbox()
    inbox.close()
    assert not inbox.request_hand_over()
    assert not inbox.take_hand_over()


# ----------------------------------------------------------- agent loop --


class _StubGateway:
    def __init__(self) -> None:
        self.calls = 0

    async def call_tool(self, _input: dict[str, Any]) -> str:
        self.calls += 1
        return '{"status": "ok"}'

    async def search_tools(self, _input: dict[str, Any]) -> str:
        return '{"tools": []}'

    async def load_mcp_server(self, _input: dict[str, Any]) -> str:
        return '{"status": "ok"}'


class _HandOverProvider(ScriptedProvider):
    """The person presses I'll finish it while the first model call runs."""

    def __init__(self, final_msgs: list[FinalMsg], inbox: TurnInbox) -> None:
        super().__init__(final_msgs)
        self._inbox = inbox

    def messages_stream(self, **kwargs: Any) -> FakeStream:
        if not self.calls:
            self._inbox.request_hand_over()
        return super().messages_stream(**kwargs)


@pytest.fixture(autouse=True)
def _no_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openexecutive.orchestrator.executive.audit_log", lambda *a, **k: None)


def _tool_round(tool_id: str = "toolu_1") -> FinalMsg:
    return FinalMsg(
        [ToolUseBlock(tool_id, "call_tool", {"name": "drive__read_file", "arguments": {}})],
        "tool_use",
    )


def _run(
    provider: ScriptedProvider, inbox: TurnInbox, max_iterations: int = 15
) -> tuple[list[Any], _StubGateway]:
    executive = Executive()
    gateway = _StubGateway()
    executive._mcp_gateway = gateway  # type: ignore[assignment]

    async def _go() -> list[Any]:
        items: list[Any] = []
        with patch("openexecutive.orchestrator.executive.get_provider", return_value=provider):
            async for item in executive._stream_agent_loop(
                system_blocks=[],
                messages=[{"role": "user", "content": "put the board pack together"}],
                model="claude-test",
                max_iterations=max_iterations,
                inbox=inbox,
            ):
                items.append(item)
        return items

    return asyncio.run(_go()), gateway


def test_hand_over_asks_for_a_write_up_after_the_running_round() -> None:
    inbox = TurnInbox()
    provider = _HandOverProvider(
        [_tool_round(), FinalMsg([TextBlock("Here is the draft so far.")], "end_turn")],
        inbox,
    )
    items, gateway = _run(provider, inbox)

    # The round already running finishes; then the note rides after its results.
    assert gateway.calls == 1
    blocks = provider.calls[1]["messages"][-1]["content"]
    assert blocks[0]["type"] == "tool_result"
    assert blocks[-1] == {"type": "text", "text": HAND_OVER_NOTE}
    assert {"type": "handing_over"} in items
    assert len(provider.calls) == 2


def test_hand_over_runs_no_tool_the_write_up_asks_for() -> None:
    inbox = TurnInbox()
    provider = _HandOverProvider(
        [
            _tool_round(),
            FinalMsg(
                [TextBlock("Numbers are in. "), ToolUseBlock("toolu_2", "call_tool", {"name": "x", "arguments": {}})],
                "tool_use",
            ),
            FinalMsg([TextBlock("never reached")], "end_turn"),
        ],
        inbox,
    )
    items, gateway = _run(provider, inbox)

    assert gateway.calls == 1
    assert len(provider.calls) == 2
    # What it wrote before asking for the tool is the reply.
    assert HAND_OVER_EMPTY_REPLY not in items


def test_hand_over_with_nothing_written_says_so() -> None:
    inbox = TurnInbox()
    provider = _HandOverProvider([_tool_round(), _tool_round("toolu_2")], inbox)
    items, gateway = _run(provider, inbox)

    assert gateway.calls == 1
    assert HAND_OVER_EMPTY_REPLY in items


def test_hand_over_in_the_last_round_is_not_taken() -> None:
    inbox = TurnInbox()
    provider = _HandOverProvider([_tool_round()], inbox)
    items, _gateway = _run(provider, inbox, max_iterations=1)

    # No round is left to write anything up in: no handing_over event, and
    # the request stays untaken.
    assert {"type": "handing_over"} not in items
    assert not inbox.hand_over_taken


# ------------------------------------------------------ /chat/hand-over --


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
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


def _hand_over(client: TestClient, turn: str) -> Any:
    return client.post("/chat/hand-over", json={"client_turn_id": turn})


def test_hand_over_reaches_the_owners_turn(client: TestClient) -> None:
    entry = _seed("live-turn-0001", "person:7")
    resp = _hand_over(client, "live-turn-0001")
    assert resp.status_code == 200
    assert resp.json() == {"status": "handing_over", "turn_id": "t-abc123456789"}
    assert entry.inbox is not None and entry.inbox.take_hand_over()


def test_hand_over_of_someone_elses_or_unknown_turn_is_the_same_404(client: TestClient) -> None:
    entry = _seed("someone-elses-1", "person:99")
    theirs = _hand_over(client, "someone-elses-1")
    unknown = _hand_over(client, "no-such-turn-1")
    assert theirs.status_code == unknown.status_code == 404
    assert theirs.json() == unknown.json()
    assert entry.inbox is not None and not entry.inbox.take_hand_over()


def test_hand_over_of_a_stopped_or_closed_turn_is_404(client: TestClient) -> None:
    stopped = _seed("stopped-turn-1", "person:7")
    stopped.event.set()
    assert _hand_over(client, "stopped-turn-1").status_code == 404
    closed = _seed("closed-turn-01", "person:7")
    assert closed.inbox is not None
    closed.inbox.close()
    assert _hand_over(client, "closed-turn-01").status_code == 404
