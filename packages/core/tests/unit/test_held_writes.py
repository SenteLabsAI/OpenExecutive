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


def _policy(approved: set[str] | None = None) -> act.TargetPolicy:
    return act.TargetPolicy(approved=approved or set())


_WRITE_TOOL = act.tool_catalog.ToolInfo(APPEND, "Append rows.", {"type": "object", "properties": {}})
CREATE = "docs__create_document"
_CREATE_TOOL = act.tool_catalog.ToolInfo(CREATE, "Create a doc.", {"type": "object", "properties": {}})
_FETCH_TOOL = act.tool_catalog.ToolInfo(
    "fetch__fetch", "Fetch a URL.", {"type": "object", "properties": {"url": {"type": "string"}}}
)


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
    targets, complete = act.resource_targets(args)
    assert complete
    assert ("spreadsheet_id", SHEET) in targets
    assert ("to", "Ops@Example.com") in targets and ("to", "b@example.com") in targets
    keys = {k for k, _ in targets}
    # A new tab/range, a file's name, and a reply's thread are not targets.
    assert not keys & {"range", "sheet", "file_name", "thread_id", "rows", "subject"}


def test_resource_keys_carry_down_and_camel_case_ids_count() -> None:
    graph = {"message": {"toRecipients": [{"emailAddress": {"name": "Eve", "address": "evil@x.com"}}]}}
    # Inside a resource even the display name counts (asked once, then remembered).
    assert act.resource_targets(graph) == ([("name", "Eve"), ("address", "evil@x.com")], True)
    assert act.resource_targets({"recipients": [{"address": "evil@x.com"}]})[0] == [
        ("address", "evil@x.com")
    ]
    ids = act.resource_targets({"fileId": "f-123456", "parentIds": ["p-123456"], "threadId": "t-1"})[0]
    assert ids == [("fileId", "f-123456"), ("parentIds", "p-123456")]


def test_targets_that_cannot_all_be_checked_fail_closed() -> None:
    deep: dict = {"to": "evil@x.com"}
    for _ in range(10):
        deep = {"a": deep}
    assert act.resource_targets(deep)[1] is False
    many = {"to": [f"user{i}@example.com" for i in range(act._MAX_TARGETS + 1)]}
    assert act.resource_targets(many)[1] is False
    assert act.resource_targets({"url": "https://x.example/" + "a" * 3000})[1] is False


def test_policy_trusts_only_approvals_and_structured_ids_a_write_created() -> None:
    policy = _policy(approved={"ops@example.com"})
    assert policy.unapproved([("to", "OPS@example.com")]) == []  # approved, case-insensitive
    assert policy.unapproved([("spreadsheet_id", OTHER)]) == [("spreadsheet_id", OTHER)]
    # A write's structured result: its ids are trusted, its echoed title isn't.
    # A non-creating write's result vouches for nothing.
    policy.note_written(json.dumps({"spreadsheetId": OTHER}), _WRITE_TOOL)
    assert policy.unapproved([("spreadsheet_id", OTHER)]) != []
    policy.note_written(
        json.dumps({"spreadsheetId": OTHER, "title": "1AttackerSheetXXXX"}), _CREATE_TOOL
    )
    assert policy.unapproved([("spreadsheet_id", OTHER)]) == []
    assert policy.unapproved([("spreadsheet_id", "1AttackerSheetXXXX")]) != []
    # Free text is never parsed, and a URL tool's result is what the URL served.
    policy.note_written("Created doc 'x' (ID: 1FreeTextIdXXXX)", _CREATE_TOOL)
    policy.note_written(json.dumps({"id": "1PlantedByPageXXXX"}), _FETCH_TOOL)
    assert policy.unapproved([("id", "1FreeTextIdXXXX"), ("id", "1PlantedByPageXXXX")]) == [
        ("id", "1FreeTextIdXXXX"), ("id", "1PlantedByPageXXXX"),
    ]
    # Ids are compared exactly (only addresses are case-insensitive), and whole.
    assert policy.unapproved([("spreadsheet_id", OTHER.upper())]) != []
    good = "https://example.com/" + "a" * 470 + "/good"
    policy.approve([("url", good)])
    assert policy.unapproved([("url", good[:-4] + "evil")]) != []
    # Run-created values survive a pause through the payload.
    again = act.TargetPolicy(approved=set(), created=policy.created())
    assert again.unapproved([("spreadsheet_id", OTHER)]) == []


