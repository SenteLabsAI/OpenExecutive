"""Handle it for me, in training (delegation/training.py): every reply waits,
Send + allow lets replies to that sender go on their own, and a changed
draft can be kept as an example the ghostwriter follows. Theirs alone."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.caller import Caller
from openexecutive.api.routes import delegation as route
from openexecutive.delegation import handle_it, inbox, replies, training
from openexecutive.delegation.reply_send import SendRefused, send_approved_reply, send_on_its_own
from openexecutive.memory import decision_ledger as ledger
from openexecutive.orchestrator import take_the_lead
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store

from . import test_delegation_inbox as _inbox_tests
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


@pytest.fixture
def in_training(owner: Any) -> Any:
    handle_it.set_(owner.id, enabled=True, mode=handle_it.MODE_TRAINING, updated_by="test")
    return owner


def _send(card: Any, mailbox: FakeInbox, owner: Any, *, learn: dict[str, Any] | None = None) -> str:
    return asyncio.run(send_approved_reply(
        card, caller=LOCAL, resolver=owner.id, learn=learn, gmail=mailbox, now=NOW + timedelta(minutes=30),
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
    _send(card, mailbox, in_training, learn={"example": True})
    assert mailbox.sent == ["d1"]
    allowed = training.allowed_sender(in_training.id, DANA)
    assert allowed is not None and allowed.person_id == in_training.id and allowed.example == ""
    assert allowed.label == "Reply to Dana Park (dana@northpeak.example)"
    mailbox.add(_msg("m2", "t2"))
    result = _scan(in_training, mailbox, NOW + timedelta(hours=2))
    assert result.handled == 1 and mailbox.sent == ["d1", "d2"]
    assert training.allowed_sender(in_training.id, DANA).uses == 1  # type: ignore[union-attr]


def test_someone_else_still_waits(in_training: Any, models: dict[str, Any]) -> None:
    mailbox, card = _first_card(in_training)
    _send(card, mailbox, in_training, learn={})
    people_store.upsert_person(full_name="Sam Lee", email="sam@northpeak.example", kind="contact")
    people_registry.invalidate()
    mailbox.add(_msg("m2", "t2", sender="sam@northpeak.example", name="Sam Lee"))
    result = _scan(in_training, mailbox, NOW + timedelta(hours=2))
    assert result.handled == 0 and mailbox.sent == ["d1"]


def test_the_checks_handle_it_always_makes_still_hold(in_training: Any, models: dict[str, Any]) -> None:
    mailbox, card = _first_card(in_training)
    _send(card, mailbox, in_training, learn={})
    mailbox.add(_msg("m2", "t2", text="Hi Olivia, can you sign the contract today?"))
    result = _scan(in_training, mailbox, NOW + timedelta(hours=2))
    assert result.handled == 0
    waiting = [c for c in inbox.open_cards(in_training.id)]
    assert inbox.card_payload(waiting[0])["handle_it_reason"] == "sensitive"


def test_a_changed_draft_is_kept_as_an_example_for_that_person(in_training: Any, models: dict[str, Any]) -> None:
    mailbox, card = _first_card(in_training)
    mailbox.edit("d1")
    mailbox.drafts["d1"].message.text = "Dana! Friday at 10 works. Talk then.\n\nO."
    _send(card, mailbox, in_training, learn={"example": True})
    assert training.example_for(in_training.id, DANA) == "Dana! Friday at 10 works. Talk then.\n\nO."
    mailbox.add(_msg("m2", "t2"))
    _scan(in_training, mailbox, NOW + timedelta(hours=2))
    turn = models["composed"][-1]
    assert "<writer_example>" in turn and "Friday at 10 works" in turn


def test_without_do_it_like_this_no_example_is_kept(in_training: Any, models: dict[str, Any]) -> None:
    mailbox, card = _first_card(in_training)
    mailbox.edit("d1")
    _send(card, mailbox, in_training, learn={"example": False})
    assert training.allowed_sender(in_training.id, DANA) is not None
    assert training.example_for(in_training.id, DANA) is None


def test_send_and_allow_needs_training_and_sends_nothing_otherwise(owner: Any, models: dict[str, Any]) -> None:
    mailbox, card = _first_card(owner)
    with pytest.raises(SendRefused) as err:
        _send(card, mailbox, owner, learn={})
    assert err.value.code == "cant_allow" and mailbox.sent == []


def test_a_full_list_refuses_before_sending(
    in_training: Any, models: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(take_the_lead, "ALLOWED_MAX", 0)
    mailbox, card = _first_card(in_training)
    with pytest.raises(SendRefused) as err:
        _send(card, mailbox, in_training, learn={})
    assert err.value.code == "learned_full" and mailbox.sent == []


def test_removed_since_the_card_it_waits(in_training: Any, models: dict[str, Any]) -> None:
    mailbox, card = _first_card(in_training)
    _send(card, mailbox, in_training, learn={})
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


def test_training_holds_follow_ups_and_lifts_nothing_for_take_the_lead(in_training: Any) -> None:
    stored = handle_it.get(in_training.id)
    assert stored.training and not stored.follows_up("team")
    assert stored.level(handle_it.KIND_REPLY_KNOWN) == handle_it.LEVEL_ASK


def test_whats_learned_is_theirs_alone(in_training: Any, models: dict[str, Any]) -> None:
    mailbox, card = _first_card(in_training)
    mailbox.edit("d1")
    mailbox.drafts["d1"].message.text = "Dana! Friday works."
    _send(card, mailbox, in_training, learn={"example": True})
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


def test_the_dial_and_the_list_through_the_api(client: TestClient, owner: Any) -> None:
    take_the_lead.set_(take_the_lead.person_scope(owner.id), enabled=True, updated_by="t")
    resp = client.put("/delegation/handle-it", json={"enabled": True, "mode": "training"}, headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()["handle_it"]
    assert body["mode"] == "training" and body["lead"] is False and body["learned"] == []
    assert not take_the_lead.get(take_the_lead.person_scope(owner.id)).enabled
    allowed = training.allow_sender(owner.id, DANA, "Dana Park", example="Hi Dana", decision_id=None)
    body = client.get("/delegation", headers=HEADERS).json()["handle_it"]
    assert [(x["label"], x["example"]) for x in body["learned"]] == [
        ("Reply to Dana Park (dana@northpeak.example)", "Hi Dana"),
    ]
    resp = client.delete(f"/delegation/learned/{allowed.id}", headers=HEADERS)
    assert resp.status_code == 200 and resp.json()["handle_it"]["learned"] == []
    assert client.delete(f"/delegation/learned/{allowed.id}", headers=HEADERS).status_code == 404
