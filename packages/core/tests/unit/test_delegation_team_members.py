"""Act as me for team members: the owner's switch, who may have it, and what
stays a team member's alone — their audit rows, their reply cards and the
conversation that read their mail, the principal included."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import decisions as decisions_route
from openexecutive.api.routes import delegation as route
from openexecutive.audit import rows_for_person
from openexecutive.audit.logger import AuditLogger
from openexecutive.delegation import settings as dsettings
from openexecutive.delegation.settings import (
    TurnDelegation,
    can_delegate,
    enabled_for_install,
    pin_turn_delegation,
    set_enabled,
    set_team_members,
    team_members_enabled,
)
from openexecutive.memory import episodic, session_store
from openexecutive.memory.decision_ledger import create_decision_instance
from openexecutive.orchestrator.schedule_tools import current_session
from openexecutive.orchestrator.session import Session
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store

OWNER = {"x-caller-email": "olivia@co.example"}
TEAMMATE = {"x-caller-email": "ben@co.example"}


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", path)
    monkeypatch.setattr(people_store, "DB_PATH", path)
    episodic.initialize_db(path)
    people_store.initialize_db(path)
    for var in ("OE_LOCAL_LOGIN", "OE_PUBLIC_DEPLOYMENT"):
        monkeypatch.delenv(var, raising=False)
    people_registry.invalidate()
    prior = current_session.get()
    current_session.set(None)
    token = dsettings._TURN.set(None)
    yield path
    dsettings._TURN.reset(token)
    current_session.set(prior)
    people_registry.invalidate()


@pytest.fixture
def available(monkeypatch: pytest.MonkeyPatch) -> None:
    """The install allows it (DELEGATION_TEAM_MEMBERS)."""
    monkeypatch.setattr(dsettings, "team_members_available", lambda: True)
    monkeypatch.setattr(route, "team_members_available", lambda: True)


@pytest.fixture
def roster() -> SimpleNamespace:
    principal = people_store.upsert_person(full_name="Olivia Owner", is_principal=True, email="olivia@co.example")
    teammate = people_store.upsert_person(full_name="Ben Teammate", email="ben@co.example", slack_user_id="U_BEN")
    contact = people_store.upsert_person(full_name="Cara Client", email="cara@client.example", kind="contact")
    people_registry.invalidate()
    return SimpleNamespace(principal=principal, teammate=teammate, contact=contact)


def _get(person_id: int) -> Any:
    person = people_store.get_person(person_id)
    assert person is not None
    return person


# --------------------------------------------------------------------------- #
# Who may have it
# --------------------------------------------------------------------------- #


def test_a_teammate_needs_the_install_and_the_owners_switch(
    roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    teammate = _get(roster.teammate)
    assert can_delegate(_get(roster.principal)) is True
    assert can_delegate(teammate) is False
    # The owner's switch alone does nothing while the install doesn't allow it.
    set_team_members(True, updated_by="test")
    assert team_members_enabled() is False and can_delegate(teammate) is False
    monkeypatch.setattr(dsettings, "team_members_available", lambda: True)
    assert can_delegate(teammate) is True
    # Never a contact or an archived member.
    assert can_delegate(_get(roster.contact)) is False
    teammate.archived = True
    assert can_delegate(teammate) is False
    set_team_members(False, updated_by="test")
    assert can_delegate(_get(roster.teammate)) is False


def test_the_install_flag_counts_a_teammate_only_while_allowed(
    roster: SimpleNamespace, available: None
) -> None:
    set_enabled(roster.teammate, True, updated_by="test")
    assert enabled_for_install() is False
    set_team_members(True, updated_by="test")
    assert enabled_for_install() is True
    set_team_members(False, updated_by="test")
    assert enabled_for_install() is False


def _web(person_id: int) -> Session:
    return Session(from_web_chat=True, caller_person_id=person_id, web_caller_signed_in=True)


@pytest.mark.parametrize(("name", "make", "offered"), [
    ("a teammate on the web, signed in", lambda r: _web(r.teammate), True),
    ("a teammate in their Slack DM",
     lambda r: Session(session_id="slack:dm:U_BEN", origin_channel="slack", origin_channel_ref="U_BEN",
                       caller_person_id=r.teammate), True),
    ("a teammate in a Slack thread",
     lambda r: Session(session_id="slack:thread:C1:171.1", origin_channel="slack", origin_channel_ref="U_BEN",
                       caller_person_id=r.teammate), False),
    ("a teammate on the web without a sign-in",
     lambda r: Session(from_web_chat=True, caller_person_id=r.teammate), False),
    ("a teammate's email turn", lambda r: Session(caller_person_id=r.teammate, email_from="ben@co.example"), False),
    ("an unattended run", lambda r: Session(caller_person_id=r.teammate, unattended=True), False),
])
def test_who_on_the_team_is_offered_it(
    roster: SimpleNamespace, available: None, name: str, make: Any, offered: bool
) -> None:
    set_team_members(True, updated_by="test")
    set_enabled(roster.teammate, True, updated_by="test")
    assert pin_turn_delegation(make(roster), "hi").offered is offered, name


def test_a_teammates_pin_is_theirs_alone(roster: SimpleNamespace, available: None) -> None:
    set_team_members(True, updated_by="test")
    set_enabled(roster.teammate, True, updated_by="test")
    # The surface must say it is them: a teammate never passes as the principal.
    assert dsettings.speaker_surface_ok(_web(roster.principal), _get(roster.teammate)) is False
    assert dsettings.speaker_surface_ok(_web(roster.teammate), _get(roster.teammate)) is True


# --------------------------------------------------------------------------- #
# Audit rows
# --------------------------------------------------------------------------- #


def test_a_teammates_rows_are_theirs_alone(roster: SimpleNamespace, db: Path) -> None:
    log = AuditLogger(db)
    with rows_for_person(roster.teammate):
        mine = log.log("delegation_inbox_scanned", "Checked person 2's inbox", details={"n": 1})
    with rows_for_person(roster.principal):
        owners = log.log("delegation_inbox_scanned", "Checked person 1's inbox", details={"n": 1})
    plain = log.log("chat_turn", "Executive: hi")
    assert mine and owners and plain
    teammate_row = log.get(mine)
    assert teammate_row is not None and teammate_row.private and teammate_row.private_to_person == roster.teammate
    owner_row = log.get(owners)
    assert owner_row is not None and owner_row.private and owner_row.private_to_person is None
    # The principal's reads (include_private) leave the teammate's row out.
    seen = {e.id for e in log.query(limit=50)}
    assert seen == {owners, plain}
    assert log.count() == 2
    assert mine in {e.id for e in log.query(limit=50, owned_by=roster.teammate)}


def test_a_teammates_mail_turn_writes_their_rows(roster: SimpleNamespace, db: Path) -> None:
    log = AuditLogger(db)
    session = _web(roster.teammate)
    session.session_id = "s-ben"
    session.turn_delegation = TurnDelegation(
        enabled=True, offered=True, touched_mail=True, person_id=roster.teammate, session_id="s-ben",
    )
    token = current_session.set(session)
    try:
        row_id = log.log("tool_call", "ghostwrite_email")
    finally:
        current_session.reset(token)
    assert row_id is not None
    row = log.get(row_id)
    assert row is not None and row.private_to_person == roster.teammate


def test_the_audit_route_hides_a_teammates_row_from_the_principal(
    roster: SimpleNamespace, db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.api.routes import audit as audit_route

    log = AuditLogger(db)
    with rows_for_person(roster.teammate):
        mine = log.log("delegation_inbox_scanned", "Checked")
    monkeypatch.setattr(audit_route, "_resolve_logger", lambda request: log)
    app = FastAPI()
    app.include_router(audit_route.router)
    assert TestClient(app).get(f"/audit/logs/{mine}", headers=OWNER).status_code == 404


# --------------------------------------------------------------------------- #
# Reply cards
# --------------------------------------------------------------------------- #


def _card(approver: int) -> int:
    return create_decision_instance(
        decision_class="delegation_reply",
        department="",
        originating_session_id=None,
        proposed_payload={"private": True, "person_id": approver, "subject": "Re: invoice"},
        idempotency_key=f"delegation_reply:{approver}:m1",
        gate_mode="propose",
        approver_person_id=approver,
        confidence=0.9,
    )


def test_a_teammates_card_is_hidden_from_the_principal(
    roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **kw: None)
    app = FastAPI()
    app.include_router(decisions_route.router)
    client = TestClient(app)
    bens = _card(roster.teammate)
    olivias = _card(roster.principal)
    assert client.get(f"/decisions/{bens}", headers=OWNER).status_code == 404
    assert client.get(f"/decisions/{bens}", headers=TEAMMATE).status_code == 200
    assert client.get(f"/decisions/{olivias}", headers=TEAMMATE).status_code == 404
    assert client.get(f"/decisions/{olivias}", headers=OWNER).status_code == 200
    listed = client.get("/decisions", params={"decision_class": "delegation_reply"}, headers=OWNER).json()
    assert [d["id"] for d in listed] == [olivias]
    listed = client.get("/decisions", params={"decision_class": "delegation_reply"}, headers=TEAMMATE).json()
    assert [d["id"] for d in listed] == [bens]
    # Nor can the principal dismiss it.
    resp = client.post(f"/decisions/{bens}/reject", json={"reason": ""}, headers=OWNER)
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# The conversation
# --------------------------------------------------------------------------- #


def test_a_conversation_that_read_their_mail_is_theirs_alone(
    roster: SimpleNamespace, db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from functools import partial

    from openexecutive.api.routes.chat import _session_access

    # session_store binds its DB path at import.
    monkeypatch.setattr(session_store, "get_session_owner", partial(session_store.get_session_owner, db_path=db))
    monkeypatch.setattr(
        session_store, "session_mail_private", partial(session_store.session_mail_private, db_path=db)
    )

    session_store.create_session("s-ben", "Inbox", "2026-10-01T00:00:00+00:00", roster.teammate, db_path=db)
    request: Any = SimpleNamespace(headers={}, client=None, state=SimpleNamespace())
    assert _session_access(request, "s-ben", roster.principal) == "allowed"
    session_store.mark_mail_private("s-ben", roster.teammate, db_path=db)
    assert _session_access(request, "s-ben", roster.principal) == "forbidden"
    assert _session_access(request, "s-ben", roster.teammate) == "allowed"


def test_marking_creates_a_channel_conversation_with_its_owner(roster: SimpleNamespace, db: Path) -> None:
    # A Slack DM's row is written after the turn: marking makes it first.
    session_store.mark_mail_private("slack:dm:U_BEN", roster.teammate, db_path=db)
    session_store.create_session("slack:dm:U_BEN", "Slack", "2026-10-01T00:00:00+00:00", roster.teammate, db_path=db)
    assert session_store.session_mail_private("slack:dm:U_BEN", db_path=db) is True
    assert session_store.get_session_owner("slack:dm:U_BEN", db_path=db) == (True, roster.teammate)


@pytest.fixture
def stores_on(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from functools import partial

    # session_store binds its DB path at import.
    for name in ("get_session_owner", "session_mail_private", "mark_mail_private"):
        monkeypatch.setattr(session_store, name, partial(getattr(session_store, name), db_path=db))


def test_a_later_turn_in_their_conversation_stays_theirs(
    roster: SimpleNamespace, available: None, stores_on: None, db: Path
) -> None:
    set_team_members(True, updated_by="test")
    set_enabled(roster.teammate, True, updated_by="test")
    session = _web(roster.teammate)
    session.session_id = "s-ben"
    session_store.create_session("s-ben", "Inbox", "2026-10-01T00:00:00+00:00", roster.teammate, db_path=db)
    assert pin_turn_delegation(session, "what did she ask?").touched_mail is False
    session_store.mark_mail_private("s-ben", roster.teammate)
    # Answered from history, with no mailbox call: still private and theirs.
    pinned = pin_turn_delegation(session, "what did she ask?")
    assert pinned.touched_mail is True and pinned.person_id == roster.teammate and pinned.offered is True


def test_nobody_else_drafts_in_a_conversation_that_is_someone_elses(
    roster: SimpleNamespace, available: None, stores_on: None
) -> None:
    set_enabled(roster.principal, True, updated_by="test")
    session = _web(roster.principal)
    session.session_id = "s-ben"
    session_store.mark_mail_private("s-ben", roster.teammate)
    pinned = pin_turn_delegation(session, "draft something")
    assert pinned.touched_mail is True and pinned.offered is False and pinned.person_id == roster.teammate


def test_a_private_flag_that_cant_be_read_counts_as_set(
    roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_a: Any, **_k: Any) -> bool:
        raise RuntimeError("db gone")

    monkeypatch.setattr(session_store, "session_mail_private", broken)
    session = _web(roster.principal)
    session.session_id = "s-1"
    assert pin_turn_delegation(session, "hi").touched_mail is True


def test_marking_keeps_an_owner_and_binds_a_missing_one(roster: SimpleNamespace, db: Path) -> None:
    session_store.create_session("s-ben", "Inbox", "2026-10-01T00:00:00+00:00", roster.teammate, db_path=db)
    assert session_store.mark_mail_private("s-ben", roster.principal, db_path=db) == roster.teammate
    session_store.create_session("s-open", "Chat", "2026-10-01T00:00:00+00:00", None, db_path=db)
    assert session_store.mark_mail_private("s-open", roster.principal, db_path=db) == roster.principal
    assert session_store.get_session_owner("s-open", db_path=db) == (True, roster.principal)


def test_the_mailbox_stays_shut_in_someone_elses_conversation(
    roster: SimpleNamespace, stores_on: None, db: Path
) -> None:
    from openexecutive.orchestrator import delegation_tools

    session_store.create_session("s-ben", "Inbox", "2026-10-01T00:00:00+00:00", roster.teammate, db_path=db)
    pinned = TurnDelegation(enabled=True, offered=True, person_id=roster.principal, session_id="s-ben")
    writer: Any = SimpleNamespace(pinned=pinned, person=_get(roster.principal))
    assert delegation_tools._keep_conversation_private(writer) is False
    pinned.session_id = "s-olivia"
    assert delegation_tools._keep_conversation_private(writer) is True


# --------------------------------------------------------------------------- #
# The routes
# --------------------------------------------------------------------------- #


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    events: list[Any] = []
    monkeypatch.setattr("openexecutive.audit.log_event", lambda et, summary, **kw: events.append((et, kw)))

    async def fake(email: str | None, *, gmail: Any = None) -> str:
        return "connected"

    monkeypatch.setattr(route, "gmail_status", fake)
    app = FastAPI()
    app.include_router(route.router)
    return TestClient(app)


def test_the_owner_turns_it_on_for_the_team(client: TestClient, roster: SimpleNamespace, available: None) -> None:
    assert client.get("/delegation", headers=TEAMMATE).status_code == 403
    body = client.get("/delegation", headers=OWNER).json()
    assert body["team"] == {"enabled": False, "members": []}
    # Only the owner may change it.
    set_team_members(True, updated_by="test")
    assert client.put("/delegation/team", json={"enabled": False}, headers=TEAMMATE).json()["detail"]["code"] == (
        "principal_only"
    )
    set_team_members(False, updated_by="test")
    body = client.put("/delegation/team", json={"enabled": True}, headers=OWNER).json()
    assert body["team"]["enabled"] is True
    # Now the teammate has their own card, without the owner's switch.
    mine = client.get("/delegation", headers=TEAMMATE).json()
    assert mine["enabled"] is False and mine["team"] is None
    assert client.put("/delegation", json={"enabled": True}, headers=TEAMMATE).json()["enabled"] is True
    # The owner sees that they use it, as counts only.
    members = client.get("/delegation", headers=OWNER).json()["team"]["members"]
    assert members == [{
        "person_id": roster.teammate, "name": "Ben Teammate", "enabled": True, "inbox": False,
        "drafts_30d": 0, "sent_30d": 0,
    }]
    # Off takes it away again.
    client.put("/delegation/team", json={"enabled": False}, headers=OWNER)
    assert client.get("/delegation", headers=TEAMMATE).status_code == 403


def test_the_team_switch_needs_the_install(client: TestClient, roster: SimpleNamespace) -> None:
    assert client.get("/delegation", headers=OWNER).json()["team"] is None
    resp = client.put("/delegation/team", json={"enabled": True}, headers=OWNER)
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "not_available"
