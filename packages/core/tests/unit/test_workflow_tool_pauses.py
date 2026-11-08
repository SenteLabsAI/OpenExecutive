"""A workflow step's own tool stops it part way: waiting for an OK, or for
something it needs (``extensions.workflow_hold`` / ``workflow_wait``).

Covers the step (the signal, the refusals after it, the report), the engine's
pause and resume (the step carries on, runs again, or the run stops), and the
resumer picking up runs once what they wait for is ready.
"""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openexecutive.memory import episodic
from openexecutive.orchestrator import extensions, levels
from openexecutive.people import store as people_store
from openexecutive.workflows import approved_targets as at
from openexecutive.workflows import dynamic_store, resumer
from openexecutive.workflows import persistence as wf_persistence
from openexecutive.workflows import tool_catalog as tc
from openexecutive.workflows.dynamic import WAITING_TIMEOUT_HOURS, DynamicWorkflow
from openexecutive.workflows.dynamic_models import DynamicWorkflowDef
from openexecutive.workflows.wait_for_human import (
    WaitForHumanEvent,
    WaitForHumanResolution,
    WorkflowResumeState,
)

from . import test_action_step as _action_step_tests
from .test_action_step import APPEND, _FakeGateway, _install, _resp, _ScriptedProvider, _text, _use

audit = _action_step_tests.audit

BROWSE = "oe__browse"


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "test.db"
    for module in (episodic, wf_persistence, people_store, dynamic_store, at):
        monkeypatch.setattr(module, "DB_PATH", db)
    episodic.initialize_db(db)
    wf_persistence.initialize_runs_db(db)
    people_store.initialize_db(db)
    dynamic_store.initialize_dynamic_workflows_db(db)
    monkeypatch.setattr(
        "openexecutive.workflows.dynamic.load_or_create_profile",
        lambda: SimpleNamespace(name="Northwind", is_empty=lambda: True),
    )
    monkeypatch.setattr(at, "approver_for", lambda name: 7)
    return db


class _Browser:
    """A stand-in extension tool: holds clicks, waits while not connected."""

    def __init__(self, *, connected: bool = True) -> None:
        self.connected = connected
        self.done: list[dict[str, Any]] = []
        self.approved: list[bool] = []
        self.drop_after_click = False

    async def __call__(self, tool_input: dict[str, Any]) -> str:
        with extensions.workflow_step("browse"):
            self.approved.append(levels.is_approved())
            if tool_input.get("do") == "broken":
                extensions.workflow_wait("device:7", "your browser")
                return json.dumps({"error": "the page crashed"})
            if not self.connected:
                return extensions.workflow_wait("device:7", "your browser")
            if tool_input.get("do") == "click" and not levels.is_approved():
                return extensions.workflow_hold({**tool_input, "_at": "https://shop.example/cart"},
                                                'Click "Buy now" on shop.example')
            self.done.append(tool_input)
            if tool_input.get("do") == "click" and self.drop_after_click:
                self.connected = False
            return json.dumps({"done": True, "page": "Order 1042 placed"})


@pytest.fixture()
def browser(monkeypatch: pytest.MonkeyPatch) -> _Browser:
    tool = _Browser()
    gw = _FakeGateway({APPEND: "Appended 1 row"})
    monkeypatch.setattr("openexecutive.orchestrator.mcp_gateway.get_active_gateway", lambda: gw)
    tool.gateway = gw  # type: ignore[attr-defined]

    async def _resolve(names: list[str]) -> dict[str, tc.ToolInfo]:
        known = {
            BROWSE: tc.ToolInfo(BROWSE, "Use the browser.", {"type": "object", "properties": {}}, None, "builtin"),
            APPEND: tc.ToolInfo(APPEND, "Append rows.", {"type": "object", "properties": {}}),
        }
        return {n: known[n] for n in names if n in known}

    monkeypatch.setattr(tc, "resolve", _resolve)
    monkeypatch.setattr(tc, "builtin_handler", lambda name: tool if name == BROWSE else None)
    return tool