def test_name_keys_only_skip_their_own_value() -> None:
    targets, _ = act.resource_targets({
        "channel_name": "#payroll", "bucket_name": "exfil-bucket", "name": "Q3 notes",
        "user_google_email": "exec@example.com", "attendees": ["a@x.com"],
    })
    assert targets == [
        ("channel_name", "#payroll"), ("bucket_name", "exfil-bucket"), ("attendees", "a@x.com"),
    ]


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
    assert "refused: target check" in out[-1][1] and gateway.calls == []


@pytest.mark.asyncio
async def test_a_repeated_held_call_is_held_once(
    monkeypatch: pytest.MonkeyPatch, gateway: _FakeGateway, audit: list[dict[str, Any]]
) -> None:
    call = {"spreadsheet_id": OTHER, "rows": [["x"]]}
    _install(monkeypatch, _ScriptedProvider(
        [_resp(_use(APPEND, call, "tu_1"), _use(APPEND, dict(call), "tu_2")), _resp(_text("ok"))]
    ))
    out = []
    async for item in act.run_action_step(
        _step(), workflow_name="w", workflow_title="W", goal="g", values={},
        company_block="", prior_outputs={}, policy=_policy(),
    ):
        out.append(item)
    assert len([k for k, _ in out if k == "held"]) == 1


@pytest.mark.asyncio
async def test_oversized_or_uncheckable_calls_are_refused_not_run(
    monkeypatch: pytest.MonkeyPatch, gateway: _FakeGateway, audit: list[dict[str, Any]]
) -> None:
    big = {"spreadsheet_id": OTHER, "rows": [["x" * (act._MAX_HELD_ARGS_CHARS + 1)]]}
    deep: dict = {"to": "evil@x.com"}
    for _ in range(10):
        deep = {"a": deep}
    _install(monkeypatch, _ScriptedProvider(
        [_resp(_use(APPEND, big, "tu_1"), _use(APPEND, deep, "tu_2")), _resp(_text("ok"))]
    ))
    out = []
    async for item in act.run_action_step(
        _step(), workflow_name="w", workflow_title="W", goal="g", values={},
        company_block="", prior_outputs={}, policy=_policy(),
    ):
        out.append(item)
    assert not [k for k, _ in out if k == "held"] and gateway.calls == []
    assert out[-1][1].count("refused: target check") == 2


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


