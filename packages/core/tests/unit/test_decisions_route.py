"""Tests for the decisions API route (approve, reject, cancel, reliability)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from openexecutive.memory.decision_ledger import (
    create_decision_instance,
    mark_resolved,
)
from openexecutive.memory.episodic import initialize_db


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from openexecutive.alerts import store as alert_store
    from openexecutive.memory import episodic
    from openexecutive.people import registry as people_registry
    from openexecutive.people import store as people_store
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(episodic, "DB_PATH", db_path)
    # approve/reject/cancel clear the companion briefing alert — wire the
    # alerts store to the same isolated DB so the clear is observable in-test.
    monkeypatch.setattr(alert_store, "DB_PATH", db_path)
    initialize_db(db_path)
    alert_store.initialize_db(db_path)
    # Only the owner (or the approver) may resolve a decision: these requests
    # carry no caller header, so they are the owner on this roster.
    monkeypatch.setattr(people_store, "DB_PATH", db_path)
    people_store.initialize_db(db_path)
    people_store.upsert_person(full_name="Olivia Owner", is_principal=True, email="olivia@co.example")
    people_registry.invalidate()
    return db_path


def _seed_companion_alert(db: Path, instance_id: int) -> None:
    """Insert the briefing alert that calendar_tools links to a proposal."""
    from openexecutive.alerts.store import insert_alert
    insert_alert(
        source="decision_scheduling",
        external_id=f"decision:{instance_id}",
        severity="medium",
        headline="Approve meeting: Sync",
        body="...",
        topic_tags=[f"decision_instance:{instance_id}", "decision_class:meeting_scheduling"],
        dedup_key=f"decision:{instance_id}",
        routed_to_person_id=None,
        db_path=db,
    )


@pytest.fixture()
def client(db: Path) -> TestClient:
    from fastapi import FastAPI

    from openexecutive.api.routes.decisions import router
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


def _seed(db: Path, idem: str = "k1") -> int:
    return create_decision_instance(
        decision_class="meeting_scheduling",
        department="operations",
        originating_session_id=None,
        proposed_payload={
            "title": "Sync",
            "start": "2025-06-15T10:00:00+00:00",
            "end": "2025-06-15T11:00:00+00:00",
            "attendee_emails": ["alice@example.com"],
            "description": "",
        },
        idempotency_key=idem,
        gate_mode="propose",
        approver_person_id=None,
        confidence=0.8,
        db_path=db,
    )


# ---------------------------------------------------------------------------
# GET /decisions
# ---------------------------------------------------------------------------

def test_list_empty(client: TestClient) -> None:
    res = client.get("/decisions")
    assert res.status_code == 200
    assert res.json() == []


def test_list_returns_instance(client: TestClient, db: Path) -> None:
    _seed(db)
    res = client.get("/decisions")
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 1
    assert data[0]["status"] == "proposed"


def test_list_status_filter(client: TestClient, db: Path) -> None:
    iid = _seed(db, "k1")
    _seed(db, "k2")
    mark_resolved(iid, "rejected", db_path=db)
    res = client.get("/decisions?status=proposed")
    assert len(res.json()) == 1


# ---------------------------------------------------------------------------
# GET /decisions/{id}
# ---------------------------------------------------------------------------

def test_get_instance(client: TestClient, db: Path) -> None:
    iid = _seed(db)
    res = client.get(f"/decisions/{iid}")
    assert res.status_code == 200
    assert res.json()["id"] == iid


def test_get_missing_404(client: TestClient) -> None:
    res = client.get("/decisions/99999")
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# POST /decisions/{id}/approve
# ---------------------------------------------------------------------------

def test_approve_creates_event(client: TestClient, db: Path) -> None:
    iid = _seed(db)
    fake_gw = type("GW", (), {})()
    fake_gw.call_tool = AsyncMock(return_value=json.dumps({"id": "evt-approve-test"}))

    # Also mock freebusy (returns no conflicts)
    async def _call_tool(args: dict) -> str:
        if args.get("name") == "google_workspace__query_freebusy":
            return json.dumps({"has_conflicts": False})
        return json.dumps({"id": "evt-approve-test"})
    fake_gw.call_tool = AsyncMock(side_effect=_call_tool)

    with patch("openexecutive.orchestrator.mcp_gateway.get_active_gateway", return_value=fake_gw):
        res = client.post(f"/decisions/{iid}/approve", json={})

    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "approved_unchanged"
    assert data["external_event_id"] == "evt-approve-test"


def test_approve_with_edit_sets_approved_with_edit(client: TestClient, db: Path) -> None:
    iid = _seed(db)
    fake_gw = type("GW", (), {})()

    async def _call_tool(args: dict) -> str:
        if args.get("name") == "google_workspace__query_freebusy":
            return json.dumps({})
        return json.dumps({"id": "evt-edit"})
    fake_gw.call_tool = AsyncMock(side_effect=_call_tool)

    with patch("openexecutive.orchestrator.mcp_gateway.get_active_gateway", return_value=fake_gw):
        res = client.post(
            f"/decisions/{iid}/approve",
            json={"edits": {"title": "Renamed Sync"}},
        )

    assert res.status_code == 200
    assert res.json()["status"] == "approved_with_edit"


def test_approve_already_approved_409(client: TestClient, db: Path) -> None:
    iid = _seed(db)
    mark_resolved(iid, "approved_unchanged", db_path=db)

    with patch("openexecutive.orchestrator.mcp_gateway.get_active_gateway",
               return_value=MagicMock()) as _:
        res = client.post(f"/decisions/{iid}/approve", json={})

    assert res.status_code == 409


def test_approve_missing_404(client: TestClient) -> None:
    with patch("openexecutive.orchestrator.mcp_gateway.get_active_gateway",
               return_value=MagicMock()):
        res = client.post("/decisions/99999/approve", json={})
    assert res.status_code == 404


def test_double_approve_books_once(client: TestClient, db: Path) -> None:
    """Second approve on an already-approved row must 409, not double-book."""
    iid = _seed(db)
    fake_gw = type("GW", (), {})()

    async def _call_tool(args: dict) -> str:
        if "freebusy" in args.get("name", ""):
            return json.dumps({})
        return json.dumps({"id": "evt-x"})
    fake_gw.call_tool = AsyncMock(side_effect=_call_tool)

    with patch("openexecutive.orchestrator.mcp_gateway.get_active_gateway", return_value=fake_gw):
        r1 = client.post(f"/decisions/{iid}/approve", json={})
        r2 = client.post(f"/decisions/{iid}/approve", json={})

    assert r1.status_code == 200
    assert r2.status_code == 409
    assert fake_gw.call_tool.await_count == 2  # freebusy + create on first call


# ---------------------------------------------------------------------------
# POST /decisions/{id}/reject
# ---------------------------------------------------------------------------

def test_reject_sets_rejected(client: TestClient, db: Path) -> None:
    iid = _seed(db)
    res = client.post(f"/decisions/{iid}/reject", json={"reason": "not needed"})
    assert res.status_code == 200
    assert res.json()["status"] == "rejected"


def test_reject_already_resolved_409(client: TestClient, db: Path) -> None:
    iid = _seed(db)
    mark_resolved(iid, "rejected", db_path=db)
    res = client.post(f"/decisions/{iid}/reject", json={})
    assert res.status_code == 409


# ---------------------------------------------------------------------------
# POST /decisions/{id}/cancel
# ---------------------------------------------------------------------------

def test_cancel_approved_event(client: TestClient, db: Path) -> None:
    iid = _seed(db)
    mark_resolved(iid, "approved_unchanged", db_path=db)

    # Inject external_event_id directly so cancel knows the event exists.
    import sqlite3

    import openexecutive.memory.episodic as _ep
    with sqlite3.connect(str(_ep.DB_PATH)) as conn:
        conn.execute(
            "UPDATE decision_instances SET external_event_id = ? WHERE id = ?",
            ("evt-cancel-test", iid),
        )

    async def _call_tool(args: dict) -> str:
        return json.dumps({"status": "cancelled"})
    fake_gw = type("GW", (), {})()
    fake_gw.call_tool = AsyncMock(side_effect=_call_tool)

    with patch("openexecutive.orchestrator.mcp_gateway.get_active_gateway", return_value=fake_gw):
        res = client.post(f"/decisions/{iid}/cancel")

    assert res.status_code == 200
    assert res.json()["status"] == "reversed"
    fake_gw.call_tool.assert_awaited_once()


def test_cancel_unknown_404(client: TestClient) -> None:
    res = client.post("/decisions/99999/cancel")
    assert res.status_code == 404


def test_cancel_rejected_409(client: TestClient, db: Path) -> None:
    iid = _seed(db)
    mark_resolved(iid, "rejected", db_path=db)
    res = client.post(f"/decisions/{iid}/cancel")
    assert res.status_code == 409


# ---------------------------------------------------------------------------
# GET /audit/reliability
# ---------------------------------------------------------------------------

def test_reliability_empty(client: TestClient) -> None:
    res = client.get("/audit/reliability?decision_class=meeting_scheduling&days=30")
    assert res.status_code == 200
    data = res.json()
    assert data["volume"] == 0
    assert data["unchanged_approval_rate"] == 0.0


# ---------------------------------------------------------------------------
# Resolving a decision clears its companion briefing alert
# ---------------------------------------------------------------------------

def _approve_gateway() -> object:
    """Gateway whose freebusy reports no conflict and create returns an id."""
    async def _call_tool(args: dict) -> str:
        if args.get("name") == "google_workspace__query_freebusy":
            return json.dumps({"has_conflicts": False})
        return json.dumps({"id": "evt-clear-test"})
    gw = type("GW", (), {})()
    gw.call_tool = AsyncMock(side_effect=_call_tool)
    return gw


def test_approve_clears_linked_alert(client: TestClient, db: Path) -> None:
    from openexecutive.alerts.store import get_alert_by_external

    iid = _seed(db)
    _seed_companion_alert(db, iid)
    with patch("openexecutive.orchestrator.mcp_gateway.get_active_gateway", return_value=_approve_gateway()):
        res = client.post(f"/decisions/{iid}/approve", json={})
    assert res.status_code == 200
    alert = get_alert_by_external("decision_scheduling", f"decision:{iid}", db_path=db)
    assert alert is not None
    assert alert.status == "ack"


def test_reject_clears_linked_alert(client: TestClient, db: Path) -> None:
    from openexecutive.alerts.store import get_alert_by_external

    iid = _seed(db)
    _seed_companion_alert(db, iid)
    res = client.post(f"/decisions/{iid}/reject", json={"reason": "no"})
    assert res.status_code == 200
    alert = get_alert_by_external("decision_scheduling", f"decision:{iid}", db_path=db)
    assert alert is not None
    assert alert.status == "dismissed"


def test_cancel_clears_linked_alert(client: TestClient, db: Path) -> None:
    from openexecutive.alerts.store import get_alert_by_external

    iid = _seed(db)
    _seed_companion_alert(db, iid)
    # Cancel a still-proposed instance (no external event) → just reverses it.
    res = client.post(f"/decisions/{iid}/cancel")
    assert res.status_code == 200
    alert = get_alert_by_external("decision_scheduling", f"decision:{iid}", db_path=db)
    assert alert is not None
    assert alert.status == "dismissed"


def test_approve_with_no_linked_alert_still_succeeds(client: TestClient, db: Path) -> None:
    """The clear is best-effort: a decision with no companion alert (e.g.
    created before the bridge) still approves cleanly."""
    iid = _seed(db)  # no companion alert seeded
    with patch("openexecutive.orchestrator.mcp_gateway.get_active_gateway", return_value=_approve_gateway()):
        res = client.post(f"/decisions/{iid}/approve", json={})
    assert res.status_code == 200
    assert res.json()["status"] == "approved_unchanged"




# ---------------------------------------------------------------------------
# CALENDAR_PROVIDER=microsoft: approval books through the Outlook backend and
# the advisory conflict check reads the Executive's calendar view
# ---------------------------------------------------------------------------

def test_approve_with_microsoft_provider(client: TestClient, db: Path) -> None:
    iid = _seed(db)
    calls: list[dict] = []

    async def _call_tool(args: dict) -> str:
        calls.append(args)
        if args.get("name") == "microsoft_365__get-calendar-view":
            return json.dumps({"value": [{"id": "busy-1", "showAs": "busy"}]})
        return json.dumps({
            "id": "evt-ms-approve",
            "onlineMeeting": {"joinUrl": "https://teams.microsoft.com/l/meetup-join/x"},
        })

    fake_gw = type("GW", (), {})()
    fake_gw.call_tool = AsyncMock(side_effect=_call_tool)
    from openexecutive.config import get_settings

    settings = get_settings().model_copy(
        update={"calendar_provider": "microsoft", "calendar_meet_links_enabled": True}
    )

    with (
        patch("openexecutive.orchestrator.mcp_gateway.get_active_gateway", return_value=fake_gw),
        patch("openexecutive.config.get_settings", return_value=settings),
    ):
        res = client.post(f"/decisions/{iid}/approve", json={})

    assert res.status_code == 200, res.text
    data = res.json()
    assert data["status"] == "approved_unchanged"
    assert data["external_event_id"] == "evt-ms-approve"
    assert [c["name"] for c in calls] == [
        "microsoft_365__get-calendar-view",
        "microsoft_365__create-calendar-event",
    ]
    assert json.loads(data["final_payload_json"])["meet_link"] == "https://teams.microsoft.com/l/meetup-join/x"
# ---------------------------------------------------------------------------
# Who may resolve a decision
# ---------------------------------------------------------------------------


def _teammates() -> tuple[int, int]:
    from openexecutive.people import registry as people_registry
    from openexecutive.people import store as people_store

    tia = people_store.upsert_person(full_name="Tia Teammate", email="tia@co.example")
    sam = people_store.upsert_person(full_name="Sam Else", email="sam@co.example")
    people_registry.invalidate()
    return tia, sam


def _seed_for(db: Path, approver: int | None, idem: str = "k1") -> int:
    return create_decision_instance(
        decision_class="meeting_scheduling",
        department="operations",
        originating_session_id=None,
        proposed_payload={
            "title": "Sync", "start": "2025-06-15T10:00:00+00:00",
            "end": "2025-06-15T11:00:00+00:00", "attendee_emails": [], "description": "",
        },
        idempotency_key=idem,
        gate_mode="propose",
        approver_person_id=approver,
        confidence=0.8,
        db_path=db,
    )


def test_only_the_approver_or_the_owner_may_decide(client: TestClient, db: Path) -> None:
    from openexecutive.memory.decision_ledger import get_decision_instance

    tia, _sam = _teammates()
    iid = _seed_for(db, approver=tia)
    sam_headers = {"x-caller-email": "sam@co.example"}
    for action in ("approve", "reject", "cancel"):
        resp = client.post(f"/decisions/{iid}/{action}", json={}, headers=sam_headers)
        assert resp.status_code == 403, action
        assert "Only the person this went to for approval" in resp.json()["detail"]
    assert get_decision_instance(iid).status == "proposed"  # type: ignore[union-attr]

    resp = client.post(f"/decisions/{iid}/reject", json={}, headers={"x-caller-email": "tia@co.example"})
    assert resp.status_code == 200
    assert resp.json()["resolver_person_id"] == tia


def test_the_owner_may_decide_anything_and_is_recorded(client: TestClient, db: Path) -> None:
    from openexecutive.people import store as people_store

    tia, _sam = _teammates()
    owner = people_store.find_principal_person()
    assert owner is not None
    iid = _seed_for(db, approver=tia)
    mock_gw = MagicMock()
    mock_gw.call_tool = AsyncMock(return_value=json.dumps({"event_id": "e1"}))
    with patch("openexecutive.orchestrator.mcp_gateway.get_active_gateway", return_value=mock_gw), \
         patch("openexecutive.api.routes.decisions._execute_booking",
               new=AsyncMock(return_value={"event_id": "e1"})):
        resp = client.post(f"/decisions/{iid}/approve", json={},
                           headers={"x-caller-email": "olivia@co.example"})
    assert resp.status_code == 200
    assert resp.json()["resolver_person_id"] == owner.id


def test_a_decision_nobody_was_asked_is_the_owners(client: TestClient, db: Path) -> None:
    _teammates()
    iid = _seed_for(db, approver=None)
    resp = client.post(f"/decisions/{iid}/reject", json={}, headers={"x-caller-email": "tia@co.example"})
    assert resp.status_code == 403
    assert client.post(f"/decisions/{iid}/reject", json={}).status_code == 200


def test_the_reliability_card_is_the_owners(client: TestClient, db: Path) -> None:
    _teammates()
    assert client.get("/audit/reliability", headers={"x-caller-email": "tia@co.example"}).status_code == 403
    assert client.get("/audit/reliability").status_code == 200

