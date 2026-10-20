"""Integration tests for the /scheduled admin endpoints."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes.scheduled import router as scheduled_router
from openexecutive.memory.episodic import (
    claim_due_actions,
    initialize_db,
    insert_scheduled_action,
)


@pytest.fixture(autouse=True)
def _isolate_audit_and_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    # A cancel writes an audit row; keep it out of ./episodic_memory.db. And a
    # BACKEND_SHARED_SECRET leaking in from the shell must not change the
    # admin-token / loopback cases these tests pin (signed-in cancels are
    # covered in tests/unit/test_scheduled_cancel_permission.py).
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **k: None)
    monkeypatch.delenv("BACKEND_SHARED_SECRET", raising=False)


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    db_path = tmp_path / "episodic.db"
    initialize_db(db_path)
    monkeypatch.setattr("openexecutive.memory.episodic.DB_PATH", db_path)
    # Test client uses 'testclient' as the host. Force loopback so the
    # fail-closed admin guard treats it like local-dev.
    monkeypatch.delenv("SCHEDULED_ADMIN_TOKEN", raising=False)

    app = FastAPI()
    app.include_router(scheduled_router)
    # TestClient defaults to ('testclient', 50000); override to loopback so
    # the fail-closed admin guard treats requests like local dev traffic.
    return TestClient(app, client=("127.0.0.1", 50000))


def _future_iso(seconds: int = 600) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


def _past_iso(seconds: int = 60) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()


def test_list_returns_pending_only_by_default(client: TestClient) -> None:
    insert_scheduled_action(
        run_at=_future_iso(), channel="telegram", channel_ref="1", intent_text="a"
    )
    insert_scheduled_action(
        run_at=_future_iso(120), channel="telegram", channel_ref="2", intent_text="b"
    )
    resp = client.get("/scheduled")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 2
    assert all(r["status"] == "pending" for r in rows)


def test_get_single(client: TestClient) -> None:
    action_id = insert_scheduled_action(
        run_at=_future_iso(), channel="telegram", channel_ref="1", intent_text="t"
    )
    resp = client.get(f"/scheduled/{action_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == action_id


def test_get_missing_returns_404(client: TestClient) -> None:
    resp = client.get("/scheduled/9999")
    assert resp.status_code == 404


def test_cancel_pending(client: TestClient) -> None:
    action_id = insert_scheduled_action(
        run_at=_future_iso(), channel="telegram", channel_ref="1", intent_text="t"
    )
    resp = client.delete(f"/scheduled/{action_id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"


def test_cancel_running_returns_409(client: TestClient) -> None:
    action_id = insert_scheduled_action(
        run_at=_past_iso(), channel="telegram", channel_ref="1", intent_text="t"
    )
    claim_due_actions(datetime.now(UTC))  # flip to running
    resp = client.delete(f"/scheduled/{action_id}")
    assert resp.status_code == 409


def test_admin_token_enforced_on_delete_when_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "episodic.db"
    initialize_db(db_path)
    monkeypatch.setattr("openexecutive.memory.episodic.DB_PATH", db_path)
    monkeypatch.setenv("SCHEDULED_ADMIN_TOKEN", "secret123")

    action_id = insert_scheduled_action(
        run_at=_future_iso(), channel="telegram", channel_ref="1", intent_text="t"
    )

    app = FastAPI()
    app.include_router(scheduled_router)
    c = TestClient(app, client=("127.0.0.1", 50000))

    # GET is unauthenticated regardless of token.
    assert c.get("/scheduled").status_code == 200

    # DELETE without token → 401.
    assert c.delete(f"/scheduled/{action_id}").status_code == 401
    # Wrong token → 401.
    assert (
        c.delete(
            f"/scheduled/{action_id}", headers={"X-Admin-Token": "nope"}
        ).status_code
        == 401
    )
    # Right token → 200.
    assert (
        c.delete(
            f"/scheduled/{action_id}", headers={"X-Admin-Token": "secret123"}
        ).status_code
        == 200
    )


def test_list_invalid_status_returns_400(client: TestClient) -> None:
    resp = client.get("/scheduled?status=garbage")
    assert resp.status_code == 400


def test_remote_host_delete_without_token_returns_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELETE from a non-loopback client with no admin token is refused outright."""
    db_path = tmp_path / "episodic.db"
    initialize_db(db_path)
    monkeypatch.setattr("openexecutive.memory.episodic.DB_PATH", db_path)
    monkeypatch.delenv("SCHEDULED_ADMIN_TOKEN", raising=False)
    action_id = insert_scheduled_action(
        run_at=_future_iso(), channel="telegram", channel_ref="1", intent_text="t"
    )

    app = FastAPI()
    app.include_router(scheduled_router)
    # Force a non-loopback client host on the test request.
    c = TestClient(app, client=("203.0.113.5", 50000))
    # GET is now unauthenticated and works from any host.
    assert c.get("/scheduled").status_code == 200
    # DELETE without a token from a non-loopback client is refused.
    assert c.delete(f"/scheduled/{action_id}").status_code == 503
