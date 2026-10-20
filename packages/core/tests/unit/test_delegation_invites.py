"""Meeting invites under Act as me (delegation/invites.py): an invite from
someone the person knows becomes a card to accept or decline in their own
calendar, Handle it for me accepts one on its own when nothing clashes, and
everything else is skipped as before."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import decisions as decisions_route
from openexecutive.api.routes import delegation as route
from openexecutive.delegation import calendar, handle_it, inbox, invites, training
from openexecutive.delegation.gmail import CalendarEvent, CalendarWriteNotGranted, MailMessage
from openexecutive.memory import decision_ledger as ledger

from . import test_delegation_inbox as _inbox_tests
from .test_delegation_inbox import DANA, NOW, OWNER, FakeInbox, _ledger, _msg, _scan

db = _inbox_tests.db
owner = _inbox_tests.owner
models = _inbox_tests.models

START = (NOW + timedelta(days=1)).replace(hour=14, minute=0)
END = START + timedelta(minutes=30)


@pytest.fixture(autouse=True)
def setup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local login (the API can tie a tap or the switch to the person), and
    the workspace in UTC."""
    monkeypatch.setenv("OE_LOCAL_LOGIN", "1")
    monkeypatch.delenv("OE_PUBLIC_DEPLOYMENT", raising=False)
    monkeypatch.delenv("CALLER_ASSERTION_PUBLIC_KEYS", raising=False)
    monkeypatch.setattr(invites, "_tz", lambda: UTC)
    monkeypatch.setattr(invites, "_now", lambda: NOW)


@pytest.fixture
def person(owner: Any) -> Any:
    calendar.set_enabled(owner.id, True, updated_by="test")
    return owner


class FakeCalendar(FakeInbox):
    """The owner's mailbox and calendar: invites it finds, what's on the
    calendar, and every answer it gave."""

    def __init__(self) -> None:
        super().__init__()
        self.events: dict[str, CalendarEvent] = {}
        self.busy: list[CalendarEvent] = []
        self.answers: list[tuple[str, bool]] = []
        self.write_granted: bool | None = True
        self.answer_error: Exception | None = None

    async def find_invite(self, message: MailMessage) -> CalendarEvent | None:
        self.calls.append(f"find_invite:{message.id}")
        return self.events.get(message.id)

    async def list_events(self, start: datetime, end: datetime, *, max_results: int = 100) -> list[CalendarEvent]:
        self.calls.append("list_events")
        return list(self.busy)

    async def calendar_write_granted(self) -> bool | None:
        return self.write_granted

    async def answer_invite(self, event_id: str, *, accept: bool) -> None:
        if self.answer_error is not None:
            raise self.answer_error
        self.answers.append((event_id, accept))


def _event(event_id: str = "ev1", *, title: str = "Pilot review", organizer: str = DANA,
           start: datetime = START, end: datetime = END, response: str = "needs_action") -> CalendarEvent:
    return CalendarEvent(
        id=event_id, title=title, start=start.isoformat(), end=end.isoformat(),
        organizer=f"Dana Park <{organizer}>", response=response,
    )


def _invite(mid: str = "i1", thread: str = "ti1", **kw: Any) -> MailMessage:
    kw.setdefault("text", "Invitation: Pilot review")
    return _msg(mid, thread, calendar_invite=True, auto_generated=True, **kw)


def _mailbox(*messages: MailMessage, event: CalendarEvent | None = None) -> FakeCalendar:
    mailbox = FakeCalendar()
    mailbox.add(*(messages or (_invite(),)))
    for m in messages or (_invite(),):
        if event is not None or m.id == "i1":
            mailbox.events[m.id] = event or _event()
    return mailbox


def _cards(person: Any) -> list[Any]:
    return ledger.list_instances(invites.DECISION_CLASS, limit=50)


# ── the card ──────────────────────────────────────────────────────────────────


def test_read_my_calendar_off_skips_an_invite_as_before(db: Path, owner: Any, models: dict[str, Any]) -> None:
    mailbox = _mailbox()
    result = _scan(owner, mailbox)
    assert result.invites == 0 and _ledger(db)["i1"] == ("skipped", "automatic")
    assert not any(c.startswith("find_invite") for c in mailbox.calls)