def _defn() -> DynamicWorkflowDef:
    return DynamicWorkflowDef.model_validate({
        "name": "buy_wf",
        "title": "Weekly order",
        "steps": [
            {"kind": "action", "id": "order", "title": "Place the order", "goal": "Order it.",
             "tools": [BROWSE, APPEND]},
            {"kind": "synthesis", "id": "assemble", "title": "Assemble"},
        ],
    })


async def _collect(wf: DynamicWorkflow) -> list[Any]:
    return [e async for e in wf.run(wf.input_model()(), store=None)]  # type: ignore[arg-type]


def _resume(state: WorkflowResumeState, decision: str) -> list[Any]:
    wf = DynamicWorkflow(_defn())
    resolution = WaitForHumanResolution(
        run_id="run-1", reply_text=decision, source_channel="web",
        parsed_decision={"decision": decision}, person_id=7,
    )

    async def collect() -> list[Any]:
        return [
            e async for e in wf.resume(
                inputs=wf.input_model()(), state=state, resolution=resolution,
                store=None,  # type: ignore[arg-type]
            )
        ]

    return asyncio.run(collect())


def _user_turn(provider: _ScriptedProvider, call: int) -> str:
    return str(provider.calls[call]["messages"][0]["content"])


# --- outside a workflow step ----------------------------------------------------


def test_signals_outside_a_workflow_step_are_errors() -> None:
    assert "error" in json.loads(extensions.workflow_hold({}, "x"))
    assert "error" in json.loads(extensions.workflow_wait("k", "your browser"))
    with extensions.collect_workflow_signals() as found:
        # Collected, but not inside a workflow step's call.
        assert "error" in json.loads(extensions.workflow_hold({}, "x"))
    assert found == []


# --- hold: the step stops, a yes carries it on ------------------------------------


def _held(monkeypatch: pytest.MonkeyPatch) -> tuple[list[Any], _ScriptedProvider]:
    provider = _ScriptedProvider([
        _resp(_use(BROWSE, {"do": "look"}, "tu_1")),
        _resp(_use(BROWSE, {"do": "click"}, "tu_2"), _use(APPEND, {"rows": [["x"]]}, "tu_3")),
        _resp(_text("Looked at the cart; the purchase waits for an OK.")),
    ])
    _install(monkeypatch, provider)
    return asyncio.run(_collect(DynamicWorkflow(_defn()))), provider


def test_a_held_call_stops_the_step_and_pauses_the_run(
    monkeypatch: pytest.MonkeyPatch, browser: _Browser, audit: list[dict[str, Any]]
) -> None:
    events, provider = _held(monkeypatch)
    pause = events[-1]
    assert isinstance(pause, WaitForHumanEvent)
    assert pause.person_id == 7
    assert 'waits for your OK to:\n- Click "Buy now" on shop.example' in pause.question
    state = pause.resume_state
    assert state is not None and state.kind == "held_writes"
    held = state.held[0]
    assert held.carry_on and held.tool == BROWSE
    assert held.arguments == {"do": "click", "_at": "https://shop.example/cart"}
    # Nothing after the held call ran, and the model was told to report.
    assert browser.done == [{"do": "look"}]
    assert browser.gateway.calls == []  # type: ignore[attr-defined]
    results = provider.calls[2]["messages"][-1]["content"]
    assert "waiting_for_approval" in results[0]["content"]
    assert results[1]["is_error"] and "this step is paused" in results[1]["content"]
    assert provider.calls[2]["tool_choice"] == {"type": "none"}
    outcomes = [r["details"]["outcome"] for r in audit if r["type"] == "workflow_tool_call"]
    assert outcomes == ["ok", "held for approval", "refused: step paused"]


