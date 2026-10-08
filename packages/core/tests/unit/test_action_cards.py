"""Act as me's approval cards: propose_actions leaves the speaker a card, and
their tap carries it out exactly as stored (orchestrator/action_card_tools.py,
delegation/action_cards.py, api/routes/decisions.py, api/routes/delegation.py)."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import decisions as decisions_route
from openexecutive.api.routes import delegation as delegation_route
from openexecutive.delegation import action_cards, lockdown
from openexecutive.delegation.settings import DelegationOverride, pin_turn_delegation, set_enabled
from openexecutive.memory.decision_ledger import (
    STATUS_APPROVED_UNCHANGED,
    STATUS_APPROVED_WITH_EDIT,
    get_decision_instance,
)
from openexecutive.orchestrator import action_card_tools as act
from openexecutive.orchestrator import calendar_tools
from openexecutive.orchestrator.action_chips import summarize_action
from openexecutive.orchestrator.delegation_tools import (
    DELEGATION_TOOL_HANDLERS,
    DELEGATION_TOOL_NAMES,
    MAILBOX_TOOL_NAMES,
)
from openexecutive.orchestrator.schedule_tools import set_session
from openexecutive.orchestrator.session import Session
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store

# ghostwrite_email's fakes and autouse fixtures (a temp DB, a fresh turn pin).
from .test_delegation_tools import (  # noqa: F401
    OWNER,
    TEAM,
    FakeMailbox,
    _session,
    db,
    fresh_turn_state,
)

SOON = (datetime.now(UTC) + timedelta(days=3)).replace(hour=14, minute=0, second=0, microsecond=0)


@pytest.fixture
def roster() -> SimpleNamespace:
    principal = people_store.upsert_person(full_name="Olivia Owner", is_principal=True, email=OWNER)
    teammate = people_store.upsert_person(full_name="Ben Teammate", role="Ops", email=TEAM)
    contact = people_store.upsert_person(full_name="Dana Client", email="dana@northpeak.example", kind="contact")
    people_registry.invalidate()
    return SimpleNamespace(principal=principal, teammate=teammate, contact=contact)


def _propose(session: Session | None, tool_input: dict[str, Any]) -> dict[str, Any]:
    async def go() -> str:
        with set_session(session):
            return await act.handle_propose_actions(tool_input)

    return json.loads(asyncio.run(go()))


def _teammate_session() -> Session:
    person = people_store.find_person_by_email(TEAM)
    session = Session(delegation_override=DelegationOverride(enabled=True, gmail=FakeMailbox(), person=person))
    assert pin_turn_delegation(session, "what does this need").offered
    return session


def _message(person_id: int, text: str = "Can you check the pilot dates?") -> dict[str, Any]:
    return {"kind": "message", "person_id": person_id, "text": text}


def _invite(*ids: int) -> dict[str, Any]:
    return {
        "kind": "invite", "title": "Pilot kickoff", "attendee_person_ids": list(ids),
        "start": SOON.isoformat(), "end": (SOON + timedelta(minutes=30)).isoformat(),
    }


# --------------------------------------------------------------------------- #
# Leaving a card
# --------------------------------------------------------------------------- #


def test_a_card_is_left_for_the_speaker_and_nothing_happens(roster: SimpleNamespace) -> None:
    result = _propose(_session(FakeMailbox()), {
        "why": "From Dana's email about the pilot",
        "actions": [_invite(roster.teammate, roster.contact), _message(roster.teammate),
                    {"kind": "add_contact", "full_name": "Sam Lee", "email": "Sam@NorthPeak.example"}],
    })
    assert result["status"] == "waiting_for_approval"
    card = get_decision_instance(result["decision_id"])
    assert card is not None and card.approver_person_id == roster.principal and card.status == "proposed"
    listed = action_cards.cards(people_store.get_person(roster.principal))
    assert [a.kind for a in listed[0].actions] == ["invite", "message", "add_contact"]
    assert listed[0].actions[0].summary.startswith('Invite Ben Teammate, Dana Client: "Pilot kickoff"')
    assert listed[0].actions[1].text == "Can you check the pilot dates?"
    assert listed[0].actions[2].summary == "Add contact Sam Lee, sam@northpeak.example"
    assert listed[0].why == "From Dana's email about the pilot"
    # Not on the roster yet: nothing was done.
    assert people_store.find_person_by_email("sam@northpeak.example", include_contacts=True) is None


def test_the_same_card_twice_is_one_card(roster: SimpleNamespace) -> None:
    tool_input = {"why": "x", "actions": [_message(roster.teammate)]}
    first = _propose(_session(FakeMailbox()), tool_input)["decision_id"]
    assert _propose(_session(FakeMailbox()), tool_input)["decision_id"] == first


@pytest.mark.parametrize(("action", "error"), [
    ({"kind": "message", "person_id": 999, "text": "hi"}, "roster"),
    ({"kind": "message", "person_id": None, "text": "hi"}, "roster"),
    ({"kind": "add_contact", "full_name": "Ben", "email": TEAM}, "already on the roster"),
    ({"kind": "add_contact", "full_name": "Sam", "email": "not-an-email"}, "valid `email`"),
    ({"kind": "invite", "title": "x", "attendee_person_ids": [], "start": "2026-01-01T09:00",
      "end": "2026-01-01T10:00"}, "passed"),
    ({"kind": "wire_money"}, "`kind`"),
])
def test_bad_actions_are_refused(roster: SimpleNamespace, action: dict[str, Any], error: str) -> None:
    assert error in _propose(_session(FakeMailbox()), {"why": "x", "actions": [action]})["error"]


def test_a_message_to_themselves_is_refused(roster: SimpleNamespace) -> None:
    assert "speaking with" in _propose(_session(FakeMailbox()), {"why": "x", "actions": [_message(roster.principal)]})["error"]


def test_a_team_members_card_reaches_neither_contacts_nor_the_roster(roster: SimpleNamespace) -> None:
    session = _teammate_session()
    assert "roster" in _propose(session, {"why": "x", "actions": [_message(roster.contact)]})["error"]
    assert "owner" in _propose(session, {"why": "x", "actions": [
        {"kind": "add_contact", "full_name": "Sam", "email": "sam@x.example"}]})["error"]
    assert _propose(session, {"why": "x", "actions": [_message(roster.principal)]})["status"] == "waiting_for_approval"


def test_cards_are_capped(roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    session = _session(FakeMailbox())
    assert "At most" in _propose(session, {"why": "x", "actions": [_message(roster.teammate)] * 6})["error"]
    for i in range(action_cards.CARDS_PER_TURN):
        assert _propose(session, {"why": "x", "actions": [_message(roster.teammate, f"note {i}")]})["status"] == "waiting_for_approval"
    assert "this turn" in _propose(session, {"why": "x", "actions": [_message(roster.teammate, "more")]})["error"]
    monkeypatch.setattr(action_cards, "OPEN_CARDS_MAX", action_cards.CARDS_PER_TURN)
    assert "already have" in _propose(_session(FakeMailbox()), {"why": "x", "actions": [_message(roster.teammate, "z")]})["error"]


def test_refused_on_a_turn_act_as_me_was_not_offered_to(roster: SimpleNamespace) -> None:
    assert "not available" in _propose(None, {"why": "x", "actions": [_message(roster.teammate)]})["error"]


def test_it_rides_with_act_as_me_stays_on_after_mail_and_opens_no_mailbox() -> None:
    assert act.PROPOSE_ACTIONS in DELEGATION_TOOL_NAMES and act.PROPOSE_ACTIONS in DELEGATION_TOOL_HANDLERS
    assert act.PROPOSE_ACTIONS not in MAILBOX_TOOL_NAMES
    assert not lockdown.mail_touched_withholds(act.PROPOSE_ACTIONS, {})
    assert not lockdown.carried_withholds(act.PROPOSE_ACTIONS, {})


def test_the_chip_says_it_is_waiting() -> None:
    chip = summarize_action(
        tool_name="propose_actions", tool_input={},
        tool_result=json.dumps({"status": "waiting_for_approval", "decision_id": 3, "actions": ["a", "b"]}),
    )
    assert chip is not None and chip["summary"] == "Waiting for your approval: 2 actions" and chip["link"] == "/today"
    assert summarize_action(tool_name="propose_actions", tool_input={}, tool_result=json.dumps({"error": "x"})) is None


# --------------------------------------------------------------------------- #
# Approving
# --------------------------------------------------------------------------- #


@pytest.fixture
def sends(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any], Any]]:
    """Stand-ins for the real handlers, recording what ran and who had approved it."""
    ran: list[tuple[str, dict[str, Any], Any]] = []

    async def message(tool_input: dict[str, Any]) -> str:
        ran.append(("message", tool_input, None))
        return json.dumps({"status": "sent"})

    async def invite(tool_input: dict[str, Any]) -> str:
        ran.append(("invite", tool_input, calendar_tools._approved_by.get()))
        return json.dumps({"status": "created", "event_id": "e1"})

    monkeypatch.setattr("openexecutive.orchestrator.schedule_tools.handle_message_person", message)
    monkeypatch.setattr(calendar_tools, "handle_create_calendar_event", invite)
    return ran


@pytest.fixture
def signed(monkeypatch: pytest.MonkeyPatch) -> dict[str, str | None]:
    state: dict[str, str | None] = {"refusal": None}
    monkeypatch.setattr("openexecutive.delegation.verified.caller_refusal", lambda caller, email: state["refusal"])
    return state


def _card(session: Session, actions: list[dict[str, Any]]) -> Any:
    card = get_decision_instance(_propose(session, {"why": "x", "actions": actions})["decision_id"])
    assert card is not None
    return card


def _approve(card: Any, only: Any = None) -> list[dict[str, Any]]:
    return asyncio.run(action_cards.approve(card, caller=None, resolver=card.approver_person_id, only=only))


def test_approving_does_each_action_exactly_as_shown_once(
    roster: SimpleNamespace, sends: list[Any], signed: dict[str, Any],
) -> None:
    set_enabled(roster.principal, True, updated_by="t")
    card = _card(_session(FakeMailbox()), [
        _invite(roster.teammate), _message(roster.teammate),
        {"kind": "add_contact", "full_name": "Sam Lee", "email": "sam@northpeak.example"},
    ])
    results = _approve(card)
    assert [r["status"] for r in results] == ["done", "done", "done"]
    assert [(k, i.get("text")) for k, i, _ in sends] == [("invite", None), ("message", "Can you check the pilot dates?")]
    # The meeting is booked as already approved by the person who tapped.
    assert sends[0][2] == roster.principal
    added = people_store.find_person_by_email("sam@northpeak.example", include_contacts=True)
    assert added is not None and added.kind == "contact"
    assert get_decision_instance(card.id).status == STATUS_APPROVED_UNCHANGED
    with pytest.raises(action_cards.ApproveRefused) as again:
        _approve(card)
    assert again.value.code == "already_handled"
    assert len(sends) == 2


def test_only_the_ticked_actions_are_done(roster: SimpleNamespace, sends: list[Any], signed: dict[str, Any]) -> None:
    set_enabled(roster.principal, True, updated_by="t")
    card = _card(_session(FakeMailbox()), [_message(roster.teammate, "one"), _message(roster.teammate, "two")])
    assert [r["status"] for r in _approve(card, only=[1])] == ["skipped", "done"]
    assert [i["text"] for _, i, _ in sends] == ["two"]
    assert get_decision_instance(card.id).status == STATUS_APPROVED_WITH_EDIT


def test_approving_needs_them_signed_in_with_act_as_me_on(
    roster: SimpleNamespace, sends: list[Any], signed: dict[str, Any],
) -> None:
    card = _card(_session(FakeMailbox()), [_message(roster.teammate)])
    for refusal, code in (("not_yours", "not_yours"), ("caller_signing_required", "caller_signing_required")):
        signed["refusal"] = refusal
        with pytest.raises(action_cards.ApproveRefused) as refused:
            _approve(card)
        assert refused.value.code == code
    signed["refusal"] = None
    with pytest.raises(action_cards.ApproveRefused) as off:
        _approve(card)
    assert off.value.code == "act_as_me_off"
    assert sends == [] and get_decision_instance(card.id).status == "proposed"


def test_an_action_that_no_longer_holds_fails_alone(
    roster: SimpleNamespace, sends: list[Any], signed: dict[str, Any],
) -> None:
    set_enabled(roster.principal, True, updated_by="t")
    card = _card(_session(FakeMailbox()), [_message(roster.teammate), _invite(roster.teammate)])
    people_store.archive_person(roster.teammate)
    people_registry.invalidate()
    results = _approve(card)
    assert [r["status"] for r in results] == ["failed", "failed"]
    assert sends == []


def test_an_old_card_is_not_listed_or_approved(
    roster: SimpleNamespace, sends: list[Any], signed: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_enabled(roster.principal, True, updated_by="t")
    card = _card(_session(FakeMailbox()), [_message(roster.teammate)])
    monkeypatch.setattr(action_cards, "CARD_TTL", timedelta(seconds=-1))
    assert action_cards.cards(people_store.get_person(roster.principal)) == []
    with pytest.raises(action_cards.ApproveRefused) as old:
        _approve(card)
    assert old.value.code == "expired"


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    for var in ("OE_LOCAL_LOGIN", "OE_PUBLIC_DEPLOYMENT"):
        monkeypatch.delenv(var, raising=False)
    # Team members may have Act as me on this install.
    monkeypatch.setattr("openexecutive.delegation.settings.team_members_enabled", lambda **_: True)
    app = FastAPI()
    app.include_router(delegation_route.router)
    app.include_router(decisions_route.router)
    return TestClient(app)


def test_the_routes_show_and_resolve_a_card_for_its_person_alone(
    roster: SimpleNamespace, client: TestClient, sends: list[Any], signed: dict[str, Any],
) -> None:
    set_enabled(roster.teammate, True, updated_by="t")
    card = _card(_teammate_session(), [_message(roster.principal)])
    ben, olivia = {"x-caller-email": TEAM}, {"x-caller-email": OWNER}
    listed = client.get("/delegation/actions", headers=ben).json()["cards"]
    assert [c["decision_id"] for c in listed] == [card.id]
    assert listed[0]["actions"][0]["summary"] == "Message Olivia Owner"
    # Not even the principal sees a team member's card.
    assert client.get("/delegation/actions", headers=olivia).json()["cards"] == []
    assert client.post(f"/decisions/{card.id}/approve", json={"edits": None}, headers=olivia).status_code == 404
    resp = client.post(f"/decisions/{card.id}/approve", json={"edits": {"only": [0]}}, headers=ben)
    assert resp.status_code == 200 and resp.json()["status"] == STATUS_APPROVED_UNCHANGED
    results = json.loads(resp.json()["final_payload_json"])["results"]
    assert results == [{"index": 0, "status": "done", "detail": "Sent."}]
    assert client.get("/delegation/actions", headers=ben).json()["cards"] == []


def test_a_refused_approval_says_why(
    roster: SimpleNamespace, client: TestClient, sends: list[Any], signed: dict[str, Any],
) -> None:
    set_enabled(roster.teammate, True, updated_by="t")
    card = _card(_teammate_session(), [_message(roster.principal)])
    signed["refusal"] = "caller_signing_required"
    resp = client.post(f"/decisions/{card.id}/approve", json={"edits": None}, headers={"x-caller-email": TEAM})
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "caller_signing_required"


def test_dismissing_a_card_does_nothing(roster: SimpleNamespace, client: TestClient, sends: list[Any]) -> None:
    card = _card(_teammate_session(), [_message(roster.principal)])
    resp = client.post(f"/decisions/{card.id}/reject", json={"reason": ""}, headers={"x-caller-email": TEAM})
    assert resp.status_code == 200 and resp.json()["status"] == "rejected"
    assert sends == []