def test_an_invite_from_a_contact_becomes_a_card_with_no_model_call(
    db: Path, person: Any, models: dict[str, Any]
) -> None:
    mailbox = _mailbox()
    result = _scan(person, mailbox)
    assert result.invites == 1 and result.drafted == 0
    assert models["classified"] == [] and models["composed"] == []  # no model reads an invite
    (card,) = _cards(person)
    assert card.status == "proposed" and card.gate_mode == "propose" and card.approver_person_id == person.id
    payload = json.loads(card.proposed_payload_json)
    assert payload["private"] is True and payload["event_id"] == "ev1" and payload["title"] == "Pilot review"
    assert payload["clashes"] == [] and payload["can_answer"] is True and "handle_it_reason" not in payload
    (shown,) = invites.cards(person)
    assert shown.when == "Thu 1 Oct, 14:00–14:30" and shown.from_email == DANA and shown.relation == "contact"
    assert _ledger(db)["i1"] == (invites.CARDED, None)
    assert mailbox.answers == []  # nothing answered until they tap


def test_a_clash_shows_busy_times_only(person: Any, models: dict[str, Any]) -> None:
    mailbox = _mailbox()
    mailbox.busy = [
        _event("other", title="Secret board prep", start=START - timedelta(minutes=15), end=START + timedelta(minutes=15)),
        _event("ev1"),  # the meeting itself
        _event("free", title="Lunch", start=END, end=END + timedelta(hours=1)),  # touching, not clashing
        _event("no", title="Declined thing", response="declined"),
    ]
    _scan(person, mailbox)
    (card,) = _cards(person)
    assert json.loads(card.proposed_payload_json)["clashes"] == ["13:45–14:15"]
    assert "Secret" not in card.proposed_payload_json


@pytest.mark.parametrize(("message", "event", "reason"), [
    (_invite(sender="pat@elsewhere.example", name="Pat"), _event(organizer="pat@elsewhere.example"), "invite_stranger"),
    (_invite(sender_authenticated=False), _event(), "invite_unverified"),
    (_invite(), None, "invite_not_found"),
    (_invite(), _event(organizer="someone@else.example"), "invite_not_organizer"),
    (_invite(), _event(response="accepted"), "invite_answered"),
    (_invite(), _event(start=NOW - timedelta(hours=1), end=NOW), "invite_past"),
    (_invite(to=["team@co.example"]), _event(), "not_addressed"),
])
def test_what_gets_no_card(
    db: Path, person: Any, models: dict[str, Any], message: MailMessage, event: CalendarEvent | None, reason: str,
) -> None:
    mailbox = FakeCalendar()
    mailbox.add(message)
    if event is not None:
        mailbox.events[message.id] = event
    result = _scan(person, mailbox)
    assert result.invites == 0 and _cards(person) == []
    assert _ledger(db)[message.id] == ("skipped", reason)


def test_one_card_per_invite_and_the_thread_waits(person: Any, models: dict[str, Any]) -> None:
    mailbox = _mailbox()
    _scan(person, mailbox)
    mailbox.add(_invite("i2", "ti1", minutes_ago=20))
    mailbox.events["i2"] = _event()
    again = _scan(person, mailbox, now=NOW + timedelta(minutes=10))
    assert again.invites == 0 and len(_cards(person)) == 1 and again.deferred == 1


def test_a_card_closes_once_the_meeting_starts(db: Path, person: Any, models: dict[str, Any]) -> None:
    mailbox = _mailbox()
    _scan(person, mailbox)
    later = _scan(person, mailbox, now=START + timedelta(minutes=1))
    assert later.closed == 1 and _cards(person)[0].status == "closed_externally"
    assert _ledger(db)["i1"][0] == "expired"


# ── on its own ────────────────────────────────────────────────────────────────


def _on(person: Any) -> None:
    handle_it.set_(person.id, enabled=True, updated_by="test")


def test_handle_it_accepts_with_no_clash_and_says_so(db: Path, person: Any, models: dict[str, Any]) -> None:
    _on(person)
    mailbox = _mailbox()
    result = _scan(person, mailbox)
    assert result.handled == 1 and mailbox.answers == [("ev1", True)]
    (card,) = _cards(person)
    assert card.status == "executed" and card.gate_mode == "auto_execute" and card.resolver_person_id is None
    assert _ledger(db)["i1"] == (invites.ACCEPTED, "handled")
    assert handle_it.sent_today(person.id, NOW) == 1  # the same daily limit as replies
    (done,) = [h for h in handle_it.handled(person.id) if h.source == "invite"]
    assert done.summary == "Accepted Dana's Pilot review, Thu 1 Oct, 14:00"
    assert invites.cards(person) == []


