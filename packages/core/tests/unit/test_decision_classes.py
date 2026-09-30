"""Per-class decision handling (api/routes/decisions.DECISION_CLASSES) and the
ledger's execution states (memory/decision_ledger)."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from openexecutive.api.routes import decisions as route
from openexecutive.memory import decision_ledger as ledger
from openexecutive.memory.episodic import initialize_db


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from openexecutive.alerts import store as alert_store
    from openexecutive.memory import episodic

    path = tmp_path / "test.db"
    monkeypatch.setattr(episodic, "DB_PATH", path)
    monkeypatch.setattr(alert_store, "DB_PATH", path)
    initialize_db(path)
    alert_store.initialize_db(path)
    return path


@pytest.fixture()
def principal(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    """Whether the caller is the principal, per test."""
    state = {"is": True}
    monkeypatch.setattr(route, "_approver_is_principal", lambda request: state["is"])
    return state


@pytest.fixture()
def client(db: Path, principal: dict[str, bool]) -> TestClient:
    app = FastAPI()
    app.include_router(route.router)
    return TestClient(app, raise_server_exceptions=False)


def _seed(decision_class: str, idem: str = "k1", payload: dict[str, Any] | None = None) -> int:
    return ledger.create_decision_instance(
        decision_class=decision_class,
        department="operations",
        originating_session_id=None,
        proposed_payload=payload or {"title": "x"},
        idempotency_key=idem,
        gate_mode="propose",
        approver_person_id=None,
        confidence=0.8,
    )


@pytest.fixture()
def private_class(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A principal-only class whose hooks record what ran."""
    seen: dict[str, Any] = {"approved": [], "after_reject": []}

    async def approve(instance: ledger.DecisionInstance, body: Any, request: Request) -> Any:
        seen["approved"].append(instance.id)
        assert ledger.mark_resolved(instance.id, ledger.STATUS_APPROVED_UNCHANGED)
        return ledger.get_decision_instance(instance.id)

    async def after_reject(instance: ledger.DecisionInstance) -> None:
        seen["after_reject"].append(instance.status)

    spec = route.DecisionClassSpec(
        name="test_private", principal_only=True, alert_source=None,
        approve=approve, after_reject=after_reject,
    )
    monkeypatch.setitem(route.DECISION_CLASSES, spec.name, spec)
    return seen


# ── the routes ────────────────────────────────────────────────────────────────