def test_yes_runs_the_held_call_approved_and_the_step_carries_on(
    monkeypatch: pytest.MonkeyPatch, browser: _Browser, audit: list[dict[str, Any]]
) -> None:
    events, _ = _held(monkeypatch)
    state = events[-1].resume_state
    provider = _ScriptedProvider([_resp(_text("Order placed and logged."))])
    _install(monkeypatch, provider)
    resumed = _resume(state, "approve")
    # Exactly the held call, as the owner's OK.
    assert browser.done[-1] == {"do": "click", "_at": "https://shop.example/cart"}
    assert browser.approved[-1] is True
    turn = _user_turn(provider, 0)
    assert "This step already started and stopped part way" in turn
    assert "Order 1042 placed" in turn and "Looked at the cart" in turn
    artifact = next(e for e in resumed if getattr(e, "type", "") == "artifact").content or ""
    assert "Order placed and logged." in artifact and "Looked at the cart" in artifact


def test_a_wait_after_a_carried_on_step_keeps_what_it_already_did(
    monkeypatch: pytest.MonkeyPatch, browser: _Browser, audit: list[dict[str, Any]]
) -> None:
    events, _ = _held(monkeypatch)
    browser.drop_after_click = True
    _install(monkeypatch, _ScriptedProvider([
        _resp(_use(BROWSE, {"do": "look"}, "tu_1")),
        _resp(_text("Ordered; logging waits for the browser.")),
    ]))
    resumed = _resume(events[-1].resume_state, "approve")
    pause = resumed[-1]
    assert isinstance(pause, WaitForHumanEvent) and pause.resume_state is not None
    assert pause.resume_state.kind == "waiting"
    # The approved click is on record, so a rerun carries on instead of buying again.
    kept = pause.resume_state.outputs["order"][1]
    assert "Order 1042 placed" in kept


def test_no_skips_the_held_call_and_the_run_goes_on(
    monkeypatch: pytest.MonkeyPatch, browser: _Browser, audit: list[dict[str, Any]]
) -> None:
    events, _ = _held(monkeypatch)
    _install(monkeypatch, _ScriptedProvider([]))  # the step does not run again
    resumed = _resume(events[-1].resume_state, "reject")
    assert browser.done == [{"do": "look"}]
    artifact = next(e for e in resumed if getattr(e, "type", "") == "artifact").content or ""
    assert 'Click "Buy now" on shop.example — skipped (declined)' in artifact


def test_a_long_carry_on_record_keeps_its_newest_part() -> None:
    from openexecutive.workflows.action_step import _latest

    record = "Looked at the cart. " * 500 + "done: Order 1042 placed"
    kept = _latest(record, 2000)
    assert kept.endswith("done: Order 1042 placed") and len(kept) < 2100
    assert _latest("short", 2000) == "short"


def test_a_yes_to_a_new_target_is_not_the_tools_ok(browser: _Browser, audit: list[dict[str, Any]]) -> None:
    """Only a hold the tool asked for runs as approved; a new-target hold
    allowed the target, nothing more."""
    from openexecutive.workflows.action_step import run_held_calls
    from openexecutive.workflows.wait_for_human import HeldCall

    step = _defn().steps[0]
    report = asyncio.run(run_held_calls(
        step, [HeldCall(tool=BROWSE, arguments={"do": "look"}, targets=[("url", "https://shop.example/")])],
        workflow_name="buy_wf", run_id="run-1", approved=True, skip_reason="declined", policy=None,
    ))
    assert browser.approved == [False] and "done" in report


def test_a_call_that_failed_does_not_pause_the_step(
    monkeypatch: pytest.MonkeyPatch, browser: _Browser, audit: list[dict[str, Any]]
) -> None:
    provider = _ScriptedProvider([
        _resp(_use(BROWSE, {"do": "broken"}, "tu_1")),
        _resp(_text("The page crashed.")),
    ])
    _install(monkeypatch, provider)
    events = asyncio.run(_collect(DynamicWorkflow(_defn())))
    assert not any(isinstance(e, WaitForHumanEvent) for e in events)
    results = provider.calls[1]["messages"][-1]["content"]
    assert results[0]["is_error"] and "the page crashed" in results[0]["content"]


