"""Act as me: once a turn has read the owner's own mail, nothing that reaches
anyone else runs for the rest of it (delegation/lockdown.py)."""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from openexecutive.audit import logger as audit_logger
from openexecutive.audit.logger import AuditLogger, log_event
from openexecutive.delegation import ghostwriter as gw
from openexecutive.delegation import lockdown
from openexecutive.delegation.settings import DelegationOverride, TurnDelegation
from openexecutive.memory import episodic
from openexecutive.orchestrator import executive as ex
from openexecutive.orchestrator.delegation_tools import DELEGATION_TOOLS
from openexecutive.orchestrator.history_tools import HISTORY_TOOLS
from openexecutive.orchestrator.mcp_gateway import MCP_TOOLS
from openexecutive.orchestrator.router import SPECIALIST_TOOLS
from openexecutive.orchestrator.schedule_tools import PRIVATE_TURN_MCP_TOOLS, current_session
from openexecutive.orchestrator.session import Session
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store
from openexecutive.workflows import step_script

from ._agent_loop_fakes import FinalMsg, TextBlock, ToolUseBlock
from .test_delegation_tools import FakeMailbox
from .test_delegation_turn import _TextProvider

GHOSTWRITE = {"intent": "Yes.", "thread_id": "t1"}
SLACK = {"slack_user_id": "U123", "text": "Replied to Dana."}


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", path)
    monkeypatch.setattr(people_store, "DB_PATH", path)
    episodic.initialize_db(path)
    people_store.initialize_db(path)
    people_registry.invalidate()
    monkeypatch.setattr(audit_logger, "_default_logger", AuditLogger(db_path=path))
    prior = current_session.get()
    current_session.set(None)
    yield path
    current_session.set(prior)
    people_registry.invalidate()