def test_an_unknown_class_is_never_carried_out(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    booking = AsyncMock(return_value={"event_id": "e1"})
    monkeypatch.setattr(route, "_execute_booking", booking)
    iid = _seed("some_future_class")
    for action in ("approve", "reject", "cancel"):
        resp = client.post(f"/decisions/{iid}/{action}", json={})
        assert resp.status_code == 409, action
    booking.assert_not_awaited()
    assert ledger.get_decision_instance(iid).status == ledger.STATUS_PROPOSED  # type: ignore[union-attr]


def test_meeting_booking_keeps_its_behaviour(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    booking = AsyncMock(return_value={"event_id": "evt-1"})
    monkeypatch.setattr(route, "_execute_booking", booking)
    iid = _seed("meeting_scheduling", payload={
        "title": "Sync", "start": "2025-06-15T10:00:00+00:00",
        "end": "2025-06-15T11:00:00+00:00", "attendee_emails": ["a@x.example"],
    })
    resp = client.post(f"/decisions/{iid}/approve", json={})
    assert resp.status_code == 200
    assert resp.json()["status"] == ledger.STATUS_APPROVED_UNCHANGED
    assert resp.json()["external_event_id"] == "evt-1"
    booking.assert_awaited_once()


def test_a_principal_only_class_is_hidden_from_everyone_else(
    client: TestClient, principal: dict[str, bool], private_class: dict[str, Any]
) -> None:
    iid = _seed("test_private")
    principal["is"] = False
    assert client.get("/decisions", params={"decision_class": "test_private"}).json() == []
    assert client.get(f"/decisions/{iid}").status_code == 404
    assert client.post(f"/decisions/{iid}/approve", json={}).status_code == 404
    assert client.post(f"/decisions/{iid}/reject", json={}).status_code == 404
    assert private_class["approved"] == []

    principal["is"] = True
    assert [d["id"] for d in client.get("/decisions", params={"decision_class": "test_private"}).json()] == [iid]
    assert client.post(f"/decisions/{iid}/approve", json={}).status_code == 200
    assert private_class["approved"] == [iid]


def test_reject_runs_the_class_hook_after_recording(
    client: TestClient, private_class: dict[str, Any]
) -> None:
    iid = _seed("test_private")
    resp = client.post(f"/decisions/{iid}/reject", json={})
    assert resp.status_code == 200 and resp.json()["status"] == ledger.STATUS_REJECTED
    assert private_class["after_reject"] == [ledger.STATUS_REJECTED]
    # A class with no undo refuses a cancel.
    assert client.post(f"/decisions/{iid}/cancel", json={}).status_code == 409


def test_a_failing_after_reject_hook_keeps_the_reject(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, private_class: dict[str, Any]
) -> None:
    async def boom(instance: ledger.DecisionInstance) -> None:
        raise RuntimeError("gmail down")

    spec = route.DECISION_CLASSES["test_private"]
    monkeypatch.setitem(route.DECISION_CLASSES, "test_private", route.DecisionClassSpec(
        name=spec.name, principal_only=True, alert_source=None, approve=spec.approve,
        after_reject=boom,
    ))
    iid = _seed("test_private")
    assert client.post(f"/decisions/{iid}/reject", json={}).status_code == 200
    assert ledger.get_decision_instance(iid).status == ledger.STATUS_REJECTED  # type: ignore[union-attr]


def test_a_reject_that_loses_the_race_is_a_409(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, private_class: dict[str, Any]
) -> None:
    iid = _seed("test_private")
    monkeypatch.setattr(route, "mark_resolved", lambda *a, **kw: False)
    assert client.post(f"/decisions/{iid}/reject", json={}).status_code == 409
    assert private_class["after_reject"] == []


def test_the_spec_approve_refusal_passes_through(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refuse(instance: Any, body: Any, request: Request) -> Any:
        raise HTTPException(status_code=409, detail={"code": "confirm_needed"})

    monkeypatch.setitem(route.DECISION_CLASSES, "test_refuse", route.DecisionClassSpec(
        name="test_refuse", principal_only=False, alert_source=None, approve=refuse,
    ))
    iid = _seed("test_refuse")
    resp = client.post(f"/decisions/{iid}/approve", json={})
    assert resp.status_code == 409 and resp.json()["detail"] == {"code": "confirm_needed"}


# ── the ledger's execution states ─────────────────────────────────────────────


def test_only_one_claim_wins(db: Path) -> None:
    iid = _seed("test_private")
    assert ledger.claim_for_execution(iid, resolver_person_id=7) is True
    assert ledger.claim_for_execution(iid, resolver_person_id=8) is False
    row = ledger.get_decision_instance(iid)
    assert row is not None and row.status == ledger.STATUS_EXECUTING and row.resolver_person_id == 7
    # Still open: a second proposal with the same key is not made.
    assert ledger.get_live_by_idem("k1") is not None
    # Nor can it be approved or rejected the ordinary way meanwhile.
    assert ledger.mark_resolved(iid, ledger.STATUS_REJECTED) is False


def test_finish_moves_only_an_executing_row(db: Path) -> None:
    iid = _seed("test_private")
    assert ledger.finish_execution(iid, ledger.STATUS_APPROVED_UNCHANGED) is False
    assert ledger.claim_for_execution(iid)
    assert ledger.finish_execution(iid, ledger.STATUS_APPROVED_WITH_EDIT, external_event_id="m1")
    row = ledger.get_decision_instance(iid)
    assert row is not None and row.status == ledger.STATUS_APPROVED_WITH_EDIT
    assert row.resolved_at is not None and row.external_event_id == "m1"
    assert ledger.finish_execution(iid, ledger.STATUS_FAILED) is False
    with pytest.raises(ValueError):
        ledger.finish_execution(iid, ledger.STATUS_REJECTED)


def test_release_puts_the_decision_back(db: Path) -> None:
    iid = _seed("test_private")
    assert ledger.release_claim(iid) is False
    assert ledger.claim_for_execution(iid, resolver_person_id=7)
    assert ledger.release_claim(iid) is True
    row = ledger.get_decision_instance(iid)
    assert row is not None and row.status == ledger.STATUS_PROPOSED and row.resolver_person_id is None


def test_close_externally_only_closes_an_open_row(db: Path) -> None:
    proposed, executing, done = _seed("c", "a"), _seed("c", "b"), _seed("c", "c")
    assert ledger.claim_for_execution(executing)
    assert ledger.mark_resolved(done, ledger.STATUS_REJECTED)
    assert ledger.close_externally(proposed, reason="sent_in_gmail")
    assert ledger.close_externally(executing, reason="draft_deleted")
    assert ledger.close_externally(done, reason="late") is False
    row = ledger.get_decision_instance(proposed)
    assert row is not None and row.status == ledger.STATUS_CLOSED_EXTERNALLY
    assert row.reversal_reason == "sent_in_gmail" and row.resolved_at is not None
    assert ledger.get_decision_instance(done).status == ledger.STATUS_REJECTED  # type: ignore[union-attr]
