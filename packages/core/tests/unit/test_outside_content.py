"""Once a turn has read content from outside the team, writes, contact changes
and the Act as me lockdown's tools stay shut for the rest of it; someone added
to People this turn is reached only from the next one
(orchestrator/outside_content.py)."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from openexecutive.audit import logger as audit_logger
from openexecutive.audit.logger import AuditLogger
from openexecutive.delegation import lockdown
from openexecutive.memory import episodic
from openexecutive.orchestrator import executive as ex
from openexecutive.orchestrator import outside_content as oc
from openexecutive.orchestrator.schedule_tools import current_session
from openexecutive.orchestrator.session import Session
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store

from ._agent_loop_fakes import FinalMsg, ScriptedProvider, TextBlock, ToolUseBlock

READ_DOC = {"name": "google_workspace__get_doc_content", "arguments": {"document_id": "d1"}}
WRITE_DOC = {"name": "google_workspace__modify_doc_text", "arguments": {"document_id": "d2", "text": "Q3"}}
SEND = {"name": "google_workspace__send_gmail_message", "arguments": {"to": "ben@co.example"}}
CONTACT = {"full_name": "Mallory", "email": "mallory@evil.example", "kind": "contact"}


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
def _principals_own_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openexecutive.orchestrator.executive.principal_only_withheld", lambda _s: frozenset())


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "openexecutive.orchestrator.executive.audit_log",
        lambda event_type, summary, **kw: rows.append({"event_type": event_type, "summary": summary, **kw}),
    )
    return rows


@pytest.fixture
def upserts(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def handler(tool_input: dict[str, Any]) -> str:
        calls.append(tool_input)
        return json.dumps({"status": "ok"})

    monkeypatch.setitem(ex._ALL_SKILL_HANDLERS, "upsert_person", handler)
    return calls


class _Gateway:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def search_tools(self, _input: dict[str, Any]) -> str:
        return json.dumps({"tools": []})

    async def call_tool(self, tool_input: dict[str, Any]) -> str:
        self.calls.append(tool_input)
        if tool_input["name"] == "drive__list_items":
            return json.dumps([{"id": "f1"}, {"id": "f2"}])
        return json.dumps({"ok": True})

    async def load_mcp_server(self, _input: dict[str, Any]) -> str:
        return json.dumps({"ok": True})


class _WebResult:
    type = "web_search_tool_result"

    def model_dump(self, **_kw: Any) -> dict[str, Any]:
        return {"type": "web_search_tool_result", "tool_use_id": "srv1", "content": []}


def _run(
    *rounds: list[Any], gateway: _Gateway | None = None, message: str = "do it", session: Session | None = None,
) -> ScriptedProvider:
    finals = [FinalMsg(uses, "tool_use") for uses in rounds]
    finals.append(FinalMsg([TextBlock("Done.")], "end_turn"))
    provider = ScriptedProvider(finals)
    exec_ = ex.Executive(mcp_gateway=gateway)  # type: ignore[arg-type]

    async def go() -> None:
        state = oc.begin_turn(session or Session(), user_message=message)
        try:
            with patch("openexecutive.orchestrator.executive.get_provider", return_value=provider):
                async for _ in exec_._stream_agent_loop(
                    system_blocks=[], messages=[{"role": "user", "content": message}], model="claude-test",
                ):
                    pass
        finally:
            oc.end_turn(state)

    asyncio.run(go())
    return provider


def _result(provider: ScriptedProvider, use_id: str) -> str:
    for call in provider.calls:
        last = call["messages"][-1]
        if isinstance(last.get("content"), list):
            for block in last["content"]:
                if isinstance(block, dict) and block.get("tool_use_id") == use_id:
                    return str(block["content"])
    raise AssertionError(f"no result for {use_id}")


# --- what counts as outside content ---------------------------------------


def test_an_attachment_or_a_synced_excerpt_starts_the_turn_read() -> None:
    def started(**kw: Any) -> set[str]:
        async def go() -> set[str]:
            state = oc.begin_turn(Session(), **kw)
            oc.end_turn(state)
            return state.sources

        return asyncio.run(go())

    assert started(user_message="what's up") == set()
    assert started(user_message="see this", attachment_blocks=[{"type": "image"}]) == {oc.ATTACHMENT}
    assert started(user_message="[Attached: deck.pdf]\nslides") == {oc.ATTACHMENT}
    assert started(user_message="hi", retrieved_context="### Synced Google Drive (unreviewed…") == {oc.SYNCED}
    assert started(user_message="hi", retrieved_context="### From your company documents:\n[a.md] x") == set()


def test_a_turn_ends_with_its_record_and_nothing_is_marked_outside_one() -> None:
    async def go() -> None:
        oc.mark(oc.WEB)  # no turn: nothing to mark
        assert oc.current() is None and not oc.read_outside()
        outer = oc.begin_turn(Session())
        inner = oc.begin_turn(Session())
        oc.mark(oc.GATEWAY)
        oc.end_turn(inner)
        assert oc.current() is outer and not oc.read_outside()
        oc.end_turn(outer)
        assert oc.current() is None

    asyncio.run(go())


@pytest.mark.parametrize(("name", "reads"), [
    ("google_workspace__get_doc_content", True),
    ("google_workspace__search_gmail_messages", True),
    ("microsoft_365__list-mail-messages", True),
    ("notion__notion-fetch", True),
    ("slack__conversations_history", True),
    (None, True),
    # Another server's result is outside text whatever its tool is called.
    ("someserver__create_note", True),
    ("google_workspace__send_gmail_message", False),
    ("google_workspace__modify_doc_text", False),
    ("google_workspace__find_and_replace_doc", False),
    ("google_workspace__manage_event", False),
    ("microsoft_365__send-mail", False),
])
def test_a_gateway_call_reads_unless_its_name_writes(name: Any, reads: bool) -> None:
    assert oc.gateway_call_reads(name) is reads


def test_what_stays_shut_after_an_outside_read() -> None:
    assert oc.outside_withholds("upsert_person", CONTACT)
    assert oc.outside_withholds("call_tool", SEND)
    assert oc.outside_withholds("call_tool", WRITE_DOC)
    assert oc.outside_withholds("call_tool", {"name": "fetch__fetch_url", "arguments": {"url": "https://x"}})
    assert oc.outside_withholds("call_tool", "not a dict")
    for tool in ("read_document", "run_script", "run_workflow", "send_company_broadcast", "schedule_followup"):
        assert oc.outside_withholds(tool, {}), tool
    assert not oc.outside_withholds("call_tool", READ_DOC)
    for tool in ("message_person", "consult_specialist", "remember_fact", "list_people"):
        assert not oc.outside_withholds(tool, {}), tool


def test_mail_from_outside_people_is_held_from_the_start_but_may_alert() -> None:
    async def go() -> None:
        state = oc.begin_turn(Session(sender_off_team=True))
        assert oc.read_outside() and state.sources == {oc.SENDER}
        assert oc.answers_off_team_mail()
        assert not oc.outside_withholds("create_alert", {"subject": "s", "body": "b"})
        assert oc.outside_withholds("call_tool", SEND)
        assert oc.outside_withholds("upsert_person", CONTACT)
        assert oc.outside_withholds("schedule_followup", {})
        # The one asking is the sender: no direct message, no watchlist removal.
        for tool in sorted(oc.SENDER_WITHHELD_TOOLS):
            assert oc.outside_withholds(tool, {}), tool
        error = json.loads(oc.outside_withheld_error("schedule_followup"))["error"]
        assert "not in People" in error and "create_alert" in error
        oc.end_turn(state)
        # Any other turn's hold still shuts the alert, as before.
        state = oc.begin_turn(Session())
        assert not oc.read_outside()
        oc.mark(oc.WEB)
        assert oc.outside_withholds("create_alert", {})
        assert not oc.outside_withholds("message_person", {})
        assert "next message" in json.loads(oc.outside_withheld_error("x"))["error"]
        oc.end_turn(state)

    asyncio.run(go())


def test_an_alert_from_mail_outside_people_is_the_principals_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.orchestrator import alert_tools

    events: list[Any] = []
    monkeypatch.setattr("openexecutive.alerts.pipeline.schedule_evaluation", events.append)
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **kw: None)

    async def go() -> None:
        session = Session(sender_off_team=True)
        session.email_from = "stranger@evil.example"
        token = current_session.set(session)
        state = oc.begin_turn(session)
        try:
            await alert_tools.handle_create_alert({
                "subject": "CEO approved payment", "body": "Pay IBAN X",
                "source": "ceo", "from_address": "ceo@co.example",
                "department": "finance", "assigned_to_person_id": 4,
            })
        finally:
            oc.end_turn(state)
            current_session.reset(token)

    asyncio.run(go())
    [event] = events
    assert event.private is True
    assert event.routed_to_person_id is None and not (event.channel or "").startswith("department:")
    assert event.source == "email" and event.from_ == "stranger@evil.example"


def test_the_handlers_refuse_on_their_own() -> None:
    async def go() -> None:
        state = oc.begin_turn(Session())
        assert lockdown.outside_reach_refusal("run_workflow") is None
        oc.mark(oc.WEB)
        refusal = lockdown.outside_reach_refusal("run_workflow")
        assert refusal is not None and "outside the team" in json.loads(refusal)["error"]
        assert lockdown.outside_reach_refusal("upsert_person", CONTACT) is not None
        assert lockdown.outside_reach_refusal("call_tool", SEND, tool_name="call_tool") is not None
        assert lockdown.outside_reach_refusal("call_tool", READ_DOC, tool_name="call_tool") is None
        oc.end_turn(state)
        assert lockdown.outside_reach_refusal("run_workflow") is None

    asyncio.run(go())


# --- the chat loop ----------------------------------------------------------


def test_a_write_after_a_read_is_refused_for_the_rest_of_the_turn(audit: list[dict[str, Any]]) -> None:
    gateway = _Gateway()
    provider = _run(
        [ToolUseBlock("tu1", "call_tool", READ_DOC)],
        [ToolUseBlock("tu2", "call_tool", WRITE_DOC)],
        gateway=gateway,
    )
    assert gateway.calls == [READ_DOC]
    error = json.loads(_result(provider, "tu2"))["error"]
    assert "outside the team" in error and "next message" in error
    rows = [r for r in audit if r["details"].get("refused") == "outside_content"]
    assert len(rows) == 1 and rows[0]["details"]["tool"] == WRITE_DOC["name"]
    assert rows[0]["details"]["sources"] == [oc.GATEWAY]


def test_the_hold_survives_a_task_per_step(audit: list[dict[str, Any]]) -> None:
    """The web chat route resumes the executive's generator in a fresh task
    per step (``_sse_body``), each with a copy of the route's context: the
    record has to outlive the step that began the turn."""
    gateway = _Gateway()
    provider = ScriptedProvider([
        FinalMsg([ToolUseBlock("tu1", "call_tool", READ_DOC)], "tool_use"),
        FinalMsg([ToolUseBlock("tu2", "call_tool", WRITE_DOC)], "tool_use"),
        FinalMsg([TextBlock("Done.")], "end_turn"),
    ])
    exec_ = ex.Executive(mcp_gateway=gateway)  # type: ignore[arg-type]
    session = Session()

    async def turn() -> Any:
        state = oc.begin_turn(session, user_message="do it")
        try:
            async for event in exec_._stream_agent_loop(
                system_blocks=[], messages=[{"role": "user", "content": "do it"}], model="claude-test",
            ):
                yield event
        finally:
            oc.end_turn(state)

    async def route() -> None:
        token = current_session.set(session)
        try:
            stream = turn()
            with patch("openexecutive.orchestrator.executive.get_provider", return_value=provider):
                while True:
                    try:
                        await asyncio.ensure_future(stream.__anext__())
                    except StopAsyncIteration:
                        break
            # Closed: nothing after the turn is held.
            assert not oc.read_outside()
        finally:
            current_session.reset(token)

    asyncio.run(route())
    assert gateway.calls == [READ_DOC]
    assert "outside the team" in json.loads(_result(provider, "tu2"))["error"]


def test_a_turn_for_mail_from_outside_people_writes_nothing_but_an_alert(
    monkeypatch: pytest.MonkeyPatch, audit: list[dict[str, Any]],
) -> None:
    alerts: list[dict[str, Any]] = []

    async def alert(tool_input: dict[str, Any]) -> str:
        alerts.append(tool_input)
        return json.dumps({"status": "alert_scheduled"})

    monkeypatch.setitem(ex._ALL_SKILL_HANDLERS, "create_alert", alert)
    gateway = _Gateway()
    provider = _run(
        [
            ToolUseBlock("tu1", "call_tool", SEND),
            ToolUseBlock("tu2", "create_alert", {"subject": "Invoice due", "body": "Pay by Friday"}),
        ],
        gateway=gateway,
        session=Session(sender_off_team=True),
    )
    assert gateway.calls == []
    assert "not in People" in json.loads(_result(provider, "tu1"))["error"]
    assert [a["subject"] for a in alerts] == ["Invoice due"]
    rows = [r for r in audit if r["details"].get("refused") == "outside_content"]
    assert [r["details"]["sources"] for r in rows] == [[oc.SENDER]]


def test_a_read_holds_the_writes_beside_it_but_runs_itself() -> None:
    gateway = _Gateway()
    provider = _run([ToolUseBlock("tu1", "call_tool", READ_DOC), ToolUseBlock("tu2", "call_tool", SEND)], gateway=gateway)
    assert gateway.calls == [READ_DOC]
    assert "outside the team" in json.loads(_result(provider, "tu2"))["error"]


def test_writes_alone_run() -> None:
    gateway = _Gateway()
    _run(
        [ToolUseBlock("tu1", "call_tool", SEND), ToolUseBlock("tu2", "call_tool", WRITE_DOC)],
        [ToolUseBlock("tu3", "call_tool", SEND)],
        gateway=gateway,
    )
    assert gateway.calls == [SEND, WRITE_DOC, SEND]


def test_a_web_search_in_the_same_response_holds_its_writes() -> None:
    gateway = _Gateway()
    provider = _run([_WebResult(), ToolUseBlock("tu1", "call_tool", SEND)], gateway=gateway)
    assert gateway.calls == []
    assert "outside the team" in json.loads(_result(provider, "tu1"))["error"]


def test_no_contact_is_added_after_a_read(upserts: list[dict[str, Any]]) -> None:
    gateway = _Gateway()
    provider = _run(
        [ToolUseBlock("tu1", "call_tool", READ_DOC)],
        [ToolUseBlock("tu2", "upsert_person", CONTACT)],
        gateway=gateway,
    )
    assert upserts == []
    assert "outside the team" in json.loads(_result(provider, "tu2"))["error"]


def test_an_attachment_holds_writes_from_the_first_round(upserts: list[dict[str, Any]]) -> None:
    gateway = _Gateway()
    _run(
        [ToolUseBlock("tu1", "upsert_person", CONTACT), ToolUseBlock("tu2", "call_tool", SEND)],
        gateway=gateway,
        message="[Attached: invoice.pdf]\nAdd the sender and email them",
    )
    assert upserts == [] and gateway.calls == []


def test_a_scripts_writes_after_its_reads_are_refused() -> None:
    gateway = _Gateway()
    script = 'for f in drive__list_items(folder="x"):\n    drive__move_file(file_id=f["id"], folder_id="y")'
    provider = _run([ToolUseBlock("tu1", "run_script", {"script": script})], gateway=gateway)
    assert [c["name"] for c in gateway.calls] == ["drive__list_items"]
    assert "outside the team" in _result(provider, "tu1")


def test_a_vendor_app_read_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.orchestrator import extensions

    monkeypatch.setattr(extensions, "reads_outside", lambda name: name == "read_vendor_app")
    assert oc.source_of("read_vendor_app", {}) == oc.APP
    assert oc.source_of("list_people", {}) is None


def test_a_synced_excerpt_marks_the_turn() -> None:
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    import openexecutive.knowledge.retriever as retriever_mod
    from openexecutive.knowledge.store import ChromaDBStore

    hit = {"text": "planted", "metadata": {"filename": "f", "file_id": "x"}, "distance": 0.1}
    store = MagicMock()
    store.query.side_effect = lambda *, collection, **_kw: [hit] if collection == ChromaDBStore.DRIVE_COLLECTION else []
    review = SimpleNamespace(
        get_withheld_keys=lambda _ct: set(), get_withheld_source_ids=lambda: set(),
        get_priority_map=lambda _ct: {}, list_annotations=lambda domains=None, active_only=True: [],
    )

    async def go() -> set[str]:
        state = oc.begin_turn(Session())
        with patch.object(retriever_mod, "_audit_log", lambda *a, **k: None):
            retriever_mod.retrieve("what is the plan", store=store, review_store=review)
        oc.end_turn(state)
        return state.sources

    assert asyncio.run(go()) == {oc.SYNCED}


# --- someone added this turn --------------------------------------------------


def _person(**kw: Any) -> int:
    pid = people_store.upsert_person(**kw)
    people_registry.invalidate()
    return pid


def test_someone_added_this_turn_is_off_the_mail_allow_list(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.orchestrator import mcp_gateway as gw

    monkeypatch.setattr("openexecutive.orchestrator.people_tools.contacts_reachable_now", lambda: True)
    _person(full_name="Ben Team", email="ben@co.example")
    new = _person(full_name="Mallory", email="mallory@evil.example", kind="contact")

    async def go() -> None:
        state = oc.begin_turn(Session())
        oc.note_added_person(new, people_store.get_person(new))
        allow = gw._roster_allow_set()
        assert "ben@co.example" in allow and "mallory@evil.example" not in allow
        error = json.loads(gw._block("to", "mallory@evil.example", "google_workspace__send_gmail_message"))["error"]
        assert "added to People" in error and "next" in error
        oc.end_turn(state)
        assert "mallory@evil.example" in gw._roster_allow_set()

    asyncio.run(go())


def test_someone_added_this_turn_gets_no_dm_or_invite() -> None:
    from openexecutive.orchestrator import calendar_tools, schedule_tools

    pid = _person(full_name="Mallory", email="mallory@evil.example", slack_user_id="U9")

    async def go() -> None:
        state = oc.begin_turn(Session())
        oc.note_added_person(pid, people_store.get_person(pid))
        held = schedule_tools._guard_outbound(tool="send_slack_dm", channel="slack_dm", channel_ref="U9", text="hi")
        assert held is not None and "added to People" in json.loads(held)["error"]
        sent = json.loads(await schedule_tools.message_person({"person_id": pid, "text": "hi"}))
        assert "added to People" in sent["error"]
        booked = calendar_tools._resolve_attendees([pid], include_principal=False, max_attendees=5)
        assert isinstance(booked, dict) and "added to People" in booked["error"]
        oc.end_turn(state)

    asyncio.run(go())


def test_upsert_person_holds_a_new_person_and_a_new_address_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.orchestrator import people_tools

    monkeypatch.setattr(people_tools, "_refuse_unless_owner", lambda _tool: None)
    monkeypatch.setattr("openexecutive.integrations.roster_intake.after_roster_write", lambda: None)
    existing = _person(full_name="Ben Team", email="ben@co.example")

    async def go() -> tuple[oc.TurnOutside, int]:
        state = oc.begin_turn(Session())
        created = json.loads(await people_tools.handle_upsert_person(dict(CONTACT)))
        await people_tools.handle_upsert_person({"person_id": existing, "full_name": "Ben Team", "role": "CFO"})
        oc.end_turn(state)
        return state, created["person_id"]

    state, created = asyncio.run(go())
    assert state.added_people == {created}
    assert "mallory@evil.example" in state.added_refs

    async def readdress() -> set[int]:
        state = oc.begin_turn(Session())
        await people_tools.handle_upsert_person(
            {"person_id": existing, "full_name": "Ben Team", "email": "ben@evil.example"}
        )
        oc.end_turn(state)
        return state.added_people

    assert asyncio.run(readdress()) == {existing}



# --- the person's go to what a turn held -----------------------------------


@pytest.fixture(autouse=True)
def _no_held_steps() -> Iterator[None]:
    oc._HELD.clear()
    yield
    oc._HELD.clear()


def _any_result(provider: ScriptedProvider, use_id: str) -> str:
    """A tool result anywhere in the turn (the loop appends to one list)."""
    for message in provider.calls[-1]["messages"]:
        for block in message["content"] if isinstance(message.get("content"), list) else []:
            if isinstance(block, dict) and block.get("tool_use_id") == use_id:
                return str(block["content"])
    raise AssertionError(f"no result for {use_id}")


def _held_contact_turn(session: Session, gateway: _Gateway) -> None:
    """A turn that read a doc, then had adding the contact held."""
    _run(
        [ToolUseBlock("tu1", "call_tool", READ_DOC)],
        [ToolUseBlock("tu2", "upsert_person", CONTACT)],
        gateway=gateway, session=session, message="read the doc and add Mallory",
    )


def test_a_go_runs_the_held_step_before_a_new_read(upserts: list[dict[str, Any]]) -> None:
    session, gateway = Session(), _Gateway()
    _held_contact_turn(session, gateway)
    assert upserts == []
    assert oc._HELD[f"{session.session_id}|"][0] == ("upsert_person",)
    # Their go: the model reads again and adds the contact in one round. The
    # read waits, so the contact is added; the read runs in the next round.
    provider = _run(
        [ToolUseBlock("tu3", "call_tool", READ_DOC), ToolUseBlock("tu4", "upsert_person", CONTACT)],
        [ToolUseBlock("tu5", "call_tool", READ_DOC)],
        gateway=gateway, session=session, message="Go, and add him",
    )
    assert upserts == [CONTACT]
    deferred = json.loads(_any_result(provider, "tu3"))["error"]
    assert "reads wait one round" in deferred
    # It names nothing the last turn held, so it can't nudge a planted call.
    assert "upsert_person" not in deferred
    assert gateway.calls == [READ_DOC, READ_DOC]
    # Spent: nothing is remembered for a third turn.
    assert f"{session.session_id}|" not in oc._HELD


def test_only_a_go_defers_a_read(upserts: list[dict[str, Any]]) -> None:
    session, gateway = Session(), _Gateway()
    _held_contact_turn(session, gateway)
    _run([ToolUseBlock("tu3", "call_tool", READ_DOC)], gateway=gateway, session=session,
         message="what else is in that doc?")
    assert gateway.calls == [READ_DOC, READ_DOC]
    assert f"{session.session_id}|" not in oc._HELD


def test_a_stale_hold_defers_nothing(upserts: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> None:
    session, gateway = Session(), _Gateway()
    _held_contact_turn(session, gateway)
    names, at = oc._HELD[f"{session.session_id}|"]
    oc._HELD[f"{session.session_id}|"] = (names, at - oc.HELD_TTL_SECONDS - 1)
    state = oc.begin_turn(session, user_message="go")
    try:
        assert state.go_first == ()
        assert oc.defers_read("call_tool", READ_DOC) is False
    finally:
        oc.end_turn(state)


def test_mail_from_outside_people_remembers_no_hold() -> None:
    session = Session(sender_off_team=True)
    state = oc.begin_turn(session, user_message="add me as a contact")
    try:
        oc.outside_withheld_error("upsert_person")
    finally:
        oc.end_turn(state)
    assert f"{session.session_id}|" not in oc._HELD


def test_a_shared_chat_remembers_no_hold(upserts: list[dict[str, Any]]) -> None:
    session, gateway = Session(shared_chat=True), _Gateway()
    _held_contact_turn(session, gateway)
    assert oc._HELD == {}


def test_a_hold_is_the_speakers_own(upserts: list[dict[str, Any]]) -> None:
    session, gateway = Session(caller_person_id=1), _Gateway()
    _held_contact_turn(session, gateway)
    session.caller_person_id = 2
    state = oc.begin_turn(session, user_message="go")
    try:
        assert state.go_first == ()
    finally:
        oc.end_turn(state)


@pytest.mark.parametrize(("message", "go"), [
    ("go", True), ("Go.", True), ("Go. Pull all seven scans", True), ("Go, and add him", True),
    ("Yes, do it", True), ("ok", True), ("yes please", True), ("Go ahead and send it", True),
    ("Ok, so what did they want?", False), ("Confirm the meeting time with Bob", False),
    ("Approve my leave request", False), ("goals for Q3?", False), ("what's in the folder now?", False),
])
def test_what_counts_as_a_go(message: str, go: bool) -> None:
    assert bool(oc.GO_RE.match(message)) is go