# --- wait: the run pauses until it is ready ---------------------------------------


def _waited(monkeypatch: pytest.MonkeyPatch, browser: _Browser, first: list[Any]) -> list[Any]:
    browser.connected = False
    _install(monkeypatch, _ScriptedProvider([*first, _resp(_text("The browser isn't connected."))]))
    return asyncio.run(_collect(DynamicWorkflow(_defn())))


def test_a_missing_device_pauses_the_run_until_it_is_ready(
    monkeypatch: pytest.MonkeyPatch, browser: _Browser, audit: list[dict[str, Any]]
) -> None:
    events = _waited(monkeypatch, browser, [_resp(_use(BROWSE, {"do": "look"}, "tu_1"))])
    pause = events[-1]
    assert isinstance(pause, WaitForHumanEvent)
    assert pause.waiting_for == "device:7" and pause.timeout_hours == WAITING_TIMEOUT_HOURS
    assert "is waiting for your browser" in pause.question
    assert json.loads(pause.model_dump_json())["waiting_for"] == "device:7"
    state = pause.resume_state
    assert state is not None and state.kind == "waiting" and state.waiting_what == "your browser"
    # It changed nothing, so it runs again from the start.
    assert "order" not in state.outputs

    browser.connected = True
    provider = _ScriptedProvider([_resp(_use(BROWSE, {"do": "look"}, "tu_1")), _resp(_text("Ordered."))])
    _install(monkeypatch, provider)
    resumed = _resume(state, "ready")
    assert "stopped part way" not in _user_turn(provider, 0)
    assert browser.done == [{"do": "look"}]
    artifact = next(e for e in resumed if getattr(e, "type", "") == "artifact").content or ""
    assert "Ordered." in artifact


def test_a_step_that_already_changed_something_carries_on_instead(
    monkeypatch: pytest.MonkeyPatch, browser: _Browser, audit: list[dict[str, Any]]
) -> None:
    events = _waited(monkeypatch, browser, [
        _resp(_use(APPEND, {"rows": [["x"]]}, "tu_1")),
        _resp(_use(BROWSE, {"do": "look"}, "tu_2")),
    ])
    state = events[-1].resume_state
    assert "order" in state.outputs
    browser.connected = True
    provider = _ScriptedProvider([_resp(_text("Ordered."))])
    _install(monkeypatch, provider)
    _resume(state, "ready")
    turn = _user_turn(provider, 0)
    assert "stopped part way" in turn and "Sheets: append rows — ok" in turn


def test_no_sign_of_it_in_time_stops_the_run(
    monkeypatch: pytest.MonkeyPatch, browser: _Browser, audit: list[dict[str, Any]]
) -> None:
    events = _waited(monkeypatch, browser, [_resp(_use(BROWSE, {"do": "look"}, "tu_1"))])
    _install(monkeypatch, _ScriptedProvider([]))
    resumed = _resume(events[-1].resume_state, "auto_proceed")
    assert resumed[-1].type == "error"
    assert "your browser wasn't available within 24 hours" in (resumed[-1].message or "")


def test_a_no_while_it_waits_stops_the_run(
    monkeypatch: pytest.MonkeyPatch, browser: _Browser, audit: list[dict[str, Any]]
) -> None:
    events = _waited(monkeypatch, browser, [_resp(_use(BROWSE, {"do": "look"}, "tu_1"))])
    _install(monkeypatch, _ScriptedProvider([]))  # the step does not run again
    resumed = _resume(events[-1].resume_state, "reject")
    errors = [e.message for e in resumed if getattr(e, "type", "") == "error"]
    assert errors == ["Stopped at 'Place the order': you stopped it while it waited for your browser."]


