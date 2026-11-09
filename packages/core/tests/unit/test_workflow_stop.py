"""Stop run: POST /workflows/runs/{id}/stop, workflows.run_control, and the
action step's own limits (repeats, time, browser calls from scripts)."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import people as people_route
from openexecutive.api.routes import workflows as workflows_route
from openexecutive.memory import episodic
from openexecutive.workflows import action_step as act
from openexecutive.workflows import persistence as wf_persistence
from openexecutive.workflows import run_control


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "test.db"
    for mod in (episodic, wf_persistence):
        monkeypatch.setattr(mod, "DB_PATH", path)
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **k: None)
    episodic.initialize_db(path)
    wf_persistence.initialize_runs_db(path)
    return path


@pytest.fixture()
def client(db: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(workflows_route, "_caller_person_id", lambda request: 1)
    monkeypatch.setattr(people_route, "caller_is_principal", lambda request: True)
    app = FastAPI()
    app.include_router(workflows_route.router)
    return TestClient(app)


# ---------------------------------------------------------------------------
# run_control
# ---------------------------------------------------------------------------


async def _slow_events(started: asyncio.Event) -> AsyncIterator[int]:
    yield 1
    started.set()
    await asyncio.sleep(60)  # a long model or tool call
    yield 2


@pytest.mark.asyncio
async def test_stop_inside_the_work_ends_the_run_not_its_driver() -> None:
    started = asyncio.Event()
    seen: list[int] = []

    async def drive() -> str:
        try:
            async for item in run_control.watch("r1", _slow_events(started)):
                seen.append(item)
        except run_control.RunStopped:
            # The driver carries on: its own task was not left cancelled.
            await asyncio.sleep(0)
            task = asyncio.current_task()
            assert task is not None and task.cancelling() == 0
            return "stopped"
        return "finished"

    task = asyncio.create_task(drive())
    await started.wait()
    assert run_control.is_running_here("r1")
    assert run_control.stop("r1") is True
    assert await asyncio.wait_for(task, 5) == "stopped"
    assert seen == [1]
    assert not run_control.is_running_here("r1")


@pytest.mark.asyncio
async def test_stop_between_events_ends_at_the_next_one() -> None:
    async def events() -> AsyncIterator[int]:
        for i in range(5):
            yield i

    seen: list[int] = []
    with pytest.raises(run_control.RunStopped):
        async for item in run_control.watch("r2", events()):
            seen.append(item)
            if item == 1:
                assert run_control.stop("r2") is True
    assert seen == [0, 1]


@pytest.mark.asyncio
async def test_an_event_made_as_stop_lands_is_not_handed_on() -> None:
    async def events() -> AsyncIterator[str]:
        run_control.stop("r3")  # Stop lands while this event is being made
        yield "sign-off question"

    seen: list[str] = []
    with pytest.raises(run_control.RunStopped):
        async for item in run_control.watch("r3", events()):
            seen.append(item)
    assert seen == []


@pytest.mark.asyncio
async def test_a_quiet_driver_just_ends() -> None:
    async def events() -> AsyncIterator[int]:
        for i in range(5):
            yield i

    seen: list[int] = []
    async for item in run_control.watch("r4", events(), quiet=True):
        seen.append(item)
        if item == 1:
            run_control.stop("r4")
    assert seen == [0, 1]


@pytest.mark.asyncio
async def test_no_sign_off_goes_out_for_a_stopped_run(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.workflows import gate, gate_delivery

    sent: list[str] = []

    async def deliver(event: Any, **kw: Any) -> Any:
        sent.append(kw["run_id"])
        raise AssertionError("must not deliver")

    monkeypatch.setattr(gate_delivery, "deliver_gate_question", deliver)
    wf_persistence.create_run("g1", "weekly_wf", "Weekly", {})
    wf_persistence.stop_run("g1")
    with pytest.raises(run_control.RunStopped):
        await gate.checkpoint_gate(run_id="g1", event=object(), db_path=db)  # type: ignore[arg-type]
    assert sent == []


def test_stopping_a_run_this_process_is_not_driving_is_a_no_op() -> None:
    assert run_control.stop("nobody") is False


# ---------------------------------------------------------------------------
# persistence + route
# ---------------------------------------------------------------------------


def test_a_stopped_run_stays_stopped(db: Path) -> None:
    wf_persistence.create_run("s1", "weekly_wf", "Weekly", {})
    assert wf_persistence.stop_run("s1") is True
    wf_persistence.complete_run("s1", "artifact")
    wf_persistence.fail_run("s1", "boom")
    run = wf_persistence.get_run("s1")
    assert run is not None and run["status"] == "stopped"
    # Already finished: nothing to stop.
    assert wf_persistence.stop_run("s1") is False
    wf_persistence.create_run("d1", "weekly_wf", "Weekly", {})
    wf_persistence.complete_run("d1", "done")
    assert wf_persistence.stop_run("d1") is False


def test_stop_route_stops_once(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    wf_persistence.create_run("s2", "weekly_wf", "Weekly", {}, owner_person_id=1)
    stopped: list[str] = []
    monkeypatch.setattr(run_control, "stop", lambda run_id: stopped.append(run_id) or True)
    resp = client.post("/workflows/runs/s2/stop")
    assert resp.status_code == 200 and resp.json()["status"] == "stopped"
    assert stopped == ["s2"]
    run = wf_persistence.get_run("s2")
    assert run is not None and run["status"] == "stopped"
    assert client.post("/workflows/runs/s2/stop").status_code == 409


def test_a_teammate_stops_only_their_own_run(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(people_route, "caller_is_principal", lambda request: False)
    monkeypatch.setattr(run_control, "stop", lambda run_id: True)
    monkeypatch.setattr(workflows_route, "_visible_run", lambda run_id, request: wf_persistence.get_run(run_id))
    wf_persistence.create_run("t1", "weekly_wf", "Weekly", {}, owner_person_id=2)
    wf_persistence.create_run("t2", "weekly_wf", "Weekly", {}, owner_person_id=1)
    assert client.post("/workflows/runs/t1/stop").status_code == 403
    assert (wf_persistence.get_run("t1") or {})["status"] == "running"
    assert client.post("/workflows/runs/t2/stop").status_code == 200


def test_a_finished_run_cannot_be_stopped(client: TestClient) -> None:
    wf_persistence.create_run("f1", "weekly_wf", "Weekly", {}, owner_person_id=1)
    wf_persistence.complete_run("f1", "done")
    assert client.post("/workflows/runs/f1/stop").status_code == 409
    assert (wf_persistence.get_run("f1") or {})["status"] == "done"


# ---------------------------------------------------------------------------
# action step limits
# ---------------------------------------------------------------------------


def _calls(limit: int = 20) -> act._StepCalls:
    from openexecutive.workflows.tool_catalog import ToolInfo

    info = ToolInfo(name="oe__browser_look", description="look", read_only=True, source="builtin")
    return act._StepCalls(
        workflow_name="wf", step_id="s", allowed={"oe__browser_look"},
        resolved={"oe__browser_look": info}, budget=act._Budget(limit),
        script_budget=act._Budget(limit * 10), policy=None, actions=[],
    )


@pytest.mark.asyncio
async def test_the_same_result_four_times_ends_the_step(monkeypatch: pytest.MonkeyPatch) -> None:
    async def same(name: str, arguments: dict[str, Any], info: Any, workflow: str) -> tuple[str, bool]:
        return '{"text": "the same page"}', False

    monkeypatch.setattr(act, "_call_tool", same)
    monkeypatch.setattr(act, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(act, "_counts_on_step_budget", lambda name: True)
    calls = _calls()
    for _ in range(act.REPEAT_LIMIT):
        content, is_error = await calls.call("oe__browser_look", {})
        assert not is_error
    assert calls.ended is not None and "found nothing new" in calls.ended
    content, is_error = await calls.call("oe__browser_look", {})
    assert is_error and "ended" in content


@pytest.mark.asyncio
async def test_a_step_past_its_time_runs_nothing_more(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[str] = []

    async def tool(name: str, arguments: dict[str, Any], info: Any, workflow: str) -> tuple[str, bool]:
        ran.append(name)
        return "{}", False

    monkeypatch.setattr(act, "_call_tool", tool)
    monkeypatch.setattr(act, "_audit", lambda *a, **k: None)
    calls = _calls()
    calls._deadline = 0.0
    content, is_error = await calls.call("oe__browser_look", {})
    assert is_error and "minutes" in content and ran == []


@pytest.mark.asyncio
async def test_browser_calls_from_a_script_count_on_the_step_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    n = 0

    async def tool(name: str, arguments: dict[str, Any], info: Any, workflow: str) -> tuple[str, bool]:
        nonlocal n
        n += 1
        return f'{{"n": {n}}}', False

    monkeypatch.setattr(act, "_call_tool", tool)
    monkeypatch.setattr(act, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(act, "_counts_on_step_budget", lambda name: True)
    calls = _calls(limit=3)
    results = [await calls.script_call("oe__browser_look", {}) for _ in range(5)]
    assert [is_error for _, is_error in results] == [False, False, False, True, True]
    assert "tool-call budget" in results[-1][0]


@pytest.mark.asyncio
async def test_a_read_polled_for_a_status_is_not_a_repeat(monkeypatch: pytest.MonkeyPatch) -> None:
    async def pending(name: str, arguments: dict[str, Any], info: Any, workflow: str) -> tuple[str, bool]:
        return '{"status": "pending"}', False

    monkeypatch.setattr(act, "_call_tool", pending)
    monkeypatch.setattr(act, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(act, "_counts_on_step_budget", lambda name: False)
    calls = _calls()
    for _ in range(act.REPEAT_LIMIT + 2):
        content, is_error = await calls.call("oe__browser_look", {})
        assert not is_error
    assert calls.ended is None


def test_progress_counts_the_calls_used() -> None:
    budget = act._Budget(20)
    budget.used = 6
    assert act._counted("Scrolling…", budget) == "Scrolling (6 of 20)"
    assert act._counted("Listing items · Drive", budget) == "Listing items · Drive (6 of 20)"
