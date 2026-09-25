"""GET / PUT /workspace (api/routes/workspace.py)."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import workspace as workspace_route
from openexecutive.departments import registry as dept_registry
from openexecutive.departments import store as dept_store
from openexecutive.memory import episodic
from openexecutive.memory import workspace_settings as ws
from openexecutive.people import store as people_store


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", path)
    monkeypatch.setattr(people_store, "DB_PATH", path)
    monkeypatch.setattr(dept_store, "DB_PATH", path)
    monkeypatch.setattr(ws, "_configured_timezone", lambda: ws.ZoneInfo("UTC"))
    dept_registry.invalidate()
    episodic.initialize_db(path)
    people_store.initialize_db(path)
    dept_store.initialize_db(path)
    yield path
    dept_registry.invalidate()


@pytest.fixture()
def audit_events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "openexecutive.audit.log_event",
        lambda event_type, summary, **kw: events.append((event_type, kw)),
    )
    return events


@pytest.fixture()
def client(db: Path, audit_events: list[Any]) -> TestClient:
    app = FastAPI()
    app.include_router(workspace_route.router)
    return TestClient(app)


def _roster_principal_and_teammate() -> None:
    people_store.upsert_person(full_name="Pat Principal", is_principal=True, email="ceo@example.com")
    people_store.upsert_person(full_name="Tia Teammate", email="tia@example.com")


PRINCIPAL = {"x-caller-email": "ceo@example.com"}

# The role fields every response carries (null until set).
# What a fresh install leaves unset: the principal's role and the monthly
# AI limit.
UNSET: dict[str, Any] = {**dict.fromkeys(ws.ROLE_FIELDS), "monthly_budget_usd": None}


def test_get_defaults_before_onboarding(client: TestClient) -> None:
    resp = client.get("/workspace")
    assert resp.status_code == 200
    assert resp.json() == {
        "mode": "team", "timezone": None, "effective_timezone": "UTC", **UNSET,
    }


def test_get_reports_the_fallback_zone(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ws, "_configured_timezone", lambda: ws.ZoneInfo("Europe/Madrid"))
    assert client.get("/workspace").json() == {
        "mode": "team", "timezone": None, "effective_timezone": "Europe/Madrid", **UNSET,
    }


def test_put_timezone_and_mode(
    client: TestClient, db: Path, audit_events: list[tuple[str, dict[str, Any]]]
) -> None:
    _roster_principal_and_teammate()
    from openexecutive.scheduler.runner import seed_principal_briefs

    seed_principal_briefs()

    def _rows() -> set[tuple[int, str, str]]:
        with sqlite3.connect(str(db)) as conn:
            return set(conn.execute("SELECT id, kind, status FROM scheduled_actions"))

    before = _rows()
    resp = client.put("/workspace", json={"timezone": "America/Denver"}, headers=PRINCIPAL)
    assert resp.status_code == 200
    assert resp.json() == {
        "mode": "team", "timezone": "America/Denver", "effective_timezone": "America/Denver",
        **UNSET,
    }
    # The zone change re-timed the principal's rhythm in place: same rows,
    # still pending, nothing inserted or cancelled.
    assert _rows() == before

    resp = client.put("/workspace", json={"mode": "solo"}, headers=PRINCIPAL)
    assert resp.status_code == 200
    assert resp.json()["mode"] == "solo"
    assert resp.json()["timezone"] == "America/Denver"  # untouched by a mode-only PUT
    assert client.get("/workspace").json()["mode"] == "solo"

    assert [e[0] for e in audit_events] == ["workspace_settings_changed"] * 2
    assert audit_events[0][1]["actor"] == "ceo@example.com"
    assert audit_events[1][1]["details"]["mode"] == {"from": "team", "to": "solo"}


def test_put_clears_timezone_with_null(client: TestClient) -> None:
    client.put("/workspace", json={"timezone": "Asia/Kolkata"})
    resp = client.put("/workspace", json={"timezone": None})
    assert resp.status_code == 200
    assert resp.json()["timezone"] is None
    assert resp.json()["effective_timezone"] == "UTC"


def test_put_unchanged_values_is_a_quiet_no_op(
    client: TestClient, audit_events: list[tuple[str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail(*_a: object, **_k: object) -> Any:
        raise AssertionError("nothing changed — no side effects expected")

    monkeypatch.setattr(ws, "set_workspace_mode", _fail)
    monkeypatch.setattr(ws, "set_timezone", _fail)
    resp = client.put("/workspace", json={"mode": "team", "timezone": None})
    assert resp.status_code == 200
    assert client.put("/workspace", json={}).status_code == 200
    assert audit_events == []


@pytest.mark.parametrize(
    "body",
    [
        {"timezone": "Mars/Olympus_Mons"},
        # zoneinfo raises IsADirectoryError / ValueError for these — a 422,
        # never a 500.
        {"timezone": "America"},
        {"timezone": "Europe/"},
        {"timezone": "../etc"},
        {"timezone": "/etc/localtime"},
        {"timezone": 7},
        {"timezone": "x" * 65},
        {"mode": "enterprise"},
        {"mode": None},
        {"time_zone": "UTC"},  # unknown field: a typo must not silently no-op
    ],
)
def test_put_rejects_bad_input(client: TestClient, body: dict[str, Any]) -> None:
    resp = client.put("/workspace", json=body)
    assert resp.status_code == 422
    assert client.get("/workspace").json() == {
        "mode": "team", "timezone": None, "effective_timezone": "UTC", **UNSET,
    }


def test_put_is_principal_only(client: TestClient) -> None:
    _roster_principal_and_teammate()
    for headers in ({"x-caller-email": "tia@example.com"}, {"x-caller-email": "nobody@example.com"}):
        resp = client.put("/workspace", json={"mode": "solo"}, headers=headers)
        assert resp.status_code == 403
    assert client.get("/workspace").json()["mode"] == "team"

    # Reading stays open to everyone.
    assert client.get("/workspace", headers={"x-caller-email": "tia@example.com"}).status_code == 200


def test_headerless_caller_is_the_principal(client: TestClient) -> None:
    """CLI / direct curl / local login (no x-caller-email) acts as the principal."""
    _roster_principal_and_teammate()
    resp = client.put("/workspace", json={"mode": "solo"})
    assert resp.status_code == 200
    assert resp.json()["mode"] == "solo"


def test_anyone_may_set_it_before_a_principal_exists(client: TestClient) -> None:
    """First-run setup chooses a mode before onboarding has created a principal."""
    resp = client.put(
        "/workspace", json={"mode": "solo", "timezone": "Europe/Rome"},
        headers={"x-caller-email": "newcomer@example.com"},
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "mode": "solo", "timezone": "Europe/Rome", "effective_timezone": "Europe/Rome",
        **UNSET,
    }


def test_principal_lookup_failure_denies(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: object, **_k: object) -> Any:
        raise RuntimeError("people store down")

    monkeypatch.setattr(people_store, "find_principal_person", _boom)
    assert client.put("/workspace", json={"mode": "solo"}).status_code == 403
