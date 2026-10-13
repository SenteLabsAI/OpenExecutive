"""Run again for a failed scheduled run: POST /workflows/runs/{id}/rerun."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.alerts import store as alert_store
from openexecutive.api.routes import people as people_route
from openexecutive.api.routes import workflows as workflows_route
from openexecutive.departments import registry as dept_registry
from openexecutive.departments import store as dept_store
from openexecutive.memory import episodic
from openexecutive.workflows import persistence as wf_persistence
from openexecutive.workflows.failure_alerts import file_failed_run_card


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "test.db"
    for mod in (dept_store, episodic, alert_store, wf_persistence):
        monkeypatch.setattr(mod, "DB_PATH", path)
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **k: None)
    dept_store.initialize_db(path)
    episodic.initialize_db(path)
    alert_store.initialize_db(path)
    wf_persistence.initialize_runs_db(path)
    dept_registry.invalidate()
    dept_store.seed_default_departments()
    yield path
    dept_registry.invalidate()


@pytest.fixture()
def client(db: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(workflows_route, "_caller_person_id", lambda request: 1)
    monkeypatch.setattr(people_route, "caller_is_principal", lambda request: True)
    app = FastAPI()
    app.include_router(workflows_route.router)
    return TestClient(app)


def _failed_check_in(run_id: str = "r-1", *, scheduled: bool = True) -> None:
    wf_persistence.create_run(
        run_id, "department_check_in", "Check-in: finance",
        {"department_slug": "finance", "period_label": "2026-10-12"},
    )
    wf_persistence.fail_run(run_id, "boom")
    if scheduled:  # the scheduler files the card; a /jobs run gets none
        file_failed_run_card(
            run_id=run_id, workflow_name="department_check_in", title="Finance check-in",
            scope="finance",
        )


def _pending_reruns() -> list[episodic.ScheduledAction]:
    return [
        a for a in episodic.list_scheduled_actions(status="pending", limit=50)
        if (a.scope_key or "").startswith("rerun:")
    ]


def test_run_again_queues_one_rerun_and_closes_the_card(client: TestClient) -> None:
    _failed_check_in()
    card = alert_store.list_alerts()[0].id
    assert card is not None

    res = client.post("/workflows/runs/r-1/rerun")
    assert res.status_code == 200 and res.json()["status"] == "queued"
    (action,) = _pending_reruns()
    assert action.kind == "dept_cadence" and action.channel_ref == "finance"
    closed = alert_store.get_alert(card)
    assert closed is not None and closed.status == "ack"
    # A double tap runs it once.
    assert client.post("/workflows/runs/r-1/rerun").status_code == 409
    assert len(_pending_reruns()) == 1


def test_parallel_run_again_taps_queue_it_once(db: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from openexecutive.workflows.failure_alerts import RerunRefused, queue_rerun

    _failed_check_in()
    run = wf_persistence.get_run("r-1") or {}

    def _tap(_: int) -> bool:
        try:
            queue_rerun(run)
            return True
        except RerunRefused:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        queued = list(pool.map(_tap, range(8)))
    assert queued.count(True) == 1
    assert len(_pending_reruns()) == 1


def test_run_again_refuses_what_it_cannot_rerun(client: TestClient) -> None:
    wf_persistence.create_run("ok", "department_check_in", "t", {"department_slug": "finance"})
    wf_persistence.complete_run("ok", "# done")
    assert client.post("/workflows/runs/ok/rerun").status_code == 409
    wf_persistence.create_run("manual", "board_prep", "t", {})
    wf_persistence.fail_run("manual", "boom")
    res = client.post("/workflows/runs/manual/rerun")
    assert res.status_code == 409 and "its page" in res.json()["detail"]
    assert client.post("/workflows/runs/missing/rerun").status_code == 404
    # A check-in started by hand has no failure card: not rerun on schedule terms.
    _failed_check_in("by-hand", scheduled=False)
    assert client.post("/workflows/runs/by-hand/rerun").status_code == 409
    assert _pending_reruns() == []


def test_only_the_principal_reruns_a_check_in(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(people_route, "caller_is_principal", lambda request: False)
    _failed_check_in()
    assert client.post("/workflows/runs/r-1/rerun").status_code == 403
    assert _pending_reruns() == []


def test_a_failed_saved_workflow_files_a_card_for_its_recipient_and_a_rerun_does_not_chain(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    from types import SimpleNamespace

    from openexecutive.scheduler import runner
    from openexecutive.workflows import dynamic_store

    defn = SimpleNamespace(name="cash_check", title="Friday cash check", is_active=True)
    monkeypatch.setattr(dynamic_store, "get_definition", lambda name: defn)

    def _broken(name: str) -> object:
        raise RuntimeError("tool gone")

    monkeypatch.setattr("openexecutive.workflows.get_workflow", _broken)
    chained: list[str] = []
    monkeypatch.setattr(
        "openexecutive.workflows.dynamic_cadence.schedule_dynamic_workflow_cadence",
        lambda d, after=None: chained.append(d.name),
    )
    monkeypatch.setattr(runner, "mark_action_done", lambda aid: None)

    action = SimpleNamespace(id=1, channel_ref="cash_check", assigned_to_person_id=11, scope_key=None)
    asyncio.run(runner._run_dynamic_workflow(action, now=__import__("datetime").datetime.now()))  # type: ignore[arg-type]
    (card,) = alert_store.list_alerts()
    assert card.headline == "Friday cash check didn't finish"
    assert card.routed_to_person_id == 11
    assert chained == ["cash_check"]

    rerun = SimpleNamespace(id=2, channel_ref="cash_check", assigned_to_person_id=11, scope_key="rerun:x")
    asyncio.run(runner._run_dynamic_workflow(rerun, now=__import__("datetime").datetime.now()))  # type: ignore[arg-type]
    assert chained == ["cash_check"]  # the one-off left the cadence alone
    assert alert_store.list_alerts()[0].occurrence_count == 2


def test_a_repeat_failure_points_the_card_at_the_newest_run(db: Path) -> None:
    _failed_check_in("monday")
    _failed_check_in("tuesday")
    (card,) = alert_store.list_alerts()
    assert card.occurrence_count == 2
    assert "failed_run:tuesday" in card.topic_tags
    assert "failed_run:monday" not in card.topic_tags