def test_no_one_to_tell_stops_the_run_and_says_why(
    monkeypatch: pytest.MonkeyPatch, browser: _Browser, audit: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(at, "approver_for", lambda name: None)
    events = _waited(monkeypatch, browser, [_resp(_use(BROWSE, {"do": "look"}, "tu_1"))])
    assert not any(isinstance(e, WaitForHumanEvent) for e in events)
    assert events[-1].type == "error" and "your browser isn't available" in (events[-1].message or "")


# --- the resumer picks them up ------------------------------------------------------


def test_resume_waiting_picks_up_only_the_runs_waiting_for_that_key(
    monkeypatch: pytest.MonkeyPatch, _isolated: Path
) -> None:
    kicked: list[str] = []
    monkeypatch.setattr(resumer, "_kick_resume", lambda run_id, db_path=None: kicked.append(run_id))
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **k: None)
    until = datetime.now(UTC) + timedelta(hours=1)
    for run_id, key in (("r-mine", "device:7"), ("r-other", "device:8"), ("r-gate", "")):
        wf_persistence.create_run(run_id, "buy_wf", "Weekly order", {})
        event = WaitForHumanEvent(person_id=7, question="q", waiting_for=key)
        wf_persistence.save_checkpoint(
            run_id=run_id, state_json=event.model_dump_json(), awaiting_person_id=7,
            awaiting_until=until, resume_state_json="{}",
        )
    listed = {r["run_id"]: r.get("waiting_for") for r in wf_persistence.list_runs()}
    assert listed == {"r-mine": "device:7", "r-other": "device:8", "r-gate": None}

    assert asyncio.run(resumer.resume_waiting("device:7")) == 1
    assert kicked == ["r-mine"]
    run = wf_persistence.get_run("r-mine")
    assert run is not None and run["status"] == "resolved"
    assert json.loads(run["resolution_json"])["parsed_decision"]["decision"] == "ready"
    assert wf_persistence.get_run("r-other")["status"] == "awaiting_human"  # type: ignore[index]
    # Again: nothing left waiting for it.
    assert asyncio.run(resumer.resume_waiting("device:7")) == 0
    assert asyncio.run(resumer.resume_waiting("")) == 0


# --- a scheduled run ----------------------------------------------------------------


def test_a_scheduled_run_checkpoints_a_waiting_pause(
    monkeypatch: pytest.MonkeyPatch, browser: _Browser, audit: list[dict[str, Any]]
) -> None:
    from openexecutive.scheduler import runner

    defn = dynamic_store.upsert_definition(_defn())
    browser.connected = False
    _install(monkeypatch, _ScriptedProvider(
        [_resp(_use(BROWSE, {"do": "look"}, "tu_1")), _resp(_text("Not connected."))]
    ))
    checkpoints: list[WaitForHumanEvent] = []

    async def _checkpoint(*, run_id: str, event: WaitForHumanEvent, workflow_title: str = "") -> None:
        checkpoints.append(event)

    completed: list[str] = []
    failed: list[str] = []
    monkeypatch.setattr("openexecutive.workflows.gate.checkpoint_gate", _checkpoint)
    monkeypatch.setattr(runner, "mark_action_done", lambda aid: None)
    monkeypatch.setattr(
        "openexecutive.workflows.dynamic_cadence.schedule_dynamic_workflow_cadence", lambda d, after=None: None
    )
    monkeypatch.setattr("openexecutive.workflows.persistence.complete_run", lambda rid, art: completed.append(rid))
    monkeypatch.setattr("openexecutive.workflows.persistence.fail_run", lambda rid, msg: failed.append(msg))
    monkeypatch.setattr("openexecutive.knowledge.store.ChromaDBStore", lambda **k: None)
    action = SimpleNamespace(id=3, channel_ref=defn.name, assigned_to_person_id=11)
    asyncio.run(runner._run_dynamic_workflow(action, now=datetime.now()))  # type: ignore[arg-type]
    assert len(checkpoints) == 1 and checkpoints[0].waiting_for == "device:7"
    state = checkpoints[0].resume_state
    assert state is not None and state.kind == "waiting" and state.deliver_to_person_id == 11
    assert completed == [] and failed == []
