"""The inbox watcher's routes: the switch, Check now, the reply cards, and
Dismiss through /decisions (api/routes/delegation.py, decisions.py)."""
from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import decisions as decisions_route
from openexecutive.api.routes import delegation as route
from openexecutive.delegation import inbox
from openexecutive.delegation.settings import set_enabled
from openexecutive.memory import episodic
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store

from .test_delegation_inbox import NOW, FakeInbox, _msg

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
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **kw: None)
    monkeypatch.setattr(inbox, "_client_slot_active", lambda: False)
    inbox._SCANNING.clear()
    inbox._LAST_SETTLE.clear()
    people_registry.invalidate()
    yield path
    people_registry.invalidate()


@pytest.fixture
def ids() -> dict[str, int]:
    principal = people_store.upsert_person(full_name="Olivia Owner", is_principal=True, email="olivia@co.example")
    teammate = people_store.upsert_person(full_name="Ben Teammate", email="ben@co.example")
    people_registry.invalidate()
    return {"principal": principal, "teammate": teammate}


@pytest.fixture
def gmail(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    state = {"status": "connected"}

    async def fake(email: str | None, *, gmail: Any = None) -> str:
        return state["status"]

    monkeypatch.setattr(route, "gmail_status", fake)
    return state


@pytest.fixture
def client(gmail: dict[str, str]) -> TestClient:
    app = FastAPI()
    app.include_router(route.router)
    app.include_router(decisions_route.router)
    return TestClient(app)


def test_the_switch_needs_act_as_me_and_gmail(client: TestClient, ids: dict[str, int], gmail: dict[str, str]) -> None:
    resp = client.put("/delegation/inbox", json={"enabled": True}, headers=OWNER)
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "act_as_me_off"
    set_enabled(ids["principal"], True, updated_by="t")
    gmail["status"] = "needs_reconnect"
    resp = client.put("/delegation/inbox", json={"enabled": True}, headers=OWNER)
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "gmail_needs_reconnect"
    gmail["status"] = "connected"
    body = client.put("/delegation/inbox", json={"enabled": True}, headers=OWNER).json()
    assert body["inbox"]["enabled"] is True and body["inbox"]["status"] == "waiting"
    assert client.get("/delegation", headers=OWNER).json()["inbox"]["watch_since"] is not None
    # Only the owner, for themselves.
    assert client.put("/delegation/inbox", json={"enabled": True}, headers=TEAMMATE).status_code == 403


def test_act_as_me_off_turns_the_watcher_off_too(client: TestClient, ids: dict[str, int]) -> None:
    set_enabled(ids["principal"], True, updated_by="t")
    client.put("/delegation/inbox", json={"enabled": True}, headers=OWNER)
    client.put("/delegation", json={"enabled": False}, headers=OWNER)
    assert inbox.get_watch(ids["principal"]).enabled is False


def test_check_now_needs_the_switch_on(client: TestClient, ids: dict[str, int], monkeypatch: pytest.MonkeyPatch) -> None:
    resp = client.post("/delegation/inbox/check", headers=OWNER)
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "inbox_off"
    set_enabled(ids["principal"], True, updated_by="t")
    client.put("/delegation/inbox", json={"enabled": True}, headers=OWNER)
    started: list[Any] = []

    async def fake_scan(person: Any, **kw: Any) -> inbox.ScanResult:
        started.append(person.id)
        return inbox.ScanResult("ok")

    monkeypatch.setattr(inbox, "scan_person", fake_scan)
    resp = client.post("/delegation/inbox/check", headers=OWNER)
    assert resp.status_code == 202 and resp.json()["checking"] is True
    assert started == [ids["principal"]]
    inbox._SCANNING.add(ids["principal"])
    assert client.post("/delegation/inbox/check", headers=OWNER).json()["detail"]["code"] == "in_progress"


def _card(owner_id: int, monkeypatch: pytest.MonkeyPatch) -> tuple[FakeInbox, int]:
    """One card, made the way the watcher makes it."""
    from openexecutive.delegation import ghostwriter as gw
    from openexecutive.delegation import inbox_classifier as ic

    async def classifier(model: str, turn: str) -> dict[str, Any]:
        return {"needs_reply": True, "kind": "question", "confidence": 0.9}

    async def composer(model: str, system: str, turn: str) -> dict[str, Any]:
        return {"subject": "Re: Thursday call", "body": "Hi Dana,\n\nI'll get back to you.\n\nOlivia"}

    monkeypatch.setattr(ic, "_call_model", classifier)
    monkeypatch.setattr(gw, "_call_model", composer)
    set_enabled(owner_id, True, updated_by="t")
    inbox.set_watch(owner_id, True, updated_by="t", now=datetime(2026, 9, 29, tzinfo=UTC))
    inbox.set_mailbox_drafts(owner_id, True, updated_by="t")
    mailbox = FakeInbox()
    mailbox.add(_msg("m1", "t1"))
    person = people_store.get_person(owner_id)
    asyncio.run(inbox.scan_person(person, gmail=mailbox, now=NOW))
    return mailbox, inbox.open_cards(owner_id)[0].id


def test_the_cards_are_the_owners_alone(
    client: TestClient, ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    _mailbox, card_id = _card(ids["principal"], monkeypatch)
    cards = client.get("/delegation/replies", headers=OWNER).json()["cards"]
    assert [c["decision_id"] for c in cards] == [card_id]
    card = cards[0]
    assert card["from_email"] == "dana@northpeak.example" and card["draft_to"] == ["dana@northpeak.example"]
    assert card["gmail_link"].startswith("https://mail.google.com/")
    assert client.get("/delegation/replies", headers=TEAMMATE).status_code == 403
    # Nor can anyone else find it through /decisions.
    assert client.get("/decisions", params={"decision_class": "delegation_reply"}, headers=TEAMMATE).json() == []
    assert client.get(f"/decisions/{card_id}", headers=TEAMMATE).status_code == 404
    assert client.post(f"/decisions/{card_id}/reject", json={}, headers=TEAMMATE).status_code == 404


def test_dismiss_through_decisions_deletes_the_unedited_draft(
    client: TestClient, ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    mailbox, card_id = _card(ids["principal"], monkeypatch)
    monkeypatch.setattr("openexecutive.delegation.gmail.gmail_for", lambda email: mailbox)
    resp = client.post(f"/decisions/{card_id}/reject", json={}, headers=OWNER)
    assert resp.status_code == 200 and resp.json()["status"] == "rejected"
    assert mailbox.deleted == ["d1"]
    assert client.get("/delegation/replies", headers=OWNER).json()["cards"] == []


def test_send_through_decisions(
    client: TestClient, ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    mailbox, card_id = _card(ids["principal"], monkeypatch)
    monkeypatch.setattr("openexecutive.delegation.gmail.gmail_for", lambda email: mailbox)
    # A server without signed callers never sends.
    resp = client.post(f"/decisions/{card_id}/approve", json={}, headers=OWNER)
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "caller_signing_required"
    monkeypatch.setenv("OE_LOCAL_LOGIN", "1")
    # A teammate can't see it, let alone send it.
    assert client.post(f"/decisions/{card_id}/approve", json={}, headers=TEAMMATE).status_code == 404
    # What changed since the card was made needs a second yes, naming who it goes to.
    mailbox.edit("d1", cc=["sam@northpeak.example"])
    resp = client.post(f"/decisions/{card_id}/approve", json={"edits": {"recipients": ["dana@northpeak.example"]}},
                       headers=OWNER)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "confirm" and detail["reasons"] == ["recipients_changed"]
    assert detail["recipients"] == ["dana@northpeak.example", "sam@northpeak.example"]
    assert mailbox.sent == []
    resp = client.post(f"/decisions/{card_id}/approve", json={"edits": {"recipients": detail["recipients"]}},
                       headers=OWNER)
    assert resp.status_code == 200 and resp.json()["status"] == "approved_with_edit"
    assert mailbox.sent == ["d1"]
    # Once: a second tap finds it settled.
    assert client.post(f"/decisions/{card_id}/approve", json={}, headers=OWNER).status_code == 409
    assert mailbox.sent == ["d1"]
    assert client.get("/delegation/replies", headers=OWNER).json()["cards"] == []


def test_local_login_sends_with_no_caller_header(
    client: TestClient, ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The local-login web app sends no caller: the person at the keyboard."""
    mailbox, card_id = _card(ids["principal"], monkeypatch)
    monkeypatch.setattr("openexecutive.delegation.gmail.gmail_for", lambda email: mailbox)
    monkeypatch.setenv("OE_LOCAL_LOGIN", "1")
    resp = client.post(f"/decisions/{card_id}/approve", json={})
    assert resp.status_code == 200 and resp.json()["status"] == "approved_unchanged"
    assert mailbox.sent == ["d1"]


def test_send_and_allow_through_decisions(
    client: TestClient, ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.delegation import training

    mailbox, card_id = _card(ids["principal"], monkeypatch)
    monkeypatch.setattr("openexecutive.delegation.gmail.gmail_for", lambda email: mailbox)
    monkeypatch.setenv("OE_LOCAL_LOGIN", "1")
    # Not in training: refused, nothing sent.
    resp = client.post(f"/decisions/{card_id}/approve", json={"edits": {"allow": True}}, headers=OWNER)
    assert resp.status_code == 422 and resp.json()["detail"]["code"] == "cant_allow"
    assert mailbox.sent == []
    training.set_(ids["principal"], {training.REPLIES: True, training.DRAFTS: True}, updated_by="t")
    assert client.get("/delegation/replies", headers=OWNER).json()["cards"][0]["can_allow"] is True
    mailbox.edit("d1")
    mailbox.drafts["d1"].message.text = "Dana, Friday is great."
    resp = client.post(
        f"/decisions/{card_id}/approve", json={"edits": {"allow": True, "example": True}}, headers=OWNER,
    )
    assert resp.status_code == 200 and mailbox.sent == ["d1"]
    assert training.example_for(ids["principal"], "dana@northpeak.example") == "Dana, Friday is great."
    assert training.allowed_sender(ids["principal"], "dana@northpeak.example") is not None


def test_keeping_replies_in_the_mailbox_is_the_callers_own_choice_and_off_by_default(
    client: TestClient, ids: dict[str, int]
) -> None:
    set_enabled(ids["principal"], True, updated_by="t")
    body = client.put("/delegation/inbox", json={"enabled": True}, headers=OWNER).json()
    assert body["inbox"]["mailbox_drafts"] is False
    body = client.put("/delegation/inbox", json={"mailbox_drafts": True}, headers=OWNER).json()
    assert body["inbox"]["mailbox_drafts"] is True and body["inbox"]["enabled"] is True
    assert client.put("/delegation/inbox", json={}, headers=OWNER).status_code == 422
    assert client.put("/delegation/inbox", json={"mailbox_drafts": True}, headers=TEAMMATE).status_code == 403
    assert inbox.get_watch(ids["teammate"]).mailbox_drafts is False


def test_changing_a_replys_words_on_its_card(
    client: TestClient, ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    mailbox, card_id = _card(ids["principal"], monkeypatch)
    monkeypatch.setattr("openexecutive.delegation.gmail.gmail_for", lambda email: mailbox)
    url = f"/delegation/replies/{card_id}/text"
    # Like Send: only a caller the API can tie to the person.
    resp = client.put(url, json={"text": "Friday at 10 works."}, headers=OWNER)
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "caller_signing_required"
    monkeypatch.setenv("OE_LOCAL_LOGIN", "1")
    assert client.put(url, json={"text": "Friday at 10 works."}, headers=TEAMMATE).status_code == 403
    resp = client.put(url, json={"text": "Friday at 10 works."}, headers=OWNER)
    assert resp.status_code == 200 and resp.json()["draft_body"] == "Friday at 10 works."
    # It was in their Drafts too (this test's setting): changed there as well.
    assert mailbox.drafts["d1"].message.text == "Friday at 10 works."
    assert client.put(url, json={"text": "x" * 20_001}, headers=OWNER).status_code == 422
    assert client.put("/delegation/replies/9999/text", json={"text": "Hi"}, headers=OWNER).status_code == 404
