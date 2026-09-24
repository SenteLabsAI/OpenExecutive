"""/executive/status, /executive/pause, /executive/resume."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import executive as executive_route
from openexecutive.memory import episodic
from openexecutive.people import store as people_store


@pytest.fixture()
def audit_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    db = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", db)
    monkeypatch.setattr(people_store, "DB_PATH", db)
    episodic.initialize_db(db)
    people_store.initialize_db(db)
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "openexecutive.audit.log_event",
        lambda event_type, summary, **kw: events.append((event_type, kw)),
    )
    return events


@pytest.fixture()
def client(audit_events: list[Any]) -> TestClient:
    app = FastAPI()
    app.include_router(executive_route.router)
    return TestClient(app)


def test_status_defaults_to_running(client: TestClient) -> None:
    resp = client.get("/executive/status")
    assert resp.status_code == 200
    assert resp.json() == {
        "paused": False,
        "paused_at": None,
        "paused_by": None,
        "reason": None,
        "held_actions": 0,
        # No principal on the roster yet: resume stays open.
        "can_resume": True,
    }


def test_pause_then_resume(
    client: TestClient, audit_events: list[tuple[str, dict[str, Any]]]
) -> None:
    episodic.insert_scheduled_action(
        run_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        channel="telegram",
        channel_ref="42",
        intent_text="say hi",
    )
    resp = client.post(
        "/executive/pause",
        json={"reason": "  offsite  "},
        headers={"x-caller-email": "ceo@example.com"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["paused"] is True
    assert body["paused_by"] == "ceo@example.com"
    assert body["reason"] == "offsite"
    assert body["held_actions"] == 1
    assert client.get("/executive/status").json()["paused"] is True

    # Re-pausing is a no-op: no second audit row, original state kept.
    again = client.post("/executive/pause", json={"reason": "other"})
    assert again.json()["reason"] == "offsite"

    resp = client.post("/executive/resume", headers={"x-caller-email": "ceo@example.com"})
    assert resp.status_code == 200
    assert resp.json()["paused"] is False
    assert resp.json()["held_actions"] == 1  # the held row fires on the next tick

    # Resuming while running is a no-op.
    client.post("/executive/resume")

    assert [e[0] for e in audit_events] == ["executive_paused", "executive_resumed"]
    assert audit_events[0][1]["actor"] == "ceo@example.com"
    assert audit_events[1][1]["details"]["held_actions"] == 1


def test_pause_without_body_or_caller(client: TestClient) -> None:
    resp = client.post("/executive/pause")
    assert resp.status_code == 200
    assert resp.json()["paused_by"] == "api"
    assert resp.json()["reason"] is None


def test_pause_rejects_overlong_reason(client: TestClient) -> None:
    resp = client.post("/executive/pause", json={"reason": "x" * 201})
    assert resp.status_code == 422
    assert client.get("/executive/status").json()["paused"] is False


def _roster_principal_and_teammate() -> None:
    people_store.upsert_person(
        full_name="Pat Principal", is_principal=True, email="ceo@example.com"
    )
    people_store.upsert_person(full_name="Tia Teammate", email="tia@example.com")


def test_only_the_principal_can_resume(
    client: TestClient, audit_events: list[tuple[str, dict[str, Any]]]
) -> None:
    _roster_principal_and_teammate()
    teammate = {"x-caller-email": "tia@example.com"}
    # Anyone signed in may pull the brake...
    resp = client.post("/executive/pause", headers=teammate)
    assert resp.status_code == 200
    assert resp.json()["paused_by"] == "tia@example.com"
    assert resp.json()["can_resume"] is False

    # ...but only the principal releases held work.
    denied = client.post("/executive/resume", headers=teammate)
    assert denied.status_code == 403
    stranger = client.post(
        "/executive/resume", headers={"x-caller-email": "nobody@example.com"}
    )
    assert stranger.status_code == 403
    assert client.get("/executive/status").json()["paused"] is True

    ok = client.post("/executive/resume", headers={"x-caller-email": "ceo@example.com"})
    assert ok.status_code == 200
    assert ok.json()["paused"] is False
    assert ok.json()["can_resume"] is True
    assert [e[0] for e in audit_events] == ["executive_paused", "executive_resumed"]


def test_headerless_api_caller_resolves_to_the_principal(client: TestClient) -> None:
    """CLI / direct curl (no x-caller-email) acts as the principal, as in chat."""
    _roster_principal_and_teammate()
    client.post("/executive/pause")
    resp = client.post("/executive/resume")
    assert resp.status_code == 200
    assert resp.json()["paused"] is False