def test_handle_it_leaves_a_card_for_someone_they_have_only_written_to(
    person: Any, models: dict[str, Any],
) -> None:
    """A sender known only from their Sent mail is no contact: an invite from
    them waits on a tap, whatever the level."""
    _on(person)
    pat = "pat@elsewhere.example"
    mailbox = _mailbox(_invite(sender=pat, name="Pat"), event=_event(organizer=pat))
    mailbox.written_to.add(pat)
    result = _scan(person, mailbox)
    assert result.handled == 0 and mailbox.answers == []
    (card,) = invites.cards(person)
    assert card.relation == "correspondent" and card.status == "proposed"


@pytest.mark.parametrize(("arrange", "reason"), [
    (lambda m: setattr(m, "busy", [_event("x", title="Standup")]), "clash"),
    (lambda m: setattr(m, "write_granted", False), "calendar_write"),
    (lambda m: m.events.__setitem__("i1", _event(title="Board pay review")), "sensitive"),
])
def test_handle_it_leaves_a_card_when_something_is_against_it(
    person: Any, models: dict[str, Any], arrange: Any, reason: str,
) -> None:
    _on(person)
    mailbox = _mailbox()
    arrange(mailbox)
    result = _scan(person, mailbox)
    assert result.handled == 0 and mailbox.answers == []
    (card,) = invites.cards(person)
    assert card.waited_because == handle_it.REASONS[reason]
    if reason == "calendar_write":
        assert card.can_answer is False and "--calendar" in card.connect_command


