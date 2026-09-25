"""Unit tests for the workflows API surface and persistence layer.

These tests cover everything except the actual specialist-driven workflow
execution (which requires a live Anthropic API key — those belong in
integration tests).
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

# Set a dummy key BEFORE importing app modules — the settings loader requires it.
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-used")

from fastapi.testclient import TestClient  # noqa: E402

from openexecutive.api.main import create_app  # noqa: E402
from openexecutive.workflows import WORKFLOW_REGISTRY, list_workflows  # noqa: E402
from openexecutive.workflows.persistence import (  # noqa: E402
    complete_run,
    create_run,
    delete_run,
    fail_run,
    get_run,
    initialize_runs_db,
    list_artifact_runs,
    list_runs,
)

# -----------------------------------------------------------------------------
# Persistence
# -----------------------------------------------------------------------------


@pytest.fixture
def temp_db() -> Path:
    fd, path_str = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    path = Path(path_str)
    initialize_runs_db(path)
    try:
        yield path
    finally:
        if path.exists():
            path.unlink()


def test_create_and_get_run(temp_db: Path) -> None:
    create_run("r-1", "board_prep", "My run", {"quarter_label": "Q2 2026"}, db_path=temp_db)
    run = get_run("r-1", db_path=temp_db)
    assert run is not None
    assert run["workflow_name"] == "board_prep"
    assert run["status"] == "running"
    assert run["title"] == "My run"
    assert run["inputs"] == {"quarter_label": "Q2 2026"}
    assert run["artifact"] is None


def test_get_missing_run_returns_none(temp_db: Path) -> None:
    assert get_run("does-not-exist", db_path=temp_db) is None


def test_complete_run_sets_artifact(temp_db: Path) -> None:
    create_run("r-2", "board_prep", "Run 2", {}, db_path=temp_db)
    complete_run("r-2", "# Board Deck\n\n...", db_path=temp_db)
    run = get_run("r-2", db_path=temp_db)
    assert run["status"] == "done"
    assert run["artifact"] == "# Board Deck\n\n..."
    assert run["error"] is None


def test_fail_run_records_error(temp_db: Path) -> None:
    create_run("r-3", "board_prep", "Run 3", {}, db_path=temp_db)
    fail_run("r-3", "boom", db_path=temp_db)
    run = get_run("r-3", db_path=temp_db)
    assert run["status"] == "error"
    assert run["error"] == "boom"


def test_list_runs_filters_by_workflow(temp_db: Path) -> None:
    create_run("a", "board_prep", "A", {}, db_path=temp_db)
    create_run("b", "board_prep", "B", {}, db_path=temp_db)
    create_run("c", "other", "C", {}, db_path=temp_db)
    assert {r["run_id"] for r in list_runs(db_path=temp_db)} == {"a", "b", "c"}
    bp_runs = list_runs(workflow_name="board_prep", db_path=temp_db)
    assert {r["run_id"] for r in bp_runs} == {"a", "b"}


def test_delete_run(temp_db: Path) -> None:
    create_run("d", "board_prep", "D", {}, db_path=temp_db)
    assert delete_run("d", db_path=temp_db) is True
    assert delete_run("d", db_path=temp_db) is False
    assert get_run("d", db_path=temp_db) is None


def test_list_artifact_runs_only_done_with_artifact(temp_db: Path) -> None:
    # Done with artifact → included.
    create_run("done-1", "board_prep", "Deck", {}, db_path=temp_db)
    complete_run("done-1", "# Board Deck\n\nbody", db_path=temp_db)
    # Still running → excluded (no artifact yet).
    create_run("running-1", "board_prep", "WIP", {}, db_path=temp_db)
    # Failed → excluded.
    create_run("failed-1", "board_prep", "Boom", {}, db_path=temp_db)
    fail_run("failed-1", "boom", db_path=temp_db)

    runs = list_artifact_runs(db_path=temp_db)
    assert [r["run_id"] for r in runs] == ["done-1"]
    # List query omits the heavy artifact body.
    assert "artifact" not in runs[0]


def test_list_artifact_runs_empty_on_missing_db(tmp_path: Path) -> None:
    missing = tmp_path / "nope.db"
    assert list_artifact_runs(db_path=missing) == []


def test_list_artifact_runs_excludes_empty_artifact(temp_db: Path) -> None:
    """An empty-string artifact is treated as "no artifact" — kept consistent
    with the artifacts detail route so a card never dead-ends in a 404."""
    create_run("empty-1", "board_prep", "Empty", {}, db_path=temp_db)
    complete_run("empty-1", "", db_path=temp_db)
    assert list_artifact_runs(db_path=temp_db) == []


# -----------------------------------------------------------------------------
# HTTP routes
# -----------------------------------------------------------------------------


@pytest.fixture
def client(temp_db: Path) -> TestClient:
    # Point the persistence layer at a temp DB for every request in this test.
    with patch("openexecutive.workflows.persistence.DB_PATH", temp_db), \
         patch("openexecutive.api.routes.workflows.list_runs",
               side_effect=lambda **kw: list_runs(db_path=temp_db, **kw)), \
         patch("openexecutive.api.routes.workflows.get_run",
               side_effect=lambda run_id: get_run(run_id, db_path=temp_db)), \
         patch("openexecutive.api.routes.workflows.create_run",
               side_effect=lambda **kw: create_run(db_path=temp_db, **kw)), \
         patch("openexecutive.api.routes.workflows.delete_run",
               side_effect=lambda run_id: delete_run(run_id, db_path=temp_db)):
        app = create_app()
        with TestClient(app) as c:
            yield c


def test_list_workflows_includes_board_prep(client: TestClient) -> None:
    r = client.get("/workflows")
    assert r.status_code == 200
    names = [w["name"] for w in r.json()["workflows"]]
    assert "board_prep" in names


def test_get_workflow_returns_schema_and_steps(client: TestClient) -> None:
    r = client.get("/workflows/board_prep")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "board_prep"
    assert body["section"] == "Board"
    props = body["input_schema"]["properties"]
    assert "quarter_label" in props
    assert "deep_dive_topic_1" in props
    step_ids = [s["id"] for s in body["steps"]]
    assert step_ids == [
        "context",
        "exec_summary",
        "business_update",
        "deep_dive_1",
        "deep_dive_2",
        "decisions",
        "assemble",
    ]


def test_get_unknown_workflow_404(client: TestClient) -> None:
    r = client.get("/workflows/totally_fake")
    assert r.status_code == 404


def test_run_missing_required_fields_returns_422(client: TestClient) -> None:
    r = client.post(
        "/workflows/board_prep/runs",
        json={"quarter_label": "Q2 2026"},  # missing most required fields
    )
    assert r.status_code == 422


def test_run_invalid_json_returns_400(client: TestClient) -> None:
    r = client.post(
        "/workflows/board_prep/runs",
        content="not actually json",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400


def test_run_unknown_workflow_returns_404(client: TestClient) -> None:
    r = client.post("/workflows/no_such_workflow/runs", json={})
    assert r.status_code == 404


def test_get_unknown_run_returns_404(client: TestClient) -> None:
    r = client.get("/workflows/runs/no-such-run")
    assert r.status_code == 404


def test_delete_unknown_run_returns_404(client: TestClient) -> None:
    r = client.delete("/workflows/runs/no-such-run")
    assert r.status_code == 404


def test_list_runs_empty_initially(client: TestClient) -> None:
    r = client.get("/workflows/runs")
    assert r.status_code == 200
    assert r.json()["runs"] == []


# -----------------------------------------------------------------------------
# Sanity checks across all registered workflows — runs without any LLM calls.
# Catches a new workflow that forgot a required attribute, has an invalid
# input model, or declared duplicate step IDs.
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("workflow", list_workflows(), ids=lambda w: w.name)
def test_workflow_metadata_is_valid(workflow) -> None:
    assert workflow.name in WORKFLOW_REGISTRY
    assert workflow.title.strip(), f"{workflow.name} has empty title"
    assert workflow.description.strip(), f"{workflow.name} has empty description"
    assert workflow.section, f"{workflow.name} has no section"
    assert workflow.estimated_minutes > 0


@pytest.mark.parametrize("workflow", list_workflows(), ids=lambda w: w.name)
def test_workflow_meta_serializes(workflow) -> None:
    meta = workflow.meta()
    # JSON-schema friendly: model_dump should round-trip via json
    import json
    blob = json.dumps(meta.model_dump())
    assert workflow.name in blob


@pytest.mark.parametrize("workflow", list_workflows(), ids=lambda w: w.name)
def test_workflow_steps_well_formed(workflow) -> None:
    steps = workflow.steps()
    # Brief workflows (morning_brief, end_of_day_digest) are intentionally
    # two-step: gather context, then synthesize. Everything else has 3+.
    assert len(steps) >= 2, f"{workflow.name} should have at least 2 steps"
    step_ids = [s.id for s in steps]
    assert len(set(step_ids)) == len(step_ids), f"{workflow.name} has duplicate step IDs: {step_ids}"
    for step in steps:
        assert step.id.strip()
        assert step.title.strip()
        assert step.description.strip()


@pytest.mark.parametrize("workflow", list_workflows(), ids=lambda w: w.name)
def test_workflow_input_schema_has_properties(workflow) -> None:
    schema = workflow.input_model().model_json_schema()
    assert "properties" in schema, f"{workflow.name} input schema has no properties"
    assert len(schema["properties"]) > 0, f"{workflow.name} input schema has no fields"


def test_all_workflows_listed_via_api(client: TestClient) -> None:
    r = client.get("/workflows")
    assert r.status_code == 200
    api_names = {w["name"] for w in r.json()["workflows"]}
    assert api_names == set(WORKFLOW_REGISTRY.keys())


# -----------------------------------------------------------------------------
# Approval gates over SSE
# -----------------------------------------------------------------------------

class _GateRouteWorkflow:
    """A workflow that pauses, with a resume payload like a real dynamic one."""

    name = "gate_route"
    title = "Gate Route Workflow"
    description = "Pauses for a sign-off."
    estimated_minutes = 1

    def __init__(self, *, resumable: bool = True) -> None:
        self._resumable = resumable

    def input_model(self):  # noqa: ANN201 - duck-typed stub
        from pydantic import BaseModel as _BM
        from pydantic import create_model
        model: type[_BM] = create_model("_GateIn", topic=(str, ""))
        return model

    def steps(self):  # noqa: ANN201
        from openexecutive.workflows.base import WorkflowStepDef
        return [WorkflowStepDef(id="gate", title="Approve", description="Sign off.")]

    async def run(self, inputs, store):  # noqa: ANN001, ANN201
        from openexecutive.workflows.wait_for_human import (
            WaitForHumanEvent,
            WorkflowResumeState,
        )
        state = (
            WorkflowResumeState(
                workflow_name="gate_route", gate_step_id="gate",
                gate_step_index=0, next_step_index=1, outputs={},
            )
            if self._resumable
            else None
        )
        yield WaitForHumanEvent(person_id=7, question="Approve?", resume_state=state)


def _sse_events(body: str) -> list[dict]:
    return [
        json.loads(line[len("data: "):])
        for line in body.split("\n")
        if line.startswith("data: ")
    ]


@pytest.fixture
def gate_client(temp_db: Path):  # noqa: ANN201
    """A client whose registry resolves `gate_route`, with delivery stubbed."""
    async def _deliver(event, **_kw):  # noqa: ANN001, ANN202
        return event.model_copy(update={"channel": "slack", "channel_ref": "U1"}), "sent"

    with patch("openexecutive.workflows.persistence.DB_PATH", temp_db), \
         patch("openexecutive.api.routes.workflows.create_run",
               side_effect=lambda **kw: create_run(db_path=temp_db, **kw)), \
         patch("openexecutive.api.routes.workflows.get_run",
               side_effect=lambda run_id: get_run(run_id, db_path=temp_db)), \
         patch("openexecutive.api.routes.workflows.get_workflow",
               lambda name: _GateRouteWorkflow()), \
         patch("openexecutive.workflows.gate_delivery.deliver_gate_question", _deliver):
        app = create_app()
        with TestClient(app) as c:
            yield c


def test_paused_run_ends_the_stream_with_a_terminal_frame(
    gate_client: TestClient, temp_db: Path
) -> None:
    """A paused run emits no `done` and no `error` — the stream just stops. The
    `terminal` flag is what tells a client that IS the end, instead of leaving
    it waiting for a frame that never comes."""
    r = gate_client.post("/workflows/gate_route/runs", json={"topic": "x"})
    assert r.status_code == 200
    events = _sse_events(r.text)

    assert events[-1]["type"] == "awaiting_human"
    assert events[-1]["terminal"] is True
    assert events[-1]["resumable"] is True
    assert events[-1]["delivery"] == "sent"
    assert not any(e["type"] in {"done", "error"} for e in events)


def test_paused_run_is_checkpointed_with_its_payload(
    gate_client: TestClient, temp_db: Path
) -> None:
    r = gate_client.post("/workflows/gate_route/runs", json={"topic": "x"})
    run_id = _sse_events(r.text)[-1]["run_id"]

    run = get_run(run_id, db_path=temp_db)
    assert run is not None
    assert run["status"] == "awaiting_human"
    assert json.loads(run["resume_state_json"])["gate_step_id"] == "gate"


def test_a_stream_error_after_the_checkpoint_does_not_fail_the_run(
    temp_db: Path,
) -> None:
    """`fail_run` has no status guard, so an exception raised after a
    successful checkpoint used to overwrite `awaiting_human` with `error` —
    destroying a resumable run for something as ordinary as a client
    disconnecting mid-frame."""
    async def _deliver(event, **_kw):  # noqa: ANN001, ANN202
        return event.model_copy(update={"channel": "slack"}), "sent"

    def _boom(payload):  # noqa: ANN001, ANN202
        if payload.get("type") == "awaiting_human":
            raise RuntimeError("client went away")
        return f"data: {json.dumps(payload)}\n\n"

    with patch("openexecutive.workflows.persistence.DB_PATH", temp_db), \
         patch("openexecutive.api.routes.workflows.create_run",
               side_effect=lambda **kw: create_run(db_path=temp_db, **kw)), \
         patch("openexecutive.api.routes.workflows.get_workflow",
               lambda name: _GateRouteWorkflow()), \
         patch("openexecutive.workflows.gate_delivery.deliver_gate_question", _deliver), \
         patch("openexecutive.api.routes.workflows._sse", _boom):
        app = create_app()
        with TestClient(app) as c:
            c.post("/workflows/gate_route/runs", json={"topic": "x"})

    runs = list_runs(db_path=temp_db)
    assert len(runs) == 1
    assert runs[0]["status"] == "awaiting_human", (
        "a broken stream must not destroy the parked run"
    )


def test_run_detail_strips_the_resume_payload(
    gate_client: TestClient, temp_db: Path
) -> None:
    """The payload holds every completed step's full text, and the run-detail
    page polls this endpoint every few seconds while a run is unfinished."""
    r = gate_client.post("/workflows/gate_route/runs", json={"topic": "x"})
    run_id = _sse_events(r.text)[-1]["run_id"]

    detail = gate_client.get(f"/workflows/runs/{run_id}").json()

    assert "resume_state_json" not in detail
    assert detail["resume_progress"]["gate_step_id"] == "gate"
    assert detail["resume_progress"]["completed_step_ids"] == []


def test_run_detail_resume_progress_is_null_for_a_pause_only_run(
    temp_db: Path,
) -> None:

    async def _deliver(event, **_kw):  # noqa: ANN001, ANN202
        return event.model_copy(), "sent"

    with patch("openexecutive.workflows.persistence.DB_PATH", temp_db), \
         patch("openexecutive.api.routes.workflows.create_run",
               side_effect=lambda **kw: create_run(db_path=temp_db, **kw)), \
         patch("openexecutive.api.routes.workflows.get_run",
               side_effect=lambda run_id: get_run(run_id, db_path=temp_db)), \
         patch("openexecutive.api.routes.workflows.get_workflow",
               lambda name: _GateRouteWorkflow(resumable=False)), \
         patch("openexecutive.workflows.gate_delivery.deliver_gate_question", _deliver):
        app = create_app()
        with TestClient(app) as c:
            r = c.post("/workflows/gate_route/runs", json={"topic": "x"})
            events = _sse_events(r.text)
            assert events[-1]["resumable"] is False
            detail = c.get(f"/workflows/runs/{events[-1]['run_id']}").json()

    assert detail["resume_progress"] is None


# -----------------------------------------------------------------------------
# Principal-only workflows over HTTP: the weekly review (both modes) and the
# solo morning brief carry the principal's own data, so on the Jobs page and
# in an eval run only the principal (or anyone before there is one) may start
# them — the rule `run_workflow` applies in chat.
# -----------------------------------------------------------------------------

PRINCIPAL_EMAIL = "pat@example.com"
TEAMMATE_EMAIL = "sam@example.com"


class _PlainWorkflow:
    """An ordinary workflow (no principal_only_modes) that just renders."""

    name = "plain"
    title = "Plain"

    def input_model(self):  # noqa: ANN201 - duck-typed stub
        from pydantic import BaseModel as _BM
        from pydantic import create_model
        model: type[_BM] = create_model("_PlainIn", topic=(str, ""))
        return model

    def steps(self):  # noqa: ANN201
        return []

    async def run(self, inputs, store):  # noqa: ANN001, ANN201
        from openexecutive.workflows.base import WorkflowEvent
        yield WorkflowEvent(type="artifact", content="# Plain")


@pytest.fixture
def principal_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """The workflows router on a temp DB, with the real weekly review and
    morning brief classes (so their real principal_only_modes apply) whose
    run is stubbed, a roster of a principal and a teammate, and audit
    captured."""
    from types import SimpleNamespace

    from fastapi import FastAPI

    from openexecutive.api.routes import workflows as wf_routes
    from openexecutive.memory import episodic
    from openexecutive.people import store as people_store
    from openexecutive.workflows import persistence
    from openexecutive.workflows.base import WorkflowEvent
    from openexecutive.workflows.morning_brief import MorningBriefWorkflow
    from openexecutive.workflows.weekly_review import WeeklyReviewWorkflow

    db = tmp_path / "episodic.db"
    for module in (episodic, people_store, persistence):
        monkeypatch.setattr(module, "DB_PATH", db)
    episodic.initialize_db(db)
    people_store.initialize_db(db)
    initialize_runs_db(db)

    class _Weekly(WeeklyReviewWorkflow):
        async def run(self, inputs, store):  # type: ignore[override]  # noqa: ANN001, ANN201
            yield WorkflowEvent(type="artifact", content="# Weekly review")

    class _Morning(MorningBriefWorkflow):
        async def run(self, inputs, store):  # type: ignore[override]  # noqa: ANN001, ANN201
            yield WorkflowEvent(type="artifact", content="# Morning brief")

    stubs = {"weekly_review": _Weekly(), "morning_brief": _Morning(), "plain": _PlainWorkflow()}
    monkeypatch.setattr(wf_routes, "get_workflow", lambda name: stubs[name])
    audit: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "openexecutive.audit.log_event",
        lambda event_type, summary, **kw: audit.append((event_type, kw)),
    )

    app = FastAPI()
    app.include_router(wf_routes.router)
    app.state.store = object()
    return SimpleNamespace(client=TestClient(app), db=db, audit=audit)


def _roster(*, with_principal: bool = True) -> dict[str, int]:
    from openexecutive.people import store as people_store

    ids = {"teammate": people_store.upsert_person(full_name="Sam Ortiz", email=TEAMMATE_EMAIL)}
    if with_principal:
        ids["principal"] = people_store.upsert_person(
            full_name="Pat Lee", is_principal=True, email=PRINCIPAL_EMAIL
        )
    return ids


def _set_mode(mode: str) -> None:
    from openexecutive.memory import workspace_settings as ws

    ws.restore_workspace_settings(ws.WorkspaceSettings(mode=mode))  # type: ignore[arg-type]


def _start(client: TestClient, workflow: str, email: str | None) -> Any:
    headers = {"x-caller-email": email} if email else {}
    return client.post(f"/workflows/{workflow}/runs", json={}, headers=headers)


def _refusals(audit: list[tuple[str, dict]]) -> list[dict]:
    return [kw["details"] for _t, kw in audit if kw.get("details", {}).get("refused") is True]


PRINCIPAL_ONLY_CASES = [
    ("weekly_review", "solo"), ("weekly_review", "team"), ("morning_brief", "solo"),
]


@pytest.mark.parametrize(("workflow", "mode"), PRINCIPAL_ONLY_CASES)
def test_principal_only_run_refuses_a_teammate_and_a_stranger(
    principal_only: Any, workflow: str, mode: str
) -> None:
    from openexecutive.api.routes.workflows import PRINCIPAL_ONLY_DETAIL

    ids = _roster()
    _set_mode(mode)
    # A rostered teammate, and a signed-in email that is on nobody's entry.
    for email in (TEAMMATE_EMAIL, "stranger@example.com"):
        r = _start(principal_only.client, workflow, email)
        assert r.status_code == 403, (email, r.text)
        assert r.json()["detail"] == PRINCIPAL_ONLY_DETAIL
    # Refused before any run row exists.
    assert list_runs(db_path=principal_only.db) == []
    refusals = _refusals(principal_only.audit)
    assert len(refusals) == 2
    assert refusals[0]["workflow"] == workflow
    assert refusals[0]["workspace_mode"] == mode
    assert refusals[0]["caller_person_id"] == ids["teammate"]
    assert refusals[0]["surface"] == "jobs"
    assert refusals[1]["caller_person_id"] is None
    assert [a[1]["actor"] for a in principal_only.audit] == [TEAMMATE_EMAIL, "stranger@example.com"]


@pytest.mark.parametrize(("workflow", "mode"), PRINCIPAL_ONLY_CASES)
def test_principal_only_run_allows_the_principal(
    principal_only: Any, workflow: str, mode: str
) -> None:
    _roster()
    _set_mode(mode)
    # Signed in as the principal, and with no caller header (CLI, local login).
    for email in (PRINCIPAL_EMAIL, None):
        r = _start(principal_only.client, workflow, email)
        assert r.status_code == 200, (email, r.text)
        assert _sse_events(r.text)[-1]["type"] == "done"
    runs = list_runs(db_path=principal_only.db)
    assert len(runs) == 2 and {r["status"] for r in runs} == {"done"}
    assert _refusals(principal_only.audit) == []


@pytest.mark.parametrize(("workflow", "mode"), PRINCIPAL_ONLY_CASES)
def test_principal_only_run_with_no_principal_on_the_roster(
    principal_only: Any, workflow: str, mode: str
) -> None:
    """No principal on the roster (the roster filled in before anyone was
    flagged, or the old principal archived to re-run onboarding) lets no
    signed-in caller in, unlike PUT /workspace. A request with no caller
    header (the CLI, local login) still counts as the principal."""
    from openexecutive.people import store as people_store

    ids = _roster()
    _set_mode(mode)
    people_store.archive_person(ids["principal"])
    for email in (TEAMMATE_EMAIL, PRINCIPAL_EMAIL):
        r = _start(principal_only.client, workflow, email)
        assert r.status_code == 403, (email, r.text)
    people_store.upsert_person(full_name="Kim Park", email="kim@example.com")
    assert _start(principal_only.client, workflow, "kim@example.com").status_code == 403
    assert list_runs(db_path=principal_only.db) == []
    assert len(_refusals(principal_only.audit)) == 3

    r = _start(principal_only.client, workflow, None)
    assert r.status_code == 200, r.text
    assert _sse_events(r.text)[-1]["type"] == "done"


@pytest.mark.parametrize(("workflow", "mode"), [
    ("morning_brief", "team"), ("plain", "solo"), ("plain", "team"),
])
def test_other_runs_are_unchanged_for_a_teammate(
    principal_only: Any, workflow: str, mode: str
) -> None:
    _roster()
    _set_mode(mode)
    r = _start(principal_only.client, workflow, TEAMMATE_EMAIL)
    assert r.status_code == 200, r.text
    assert _sse_events(r.text)[-1]["type"] == "done"
    assert principal_only.audit == []


def test_an_unreadable_mode_refuses_a_teammate(
    principal_only: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mode that cannot be read counts as one the workflow is the
    principal's alone in — even the brief that is open in team mode."""
    from openexecutive.memory import workspace_settings as ws

    _roster()
    monkeypatch.setattr(ws, "read_stored_mode", lambda db_path=None: None)
    assert _start(principal_only.client, "morning_brief", TEAMMATE_EMAIL).status_code == 403
    assert _refusals(principal_only.audit)[0]["workspace_mode"] == "unknown"
    assert _start(principal_only.client, "morning_brief", PRINCIPAL_EMAIL).status_code == 200
    assert _start(principal_only.client, "plain", TEAMMATE_EMAIL).status_code == 200


