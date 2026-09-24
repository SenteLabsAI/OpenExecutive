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