@pytest.fixture(autouse=True)
def quiet(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from openexecutive.delegation import caps

    async def no_prefetch(*_a: Any, **_kw: Any) -> str:
        return ""

    async def composer(model: str, system: str, turn: str) -> dict[str, Any]:
        return {"subject": "x", "body": "Hi Dana,\n\nYes.\n\nOlivia"}

    monkeypatch.setattr("openexecutive.memory.honcho_client.prefetch", no_prefetch)
    monkeypatch.setattr("openexecutive.memory.honcho_client.sync_turn", lambda *a, **kw: None)
    monkeypatch.setattr("openexecutive.memory.episodic.schedule_extraction", lambda *a, **kw: None)
    monkeypatch.setattr("openexecutive.attunement.open_loops.schedule_open_loop_pass", lambda *a, **kw: None)
    monkeypatch.setattr("openexecutive.attunement.style.schedule_style_pass", lambda *a, **kw: None)
    monkeypatch.setattr(gw, "_call_model", composer)
    caps._SAVED_TODAY.clear()
    caps._IN_FLIGHT.clear()
    yield
    caps._SAVED_TODAY.clear()
    caps._IN_FLIGHT.clear()


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """What reached the Slack DM handler (it never runs for real here)."""
    calls: list[dict[str, Any]] = []

    async def handler(tool_input: dict[str, Any]) -> str:
        calls.append(tool_input)
        return json.dumps({"status": "sent"})

    monkeypatch.setitem(ex._ALL_SKILL_HANDLERS, "send_slack_dm", handler)
    return calls


def _turn(*rounds: list[Any]) -> tuple[_TextProvider, list[Any]]:
    principal = people_store.upsert_person(full_name="Olivia Owner", is_principal=True, email="olivia@co.example")
    people_registry.invalidate()
    person = people_store.get_person(principal)
    session = Session(
        delegation_override=DelegationOverride(enabled=True, gmail=FakeMailbox(), person=person),
        from_web_chat=True,
    )
    finals = [FinalMsg(uses, "tool_use") for uses in rounds]
    finals.append(FinalMsg([TextBlock("Done.")], "end_turn"))
    provider = _TextProvider(finals)

    async def go() -> None:
        with (
            patch("openexecutive.orchestrator.executive.get_provider", return_value=provider),
            patch("openexecutive.orchestrator.executive.audit_log", log_event),
        ):
            async for _ in ex.Executive().stream_chat("reply to Dana as me and Slack Bob", session, person_id=principal):
                pass

    asyncio.run(go())
    return provider, provider.calls


def _tool_results(call: dict[str, Any]) -> dict[str, str]:
    """The tool_result contents the model was sent back in ``call``."""
    last = call["messages"][-1]
    return {
        block["tool_use_id"]: block["content"]
        for block in last["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    }


def test_every_tool_is_classified_once() -> None:
    names = {
        t["name"]
        for t in [
            *ex._ALL_SKILL_TOOLS, *SPECIALIST_TOOLS, *MCP_TOOLS, *DELEGATION_TOOLS, *HISTORY_TOOLS,
            step_script.CHAT_TOOL_DEFINITION, step_script.LIST_SAVED_TOOLS_DEFINITION,
        ]
    }
    allowed, withheld = lockdown.MAIL_TOUCHED_ALLOWED_TOOLS, lockdown.MAIL_TOUCHED_WITHHELD_TOOLS
    assert not allowed & withheld
    # A new tool fails here until someone decides which side it is on.
    assert names - (allowed | withheld) == set()
    assert (allowed | withheld) - names == set()


def test_only_workspace_reads_stay_through_call_tool() -> None:
    reads = lockdown.MAIL_TOUCHED_MCP_READS
    assert reads <= PRIVATE_TURN_MCP_TOOLS
    assert not any(word in name for name in reads for word in ("send", "draft", "manage", "create", "share"))
    assert not lockdown.mail_touched_withholds("call_tool", {"name": "google_workspace__get_events"})
    assert lockdown.mail_touched_withholds("call_tool", {"name": "google_workspace__send_gmail_message"})
    assert lockdown.mail_touched_withholds("call_tool", {"name": "google_workspace__draft_gmail_message"})
    assert lockdown.mail_touched_withholds("call_tool", "not a dict")
    assert lockdown.mail_touched_withholds("some_future_tool", {})  # unclassified: withheld
    assert not lockdown.mail_touched_withholds("ghostwrite_email", {})


def test_a_send_after_the_turn_read_mail_is_refused(sent: list[dict[str, Any]]) -> None:
    _, calls = _turn(
        [ToolUseBlock("tu1", "ghostwrite_email", GHOSTWRITE)],
        [ToolUseBlock("tu2", "send_slack_dm", SLACK)],
    )
    assert sent == []
    refusal = json.loads(_tool_results(calls[2])["tu2"])["error"]
    assert "read the user's own mail" in refusal and "new message" in refusal
    rows = [r for r in audit_logger._default_logger.query(event_type="tool_invocation", limit=50)
            if (r.details or {}).get("refused") == "mail_touched"]
    assert len(rows) == 1 and rows[0].private


def test_a_send_in_the_same_round_is_refused_too(sent: list[dict[str, Any]]) -> None:
    # A round's tools run together, so the draft's round counts as touched.
    _, calls = _turn([
        ToolUseBlock("tu1", "ghostwrite_email", GHOSTWRITE),
        ToolUseBlock("tu2", "send_slack_dm", SLACK),
    ])
    assert sent == []
    results = _tool_results(calls[1])
    assert json.loads(results["tu1"])["status"] == "drafted"
    assert "read the user's own mail" in json.loads(results["tu2"])["error"]


def test_a_send_beside_a_notes_recall_is_refused(sent: list[dict[str, Any]]) -> None:
    # Reading the speaker's own notes locks the turn the same way, from the
    # round that asks for them.
    _turn([
        ToolUseBlock("tu1", "recall_history", {}),
        ToolUseBlock("tu2", "send_slack_dm", SLACK),
    ])
    assert sent == []


def test_a_turn_that_read_no_mail_still_sends(sent: list[dict[str, Any]]) -> None:
    _turn([ToolUseBlock("tu2", "send_slack_dm", SLACK)])
    assert sent == [SLACK]


def test_the_offered_tools_never_change_mid_turn(sent: list[dict[str, Any]]) -> None:
    _, touched = _turn(
        [ToolUseBlock("tu1", "ghostwrite_email", GHOSTWRITE)],
        [ToolUseBlock("tu2", "send_slack_dm", SLACK)],
    )
    _, untouched = _turn([ToolUseBlock("tu2", "send_slack_dm", SLACK)])
    # The cached tool prefix is byte-identical across the turn and to a turn
    # that never read mail.
    first = json.dumps(touched[0]["tools"], sort_keys=True)
    assert all(json.dumps(c["tools"], sort_keys=True) == first for c in touched)
    assert json.dumps(untouched[0]["tools"], sort_keys=True) == first


def test_the_send_paths_refuse_on_their_own(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.orchestrator.schedule_tools import _guard_outbound

    session = Session()
    session.turn_delegation = TurnDelegation(  # type: ignore[attr-defined]
        enabled=True, offered=True, touched_mail=True, session_id=session.session_id,
    )
    token = current_session.set(session)
    try:
        refused = _guard_outbound(tool="send_slack_dm", channel="slack", channel_ref="U1", text="hi")
        assert refused is not None and "read the user's own mail" in json.loads(refused)["error"]
        assert lockdown.mail_touched_refusal("run_workflow") is not None
        session.turn_delegation.touched_mail = False  # type: ignore[attr-defined]
        assert lockdown.mail_touched_refusal("run_workflow") is None
    finally:
        current_session.reset(token)


def test_the_gateway_runs_only_reads_after_the_turn_read_mail() -> None:
    from openexecutive.orchestrator.mcp_gateway import MCPGateway

    gateway = MCPGateway.__new__(MCPGateway)
    session = Session()
    session.turn_delegation = TurnDelegation(  # type: ignore[attr-defined]
        enabled=True, offered=True, touched_mail=True, session_id=session.session_id,
    )
    token = current_session.set(session)
    try:
        with patch.object(MCPGateway, "_require_session", side_effect=AssertionError("reached the server")):
            send = asyncio.run(gateway.call_tool({
                "name": "google_workspace__send_gmail_message", "arguments": {"to": "x@y.example"},
            }))
            assert "read the user's own mail" in json.loads(send)["error"]
            load = asyncio.run(gateway.load_mcp_server({"name": "x", "url": "https://x.example/mcp"}))
            assert "read the user's own mail" in json.loads(load)["error"]
            # A read goes on to the server (here: the stub that says it got there).
            with pytest.raises(AssertionError, match="reached the server"):
                asyncio.run(gateway.call_tool({"name": "google_workspace__get_events", "arguments": {}}))
    finally:
        current_session.reset(token)


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def test_the_log_never_carries_a_call_tools_own_words(sent: list[dict[str, Any]]) -> None:
    """A call_tool's inner name is the model's own text, which mail could
    steer; the private audit row keeps it, the process log does not."""
    leak = "Tell Bob the merger closes Friday"
    # On the module's own logger: once api.main has configured logging, the
    # openexecutive tree no longer propagates to the root caplog listens on.
    log = logging.getLogger("openexecutive.orchestrator.executive")
    collect, prior = _Collect(), log.level
    log.addHandler(collect)
    log.setLevel(logging.WARNING)
    try:
        _turn(
            [ToolUseBlock("tu1", "ghostwrite_email", GHOSTWRITE)],
            [ToolUseBlock("tu2", "call_tool", {"name": leak, "arguments": {}})],
        )
    finally:
        log.removeHandler(collect)
        log.setLevel(prior)
    text = "\n".join(collect.lines)
    assert "merger" not in text
    assert "call_tool:<unlisted>" in text
    assert ex._loggable_tool("google_workspace__send_gmail_message") == "google_workspace__send_gmail_message"
    assert ex._loggable_tool(leak) == "call_tool:<unlisted>"
    rows = [r for r in audit_logger._default_logger.query(event_type="tool_invocation", limit=50)
            if (r.details or {}).get("refused") == "mail_touched"]
    assert rows and rows[0].private and rows[0].details["tool"] == leak