def test_an_unreadable_roster_refuses(
    principal_only: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.people import store as people_store

    _roster()
    _set_mode("team")

    def _boom(*_a: Any, **_kw: Any) -> None:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(people_store, "find_person_by_email", _boom)
    monkeypatch.setattr(people_store, "find_principal_person", _boom)
    # Even the principal's own email cannot be confirmed, so it is refused...
    assert _start(principal_only.client, "weekly_review", PRINCIPAL_EMAIL).status_code == 403
    assert list_runs(db_path=principal_only.db) == []
    assert _refusals(principal_only.audit)[0]["caller_person_id"] is None
    # ...while a request with no caller header needs no roster read.
    assert _start(principal_only.client, "weekly_review", None).status_code == 200


# The eval runner calls workflow.run directly on live data and streams the
# artifact back, so POST /evals/runs applies the same rule.

def _eval_client(
    principal_only: Any, monkeypatch: pytest.MonkeyPatch, scenarios: list[dict]
) -> tuple[TestClient, list[list[dict]]]:
    from fastapi import FastAPI

    from openexecutive.api.routes import evals as evals_routes

    ran: list[list[dict]] = []

    async def _run_scenarios(*, kind, scenario_id=None, store=None,  # noqa: ANN001, ANN202
                             cancel_event=None, scenarios=None):
        ran.append(scenarios)
        yield {"type": "suite_done", "kind": kind, "passed": 0, "total": len(scenarios or [])}

    monkeypatch.setattr(evals_routes, "load_scenarios", lambda kind, scenario_id=None: scenarios)
    monkeypatch.setattr(evals_routes, "run_scenarios", _run_scenarios)
    for fn in ("create_eval_run", "complete_eval_run", "fail_eval_run", "append_scenario_result"):
        monkeypatch.setattr(evals_routes, fn, lambda *_a, **_kw: None)
    app = FastAPI()
    app.include_router(evals_routes.router)
    return TestClient(app), ran


def _scenario(workflow: str, mode: str | None = None) -> dict:
    s = {"id": f"wf_{workflow}", "type": "workflow", "workflow": workflow, "_kind": "workflow"}
    if mode:
        s["workspace_mode"] = mode
    return s


def test_eval_run_of_a_principal_only_workflow_refuses_a_teammate(
    principal_only: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _roster()
    _set_mode("team")
    # The weekly review in either mode; the morning brief as a solo scenario
    # on a team install (the scenario's mode is the one it runs in).
    for scenario in (_scenario("weekly_review", "solo"), _scenario("weekly_review"),
                     _scenario("morning_brief", "solo")):
        client, ran = _eval_client(principal_only, monkeypatch, [_scenario("board_prep"), scenario])
        body = {"kind": "workflow"}
        r = client.post("/evals/runs", json=body, headers={"x-caller-email": TEAMMATE_EMAIL})
        assert r.status_code == 403, (scenario, r.text)
        assert r.json()["detail"] == f"Only the principal can run the {scenario['id']} eval."
        assert ran == []
    refusals = _refusals(principal_only.audit)
    assert [d["surface"] for d in refusals] == ["evals"] * 3
    assert [d["workspace_mode"] for d in refusals] == ["solo", "team", "solo"]
    assert refusals[0]["scenario_id"] == "wf_weekly_review"


def test_eval_run_with_no_principal_on_the_roster(
    principal_only: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _roster(with_principal=False)
    _set_mode("team")
    client, ran = _eval_client(principal_only, monkeypatch, [_scenario("weekly_review")])
    r = client.post("/evals/runs", json={"kind": "workflow"},
                    headers={"x-caller-email": TEAMMATE_EMAIL})
    assert r.status_code == 403, r.text
    assert ran == []
    r = client.post("/evals/runs", json={"kind": "workflow"})
    assert r.status_code == 200, r.text
    assert len(ran) == 1


def test_eval_run_of_a_principal_only_workflow_allows_the_principal(
    principal_only: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _roster()
    _set_mode("team")
    scenarios = [_scenario("weekly_review", "solo"), _scenario("morning_brief")]
    client, ran = _eval_client(principal_only, monkeypatch, scenarios)
    for headers in ({"x-caller-email": PRINCIPAL_EMAIL}, {}):
        r = client.post("/evals/runs", json={"kind": "workflow"}, headers=headers)
        assert r.status_code == 200, r.text
        assert _sse_events(r.text)[-1]["type"] == "done"
    # The runner gets the very list that was checked, not a fresh load.
    assert ran == [scenarios, scenarios]
    # A teammate may still run the team morning brief scenario.
    client, ran = _eval_client(principal_only, monkeypatch, [_scenario("morning_brief")])
    r = client.post("/evals/runs", json={"kind": "workflow"},
                    headers={"x-caller-email": TEAMMATE_EMAIL})
    assert r.status_code == 200, r.text
    assert _refusals(principal_only.audit) == []
