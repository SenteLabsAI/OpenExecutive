"""Delegate → Approvals: the last 7 days and Could go on its own
(orchestrator/go_on_its_own.py, api/routes/approvals.py)."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.alerts import store as alerts_store
from openexecutive.api.routes import approvals as approvals_route
from openexecutive.delegation import training
from openexecutive.delegation.inbox import DECISION_CLASS as REPLY_CLASS
from openexecutive.memory import decision_ledger as ledger
from openexecutive.orchestrator import go_on_its_own
from openexecutive.orchestrator import take_the_lead as ttl

from . import test_delegation_inbox as _inbox_tests
from .test_delegation_inbox import DANA

db = _inbox_tests.db
owner = _inbox_tests.owner

KENJI = "kenji@harbor.example"


@pytest.fixture(autouse=True)
def local_login(monkeypatch: pytest.MonkeyPatch, db: Path) -> Iterator[None]:
    monkeypatch.setenv("OE_LOCAL_LOGIN", "1")
    monkeypatch.delenv("OE_PUBLIC_DEPLOYMENT", raising=False)
    monkeypatch.delenv("CALLER_ASSERTION_PUBLIC_KEYS", raising=False)
    monkeypatch.setattr(alerts_store, "DB_PATH", db)
    alerts_store.initialize_db(db)
    yield


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(approvals_route.router)
    return TestClient(app)


def _reply(person_id: int, to: str, *, name: str = "Kenji Watanabe", status: str = ledger.STATUS_APPROVED_UNCHANGED,
           reason: str = "", gate: str = "propose") -> int:
    payload: dict[str, Any] = {
        "person_id": person_id, "from_name": name, "from_email": to, "draft_to": [to], "subject": "Sample cells",
        **({"handle_it_reason": reason} if reason else {}),
    }
    card = ledger.create_decision_instance(
        decision_class=REPLY_CLASS, department="", originating_session_id=None, proposed_payload=payload,
        idempotency_key=None, gate_mode=gate, approver_person_id=person_id, confidence=None,
    )
    if status == ledger.STATUS_EXECUTED:
        ledger.mark_executed(card)
    elif status != ledger.STATUS_PROPOSED:
        ledger.mark_resolved(card, status, resolver_person_id=person_id)
    return card


def _sent_unchanged(owner: Any, to: str = KENJI, times: int = ttl.SUGGEST_AFTER) -> None:
    for _ in range(times):
        _reply(owner.id, to)


# ── As you: who could go on its own ───────────────────────────────────────────


def test_three_unchanged_sends_in_training_offer_that_person(owner: Any) -> None:
    training.set_(owner.id, {training.TRAINING: True}, updated_by="t")
    _sent_unchanged(owner, times=ttl.SUGGEST_AFTER - 1)
    assert training.offers(owner.id) == []
    _reply(owner.id, KENJI, status=ledger.STATUS_APPROVED_WITH_EDIT)  # an edit doesn't count
    assert training.offers(owner.id) == []
    _reply(owner.id, KENJI)
    [offer] = training.offers(owner.id)
    assert offer.address == KENJI and offer.approvals == ttl.SUGGEST_AFTER
    assert offer.label == "Replies to Kenji Watanabe"


def test_no_offer_outside_training(owner: Any) -> None:
    # Outside In training Approvals alone decides: allowing someone would change nothing.
    _sent_unchanged(owner)
    assert training.offers(owner.id) == []


def test_let_it_go_allows_the_person(owner: Any) -> None:
    training.set_(owner.id, {training.TRAINING: True}, updated_by="t")
    _sent_unchanged(owner)
    [offer] = training.offers(owner.id)
    assert training.let_go(owner.id, "act_as_me|person:1|person|nobody@x.example") is None
    allowed = training.let_go(owner.id, offer.key)
    assert allowed is not None and training.allowed_person(owner.id, KENJI) is not None
    assert training.offers(owner.id) == []


def test_keep_asking_waits_for_three_more(owner: Any) -> None:
    training.set_(owner.id, {training.TRAINING: True}, updated_by="t")
    _sent_unchanged(owner)
    [offer] = training.offers(owner.id)
    assert training.keep_asking(owner.id, offer.key) is True
    assert training.offers(owner.id) == [] and training.allowed_person(owner.id, KENJI) is None
    _sent_unchanged(owner, times=ttl.SUGGEST_AFTER - 1)
    assert training.offers(owner.id) == []
    _reply(owner.id, KENJI)
    assert [o.approvals for o in training.offers(owner.id)] == [ttl.SUGGEST_AFTER]


def test_someone_elses_cards_never_count(owner: Any) -> None:
    from openexecutive.people import store as people_store

    sam = people_store.upsert_person(full_name="Sam Teammate", email="sam@co.example")
    training.set_(owner.id, {training.TRAINING: True}, updated_by="t")
    for _ in range(ttl.SUGGEST_AFTER):
        _reply(sam, KENJI)
    assert training.offers(owner.id) == []


# ── As me: Keep asking on its suggestions ─────────────────────────────────────


def _as_me_unchanged(times: int) -> str:
    key = ""
    for n in range(times):
        decision_id = ttl.hold("message_person", {"person_id": 1, "text": f"Note {n}"},
                               ttl.Hit(ttl.TRAINING, "it's in training"), source="reflection", mcp=False)
        payload = ledger.get_decision_instance(decision_id).proposed_payload_json  # type: ignore[union-attr]
        key = __import__("json").loads(payload)["allow"]["key"]
        ledger.mark_resolved(decision_id, ledger.STATUS_APPROVED_UNCHANGED)
    return key


def test_keep_asking_on_an_as_me_suggestion(owner: Any) -> None:
    ttl.set_(ttl.SCOPE_EXECUTIVE, enabled=True, training=True, updated_by="t")
    key = _as_me_unchanged(ttl.SUGGEST_AFTER)
    [suggested] = ttl.suggestions()
    assert suggested.key == key and suggested.last_at
    assert go_on_its_own.keep_asking_as_me(key) is True
    assert ttl.suggestions() == []
    _as_me_unchanged(ttl.SUGGEST_AFTER)
    assert [s.key for s in ttl.suggestions()] == [key]


# ── the last 7 days ───────────────────────────────────────────────────────────


def test_the_week_counts_what_went_and_what_waited(owner: Any) -> None:
    _reply(owner.id, KENJI, status=ledger.STATUS_EXECUTED, gate="auto_execute")
    _reply(owner.id, KENJI, status=ledger.STATUS_EXECUTED, gate="auto_execute")
    _reply(owner.id, DANA, reason="stranger")
    _reply(owner.id, DANA, reason="stranger", status=ledger.STATUS_REJECTED)
    _reply(owner.id, DANA, reason="amount", status=ledger.STATUS_PROPOSED)
    ttl.record(scope=ttl.SCOPE_EXECUTIVE, source="reflection", tool="message_person", summary="Messaged Sam",
               status="done")
    week = go_on_its_own.week(owner.id, as_you=True, as_me_done=True)
    assert (week.went, week.waited, week.approved, week.declined) == (3, 3, 1, 1)
    assert (week.because_count, week.because) == (2, "they went to someone outside People")
    assert {i.outcome for i in week.items} == {"approved", "declined", "waiting"}
    assert week.items[0].label == "Reply to Kenji Watanabe: Sample cells"
    # A teammate reading sees none of the Executive's own.
    assert go_on_its_own.week(owner.id, as_you=True, as_me_done=False).went == 2


def test_a_card_that_lapsed_is_not_declined(owner: Any) -> None:
    _reply(owner.id, KENJI, reason="stranger", status=ledger.STATUS_AUTO_NO_RESPONSE)
    week = go_on_its_own.week(owner.id, as_you=True, as_me_done=False)
    assert (week.waited, week.declined) == (1, 0)
    assert week.items[0].outcome == go_on_its_own.CLOSED


# ── the route ─────────────────────────────────────────────────────────────────


def test_the_route_lists_and_answers_offers(client: TestClient, owner: Any) -> None:
    training.set_(owner.id, {training.TRAINING: True}, updated_by="t")
    _sent_unchanged(owner)
    body = client.get("/approvals/summary").json()
    [offer] = body["offers"]
    assert offer["side"] == "as_you" and offer["address"] == KENJI and body["week"]["waited"] == 3
    assert client.post("/approvals/offers/let-go", json={"key": "act_as_me|person:999|person|x@y.example"}).status_code == 404
    assert client.post("/approvals/offers/let-go", json={"key": "nonsense"}).status_code == 404
    after = client.post("/approvals/offers/let-go", json={"key": offer["key"]}).json()
    assert after["offers"] == [] and training.allowed_person(owner.id, KENJI) is not None


def test_a_teammate_cant_answer_someone_elses_offers(client: TestClient, owner: Any) -> None:
    from openexecutive.people import registry as people_registry
    from openexecutive.people import store as people_store

    sam = "sam@co.example"
    people_store.upsert_person(full_name="Sam Teammate", email=sam)
    people_registry.invalidate()
    ttl.set_(ttl.SCOPE_EXECUTIVE, enabled=True, training=True, updated_by="t")
    as_me_key = _as_me_unchanged(ttl.SUGGEST_AFTER)
    training.set_(owner.id, {training.TRAINING: True}, updated_by="t")
    _sent_unchanged(owner)
    [as_you_key] = [o.key for o in training.offers(owner.id)]
    headers = {"x-caller-email": sam}
    for path in ("/approvals/offers/let-go", "/approvals/offers/keep-asking"):
        assert client.post(path, json={"key": as_me_key}, headers=headers).status_code == 403
        assert client.post(path, json={"key": as_you_key}, headers=headers).status_code in (403, 404)
    assert [s.key for s in ttl.suggestions()] == [as_me_key]
    assert [o.key for o in training.offers(owner.id)] == [as_you_key]
    assert training.allowed_person(owner.id, KENJI) is None
    body = client.get("/approvals/summary", headers=headers).json()
    assert body["offers"] == []


def test_letting_go_needs_a_provable_person(client: TestClient, owner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    training.set_(owner.id, {training.TRAINING: True}, updated_by="t")
    _sent_unchanged(owner)
    [offer] = client.get("/approvals/summary").json()["offers"]
    monkeypatch.delenv("OE_LOCAL_LOGIN", raising=False)
    assert client.post("/approvals/offers/let-go", json={"key": offer["key"]}).status_code in (403, 409)
    assert training.allowed_person(owner.id, KENJI) is None


def test_an_address_with_a_bar_is_offered_whole(owner: Any) -> None:
    training.set_(owner.id, {training.TRAINING: True}, updated_by="t")
    _sent_unchanged(owner, to="x|ceo@partner.example")
    [offer] = training.offers(owner.id)
    assert offer.address == "x|ceo@partner.example"
    training.let_go(owner.id, offer.key)
    assert training.allowed_person(owner.id, "ceo@partner.example") is None


def test_a_request_that_names_no_one_reads_nothing_off_local_login(
    client: TestClient, owner: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reply(owner.id, KENJI, reason="stranger")
    monkeypatch.delenv("OE_LOCAL_LOGIN", raising=False)
    body = client.get("/approvals/summary").json()
    assert body["week"]["waited"] == 0 and body["offers"] == []
