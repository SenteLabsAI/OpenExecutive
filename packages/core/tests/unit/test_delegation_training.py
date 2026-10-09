"""Act as me, in training, per setting (delegation/training.py): with Replies
in training every reply waits and Send + allow lets replies to that sender go
on their own; Follow-ups the same for follow-ups to those people; Drafts
keeps a changed draft as an example the ghostwriter follows. Theirs alone.
Suggested actions' Approve + allow is in test_action_cards.py."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.caller import Caller
from openexecutive.api.routes import delegation as route
from openexecutive.delegation import follow_ups, handle_it, inbox, replies, training
from openexecutive.delegation.reply_send import SendRefused, send_approved_reply, send_on_its_own
from openexecutive.memory import decision_ledger as ledger
from openexecutive.orchestrator import take_the_lead
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store

from . import test_delegation_inbox as _inbox_tests
from .test_delegation_follow_ups import _asked
from .test_delegation_inbox import DANA, NOW, OWNER, FakeInbox, _msg, _scan

db = _inbox_tests.db
owner = _inbox_tests.owner
models = _inbox_tests.models

LOCAL = Caller("open", "")
HEADERS = {"x-caller-email": OWNER}


@pytest.fixture(autouse=True)
def local_login(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OE_LOCAL_LOGIN", "1")
    monkeypatch.delenv("OE_PUBLIC_DEPLOYMENT", raising=False)
    monkeypatch.delenv("CALLER_ASSERTION_PUBLIC_KEYS", raising=False)
    monkeypatch.setattr(follow_ups, "_LAST_LOOK", {})


@pytest.fixture
def in_training(owner: Any) -> Any:
    handle_it.set_(owner.id, enabled=True, mode=handle_it.MODE_BALANCED, updated_by="test")
    training.set_(owner.id, {training.REPLIES: True, training.DRAFTS: True}, updated_by="test")
    return owner


def _send(card: Any, mailbox: FakeInbox, owner: Any, *, allow: bool = False, example: bool = False) -> str:
    return asyncio.run(send_approved_reply(
        card, caller=LOCAL, resolver=owner.id, allow=allow, example=example, gmail=mailbox,
        now=NOW + timedelta(minutes=30),
    ))


def _first_card(owner: Any) -> tuple[FakeInbox, Any]:
    mailbox = FakeInbox()
    mailbox.add(_msg("m1", "t1"))
    _scan(owner, mailbox)
    return mailbox, inbox.open_cards(owner.id)[0]


def test_in_training_every_reply_waits_and_offers_send_and_allow(in_training: Any, models: dict[str, Any]) -> None:
    mailbox, card = _first_card(in_training)
    assert mailbox.sent == [] and card.gate_mode == "propose"
    assert inbox.card_payload(card)["handle_it_reason"] == "training"
    [shown] = replies.cards(in_training)
    assert shown.can_allow and shown.waited_because == handle_it.REASONS["training"]


def test_send_and_allow_lets_the_next_reply_to_them_go(in_training: Any, models: dict[str, Any]) -> None:
    mailbox, card = _first_card(in_training)
    _send(card, mailbox, in_training, allow=True, example=True)
    assert mailbox.sent == ["d1"]
    allowed = training.allowed_sender(in_training.id, DANA)
    assert allowed is not None and allowed.person_id == in_training.id and allowed.example == ""
    # Nothing was changed in the mailbox, so no example either.
    assert training.example_for(in_training.id, DANA) is None
    assert allowed.label == "Reply to Dana Park (dana@northpeak.example)"
    mailbox.add(_msg("m2", "t2"))
    result = _scan(in_training, mailbox, NOW + timedelta(hours=2))
    assert result.handled == 1 and mailbox.sent == ["d1", "d2"]
    assert training.allowed_sender(in_training.id, DANA).uses == 1  # type: ignore[union-attr]


def test_someone_else_still_waits(in_training: Any, models: dict[str, Any]) -> None:
    mailbox, card = _first_card(in_training)
    _send(card, mailbox, in_training, allow=True)
    people_store.upsert_person(full_name="Sam Lee", email="sam@northpeak.example", kind="contact")
    people_registry.invalidate()
    mailbox.add(_msg("m2", "t2", sender="sam@northpeak.example", name="Sam Lee"))
    result = _scan(in_training, mailbox, NOW + timedelta(hours=2))
    assert result.handled == 0 and mailbox.sent == ["d1"]


def test_the_checks_handle_it_always_makes_still_hold(in_training: Any, models: dict[str, Any]) -> None:
    mailbox, card = _first_card(in_training)
    _send(card, mailbox, in_training, allow=True)
    mailbox.add(_msg("m2", "t2", text="Hi Olivia, can you sign the contract today?"))
    result = _scan(in_training, mailbox, NOW + timedelta(hours=2))
    assert result.handled == 0
    waiting = [c for c in inbox.open_cards(in_training.id)]
    assert inbox.card_payload(waiting[0])["handle_it_reason"] == "sensitive"


def test_a_changed_draft_is_kept_as_an_example_for_that_person(in_training: Any, models: dict[str, Any]) -> None:
    mailbox, card = _first_card(in_training)
    mailbox.edit("d1")
    mailbox.drafts["d1"].message.text = "Dana! Friday at 10 works. Talk then.\n\nO."
    _send(card, mailbox, in_training, allow=True, example=True)
    assert training.example_for(in_training.id, DANA) == "Dana! Friday at 10 works. Talk then.\n\nO."
    mailbox.add(_msg("m2", "t2"))
    _scan(in_training, mailbox, NOW + timedelta(hours=2))
    turn = models["composed"][-1]
    assert "<writer_example>" in turn and "Friday at 10 works" in turn


def test_without_do_it_like_this_no_example_is_kept(in_training: Any, models: dict[str, Any]) -> None:
    mailbox, card = _first_card(in_training)
    mailbox.edit("d1")
    _send(card, mailbox, in_training, allow=True)
    assert training.allowed_sender(in_training.id, DANA) is not None
    assert training.example_for(in_training.id, DANA) is None


def test_drafts_learns_from_a_plain_send_and_only_in_training(owner: Any, models: dict[str, Any]) -> None:
    # Drafts in training alone: the dial works as usual, and Send keeps the edit.
    training.set_(owner.id, {training.DRAFTS: True}, updated_by="t")
    mailbox, card = _first_card(owner)
    assert not replies.cards(owner)[0].can_allow
    mailbox.edit("d1")
    mailbox.drafts["d1"].message.text = "Dana! Friday works."
    _send(card, mailbox, owner, example=True)
    kept = training.learned(owner.id)
    assert [(a.label, training.setting_of(a.key)) for a in kept] == [
        ("Writing to Dana Park (dana@northpeak.example)", training.DRAFTS),
    ]
    assert training.example_for(owner.id, DANA) == "Dana! Friday works."
    assert training.allowed_sender(owner.id, DANA) is None


def test_drafts_out_of_training_keeps_nothing(owner: Any, models: dict[str, Any]) -> None:
    mailbox, card = _first_card(owner)
    mailbox.edit("d1")
    _send(card, mailbox, owner, example=True)
    assert mailbox.sent == ["d1"] and training.learned(owner.id) == []


def test_send_and_allow_needs_training_and_sends_nothing_otherwise(owner: Any, models: dict[str, Any]) -> None:
    # Drafts in training isn't Replies in training.
    training.set_(owner.id, {training.DRAFTS: True, training.FOLLOW_UPS: True}, updated_by="t")
    mailbox, card = _first_card(owner)
    with pytest.raises(SendRefused) as err:
        _send(card, mailbox, owner, allow=True)
    assert err.value.code == "cant_allow" and mailbox.sent == []


def test_a_full_list_refuses_before_sending(
    in_training: Any, models: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(take_the_lead, "ALLOWED_MAX", 0)
    mailbox, card = _first_card(in_training)
    with pytest.raises(SendRefused) as err:
        _send(card, mailbox, in_training, allow=True)
    assert err.value.code == "learned_full" and mailbox.sent == []


def test_removed_since_the_card_it_waits(in_training: Any, models: dict[str, Any]) -> None:
    mailbox, card = _first_card(in_training)
    _send(card, mailbox, in_training, allow=True)
    allowed = training.allowed_sender(in_training.id, DANA)
    assert allowed is not None
    mailbox.add(_msg("m2", "t2"))
    real = inbox._send_on_its_own

    async def remove_first(person: Any, client: Any, decision_id: int, result: Any, *, now: Any) -> None:
        training.forget(person.id, allowed.id)
        await real(person, client, decision_id, result, now=now)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(inbox, "_send_on_its_own", remove_first)
    try:
        result = _scan(in_training, mailbox, NOW + timedelta(hours=2))
    finally:
        monkeypatch.undo()
    assert result.handled == 0 and mailbox.sent == ["d1"]
    [waiting] = inbox.open_cards(in_training.id)
    assert inbox.card_payload(waiting)["handle_it_reason"] == "handle_it_off"
    with pytest.raises(SendRefused):
        asyncio.run(send_on_its_own(ledger.get_decision_instance(waiting.id), gmail=mailbox, now=NOW))


def test_with_handle_it_off_an_allowed_sender_still_waits(in_training: Any, models: dict[str, Any]) -> None:
    mailbox, card = _first_card(in_training)
    _send(card, mailbox, in_training, allow=True)
    handle_it.set_(in_training.id, enabled=False, updated_by="t")
    mailbox.add(_msg("m2", "t2"))
    result = _scan(in_training, mailbox, NOW + timedelta(hours=2))
    assert result.handled == 0 and mailbox.sent == ["d1"]


def test_out_of_training_the_dial_decides_again(in_training: Any, models: dict[str, Any]) -> None:
    training.set_(in_training.id, {training.REPLIES: False}, updated_by="t")
    mailbox = FakeInbox()
    mailbox.add(_msg("m1", "t1"))
    _scan(in_training, mailbox)
    # Dana is a contact: Balanced sends it on its own.
    assert mailbox.sent == ["d1"]


def test_take_the_lead_lifts_nothing_in_training(in_training: Any, models: dict[str, Any]) -> None:
    take_the_lead.set_(take_the_lead.person_scope(in_training.id), enabled=True, updated_by="t")
    mailbox, card = _first_card(in_training)
    assert mailbox.sent == [] and inbox.card_payload(card)["handle_it_reason"] == "training"


def _follow_up_card(owner: Any, mailbox: FakeInbox, sent: Any) -> Any:
    mailbox.add(sent)
    _scan(owner, mailbox)
    [card] = [c for c in inbox.open_cards(owner.id) if inbox.card_payload(c).get("thread_id") == sent.thread_id]
    return card


def test_follow_ups_in_training_wait_until_allowed(owner: Any, models: dict[str, Any]) -> None:
    handle_it.set_(owner.id, enabled=True, mode=handle_it.MODE_BALANCED, updated_by="t")
    training.set_(owner.id, {training.FOLLOW_UPS: True}, updated_by="t")
    mailbox = FakeInbox()
    card = _follow_up_card(owner, mailbox, _asked())
    payload = inbox.card_payload(card)
    assert mailbox.sent == [] and payload["handle_it_reason"] == "follow_up_training"
    [shown] = replies.cards(owner)
    assert shown.source == "follow_up" and shown.can_allow
    _send(card, mailbox, owner, allow=True)
    allowed = training.allowed_follow_up(owner.id, [DANA])
    assert allowed is not None and allowed.label == f"Follow up with {DANA}"
    assert training.setting_of(allowed.key) == training.FOLLOW_UPS
    # The next follow-up to Dana alone goes on its own.
    follow_ups._LAST_LOOK.clear()
    later = NOW + timedelta(hours=2)
    mailbox.add(_asked("s2", "t8", days=4, text="Hi Dana, did you get a chance to look at the plan?"))
    result = _scan(owner, mailbox, later)
    assert result.handled == 1 and len(mailbox.sent) == 2


def test_follow_up_refusal_in_training_ignores_how_well_they_know_them(owner: Any) -> None:
    from .test_delegation_follow_ups import _check, _nudge

    handle_it.set_(owner.id, enabled=True, mode=handle_it.MODE_CAREFUL, updated_by="t")
    training.set_(owner.id, {training.FOLLOW_UPS: True}, updated_by="t")
    assert _check(owner, _nudge(), relation="stranger") == "follow_up_training"
    training.allow_follow_up(owner.id, [DANA], decision_id=None)
    # Careful never follows up by itself, but an allowed person goes.
    assert _check(owner, _nudge(), relation="stranger") is None
    handle_it.set_(owner.id, enabled=False, updated_by="t")
    assert _check(owner, _nudge(), relation="stranger") == "follow_up_level"


def test_an_old_dial_in_training_moves_to_replies_in_training(owner: Any, db: Any) -> None:
    import sqlite3

    from openexecutive.delegation.schema import HANDLE_IT_TABLE

    conn = sqlite3.connect(str(db))
    conn.execute(
        f"INSERT OR REPLACE INTO {HANDLE_IT_TABLE} (person_id, enabled, mode) VALUES (?, 1, 'training')",
        (owner.id,),
    )
    conn.commit()
    conn.close()
    stored = handle_it.get(owner.id)
    assert stored.enabled and stored.mode == handle_it.MODE_BALANCED
    # Follow-ups always waited in training, so they still do.
    assert training.get(owner.id) == training.Training(person_id=owner.id, replies=True, follow_ups=True)


def test_whats_learned_is_theirs_alone(in_training: Any, models: dict[str, Any]) -> None:
    mailbox, card = _first_card(in_training)
    mailbox.edit("d1")
    mailbox.drafts["d1"].message.text = "Dana! Friday works."
    _send(card, mailbox, in_training, allow=True, example=True)
    allowed = training.allowed_sender(in_training.id, DANA)
    assert allowed is not None
    # Never in the Executive's own list or its prompts.
    assert take_the_lead.list_allowed(feature=take_the_lead.FEATURE) == []
    take_the_lead.set_(take_the_lead.SCOPE_EXECUTIVE, enabled=True, training=True, updated_by="t")
    assert "Friday works" not in take_the_lead.learned_note()
    # Someone else can't remove it, nor read it as theirs.
    assert not training.forget(in_training.id + 1, allowed.id)
    assert not take_the_lead.disallow(allowed.id)
    assert training.learned(in_training.id + 1) == []
    assert training.allowed_sender(in_training.id + 1, DANA) is None
    assert training.forget(in_training.id, allowed.id)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    async def connected(email: str | None, *, gmail: Any = None) -> str:
        return "connected"

    monkeypatch.setattr(route, "gmail_status", connected)
    app = FastAPI()
    app.include_router(route.router)
    return TestClient(app)


def test_the_switches_and_the_list_through_the_api(client: TestClient, owner: Any) -> None:
    take_the_lead.set_(take_the_lead.person_scope(owner.id), enabled=True, updated_by="t")
    # Training is off the dial.
    resp = client.put("/delegation/handle-it", json={"enabled": True, "mode": "training"}, headers=HEADERS)
    assert resp.status_code == 422
    resp = client.put("/delegation/training", json={"replies": True, "drafts": True}, headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()["training"]
    assert body == {"replies": True, "follow_ups": False, "actions": False, "drafts": True, "learned": []}
    assert resp.json()["handle_it"]["lead"] is False
    assert not take_the_lead.get(take_the_lead.person_scope(owner.id)).enabled
    allowed = training.allow_sender(owner.id, DANA, "Dana Park", decision_id=None)
    training.keep_style(owner.id, DANA, "Dana Park", "Hi Dana", decision_id=None)
    body = client.get("/delegation", headers=HEADERS).json()["training"]
    assert [(x["label"], x["setting"], x["example"]) for x in body["learned"]] == [
        ("Writing to Dana Park (dana@northpeak.example)", "drafts", "Hi Dana"),
        ("Reply to Dana Park (dana@northpeak.example)", "replies", ""),
    ]
    resp = client.delete(f"/delegation/learned/{allowed.id}", headers=HEADERS)
    assert resp.status_code == 200 and [x["setting"] for x in resp.json()["training"]["learned"]] == ["drafts"]
    assert client.delete(f"/delegation/learned/{allowed.id}", headers=HEADERS).status_code == 404
    assert client.put("/delegation/training", json={"bogus": True}, headers=HEADERS).status_code == 422


def test_take_the_lead_as_you_waits_for_training_to_end(client: TestClient, owner: Any) -> None:
    training.set_(owner.id, {training.FOLLOW_UPS: True}, updated_by="t")
    resp = client.put("/delegation/take-the-lead", json={"enabled": True}, headers=HEADERS)
    assert resp.status_code == 409


def test_widening_needs_a_provable_caller(client: TestClient, owner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "openexecutive.delegation.verified.caller_refusal", lambda caller, email: "caller_signing_required",
    )
    # Holding more back is fine without signed sign-ins...
    assert client.put("/delegation/training", json={"replies": True, "actions": False}, headers=HEADERS).status_code == 200
    # ...letting more go is not.
    for body in ({"replies": False}, {"actions": True}, {"drafts": True}):
        assert client.put("/delegation/training", json=body, headers=HEADERS).status_code == 409
