"""Editing a department's check-in cadence re-times its queued check-in.

Covers `cadence.reschedule_check_in` / `cadence.next_check_in_at` and the
`next_check_in_at` field GET and PATCH /departments/{slug} return. "Off" is an
empty `check_in` spec.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import departments as departments_route
from openexecutive.departments import registry, store
from openexecutive.departments.cadence import (
    bootstrap_cadences,
    next_check_in_at,
    reschedule_check_in,
)
from openexecutive.memory import episodic, workspace_settings


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "episodic.db"
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(episodic, "DB_PATH", path)
    registry.invalidate()
    episodic.initialize_db(path)
    store.initialize_db()
    store.seed_default_departments()
    return path


@pytest.fixture()
def client(db: Path) -> TestClient:
    app = FastAPI()
    app.include_router(departments_route.router)
    return TestClient(app)


def _pending(slug: str, db: Path) -> list[episodic.ScheduledAction]:
    return [
        a for a in episodic.list_scheduled_actions(db_path=db)
        if a.kind == "dept_cadence" and a.department == slug and a.status == "pending"
    ]


def test_next_check_in_at_reads_the_pending_row(db: Path) -> None:
    assert next_check_in_at("finance") is None
    bootstrap_cadences()
    when = next_check_in_at("finance")
    assert when is not None
    # The default is weekly, Monday 09:00 UTC.
    parsed = datetime.fromisoformat(when).astimezone(UTC)
    assert (parsed.weekday(), parsed.hour, parsed.minute) == (0, 9, 0)


def test_reschedule_replaces_the_pending_row(db: Path) -> None:
    bootstrap_cadences()
    [old] = _pending("finance", db)
    store.update_department("finance", cadences={"check_in": "daily@14:30"})

    new_id = reschedule_check_in("finance")

    assert new_id is not None
    [new] = _pending("finance", db)
    assert new.id == new_id != old.id
    run_at = datetime.fromisoformat(new.run_at)
    assert (run_at.hour, run_at.minute) == (14, 30)
    old_after = episodic.get_scheduled_action(old.id, db_path=db)
    assert old_after is not None and old_after.status == "cancelled"
    # Other departments' check-ins are untouched.
    assert len(_pending("strategy", db)) == 1


def test_reschedule_keeps_the_old_row_when_the_new_one_cannot_be_queued(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.departments import cadence

    bootstrap_cadences()
    [old] = _pending("finance", db)
    store.update_department("finance", cadences={"check_in": "daily@14:30"})
    monkeypatch.setattr(cadence, "enqueue_next", lambda slug, **kw: None)

    assert reschedule_check_in("finance") is None
    assert [a.id for a in _pending("finance", db)] == [old.id]


def test_reschedule_leaves_a_queued_rerun_alone(db: Path) -> None:
    bootstrap_cadences()
    rerun_id = episodic.insert_scheduled_action(
        run_at=datetime.now(UTC).isoformat(),
        channel="__internal__",
        channel_ref="finance",
        intent_text="Run again: Finance check-in",
        department="finance",
        kind="dept_cadence",
        scope_key="rerun:r-1",
        db_path=db,
    )
    store.update_department("finance", cadences={"check_in": "daily@14:30"})

    new_id = reschedule_check_in("finance")

    assert sorted(a.id for a in _pending("finance", db)) == sorted([rerun_id, new_id])


def test_reschedule_with_check_in_off_queues_nothing(db: Path) -> None:
    bootstrap_cadences()
    store.update_department("finance", cadences={"check_in": ""})

    assert reschedule_check_in("finance") is None
    assert _pending("finance", db) == []
    assert next_check_in_at("finance") is None


def test_reschedule_in_solo_queues_nothing(db: Path) -> None:
    workspace_settings._upsert(None, mode="solo")
    store.update_department("finance", cadences={"check_in": "daily@09:00"})
    assert reschedule_check_in("finance") is None
    assert _pending("finance", db) == []


def test_get_returns_next_check_in_at(client: TestClient, db: Path) -> None:
    assert client.get("/departments/finance").json()["next_check_in_at"] is None
    bootstrap_cadences()
    data = client.get("/departments/finance").json()
    assert data["next_check_in_at"] == _pending("finance", db)[0].run_at


def test_patch_cadence_re_times_the_check_in(client: TestClient, db: Path) -> None:
    bootstrap_cadences()
    resp = client.patch("/departments/finance", json={"cadences": {"check_in": "weekly@fri@16:00"}})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    [pending] = _pending("finance", db)
    assert data["next_check_in_at"] == pending.run_at
    run_at = datetime.fromisoformat(pending.run_at).astimezone(UTC)
    assert (run_at.weekday(), run_at.hour) == (4, 16)


def test_patch_check_in_off_cancels_the_pending_check_in(client: TestClient, db: Path) -> None:
    bootstrap_cadences()
    resp = client.patch("/departments/finance", json={"cadences": {"check_in": ""}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["config"]["cadences"] == {"check_in": ""}
    assert resp.json()["next_check_in_at"] is None
    assert _pending("finance", db) == []

    # Turning it back on queues the next one straight away.
    resp = client.patch("/departments/finance", json={"cadences": {"check_in": "daily@09:00"}})
    assert resp.json()["next_check_in_at"] is not None
    assert len(_pending("finance", db)) == 1


def test_patch_without_a_cadence_change_leaves_the_row(client: TestClient, db: Path) -> None:
    bootstrap_cadences()
    [before] = _pending("finance", db)
    # The settings form always sends the cadences; an unchanged one is no edit.
    resp = client.patch(
        "/departments/finance",
        json={"headcount": 4, "cadences": {"check_in": "weekly@mon@09:00"}},
    )
    assert resp.status_code == 200
    [after] = _pending("finance", db)
    assert after.id == before.id


def test_concurrent_reschedules_leave_one_pending_row(db: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    bootstrap_cadences()
    store.update_department("finance", cadences={"check_in": "daily@10:00"})
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: reschedule_check_in("finance"), range(8)))
    assert len(_pending("finance", db)) == 1
