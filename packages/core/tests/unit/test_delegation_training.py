"""Act as me, in training (delegation/training.py): Drafts keeps a changed
draft as an example the ghostwriter follows. Theirs alone. Nothing here lets
anything go on its own: Approvals and Handle it for me decide that."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.caller import Caller
from openexecutive.api.routes import delegation as route
from openexecutive.delegation import follow_ups, inbox, replies, training
from openexecutive.delegation.reply_send import send_approved_reply
from openexecutive.orchestrator import take_the_lead

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
    monkeypatch.setattr(follow_ups, "_LAST_LOOK", {})


@pytest.fixture
def drafts_on(owner: Any) -> Any:
    training.set_(owner.id, {training.DRAFTS: True}, updated_by="test")
    return owner


def _send(card: Any, mailbox: FakeInbox, owner: Any, *, example: bool = False) -> str:
    return asyncio.run(send_approved_reply(
        card, caller=LOCAL, resolver=owner.id, example=example, gmail=mailbox,
        now=NOW + timedelta(minutes=30),
    ))


def _first_card(owner: Any) -> tuple[FakeInbox, Any]:
    mailbox = FakeInbox()
    mailbox.add(_msg("m1", "t1"))
    _scan(owner, mailbox)
    return mailbox, inbox.open_cards(owner.id)[0]


def test_a_changed_draft_is_kept_as_an_example_for_that_person(drafts_on: Any, models: dict[str, Any]) -> None:
    mailbox, card = _first_card(drafts_on)
    assert replies.cards(drafts_on)[0].learns_style
    mailbox.edit("d1")
    mailbox.drafts["d1"].message.text = "Dana! Friday at 10 works. Talk then.\n\nO."
    _send(card, mailbox, drafts_on, example=True)
    assert training.example_for(drafts_on.id, DANA) == "Dana! Friday at 10 works. Talk then.\n\nO."
    kept = training.learned(drafts_on.id)
    assert [(a.label, training.setting_of(a.key)) for a in kept] == [
        ("Writing to Dana Park (dana@northpeak.example)", training.DRAFTS),
    ]
    mailbox.add(_msg("m2", "t2"))
    _scan(drafts_on, mailbox, NOW + timedelta(hours=2))
    turn = models["composed"][-1]
    assert "<writer_example>" in turn and "Friday at 10 works" in turn


def test_without_do_it_like_this_no_example_is_kept(drafts_on: Any, models: dict[str, Any]) -> None:
    mailbox, card = _first_card(drafts_on)
    mailbox.edit("d1")
    _send(card, mailbox, drafts_on)
    assert training.example_for(drafts_on.id, DANA) is None


def test_drafts_out_of_training_keeps_nothing(owner: Any, models: dict[str, Any]) -> None:
    mailbox, card = _first_card(owner)
    mailbox.edit("d1")
    _send(card, mailbox, owner, example=True)
    assert mailbox.sent == ["d1"] and training.learned(owner.id) == []


def test_whats_kept_is_theirs_alone(drafts_on: Any, models: dict[str, Any]) -> None:
    mailbox, card = _first_card(drafts_on)
    mailbox.edit("d1")
    mailbox.drafts["d1"].message.text = "Dana! Friday works."
    _send(card, mailbox, drafts_on, example=True)
    [kept] = training.learned(drafts_on.id)
    # Never in the Executive's own list or its prompts.
    assert take_the_lead.list_allowed(feature=take_the_lead.FEATURE) == []
    assert "Friday works" not in take_the_lead.learned_note()
    # Someone else can't remove it, nor read it as theirs.
    assert not training.forget(drafts_on.id + 1, kept.id)
    assert not take_the_lead.disallow(kept.id)
    assert training.learned(drafts_on.id + 1) == []
    assert training.example_for(drafts_on.id + 1, DANA) is None
    assert training.forget(drafts_on.id, kept.id)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    async def connected(email: str | None, *, gmail: Any = None) -> str:
        return "connected"

    monkeypatch.setattr(route, "gmail_status", connected)
    app = FastAPI()
    app.include_router(route.router)
    return TestClient(app)


def test_the_switch_and_the_list_through_the_api(client: TestClient, owner: Any) -> None:
    resp = client.put("/delegation/training", json={"drafts": True}, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["training"] == {"training": False, "drafts": True, "learned": []}
    kept = training.keep_style(owner.id, DANA, "Dana Park", "Hi Dana", decision_id=None)
    assert kept is not None
    body = client.get("/delegation", headers=HEADERS).json()["training"]
    assert [(x["label"], x["setting"], x["example"]) for x in body["learned"]] == [
        ("Writing to Dana Park (dana@northpeak.example)", "drafts", "Hi Dana"),
    ]
    resp = client.delete(f"/delegation/learned/{kept.id}", headers=HEADERS)
    assert resp.status_code == 200 and resp.json()["training"]["learned"] == []
    assert client.delete(f"/delegation/learned/{kept.id}", headers=HEADERS).status_code == 404
    for gone in ({"bogus": True}, {"replies": True}, {"actions_on": True}):
        assert client.put("/delegation/training", json=gone, headers=HEADERS).status_code == 422


def test_turning_it_on_needs_a_provable_caller(
    client: TestClient, owner: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "openexecutive.delegation.verified.caller_refusal", lambda caller, email: "caller_signing_required",
    )
    assert client.put("/delegation/training", json={"drafts": True}, headers=HEADERS).status_code == 409
    assert client.put("/delegation/training", json={"drafts": False}, headers=HEADERS).status_code == 200
