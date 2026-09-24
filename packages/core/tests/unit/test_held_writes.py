"""First write to a new target: held, asked, then run exactly as given (or skipped).

Covers the target policy in action steps, the held-writes pause and resume in
the engine, remembered targets, workflow owners (who gets asked), the web
decision endpoint, and scheduled runs pausing for held writes.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from openexecutive.memory import episodic
from openexecutive.people import store as people_store
from openexecutive.workflows import action_step as act
from openexecutive.workflows import approved_targets as at
from openexecutive.workflows import dynamic_store
from openexecutive.workflows import persistence as wf_persistence
from openexecutive.workflows.dynamic import DynamicWorkflow
from openexecutive.workflows.dynamic_models import DynamicWorkflowDef
from openexecutive.workflows.wait_for_human import (
    HeldCall,
    WaitForHumanEvent,
    WaitForHumanResolution,
    WorkflowResumeState,
)

from . import test_action_step as _action_step_tests
from .test_action_step import (
    APPEND,
    READ,
    _FakeGateway,
    _install,
    _resp,
    _run,
    _ScriptedProvider,
    _step,
    _text,
    _use,
)

# Reuse the action-step tests' fixtures (a fake gateway + resolver, and a
# captured audit log) under the same names.
gateway = _action_step_tests.gateway
audit = _action_step_tests.audit

SHEET = "1AbCdEfGhIjK_tracker"
OTHER = "9ZyXwVuTsRqP_payroll"


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
    monkeypatch.delenv("BACKEND_SHARED_SECRET", raising=False)
    return db


def _defn(**overrides: Any) -> DynamicWorkflowDef:
    base: dict[str, Any] = {
        "name": "file_bills_wf",
        "title": "File bills",
        "steps": [
            {"kind": "action", "id": "file_bills", "title": "File bills",
             "goal": "File them.", "tools": [APPEND, READ]},
            {"kind": "synthesis", "id": "assemble", "title": "Assemble"},
        ],
    }
    base.update(overrides)
    return DynamicWorkflowDef.model_validate(base)


def _policy(approved: set[str] | None = None, text: str = "") -> act.TargetPolicy:
    return act.TargetPolicy(approved=approved or set(), definition_text=text)


# --- resource_targets --------------------------------------------------------


def test_resource_targets_walk_nested_objects_and_lists() -> None:
    args = {
        "spreadsheet_id": SHEET,
        "range": "Sheet1!A1",
        "sheet": "March",
        "rows": [["Acme", "10"]],
        "message": {"to": ["Ops@Example.com", "b@example.com"], "subject": "hi"},
        "file_name": "bills.csv",
        "thread_id": "t-123456789",
    }
    targets = act.resource_targets(args)
    assert ("spreadsheet_id", SHEET) in targets
    assert ("to", "Ops@Example.com") in targets and ("to", "b@example.com") in targets
    keys = {k for k, _ in targets}
    # A new tab/range, a file's name, and a reply's thread are not targets.
    assert not keys & {"range", "sheet", "file_name", "thread_id", "rows", "subject"}


def test_policy_trusts_approved_definition_and_run_created_values() -> None:
    policy = _policy(approved={"ops@example.com"}, text=f'"goal": "append to {SHEET}"')
    assert policy.unapproved([("to", "OPS@example.com")]) == []  # approved, case-insensitive
    assert policy.unapproved([("spreadsheet_id", SHEET)]) == []  # named in the definition
    assert policy.unapproved([("spreadsheet_id", OTHER)]) == [("spreadsheet_id", OTHER)]
    policy.note_written(f'{{"spreadsheetId": "{OTHER}"}}')  # the run created it
    assert policy.unapproved([("spreadsheet_id", OTHER)]) == []
    # Short values can't be trusted just by appearing inside a bigger text.
    assert policy.unapproved([("id", "12")]) == [("id", "12")]


# --- holding in the action step ------------------------------------------------


@pytest.mark.asyncio
async def test_new_target_is_held_not_run(
    monkeypatch: pytest.MonkeyPatch, gateway: _FakeGateway, audit: list[dict[str, Any]]
) -> None:
    provider = _ScriptedProvider(
        [
            _resp(_use(APPEND, {"spreadsheet_id": OTHER, "rows": [["x"]]}, "tu_1")),
            _resp(_use(APPEND, {"spreadsheet_id": SHEET, "rows": [["y"]]}, "tu_2")),
            _resp(_text("Filed one; one is waiting.")),
        ]
    )
    _install(monkeypatch, provider)
    out = []
    async for item in act.run_action_step(
        _step(max_tool_calls=1),
        workflow_name="file_bills_wf", workflow_title="File bills", goal="File them.",
        values={}, company_block="", prior_outputs={},
        policy=_policy(approved={SHEET}),
    ):
        out.append(item)
    held = [p for k, p in out if k == "held"]
    assert len(held) == 1 and held[0].targets == [("spreadsheet_id", OTHER)]
    assert held[0].arguments == {"spreadsheet_id": OTHER, "rows": [["x"]]}
    # Only the approved write reached the gateway — and the held one didn't
    # spend the budget of 1, or the second call would have been refused.
    assert [c["arguments"]["spreadsheet_id"] for c in gateway.calls] == [SHEET]
    tool_result = provider.calls[1]["messages"][-1]["content"][0]
    assert json.loads(tool_result["content"])["status"] == "held"
    assert tool_result["is_error"] is False
    assert any(r["details"]["outcome"] == "held for approval" for r in audit
               if r["type"] == "workflow_tool_call")
    assert out[-1][0] == "output" and "held for approval" in out[-1][1]


@pytest.mark.asyncio
async def test_read_only_calls_and_no_policy_never_hold(
    monkeypatch: pytest.MonkeyPatch, gateway: _FakeGateway, audit: list[dict[str, Any]]
) -> None:
    provider = _ScriptedProvider(
        [
            _resp(_use(READ, {"spreadsheet_id": OTHER}, "tu_1")),
            _resp(_text("Read it.")),
        ]
    )
    _install(monkeypatch, provider)
    out = []
    async for item in act.run_action_step(
        _step(), workflow_name="w", workflow_title="W", goal="g", values={},
        company_block="", prior_outputs={}, policy=_policy(),
    ):
        out.append(item)
    assert not [k for k, _ in out if k == "held"] and len(gateway.calls) == 1

    _install(monkeypatch, _ScriptedProvider(
        [_resp(_use(APPEND, {"spreadsheet_id": OTHER}, "tu_1")), _resp(_text("ok"))]
    ))
    out = await _run(_step())  # no policy: nothing is held
    assert not [k for k, _ in out if k == "held"] and len(gateway.calls) == 2


@pytest.mark.asyncio
async def test_hold_limit_refuses_further_new_targets(
    monkeypatch: pytest.MonkeyPatch, gateway: _FakeGateway, audit: list[dict[str, Any]]
) -> None:
    n = act.MAX_HELD_PER_STEP + 1
    provider = _ScriptedProvider(
        [_resp(*[_use(APPEND, {"spreadsheet_id": f"sheet-{i:08d}"}, f"tu_{i}") for i in range(n)]),
         _resp(_text("done"))]
    )
    _install(monkeypatch, provider)
    out = []
    async for item in act.run_action_step(
        _step(max_tool_calls=20), workflow_name="w", workflow_title="W", goal="g",
        values={}, company_block="", prior_outputs={}, policy=_policy(),
    ):
        out.append(item)
    assert len([k for k, _ in out if k == "held"]) == act.MAX_HELD_PER_STEP
    assert "refused: hold limit" in out[-1][1] and gateway.calls == []


# --- engine: pause and resume --------------------------------------------------


def _held_run(monkeypatch: pytest.MonkeyPatch, approver: int | None = 7) -> list[Any]:
    monkeypatch.setattr(at, "approver_for", lambda name: approver)
    _install(monkeypatch, _ScriptedProvider(
        [_resp(_use(APPEND, {"spreadsheet_id": OTHER, "rows": [["x"]]}, "tu_1")),
         _resp(_text("One write is waiting."))]
    ))
    wf = DynamicWorkflow(_defn())

    async def collect() -> list[Any]:
        return [e async for e in wf.run(wf.input_model()(), store=None)]  # type: ignore[arg-type]

    return asyncio.run(collect())


def test_run_pauses_after_the_step_and_asks_the_owner(
    monkeypatch: pytest.MonkeyPatch, gateway: _FakeGateway, audit: list[dict[str, Any]]
) -> None:
    events = _held_run(monkeypatch, approver=7)
    pause = events[-1]
    assert isinstance(pause, WaitForHumanEvent)
    assert pause.person_id == 7 and pause.on_timeout == "auto_proceed"
    assert OTHER in pause.question and APPEND in pause.question
    state = pause.resume_state
    assert state is not None and state.kind == "held_writes"
    assert state.gate_step_id == "file_bills" and state.gate_step_index == 0
    assert state.held[0].arguments["spreadsheet_id"] == OTHER
    assert "file_bills" in state.outputs  # the step itself finished
    assert gateway.calls == []


def test_no_one_to_ask_skips_the_held_writes_and_finishes(
    monkeypatch: pytest.MonkeyPatch, gateway: _FakeGateway, audit: list[dict[str, Any]]
) -> None:
    events = _held_run(monkeypatch, approver=None)
    assert not any(isinstance(e, WaitForHumanEvent) for e in events)
    artifact = next(e for e in events if getattr(e, "type", "") == "artifact").content or ""
    assert "skipped (no one to approve it)" in artifact and gateway.calls == []


def _resume(wf: DynamicWorkflow, state: WorkflowResumeState, decision: str) -> list[Any]:
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


def test_approve_runs_exactly_the_held_call_and_remembers_it(
    monkeypatch: pytest.MonkeyPatch, gateway: _FakeGateway, audit: list[dict[str, Any]]
) -> None:
    state = _held_run(monkeypatch)[-1].resume_state
    events = _resume(DynamicWorkflow(_defn()), state, "approve")
    assert gateway.calls == [
        {"name": APPEND, "arguments": {"spreadsheet_id": OTHER, "rows": [["x"]]}}
    ]
    artifact = next(e for e in events if e.type == "artifact").content or ""
    assert f"`{APPEND}` → spreadsheet_id `{OTHER}` — done" in artifact
    assert OTHER in at.approved_values("file_bills_wf")
    # The next run writes there without asking.
    _install(monkeypatch, _ScriptedProvider(
        [_resp(_use(APPEND, {"spreadsheet_id": OTHER}, "tu_1")), _resp(_text("Filed."))]
    ))
    wf = DynamicWorkflow(_defn())
    again = asyncio.run(_collect(wf))
    assert not any(isinstance(e, WaitForHumanEvent) for e in again)
    assert len(gateway.calls) == 2


async def _collect(wf: DynamicWorkflow) -> list[Any]:
    return [e async for e in wf.run(wf.input_model()(), store=None)]  # type: ignore[arg-type]


@pytest.mark.parametrize(("decision", "reason"), [
    ("reject", "declined"), ("auto_proceed", "no answer in time"), ("defer", "not approved"),
])
def test_anything_but_approve_skips_the_held_call_and_continues(
    monkeypatch: pytest.MonkeyPatch, gateway: _FakeGateway, audit: list[dict[str, Any]],
    decision: str, reason: str,
) -> None:
    state = _held_run(monkeypatch)[-1].resume_state
    events = _resume(DynamicWorkflow(_defn()), state, decision)
    artifact = next(e for e in events if e.type == "artifact").content or ""
    assert f"skipped ({reason})" in artifact
    assert gateway.calls == [] and at.approved_values("file_bills_wf") == set()


def test_resume_refuses_when_the_steps_changed(
    monkeypatch: pytest.MonkeyPatch, gateway: _FakeGateway, audit: list[dict[str, Any]]
) -> None:
    state = _held_run(monkeypatch)[-1].resume_state
    edited = _defn(steps=[
        {"kind": "action", "id": "file_bills", "title": "File bills",
         "goal": "File them somewhere else.", "tools": [APPEND, READ]},
        {"kind": "synthesis", "id": "assemble", "title": "Assemble"},
    ])
    events = _resume(DynamicWorkflow(edited), state, "approve")
    assert events[-1].type == "error" and "changed" in (events[-1].message or "")
    assert gateway.calls == []


def test_approved_call_is_skipped_if_its_tool_no_longer_resolves(
    monkeypatch: pytest.MonkeyPatch, gateway: _FakeGateway, audit: list[dict[str, Any]]
) -> None:
    state = _held_run(monkeypatch)[-1].resume_state

    async def _nothing(names: list[str]) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(act.tool_catalog, "resolve", _nothing)
    monkeypatch.setattr(
        "openexecutive.workflows.dynamic.unavailable_step_tools", _async_empty
    )
    events = _resume(DynamicWorkflow(_defn()), state, "approve")
    artifact = next(e for e in events if e.type == "artifact").content or ""
    assert "skipped (the tool is no longer available to this step)" in artifact
    assert gateway.calls == []


async def _async_empty(*_a: Any, **_k: Any) -> list[str]:
    return []


def test_old_gate_payloads_still_parse_as_gates() -> None:
    state = WorkflowResumeState.model_validate_json(json.dumps({
        "workflow_name": "w", "gate_step_id": "g", "gate_step_index": 1,
        "steps_fingerprint": "x", "outputs": {},
    }))
    assert state.kind == "gate" and state.held == [] and state.deliver_to_person_id is None


# --- remembered targets and owners -------------------------------------------


def test_approved_targets_store_round_trip() -> None:
    at.remember("wf", [("to", " Ops@Example.com "), ("spreadsheet_id", SHEET)], run_id="r1")
    at.remember("wf", [("to", "ops@example.com")])  # re-approval is a no-op
    assert at.approved_values("wf") == {"ops@example.com", SHEET}
    assert {t["value"] for t in at.list_targets("wf")} == {"ops@example.com", SHEET}
    assert at.forget("wf", "OPS@example.com") is True
    assert at.approved_values("wf") == {SHEET}
    assert at.approved_values("other") == set()


def test_deleting_a_workflow_forgets_its_targets() -> None:
    dynamic_store.upsert_definition(_defn())
    at.remember("file_bills_wf", [("spreadsheet_id", SHEET)])
    assert dynamic_store.delete_definition("file_bills_wf")
    assert at.approved_values("file_bills_wf") == set()


def test_owner_is_set_on_insert_only_and_approver_falls_back() -> None:
    owner = people_store.upsert_person(full_name="Dana Ops")
    principal = people_store.upsert_person(full_name="Pat Principal", is_principal=True)
    dynamic_store.upsert_definition(_defn(), owner_person_id=owner)
    # Later writes (edit, overwrite, activate) never change it.
    dynamic_store.upsert_definition(_defn(title="Renamed"), owner_person_id=principal)
    dynamic_store.set_active("file_bills_wf", False)
    assert dynamic_store.get_owner("file_bills_wf") == owner
    assert at.approver_for("file_bills_wf") == owner
    people_store.archive_person(owner)
    assert at.approver_for("file_bills_wf") == principal
    dynamic_store.upsert_definition(_defn(name="unowned_wf"))
    assert at.approver_for("unowned_wf") == principal


# --- HTTP: owner on create, decisions, targets --------------------------------


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from openexecutive.api.main import create_app

    with TestClient(create_app()) as c:
        yield c


def _caller(monkeypatch: pytest.MonkeyPatch, person_id: int | None) -> None:
    monkeypatch.setattr(
        "openexecutive.api.routes.chat._resolve_caller_person_id", lambda _r: person_id
    )


def test_create_records_the_caller_and_ignores_a_client_owner(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    _caller(monkeypatch, 42)
    body = {**_defn().model_dump(), "owner_person_id": 99, "steps": [
        {"kind": "action", "id": "file_bills", "title": "File bills",
         "goal": "File them.", "tools": ["oe__read_file"]},
        {"kind": "synthesis", "id": "assemble", "title": "Assemble"},
    ]}
    r = client.post("/workflows/custom", json=body)
    assert r.status_code == 201, r.text
    assert r.json()["owner_person_id"] == 42
    _caller(monkeypatch, 7)
    assert client.put("/workflows/custom/file_bills_wf", json=body).status_code == 200
    assert client.get("/workflows/custom/file_bills_wf").json()["owner_person_id"] == 42


def _awaiting(run_id: str, person_id: int, shape: str = "approve_reject") -> None:
    wf_persistence.create_run(run_id, "file_bills_wf", "Run", {})
    state = json.dumps({"question": "OK?", "expected_reply_shape": shape})
    from datetime import UTC, datetime, timedelta

    wf_persistence.save_checkpoint(run_id, state, person_id, datetime.now(UTC) + timedelta(hours=1))


def test_decision_endpoint_authorisation_and_states(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    asker = people_store.upsert_person(full_name="Dana Ops")
    other = people_store.upsert_person(full_name="Sam Else")
    principal = people_store.upsert_person(full_name="Pat Principal", is_principal=True)
    kicked: list[str] = []
    monkeypatch.setattr("openexecutive.workflows.resumer._kick_resume", lambda rid: kicked.append(rid))

    _awaiting("run-a", asker)
    _caller(monkeypatch, other)
    assert client.post("/workflows/runs/run-a/decision", json={"decision": "approve"}).status_code == 403
    _caller(monkeypatch, None)
    assert client.post("/workflows/runs/run-a/decision", json={"decision": "approve"}).status_code == 403
    _caller(monkeypatch, asker)
    assert client.post("/workflows/runs/run-a/decision", json={"decision": "maybe"}).status_code == 422
    r = client.post("/workflows/runs/run-a/decision", json={"decision": "approve"})
    assert r.status_code == 200, r.text
    run = wf_persistence.get_run("run-a")
    assert run is not None and run["status"] == "resolved"
    assert json.loads(run["resolution_json"])["parsed_decision"]["decision"] == "approve"
    # Answered already.
    assert client.post("/workflows/runs/run-a/decision", json={"decision": "reject"}).status_code == 409

    _awaiting("run-b", asker)
    _caller(monkeypatch, principal)  # the principal can always answer
    assert client.post("/workflows/runs/run-b/decision", json={"decision": "reject"}).status_code == 200

    _awaiting("run-c", asker, shape="free_text")
    _caller(monkeypatch, asker)
    assert client.post("/workflows/runs/run-c/decision", json={"decision": "approve"}).status_code == 409
    assert client.post("/workflows/runs/nope/decision", json={"decision": "approve"}).status_code == 404


def test_targets_routes_list_and_forget(client: TestClient) -> None:
    dynamic_store.upsert_definition(_defn())
    at.remember("file_bills_wf", [("spreadsheet_id", SHEET)])
    listed = client.get("/workflows/custom/file_bills_wf/targets").json()["targets"]
    assert [t["value"] for t in listed] == [SHEET]
    assert client.delete(f"/workflows/custom/file_bills_wf/targets?value={SHEET}").status_code == 200
    assert client.delete(f"/workflows/custom/file_bills_wf/targets?value={SHEET}").status_code == 404
    assert client.get("/workflows/custom/nope/targets").status_code == 404


# --- chat save records the chat caller ------------------------------------------


def test_chat_save_records_the_session_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.orchestrator import workflow_authoring_tools as wat
    from openexecutive.orchestrator.schedule_tools import current_session

    definition = {
        "name": "weekly_watch", "title": "Weekly Watch",
        "steps": [
            {"kind": "specialist", "id": "research", "title": "R", "specialist": "cso",
             "goal": "Analyze."},
            {"kind": "synthesis", "id": "assemble", "title": "A"},
        ],
    }
    token = current_session.set(SimpleNamespace(caller_person_id=5))
    try:
        out = json.loads(asyncio.run(wat.handle_save_workflow(
            {"definition": definition, "confirm_token": wat._canonical_token(definition)}
        )))
    finally:
        current_session.reset(token)
    assert out.get("status") == "saved", out
    assert dynamic_store.get_owner("weekly_watch") == 5


# --- scheduled runs -------------------------------------------------------------


def test_scheduled_run_pauses_for_held_writes_and_rechains(
    monkeypatch: pytest.MonkeyPatch, gateway: _FakeGateway, audit: list[dict[str, Any]]
) -> None:
    from openexecutive.scheduler import runner

    defn = dynamic_store.upsert_definition(_defn())
    monkeypatch.setattr(at, "approver_for", lambda name: 7)
    _install(monkeypatch, _ScriptedProvider(
        [_resp(_use(APPEND, {"spreadsheet_id": OTHER}, "tu_1")), _resp(_text("Waiting."))]
    ))
    checkpoints: list[WaitForHumanEvent] = []

    async def _checkpoint(*, run_id: str, event: WaitForHumanEvent, workflow_title: str = "") -> None:
        checkpoints.append(event)

    done: list[int] = []
    chained: list[str] = []
    completed: list[str] = []
    monkeypatch.setattr("openexecutive.workflows.gate.checkpoint_gate", _checkpoint)
    monkeypatch.setattr(runner, "mark_action_done", lambda aid: done.append(aid))
    monkeypatch.setattr(
        "openexecutive.workflows.dynamic_cadence.schedule_dynamic_workflow_cadence",
        lambda d, after=None: chained.append(d.name),
    )
    monkeypatch.setattr(
        "openexecutive.workflows.persistence.complete_run", lambda rid, art: completed.append(rid)
    )
    monkeypatch.setattr("openexecutive.knowledge.store.ChromaDBStore", lambda **k: None)
    action = SimpleNamespace(id=3, channel_ref=defn.name, assigned_to_person_id=11)
    asyncio.run(runner._run_dynamic_workflow(action, now=__import__("datetime").datetime.now()))  # type: ignore[arg-type]
    assert len(checkpoints) == 1
    state = checkpoints[0].resume_state
    assert state is not None and state.kind == "held_writes" and state.deliver_to_person_id == 11
    assert completed == [] and done == [3] and chained == [defn.name]


def test_resumer_delivers_a_scheduled_runs_artifact(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.workflows import resumer

    sent: list[dict[str, Any]] = []

    async def _send(payload: dict[str, Any]) -> str:
        sent.append(payload)
        return "{}"

    monkeypatch.setattr("openexecutive.orchestrator.schedule_tools.handle_message_person", _send)
    asyncio.run(resumer._deliver_artifact("run-1", 11, "# Report"))
    assert sent == [{"person_id": 11, "text": "# Report"}]


def test_held_call_model_round_trips_through_resume_json() -> None:
    state = WorkflowResumeState(
        workflow_name="w", gate_step_id="s", gate_step_index=0, steps_fingerprint="f",
        kind="held_writes", deliver_to_person_id=3,
        held=[HeldCall(tool=APPEND, arguments={"spreadsheet_id": OTHER}, targets=[("spreadsheet_id", OTHER)])],
    )
    back = WorkflowResumeState.model_validate_json(state.model_dump_json())
    assert back == state