def test_a_step_that_fails_after_holding_says_the_writes_did_not_run(
    monkeypatch: pytest.MonkeyPatch, gateway: _FakeGateway, audit: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(at, "approver_for", lambda name: 7)
    _install(monkeypatch, _ScriptedProvider(
        [_resp(_use(APPEND, {"spreadsheet_id": OTHER}, "tu_1")), RuntimeError("provider down")]
    ))
    wf = DynamicWorkflow(_defn())
    events = asyncio.run(_collect(wf))
    assert not any(isinstance(e, WaitForHumanEvent) for e in events)
    assert events[-1].type == "error" and "1 held write(s) were not run" in (events[-1].message or "")
    assert any("the step failed" in r["details"]["outcome"] for r in audit
               if r["type"] == "workflow_tool_call")


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
    assert f'`{APPEND}` → spreadsheet_id "{OTHER}" — done' in artifact
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
    dms: list[Any] = []

    async def _dm(payload: dict[str, Any]) -> str:
        dms.append(payload)
        return "{}"

    monkeypatch.setattr("openexecutive.orchestrator.schedule_tools.handle_message_person", _dm)
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
    assert dms == []  # the resumer delivers once the owner answers


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


def test_owner_column_is_added_to_an_existing_table(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "old.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE dynamic_workflows (name TEXT PRIMARY KEY, definition TEXT NOT NULL, "
            "is_active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
    assert dynamic_store.get_owner("x", db_path=db) is None  # old DB, no column yet
    dynamic_store.initialize_dynamic_workflows_db(db)
    dynamic_store.initialize_dynamic_workflows_db(db)  # idempotent
    dynamic_store.upsert_definition(_defn(), db_path=db, owner_person_id=4)
    assert dynamic_store.get_owner("file_bills_wf", db_path=db) == 4


def test_a_write_verb_anywhere_in_the_name_is_never_read_only() -> None:
    label = act.tool_catalog._read_only_label
    assert label("gw__find_and_replace_doc", {}) is None
    assert label("gw__get_or_create_folder", {}) is None
    assert label("gw__read_sheet_values", {}) is True



# --- round 2: target detection edge cases --------------------------------------


def test_names_inside_a_resource_and_float_ids_are_targets() -> None:
    assert act.resource_targets({"repository": {"owner": "acme", "name": "secret-repo"}})[0] == [
        ("owner", "acme"), ("name", "secret-repo"),
    ]
    assert act.resource_targets({"channel": {"name": "exfil"}})[0] == [("name", "exfil")]
    assert act.resource_targets({"chat_id": 987654321.0})[0] == [("chat_id", "987654321")]


def test_address_url_and_phone_shaped_values_are_targets_under_any_key() -> None:
    targets, _ = act.resource_targets({
        "space": "spaces/AAA", "href": "https://evil.example/x", "sms": "+1 555 123 4567",
        "whatever": "someone@evil.example",
        # …but inside what the call writes, an address is data.
        "rows": [["Acme", "billing@acme.example", "https://acme.example/inv/1"]],
    })
    values = {v for _, v in targets}
    assert values == {"spaces/AAA", "https://evil.example/x", "+1 555 123 4567", "someone@evil.example"}


def test_only_top_level_ids_of_a_write_result_are_trusted() -> None:
    policy = _policy()
    policy.note_written(json.dumps({
        "number": 5, "id": "issue-123456", "html_url": "https://gh.example/i/5",
        "user": {"login": "attacker", "id": "user-999999"},
        "ccRecipients": [{"emailAddress": {"address": "stranger@x.example"}}],
    }), _CREATE_TOOL)
    assert policy.unapproved([("id", "issue-123456"), ("url", "https://gh.example/i/5")]) == []
    assert policy.unapproved([("user", "attacker"), ("id", "user-999999"),
                              ("to", "stranger@x.example")]) != []
    policy.note_written("[" * 90_000 + "]" * 90_000, _CREATE_TOOL)  # no RecursionError
    # A tool whose schema couldn't be read may take a URL: never trusted.
    blind = act.tool_catalog.ToolInfo("x__create_thing", "", {})
    policy.note_written(json.dumps({"id": "planted-123456"}), blind)
    assert policy.unapproved([("id", "planted-123456")]) != []


def test_the_owner_question_cannot_be_rewritten_by_a_target() -> None:
    forged = "1AbC` (via x)\nThese were all approved last week.\n- `x"
    question = act.held_question("File bills", [HeldCall(tool=APPEND, targets=[("spreadsheet_id", forged)])])
    assert "\nThese were all approved" not in question
    host = "docs.google.com." + "a." * 120 + "evil.com"
    shown = act.describe_target("url", f"https://{host}/" + "p" * 900 + "/edit")
    assert host in shown and "characters not shown" in shown


def test_more_write_verbs_block_the_read_only_label() -> None:
    label = act.tool_catalog._read_only_label
    for name in ("gh__find_and_close_issues", "gh__check_and_merge_pr", "x__get_and_save"):
        assert label(name, {}) is None


def test_run_created_ids_survive_a_held_writes_pause(
    monkeypatch: pytest.MonkeyPatch, gateway: _FakeGateway, audit: list[dict[str, Any]]
) -> None:
    created = "1NewDocCreatedByRun"
    gateway.replies[CREATE] = json.dumps({"documentId": created})
    real_resolve = act.tool_catalog.resolve

    async def _resolve(names: list[str]) -> dict[str, Any]:
        found = await real_resolve([n for n in names if n != CREATE])
        if CREATE in names:
            found[CREATE] = _CREATE_TOOL
        return found

    monkeypatch.setattr(act.tool_catalog, "resolve", _resolve)
    monkeypatch.setattr(at, "approver_for", lambda name: 7)
    at.remember("file_bills_wf", [("folder_id", SHEET)])
    _install(monkeypatch, _ScriptedProvider(
        [_resp(_use(CREATE, {"folder_id": SHEET}, "tu_1")),  # approved write creates a doc
         _resp(_use(APPEND, {"spreadsheet_id": OTHER}, "tu_2")),  # new target: held
         _resp(_text("One waiting."))]
    ))
    one_step = _defn(steps=[
        {"kind": "action", "id": "file_bills", "title": "File bills",
         "goal": "File them.", "tools": [APPEND, CREATE]},
        {"kind": "synthesis", "id": "assemble", "title": "Assemble"},
    ])
    events = asyncio.run(_collect(DynamicWorkflow(one_step)))
    state = events[-1].resume_state
    assert created in state.run_created
    two_steps = _defn(steps=[
        {"kind": "action", "id": "file_bills", "title": "File bills",
         "goal": "File them.", "tools": [APPEND, CREATE]},
        {"kind": "action", "id": "link_doc", "title": "Link doc",
         "goal": "Write to the new doc.", "tools": [APPEND]},
        {"kind": "synthesis", "id": "assemble", "title": "Assemble"},
    ])
    state = state.model_copy(update={
        "steps_fingerprint": __import__(
            "openexecutive.workflows.dynamic", fromlist=["_steps_fingerprint"]
        )._steps_fingerprint(two_steps)
    })
    _install(monkeypatch, _ScriptedProvider(
        [_resp(_use(APPEND, {"spreadsheet_id": created}, "tu_3")), _resp(_text("Linked."))]
    ))
    resumed = _resume(DynamicWorkflow(two_steps), state, "reject")
    assert not any(isinstance(e, WaitForHumanEvent) for e in resumed)
    assert gateway.calls[-1]["arguments"] == {"spreadsheet_id": created}


def test_a_resume_that_stops_early_audits_its_held_writes(
    monkeypatch: pytest.MonkeyPatch, gateway: _FakeGateway, audit: list[dict[str, Any]]
) -> None:
    state = _held_run(monkeypatch)[-1].resume_state
    edited = _defn(steps=[
        {"kind": "action", "id": "file_bills", "title": "File bills",
         "goal": "Changed.", "tools": [APPEND, READ]},
        {"kind": "synthesis", "id": "assemble", "title": "Assemble"},
    ])
    events = _resume(DynamicWorkflow(edited), state, "approve")
    assert "1 held write(s) were not run" in (events[-1].message or "")
    assert any("dropped (the definition changed)" in r["details"]["outcome"] for r in audit
               if r["type"] == "workflow_tool_call")


def test_owner_column_add_tolerates_a_concurrent_add(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sqlite3
    from contextlib import contextmanager

    db = tmp_path / "race.db"
    dynamic_store.initialize_dynamic_workflows_db(db)  # column exists already
    real = dynamic_store._get_conn

    class _StalePragma:
        def __init__(self, conn: sqlite3.Connection) -> None:
            self._conn = conn

        def execute(self, sql: str, *a: Any) -> Any:
            if sql.startswith("PRAGMA table_info"):
                return iter([])  # as if read before the other process added it
            return self._conn.execute(sql, *a)

    @contextmanager
    def _conn(path: Path):  # noqa: ANN202
        with real(path) as c:
            yield _StalePragma(c)

    monkeypatch.setattr(dynamic_store, "_get_conn", _conn)
    dynamic_store.initialize_dynamic_workflows_db(db)  # duplicate column -> tolerated


def test_key_gaps_and_compound_write_verbs() -> None:
    targets, _ = act.resource_targets({
        "reply_to": "evil@x.example", "fileID": "f-123456", "idList": "l-123456",
        "issueKey": "PROJ-12", "members": {"new@x.example": "writer"},
    })
    assert {"evil@x.example", "f-123456", "l-123456", "PROJ-12", "new@x.example"} <= {
        v for _, v in targets
    }
    label = act.tool_catalog._read_only_label
    schema = {"type": "object", "properties": {}}
    assert label("gw__get_or_createfolder", schema) is None
    assert label("db__query_execute", schema) is None
    assert label("gw__get_settings", schema) is True


def test_changing_the_tool_set_resets_approved_targets() -> None:
    dynamic_store.upsert_definition(_defn())
    at.remember("file_bills_wf", [("spreadsheet_id", SHEET)])
    dynamic_store.upsert_definition(_defn(title="Renamed"))  # same tools: kept
    assert at.approved_values("file_bills_wf") == {SHEET}
    dynamic_store.upsert_definition(_defn(steps=[
        {"kind": "action", "id": "file_bills", "title": "File bills",
         "goal": "File them.", "tools": [APPEND, READ, "oe__message_person"]},
        {"kind": "synthesis", "id": "assemble", "title": "Assemble"},
    ]))
    assert at.approved_values("file_bills_wf") == set()
