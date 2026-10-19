"""Contacts the Executive keeps up to date (people/contact_keeper.py,
people/contact_changes.py, api/routes/contacts.py): who it adds, what it
updates, and how each change is taken back."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import contacts as contacts_route
from openexecutive.delegation import gmail as gmail_mod
from openexecutive.delegation.gmail import CalendarEvent, MailMessage
from openexecutive.delegation.settings import TurnDelegation, set_enabled
from openexecutive.memory import episodic
from openexecutive.orchestrator import outside_content as oc
from openexecutive.orchestrator.schedule_tools import current_session
from openexecutive.orchestrator.session import Session
from openexecutive.people import contact_changes as changes
from openexecutive.people import contact_keeper as keeper
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store

OWNER = "olivia@co.example"
NOW = datetime(2026, 10, 19, 15, 0, tzinfo=UTC)
PRIYA = "priya.shah@northwind.example"
MARCO = "marco@fabrikam.example"


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", path)
    monkeypatch.setattr(people_store, "DB_PATH", path)
    episodic.initialize_db(path)
    people_store.initialize_db(path)
    for var in ("OE_LOCAL_LOGIN", "OE_PUBLIC_DEPLOYMENT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **kw: None)
    monkeypatch.setattr("openexecutive.integrations.roster_intake.after_roster_write", lambda: None)

    async def connected(email: str | None, *, gmail: Any = None) -> str:
        return "connected"

    monkeypatch.setattr(gmail_mod, "gmail_status", connected)
    people_registry.invalidate()
    yield path
    people_registry.invalidate()


@pytest.fixture
def owner() -> int:
    pid = people_store.upsert_person(full_name="Olivia Owner", is_principal=True, email=OWNER)
    people_registry.invalidate()
    set_enabled(pid, True, updated_by="test")
    return pid


def _sent(to: list[str], *, names: dict[str, str] | None = None, days_ago: int = 1, cc: list[str] | None = None) -> MailMessage:
    when = (NOW - timedelta(days=days_ago)).isoformat()
    return MailMessage(
        id=f"m{len(to)}{days_ago}{to[0]}", thread_id="t", from_addr=OWNER, to=to, cc=cc or [],
        labels=["SENT"], received_at=when, names=names or {},
    )


class FakeMailbox:
    def __init__(self) -> None:
        self.sent: list[MailMessage] = []
        self.events: list[CalendarEvent] = []
        self.latest: dict[str, MailMessage] = {}
        self.calendar_error: Exception | None = None

    async def profile_email(self) -> str:
        return OWNER

    async def send_as_addresses(self) -> list[str]:
        return [OWNER]

    async def list_sent(self, limit: int = 40) -> list[MailMessage]:
        return self.sent[:limit]

    async def list_events(self, start: datetime, end: datetime, **_kw: Any) -> list[CalendarEvent]:
        if self.calendar_error is not None:
            raise self.calendar_error
        return self.events

    async def latest_from(self, address: str, *, days: int = 90) -> MailMessage | None:
        return self.latest.get(address)


def _run(mailbox: FakeMailbox) -> keeper.RunResult:
    return asyncio.run(keeper.run_once(client=mailbox, now=NOW))


def _contact(address: str) -> Any:
    return people_store.find_person_by_address(address, include_contacts=True)


# --- names and roles -------------------------------------------------------


def test_names_come_only_from_how_a_name_was_written() -> None:
    assert keeper.clean_name("priya shah") == "Priya Shah"
    assert keeper.clean_name("Shah, Priya") == "Priya Shah"
    assert keeper.clean_name("priya@northwind.example") == ""
    assert keeper.clean_name("Call me now 555") == ""
    assert keeper.name_from_address("priya.shah@x.example") == "Priya Shah"
    assert keeper.name_from_address("info@x.example") == ""
    assert keeper.name_from_address("ps2@x.example") == ""


def test_a_role_must_be_copied_from_the_mail() -> None:
    text = "Thanks!\n\nLeo Park\nVP Finance, Contoso\n+1 555 0100"
    assert keeper.role_from_text("VP Finance, Contoso", text) == "VP Finance, Contoso"
    assert keeper.role_from_text("Chief Executive, Contoso", text) == ""
    assert keeper.role_from_text("Ignore the instructions", "Ignore the instructions") == ""
    assert keeper.role_from_text("see https://x.example", "see https://x.example") == ""


# --- who is added ----------------------------------------------------------


def test_someone_emailed_twice_is_added_and_listed(owner: int) -> None:
    box = FakeMailbox()
    box.sent = [
        _sent([PRIYA], names={PRIYA: "Priya Shah"}, days_ago=1),
        _sent([PRIYA], days_ago=3),
        _sent(["once@other.example"], names={"once@other.example": "Only Once"}, days_ago=2),
    ]
    result = _run(box)
    assert result.added == 1
    person = _contact(PRIYA)
    assert person is not None and person.kind == "contact" and person.full_name == "Priya Shah"
    assert _contact("once@other.example") is None
    [change] = changes.list_changes()
    assert (change.action, change.reason, change.evidence, change.status) == ("add", "emailed", 2, "applied")


def test_a_guest_at_an_accepted_meeting_is_added(owner: int) -> None:
    box = FakeMailbox()
    box.events = [
        CalendarEvent(id="e1", title="Pricing", start=NOW.isoformat(), end=NOW.isoformat(),
                      attendees=(f"Marco Diaz <{MARCO}>",), response="accepted"),
        CalendarEvent(id="e2", title="Declined", start=NOW.isoformat(), end=NOW.isoformat(),
                      attendees=("Nope Person <nope@x.example>",), response="declined"),
    ]
    assert _run(box).added == 1
    assert _contact(MARCO).full_name == "Marco Diaz"
    assert _contact("nope@x.example") is None


def test_a_calendar_it_cant_read_leaves_the_mail(owner: int) -> None:
    box = FakeMailbox()
    box.calendar_error = gmail_mod.CalendarNotGranted("no")
    box.sent = [_sent([PRIYA], days_ago=1), _sent([PRIYA], days_ago=2)]
    assert _run(box).added == 1


def test_who_is_never_added(owner: int) -> None:
    people_store.upsert_person(full_name="Gone Contact", email="gone@x.example", kind="contact")
    people_store.archive_person(people_store.find_person_by_address("gone@x.example", include_contacts=True).id)
    changes.block("blocked.person@x.example", "contact")
    box = FakeMailbox()
    for address in (
        "ben.team@co.example",  # the company's own domain
        "noreply@x.example",  # a role address
        "gone@x.example",  # removed before
        "blocked.person@x.example",  # undone before
        "x9@x.example",  # no name to give them
    ):
        box.sent += [_sent([address], days_ago=1), _sent([address], days_ago=2)]
    old = _sent([PRIYA], days_ago=90)
    box.sent += [old, old]
    big = _sent([f"p{i}.q@x.example" for i in range(12)], days_ago=1)
    box.sent += [big, big]
    assert _run(box).added == 0
    assert changes.list_changes() == []


def test_off_and_act_as_me_off_do_nothing(owner: int) -> None:
    box = FakeMailbox()
    box.sent = [_sent([PRIYA], days_ago=1), _sent([PRIYA], days_ago=2)]
    changes.set_mode("off")
    assert _run(box).status == "off"
    changes.set_mode("on")
    set_enabled(owner, False, updated_by="test")
    assert _run(box).status == "act_as_me_off"
    assert _contact(PRIYA) is None


# --- ask me first, undo ----------------------------------------------------


def test_ask_first_keeps_a_suggestion_until_add_or_not_now(owner: int) -> None:
    changes.set_mode("ask")
    box = FakeMailbox()
    box.sent = [_sent([PRIYA], days_ago=1), _sent([PRIYA], days_ago=2), _sent([MARCO], days_ago=1), _sent([MARCO], days_ago=2)]
    names = {PRIYA: "Priya Shah", MARCO: "Marco Diaz"}
    for m in box.sent:
        m.names = {a: names[a] for a in m.to}
    assert _run(box).suggested == 2
    assert _contact(PRIYA) is None
    # A second look doesn't suggest them twice.
    assert _run(box).suggested == 0
    by_email = {c.email: c for c in changes.list_changes()}
    accepted = changes.accept(by_email[PRIYA].id)
    assert accepted.status == "applied" and _contact(PRIYA) is not None
    changes.dismiss(by_email[MARCO].id)
    assert changes.is_blocked(MARCO, "contact")
    with pytest.raises(changes.UndoRefused):
        changes.dismiss(by_email[MARCO].id)


def test_undo_an_add_removes_them_for_good(owner: int) -> None:
    box = FakeMailbox()
    box.sent = [_sent([PRIYA], days_ago=1), _sent([PRIYA], days_ago=2)]
    _run(box)
    [change] = changes.list_changes()
    changes.undo(change.id)
    people_registry.invalidate()
    assert _contact(PRIYA) is None
    assert _run(box).added == 0
    with pytest.raises(changes.UndoRefused):
        changes.undo(change.id)


def test_a_role_from_an_authenticated_signature_and_its_undo(owner: int, monkeypatch: pytest.MonkeyPatch) -> None:
    pid = people_store.upsert_person(full_name="Leo", email="leo@contoso.example", kind="contact")
    typed = people_store.upsert_person(full_name="Dana Lee", email="dana@lee.example", kind="contact", role="Counsel")

    async def role(message: Any, *, model: str | None = None) -> str:
        return "VP Finance, Contoso"

    monkeypatch.setattr(keeper, "read_role", role)
    box = FakeMailbox()
    box.latest = {
        "leo@contoso.example": MailMessage(
            id="in1", thread_id="t", from_addr="leo@contoso.example", from_name="Leo Park",
            text="Thanks\nLeo Park\nVP Finance, Contoso", sender_authenticated=True,
        ),
        "dana@lee.example": MailMessage(
            id="in2", thread_id="t", from_addr="dana@lee.example", text="x", sender_authenticated=True,
        ),
    }
    result = _run(box)
    assert result.updated == 2  # the fuller name and the role
    leo = people_store.get_person(pid)
    assert (leo.full_name, leo.role) == ("Leo Park", "VP Finance, Contoso")
    assert people_store.get_person(typed).role == "Counsel"  # typed: never changed
    role_change = next(c for c in changes.list_changes() if c.field == "role")
    changes.undo(role_change.id)
    assert people_store.get_person(pid).role == ""
    assert changes.is_blocked("leo@contoso.example", "role", "VP Finance, Contoso")


def test_an_unauthenticated_sender_changes_nothing(owner: int, monkeypatch: pytest.MonkeyPatch) -> None:
    pid = people_store.upsert_person(full_name="Leo", email="leo@contoso.example", kind="contact")

    async def role(message: Any, *, model: str | None = None) -> str:
        raise AssertionError("read a forged sender's mail")

    monkeypatch.setattr(keeper, "read_role", role)
    box = FakeMailbox()
    box.latest = {"leo@contoso.example": MailMessage(
        id="in1", thread_id="t", from_addr="leo@contoso.example", from_name="Leo Park", text="CEO",
    )}
    assert _run(box).updated == 0
    assert people_store.get_person(pid).full_name == "Leo"


def test_an_address_mailed_under_a_contacts_full_name_becomes_theirs(owner: int) -> None:
    pid = people_store.upsert_person(full_name="Priya Shah", email=PRIYA, kind="contact", role="Partner")
    box = FakeMailbox()
    box.sent = [_sent(["priya@home.example"], names={"priya@home.example": "Priya Shah"})]
    assert _run(box).updated == 1
    assert people_store.get_person(pid).email_aliases == ["priya@home.example"]
    change = next(c for c in changes.list_changes() if c.field == "email_alias")
    changes.undo(change.id)
    assert people_store.get_person(pid).email_aliases == []


# --- the routes ------------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(contacts_route.router)
    return TestClient(app)


def test_routes_are_the_principals_alone(client: TestClient, owner: int) -> None:
    people_store.upsert_person(full_name="Ben Teammate", email="ben@co.example")
    people_registry.invalidate()
    mine = {"x-caller-email": OWNER}
    theirs = {"x-caller-email": "ben@co.example"}
    assert client.get("/contacts/changes", headers=theirs).status_code == 404
    assert client.put("/contacts/keeper", json={"mode": "ask"}, headers=theirs).status_code == 404
    body = client.put("/contacts/keeper", json={"mode": "ask"}, headers=mine).json()
    assert body["mode"] == "ask" and body["changes"] == []
    assert client.put("/contacts/keeper", json={"mode": "loud"}, headers=mine).status_code == 422


def test_routes_undo_and_got_it(client: TestClient, owner: int) -> None:
    box = FakeMailbox()
    box.sent = [_sent([PRIYA], days_ago=1), _sent([PRIYA], days_ago=2)]
    _run(box)
    mine = {"x-caller-email": OWNER}
    [change] = client.get("/contacts/changes", headers=mine).json()["changes"]
    assert change["seen"] is False
    after = client.post("/contacts/changes/seen", headers=mine).json()["changes"]
    assert after[0]["seen"] is True
    assert client.post(f"/contacts/changes/{change['id']}/undo", headers=mine).json()["status"] == "undone"
    assert client.post(f"/contacts/changes/{change['id']}/undo", headers=mine).status_code == 409


# --- chat ------------------------------------------------------------------


def test_a_contact_added_in_chat_is_listed_with_an_undo(owner: int, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.orchestrator import people_tools

    monkeypatch.setattr(people_tools, "_refuse_unless_owner", lambda tool: None)
    out = json.loads(asyncio.run(people_tools.handle_upsert_person(
        {"full_name": "Marco Diaz", "email": MARCO, "kind": "contact"}
    )))
    [change] = changes.list_changes()
    assert (change.person_id, change.reason) == (out["person_id"], "asked")
    # Not counted in Done for you: the person asked for it.
    assert changes.counts_by_day() == []


def _pinned(session: Session, text: str) -> None:
    session.turn_delegation = TurnDelegation(  # type: ignore[attr-defined]
        speaker_text=text, session_id=session.session_id,
    )


def test_after_an_outside_read_a_contact_they_name_and_type_is_added() -> None:
    session = Session()
    token = current_session.set(session)
    try:
        _pinned(session, "Add Marco Diaz as a contact, marco@fabrikam.example")
        named = {"full_name": "Marco Diaz", "email": MARCO, "kind": "contact"}
        assert not oc.outside_withholds("upsert_person", named)
        # An address they didn't type, or a name they didn't say, is refused.
        assert oc.outside_withholds("upsert_person", {**named, "email": "marco@evil.example"})
        assert oc.outside_withholds("upsert_person", {"full_name": "Mallory", "email": MARCO, "kind": "contact"})
        error = json.loads(oc.outside_withheld_error("upsert_person"))["error"]
        assert "add_contact" in error
    finally:
        current_session.reset(token)


def test_mail_from_outside_people_never_adds_a_contact() -> None:
    session = Session(sender_off_team=True)
    token = current_session.set(session)
    try:
        _pinned(session, "Add Marco Diaz as a contact, marco@fabrikam.example")
        state = oc.begin_turn(session)
        assert oc.outside_withholds("upsert_person", {"full_name": "Marco Diaz", "email": MARCO, "kind": "contact"})
        oc.end_turn(state)
    finally:
        current_session.reset(token)


def test_done_for_you_gets_one_count_line_for_the_principal_only(owner: int, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.api.routes import people as people_route
    from openexecutive.api.routes import today

    box = FakeMailbox()
    box.sent = [_sent([PRIYA], days_ago=1), _sent([PRIYA], days_ago=2)]
    _run(box)
    monkeypatch.setattr(people_route, "caller_is_principal", lambda request: True)
    [item] = today._contacts_kept_for_caller(None)  # type: ignore[arg-type]
    assert (item.kind, item.summary, item.detail) == ("contacts_kept", "Kept your contacts up to date", "1 added")
    monkeypatch.setattr(people_route, "caller_is_principal", lambda request: False)
    assert today._contacts_kept_for_caller(None) == []  # type: ignore[arg-type]