def test_it_never_declines_on_its_own_and_counts_the_daily_limit(
    person: Any, models: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _on(person)
    monkeypatch.setenv("DELEGATION_HANDLE_IT_MAX_SENDS_PER_DAY", "1")
    handle_it.record_handled(person.id, "t-earlier", None, "s1", now=NOW - timedelta(hours=1))
    mailbox = _mailbox()
    _scan(person, mailbox)
    (card,) = invites.cards(person)
    assert card.waited_because == handle_it.REASONS["daily_limit"] and mailbox.answers == []


def test_an_accept_that_fails_hands_the_card_back(person: Any, models: dict[str, Any]) -> None:
    _on(person)
    mailbox = _mailbox()
    mailbox.answer_error = CalendarWriteNotGranted("read only")
    result = _scan(person, mailbox)
    assert result.handled == 0
    (card,) = invites.cards(person)
    assert card.status == "proposed" and card.can_answer is False


def test_in_training_it_waits_until_they_tick_accept_next_time(person: Any, models: dict[str, Any]) -> None:
    _on(person)
    training.set_(person.id, {training.REPLIES: True}, updated_by="test")
    mailbox = _mailbox()
    _scan(person, mailbox)
    (card,) = invites.cards(person)
    assert card.can_allow is True and card.waited_because == handle_it.REASONS["invite_training"]
    instance = ledger.get_decision_instance(card.decision_id)
    from openexecutive.delegation.verified import ChatTap

    assert asyncio.run(invites.answer(
        instance, caller=ChatTap(OWNER), resolver=person.id, answer="accept", allow=True, mailbox=mailbox,
    )) == "accepted"
    allowed = training.allowed_inviter(person.id, DANA)
    assert allowed is not None and allowed.label.startswith("Accept invites from Dana Park")
    assert training.setting_of(allowed.key) == training.REPLIES
    # The next invite from them goes on its own.
    mailbox.add(_invite("i2", "ti2", minutes_ago=20))
    mailbox.events["i2"] = _event("ev2", title="Pilot follow-up")
    assert _scan(person, mailbox, now=NOW + timedelta(minutes=10)).handled == 1
    assert mailbox.answers == [("ev1", True), ("ev2", True)]


# ── answering on a tap (the routes) ───────────────────────────────────────────


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    app = FastAPI()
    app.include_router(route.router)
    app.include_router(decisions_route.router)
    return TestClient(app)


def _carded(person: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[FakeCalendar, int]:
    mailbox = _mailbox()
    _scan(person, mailbox)
    monkeypatch.setattr("openexecutive.delegation.gmail.gmail_for", lambda email: mailbox)
    (card,) = invites.cards(person)
    return mailbox, card.decision_id


def test_the_route_lists_the_callers_invites(client: TestClient, person: Any, models: dict[str, Any],
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    _carded(person, monkeypatch)
    body = client.get("/delegation/invites", headers={"x-caller-email": OWNER}).json()
    (card,) = body["cards"]
    assert card["title"] == "Pilot review" and card["can_answer"] is True and card["clashes"] == []


def test_decline_answers_in_their_calendar(
    db: Path, client: TestClient, person: Any, models: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    mailbox, card_id = _carded(person, monkeypatch)
    resp = client.post(f"/decisions/{card_id}/approve", json={"edits": {"answer": "decline"}},
                       headers={"x-caller-email": OWNER})
    assert resp.status_code == 200 and resp.json()["status"] == "approved_unchanged"
    assert mailbox.answers == [("ev1", False)] and _ledger(db)["i1"] == (invites.DECLINED, "tapped")
    again = client.post(f"/decisions/{card_id}/approve", json={}, headers={"x-caller-email": OWNER})
    assert again.status_code == 409


def test_only_their_own_tap_answers(
    client: TestClient, person: Any, models: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.people import registry, store

    store.upsert_person(full_name="Ben Teammate", email="ben@co.example")
    registry.invalidate()
    mailbox, card_id = _carded(person, monkeypatch)
    assert client.post(f"/decisions/{card_id}/approve", json={},
                       headers={"x-caller-email": "ben@co.example"}).status_code == 404
    monkeypatch.delenv("OE_LOCAL_LOGIN")
    resp = client.post(f"/decisions/{card_id}/approve", json={}, headers={"x-caller-email": OWNER})
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "caller_signing_required"
    assert mailbox.answers == []


def test_a_read_only_sign_in_says_to_connect_again(
    client: TestClient, person: Any, models: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    mailbox, card_id = _carded(person, monkeypatch)
    mailbox.answer_error = CalendarWriteNotGranted("read only")
    resp = client.post(f"/decisions/{card_id}/approve", json={}, headers={"x-caller-email": OWNER})
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "calendar_write_needed"
    (card,) = invites.cards(person)
    assert card.status == "proposed" and card.can_answer is False  # still there, to try after reconnecting


def test_dismiss_answers_nothing(
    db: Path, client: TestClient, person: Any, models: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    mailbox, card_id = _carded(person, monkeypatch)
    resp = client.post(f"/decisions/{card_id}/reject", json={}, headers={"x-caller-email": OWNER})
    assert resp.status_code == 200 and mailbox.answers == []
    assert _ledger(db)["i1"][0] == inbox.DISMISSED and invites.cards(person) == []


# ── a dismissed card doesn't block a new one; a sent one does (reply cards too) ─


@pytest.mark.parametrize("decision_class", [invites.DECISION_CLASS, inbox.DECISION_CLASS])
def test_a_settled_card_under_the_key_doesnt_block_a_new_one(db: Path, decision_class: str) -> None:
    key = f"{decision_class}:1:m1"
    first = invites.create_once(decision_class, key, {"n": 1}, approver_person_id=1, gate_mode="propose")
    assert invites.create_once(decision_class, key, {"n": 2}, approver_person_id=1, gate_mode="propose") == first
    assert ledger.mark_resolved(first, ledger.STATUS_REJECTED)
    second = invites.create_once(decision_class, key, {"n": 3}, approver_person_id=1, gate_mode="propose")
    assert second != first and ledger.get_decision_instance(second).status == "proposed"  # type: ignore[union-attr]
    with sqlite3.connect(db) as conn:
        keys = [r[0] for r in conn.execute("SELECT idempotency_key FROM decision_instances ORDER BY id")]
    assert keys[0] == key and keys[1].startswith(f"{key}:")


@pytest.mark.parametrize("decision_class", [invites.DECISION_CLASS, inbox.DECISION_CLASS])
@pytest.mark.parametrize("status", ["executed", "approved_unchanged", "closed_externally"])
def test_a_sent_or_settled_card_means_no_second_card(db: Path, decision_class: str, status: str) -> None:
    key = f"{decision_class}:1:m1"
    first = invites.create_once(decision_class, key, {"n": 1}, approver_person_id=1, gate_mode="propose")
    assert first is not None and ledger.mark_resolved(first, status)
    assert invites.create_once(decision_class, key, {"n": 2}, approver_person_id=1, gate_mode="propose") is None


def test_today_says_what_it_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.api.routes import today
    from openexecutive.delegation.handle_it import HandledReply

    monkeypatch.setattr("openexecutive.api.routes.chat._resolve_caller_person_id", lambda request: 1)
    monkeypatch.setattr(handle_it, "handled", lambda person_id: [HandledReply(
        decision_id=9, sent_at=NOW.isoformat(), to_name="Dana Park", to_email=DANA, subject="Pilot review",
        body="", open_questions=[], thread_id="ti1", source="invite",
        summary="Accepted Dana's Pilot review, Thu 1 Oct, 14:00",
    )])
    (item,) = today._sent_as_caller(object())  # type: ignore[arg-type]
    assert item.kind == "sent_as_you" and item.summary == "Accepted Dana's Pilot review, Thu 1 Oct, 14:00"
