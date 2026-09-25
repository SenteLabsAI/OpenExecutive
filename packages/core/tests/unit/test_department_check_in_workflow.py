"""Tests for the DepartmentCheckInWorkflow."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openexecutive.audit import logger as audit_logger_module
from openexecutive.audit.logger import AuditLogger
from openexecutive.departments import registry as dept_registry
from openexecutive.departments import store as dept_store
from openexecutive.departments.models import AuthorityLevel, DepartmentState
from openexecutive.memory import episodic
from openexecutive.workflows.department_check_in import (
    DepartmentCheckInInput,
    DepartmentCheckInWorkflow,
    needs_check_in,
)


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "test.db"
    monkeypatch.setattr(dept_store, "DB_PATH", db)
    monkeypatch.setattr(episodic, "DB_PATH", db)
    dept_registry.invalidate()
    episodic.initialize_db(db)
    dept_store.initialize_db(db)
    dept_store.seed_default_departments(db_path=db)
    yield
    dept_registry.invalidate()


_FAKE_SPECIALIST_RESPONSE = (
    "All Goals are on track for the period.\n"
    "- [on_track] Revenue target: $1M — current $950K."
)


def _run_workflow(slug: str = "finance", period: str = "2026-05-21") -> list:
    """Drive the workflow to exhaustion, collecting all events."""
    workflow = DepartmentCheckInWorkflow()
    inputs = DepartmentCheckInInput(department_slug=slug, period_label=period)
    store = MagicMock()  # ChromaDBStore not used by mocked specialists

    events = []

    async def _collect() -> None:
        async for event in workflow.run(inputs=inputs, store=store):
            events.append(event)

    with patch(
        "openexecutive.orchestrator.router.route_to_specialist",
        new_callable=AsyncMock,
        return_value=_FAKE_SPECIALIST_RESPONSE,
    ), patch(
        "openexecutive.knowledge.retriever.retrieve",
        return_value="",
    ), patch(
        "openexecutive.audit.logger.get_audit_logger",
        return_value=MagicMock(query=MagicMock(return_value=[])),
    ):
        asyncio.run(_collect())

    return events


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

def test_workflow_runs_and_produces_artifact() -> None:
    events = _run_workflow("finance")
    types = [e.type for e in events]
    assert "artifact" in types

    artifact_event = next(e for e in events if e.type == "artifact")
    assert artifact_event.content is not None
    assert "Finance" in artifact_event.content
    assert "Check-In Report" in artifact_event.content


def test_workflow_produces_all_steps() -> None:
    events = _run_workflow("finance")
    step_starts = {e.step_id for e in events if e.type == "step_start"}
    step_dones = {e.step_id for e in events if e.type == "step_done"}
    expected = {"load_context", "goal_status", "blockers", "proposed_actions", "assemble"}
    assert expected == step_starts
    assert expected == step_dones


def test_workflow_no_error_events_on_success() -> None:
    events = _run_workflow("finance")
    assert not any(e.type == "error" for e in events)


def test_workflow_errors_for_unknown_department() -> None:
    """Running with a non-existent dept slug should yield an error event."""
    workflow = DepartmentCheckInWorkflow()
    inputs = DepartmentCheckInInput(department_slug="nonexistent", period_label="2026-05-21")
    store = MagicMock()
    events = []

    async def _collect() -> None:
        async for event in workflow.run(inputs=inputs, store=store):
            events.append(event)

    with patch(
        "openexecutive.orchestrator.router.route_to_specialist",
        new_callable=AsyncMock,
        return_value=_FAKE_SPECIALIST_RESPONSE,
    ):
        asyncio.run(_collect())

    assert any(e.type == "error" for e in events)
    assert not any(e.type == "artifact" for e in events)


def test_workflow_proposed_actions_gated() -> None:
    """Proposed actions in the specialist response are piped through gate_action."""
    action_response = (
        '{"kind": "vendor_renegotiation", "payload": "Renegotiate vendor X contract", '
        '"required_scope": "spend_gt_10k"}\n'
        '{"kind": "budget_review", "payload": "Review Q2 budget variance"}\n'
    )

    dept_store.update_department("finance", authority_level=AuthorityLevel.PROPOSE_ONLY)
    dept_registry.invalidate()

    workflow = DepartmentCheckInWorkflow()
    inputs = DepartmentCheckInInput(department_slug="finance", period_label="2026-05-21")
    store = MagicMock()
    events = []

    call_count = 0

    async def _specialist_side_effect(*args, **kwargs) -> str:  # type: ignore[no-untyped-def]
        nonlocal call_count
        call_count += 1
        # Step 4 (proposed_actions) is the 3rd specialist call
        if call_count == 3:
            return action_response
        return _FAKE_SPECIALIST_RESPONSE

    async def _collect() -> None:
        async for event in workflow.run(inputs=inputs, store=store):
            events.append(event)

    with patch(
        "openexecutive.orchestrator.router.route_to_specialist",
        new_callable=AsyncMock,
        side_effect=_specialist_side_effect,
    ), patch(
        "openexecutive.knowledge.retriever.retrieve",
        return_value="",
    ), patch(
        "openexecutive.audit.logger.get_audit_logger",
        return_value=MagicMock(query=MagicMock(return_value=[])),
    ):
        asyncio.run(_collect())

    # Should have an artifact
    artifact_event = next((e for e in events if e.type == "artifact"), None)
    assert artifact_event is not None

    # Proposed actions section should appear in artifact
    assert "vendor_renegotiation" in artifact_event.content or "Proposed Actions" in artifact_event.content


def test_workflow_run_recorded_in_persistence(tmp_path: Path) -> None:
    """Verify create_run/complete_run work with the workflow output."""
    import uuid

    from openexecutive.workflows.persistence import complete_run, create_run, get_run

    run_id = str(uuid.uuid4())
    inputs_dict = {"department_slug": "finance", "period_label": "2026-05-21"}
    db = tmp_path / "persist.db"

    create_run(run_id, "department_check_in", "Test run", inputs_dict, db_path=db)

    events = _run_workflow("finance", "2026-05-21")
    artifact = next((e.content for e in events if e.type == "artifact"), "(none)")

    complete_run(run_id, artifact, db_path=db)

    run = get_run(run_id, db_path=db)
    assert run is not None
    assert run["status"] == "done"
    assert run["artifact"] == artifact
    assert run["workflow_name"] == "department_check_in"


def test_workflow_registered_in_registry() -> None:
    from openexecutive.workflows import WORKFLOW_REGISTRY
    assert "department_check_in" in WORKFLOW_REGISTRY
    wf = WORKFLOW_REGISTRY["department_check_in"]
    assert wf.name == "department_check_in"
    assert wf.input_model() is DepartmentCheckInInput


def test_workflow_meta() -> None:
    wf = DepartmentCheckInWorkflow()
    meta = wf.meta()
    assert meta.name == "department_check_in"
    assert len(meta.steps) == 5
    step_ids = [s.id for s in meta.steps]
    assert "load_context" in step_ids
    assert "assemble" in step_ids


# --------------------------------------------------------------------------- #
# Phase A: structured JSON verdicts persist back to the goal row.
# --------------------------------------------------------------------------- #

def _run_workflow_with_side_effect(
    slug: str,
    period: str,
    specialist_side_effect,  # type: ignore[no-untyped-def]
) -> list:
    """Like ``_run_workflow`` but accepts a per-call specialist side_effect
    so the structured `goal_status` response can differ from subsequent
    `blockers` / `proposed_actions` responses."""
    workflow = DepartmentCheckInWorkflow()
    inputs = DepartmentCheckInInput(department_slug=slug, period_label=period)
    store = MagicMock()
    events: list = []

    async def _collect() -> None:
        async for event in workflow.run(inputs=inputs, store=store):
            events.append(event)

    with patch(
        "openexecutive.orchestrator.router.route_to_specialist",
        new_callable=AsyncMock,
        side_effect=specialist_side_effect,
    ), patch(
        "openexecutive.knowledge.retriever.retrieve",
        return_value="",
    ), patch(
        "openexecutive.audit.logger.get_audit_logger",
        return_value=MagicMock(query=MagicMock(return_value=[]), log=MagicMock()),
    ):
        asyncio.run(_collect())

    return events


def test_workflow_persists_verdicts_to_goal_row() -> None:
    """The `goal_status` step's structured JSON output must move
    status + last_reviewed_at on the matching goal row."""
    goal_id = dept_store.insert_goal(
        "finance",
        period_type="quarter",
        period_value="Q2 2026",
        key_result="Hit $1M ARR by Jun 30",
        target="$1M ARR",
        current="$650K",
        status="on_track",
    )
    dept_registry.invalidate()

    verdict_response = (
        '{"verdicts": [{"goal_id": ' + str(goal_id) +
        ', "status": "at_risk", "rationale": "Q2 deals slipping into Q3"}],'
        ' "narrative": "One Q2 goal at risk per latest pipeline review."}'
    )

    async def _side_effect(*args, **kwargs) -> str:  # type: ignore[no-untyped-def]
        # Step 2 (goal_status) is the first specialist call; steps 3+4 still
        # use the fake prose response — they aren't under test here.
        if _side_effect.call_count == 0:  # type: ignore[attr-defined]
            _side_effect.call_count += 1  # type: ignore[attr-defined]
            return verdict_response
        _side_effect.call_count += 1  # type: ignore[attr-defined]
        return _FAKE_SPECIALIST_RESPONSE
    _side_effect.call_count = 0  # type: ignore[attr-defined]

    events = _run_workflow_with_side_effect("finance", "2026-05-21", _side_effect)
    assert any(e.type == "artifact" for e in events)

    after = dept_store.get_goal(goal_id)
    assert after is not None
    assert after.status == "at_risk"
    assert after.last_reviewed_at != ""
    # The artifact renders the narrative, never the raw JSON.
    artifact = next(e.content for e in events if e.type == "artifact")
    assert "Q2 goal at risk" in artifact
    assert "verdicts" not in artifact  # i.e. no leaked JSON


def test_workflow_audits_goal_status_review_with_transitions() -> None:
    """A single `goal_status_review` audit row carries the from→to
    transitions for every persisted verdict."""
    goal_id = dept_store.insert_goal(
        "finance",
        period_type="quarter",
        period_value="Q2 2026",
        key_result="Hit $1M ARR",
        target="$1M",
        status="on_track",
    )
    dept_registry.invalidate()

    verdict_response = (
        '{"verdicts": [{"goal_id": ' + str(goal_id) +
        ', "status": "off_track", "rationale": "lost two enterprise deals"}],'
        ' "narrative": "Off track on ARR."}'
    )
    fake_logger = MagicMock(query=MagicMock(return_value=[]), log=MagicMock())

    async def _side_effect(*args, **kwargs) -> str:  # type: ignore[no-untyped-def]
        if _side_effect.call_count == 0:  # type: ignore[attr-defined]
            _side_effect.call_count += 1  # type: ignore[attr-defined]
            return verdict_response
        _side_effect.call_count += 1  # type: ignore[attr-defined]
        return _FAKE_SPECIALIST_RESPONSE
    _side_effect.call_count = 0  # type: ignore[attr-defined]

    workflow = DepartmentCheckInWorkflow()
    inputs = DepartmentCheckInInput(department_slug="finance", period_label="2026-05-21")
    store = MagicMock()

    async def _collect() -> None:
        async for _ in workflow.run(inputs=inputs, store=store):
            pass

    with patch(
        "openexecutive.orchestrator.router.route_to_specialist",
        new_callable=AsyncMock,
        side_effect=_side_effect,
    ), patch(
        "openexecutive.knowledge.retriever.retrieve",
        return_value="",
    ), patch(
        "openexecutive.audit.logger.get_audit_logger",
        return_value=fake_logger,
    ):
        asyncio.run(_collect())

    # Find the goal_status_review call.
    review_calls = [
        c for c in fake_logger.log.call_args_list
        if c.args and c.args[0] == "goal_status_review"
    ]
    assert len(review_calls) == 1
    details = review_calls[0].kwargs.get("details") or {}
    transitions = details.get("transitions", [])
    assert len(transitions) == 1
    assert transitions[0]["goal_id"] == goal_id
    assert transitions[0]["from"] == "on_track"
    assert transitions[0]["to"] == "off_track"
    assert review_calls[0].kwargs.get("department") == "finance"
    assert review_calls[0].kwargs.get("actor") == "department_check_in"


def test_workflow_degrades_gracefully_on_unparseable_specialist_output() -> None:
    """When the specialist returns prose (not JSON), the workflow still
    ships an artifact, mutates nothing, and emits no error event —
    matches the existing best-effort Honcho-mirror contract."""
    goal_id = dept_store.insert_goal(
        "finance",
        period_type="quarter",
        period_value="Q2 2026",
        key_result="Hit $1M ARR",
        target="$1M",
        status="on_track",
    )
    dept_registry.invalidate()
    seeded = dept_store.get_goal(goal_id)
    assert seeded is not None
    seeded_status = seeded.status
    seeded_last_reviewed = seeded.last_reviewed_at

    events = _run_workflow("finance", "2026-05-21")  # uses _FAKE_SPECIALIST_RESPONSE
    # Artifact still shipped, no error events.
    assert any(e.type == "artifact" for e in events)
    assert not any(e.type == "error" for e in events)

    # Goal row unchanged.
    after = dept_store.get_goal(goal_id)
    assert after is not None
    assert after.status == seeded_status
    assert after.last_reviewed_at == seeded_last_reviewed


def test_workflow_drops_hallucinated_goal_id_silently() -> None:
    """If the specialist invents a goal_id we don't have, the workflow
    skips that verdict — no DB writes, no error event."""
    real_goal = dept_store.insert_goal(
        "finance",
        period_type="quarter",
        period_value="Q2 2026",
        key_result="Hit $1M ARR",
        target="$1M",
        status="on_track",
    )
    dept_registry.invalidate()
    bogus_id = real_goal + 9999  # guaranteed-unused id

    verdict_response = (
        '{"verdicts": [{"goal_id": ' + str(bogus_id) +
        ', "status": "off_track", "rationale": "hallucinated"}],'
        ' "narrative": "Nothing real here."}'
    )

    async def _side_effect(*args, **kwargs) -> str:  # type: ignore[no-untyped-def]
        if _side_effect.call_count == 0:  # type: ignore[attr-defined]
            _side_effect.call_count += 1  # type: ignore[attr-defined]
            return verdict_response
        _side_effect.call_count += 1  # type: ignore[attr-defined]
        return _FAKE_SPECIALIST_RESPONSE
    _side_effect.call_count = 0  # type: ignore[attr-defined]

    events = _run_workflow_with_side_effect("finance", "2026-05-21", _side_effect)
    assert not any(e.type == "error" for e in events)

    # Real goal untouched.
    after = dept_store.get_goal(real_goal)
    assert after is not None
    assert after.status == "on_track"
    assert after.last_reviewed_at == ""


def test_workflow_strips_json_code_fences() -> None:
    """Real models still emit ```json … ``` fences ~10% of the time
    even when told not to. The parser must tolerate this."""
    goal_id = dept_store.insert_goal(
        "finance",
        period_type="quarter",
        period_value="Q2 2026",
        key_result="Hit $1M ARR",
        target="$1M",
        status="on_track",
    )
    dept_registry.invalidate()

    fenced_response = (
        "```json\n"
        '{"verdicts": [{"goal_id": ' + str(goal_id) +
        ', "status": "at_risk", "rationale": "fenced"}],'
        ' "narrative": "Fenced response."}\n'
        "```"
    )

    async def _side_effect(*args, **kwargs) -> str:  # type: ignore[no-untyped-def]
        if _side_effect.call_count == 0:  # type: ignore[attr-defined]
            _side_effect.call_count += 1  # type: ignore[attr-defined]
            return fenced_response
        _side_effect.call_count += 1  # type: ignore[attr-defined]
        return _FAKE_SPECIALIST_RESPONSE
    _side_effect.call_count = 0  # type: ignore[attr-defined]

    events = _run_workflow_with_side_effect("finance", "2026-05-21", _side_effect)
    assert any(e.type == "artifact" for e in events)
    after = dept_store.get_goal(goal_id)
    assert after is not None
    assert after.status == "at_risk"


def test_workflow_empty_narrative_does_not_leak_raw_json_into_artifact() -> None:
    """Regression: when the specialist returns a valid JSON object with
    an EMPTY narrative, `_parse_verdicts` previously fell back to the
    raw `text` (the JSON-encoded response). `_assemble_artifact` then
    pasted that raw JSON literally under "## Goal Status" in the
    check-in report. The verdicts still need to persist — empty
    narrative ≠ empty verdicts — but the rendered prose must be the
    graceful "(No content generated.)" placeholder, not raw JSON.
    """
    goal_id = dept_store.insert_goal(
        "finance",
        period_type="quarter",
        period_value="Q2 2026",
        key_result="Hit $1M ARR",
        target="$1M",
        status="on_track",
    )
    dept_registry.invalidate()

    empty_narrative_response = (
        '{"verdicts": [{"goal_id": ' + str(goal_id) +
        ', "status": "at_risk", "rationale": "deals slipping"}],'
        ' "narrative": ""}'
    )

    async def _side_effect(*args, **kwargs) -> str:  # type: ignore[no-untyped-def]
        if _side_effect.call_count == 0:  # type: ignore[attr-defined]
            _side_effect.call_count += 1  # type: ignore[attr-defined]
            return empty_narrative_response
        _side_effect.call_count += 1  # type: ignore[attr-defined]
        return _FAKE_SPECIALIST_RESPONSE
    _side_effect.call_count = 0  # type: ignore[attr-defined]

    events = _run_workflow_with_side_effect("finance", "2026-05-21", _side_effect)
    artifact_event = next((e for e in events if e.type == "artifact"), None)
    assert artifact_event is not None
    artifact = artifact_event.content

    # No JSON leak — neither the literal key names nor the object braces
    # of the specialist response should appear in the rendered prose.
    assert '"verdicts"' not in artifact
    assert '"narrative"' not in artifact
    assert '"goal_id"' not in artifact

    # The Goal Status section should render the graceful-empty placeholder.
    assert "(No content generated.)" in artifact

    # Empty narrative must NOT block persistence — the verdict still landed.
    after = dept_store.get_goal(goal_id)
    assert after is not None
    assert after.status == "at_risk"
    assert after.last_reviewed_at != ""


# --------------------------------------------------------------------------- #
# needs_check_in — the skip rule the scheduler applies before creating a run
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def audit_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AuditLogger:
    """A per-test audit log. Autouse: Goal writes mirror to Honcho, which
    audits, and would otherwise create ./episodic_memory.db."""
    al = AuditLogger(tmp_path / "audit.db")
    monkeypatch.setattr(audit_logger_module, "get_audit_logger", lambda: al)
    return al


def _finance() -> DepartmentState:
    state = dept_store.get_department("finance")
    assert state is not None
    return state


def _add_goal() -> int:
    """A Goal last edited four days ago, so only what a test adds is "new"."""
    goal_id = dept_store.insert_goal(
        "finance", period_value="Q3 2026", key_result="Close the round", target="$2M",
    )
    edited = (datetime.now(UTC) - timedelta(days=4)).isoformat()
    with dept_store._get_conn(None) as conn:
        conn.execute(
            "UPDATE department_goals SET created_at = ?, updated_at = ? WHERE id = ?",
            (edited, edited, goal_id),
        )
    return goal_id


def _review_all(at: datetime) -> None:
    for goal in _finance().goals:
        assert goal.id is not None
        dept_store.record_goal_review(goal.id, status="on_track", last_reviewed_at=at.isoformat())


def test_needs_check_in_skips_an_informational_department() -> None:
    _add_goal()
    state = _finance()
    state = state.model_copy(
        update={"config": state.config.model_copy(update={"specialist_key": None})}
    )
    reason = needs_check_in(state, datetime.now(UTC))
    assert reason is not None and "no specialist" in reason


def test_needs_check_in_skips_a_department_without_goals() -> None:
    reason = needs_check_in(_finance(), datetime.now(UTC))
    assert reason is not None and "no Goals" in reason


def test_needs_check_in_runs_when_a_goal_was_never_reviewed() -> None:
    _add_goal()
    assert needs_check_in(_finance(), datetime.now(UTC)) is None


def test_needs_check_in_skips_when_nothing_is_new(audit_db: AuditLogger) -> None:
    _add_goal()
    _review_all(datetime.now(UTC) + timedelta(seconds=1))
    # The check-in's own review row lands after the stamp; it is not news.
    audit_db.log(
        "goal_status_review", "Finance: reviewed 1 goal(s)",
        actor="department_check_in", department="finance",
    )
    # Activity in another department is not news for this one.
    audit_db.log("tool_invocation", "sales goal edit", actor="executive", department="sales")

    reason = needs_check_in(_finance(), datetime.now(UTC))
    assert reason is not None and "nothing new" in reason


def test_needs_check_in_runs_after_a_department_audit_row(audit_db: AuditLogger) -> None:
    _add_goal()
    _review_all(datetime.now(UTC) - timedelta(hours=1))
    audit_db.log(
        "tool_invocation", "update_department_goal finance", actor="executive",
        department="finance",
    )
    assert needs_check_in(_finance(), datetime.now(UTC)) is None


def test_needs_check_in_ignores_rows_before_the_review(audit_db: AuditLogger) -> None:
    _add_goal()
    audit_db.log(
        "tool_invocation", "update_department_goal finance", actor="executive",
        department="finance",
    )
    _review_all(datetime.now(UTC) + timedelta(seconds=1))
    assert needs_check_in(_finance(), datetime.now(UTC)) is not None


def test_needs_check_in_runs_after_a_goal_content_edit(audit_db: AuditLogger) -> None:
    goal_id = _add_goal()
    _review_all(datetime.now(UTC) - timedelta(hours=1))
    dept_store.update_goal(goal_id, current="$1.2M committed")
    assert needs_check_in(_finance(), datetime.now(UTC)) is None


def test_needs_check_in_runs_after_a_department_decision(audit_db: AuditLogger) -> None:
    _add_goal()
    _review_all(datetime.now(UTC) - timedelta(hours=1))
    episodic.store_decision(
        domain="finance", summary="Move the close to October", department="finance",
        db_path=episodic.DB_PATH,
    )
    assert needs_check_in(_finance(), datetime.now(UTC)) is None


def test_needs_check_in_sees_a_decision_beyond_the_newest_page(audit_db: AuditLogger) -> None:
    """The department's decision is filtered in SQL, so a busy week of other
    departments' decisions cannot push it out of view."""
    _add_goal()
    _review_all(datetime.now(UTC) - timedelta(hours=1))
    episodic.store_decision(
        domain="finance", summary="Move the close to October", department="finance",
        db_path=episodic.DB_PATH,
    )
    for n in range(60):
        episodic.store_decision(
            domain="sales", summary=f"Sales call outcome {n}", department="sales",
            db_path=episodic.DB_PATH,
        )
    assert needs_check_in(_finance(), datetime.now(UTC)) is None


def test_needs_check_in_ignores_another_departments_decision(audit_db: AuditLogger) -> None:
    _add_goal()
    _review_all(datetime.now(UTC) - timedelta(hours=1))
    episodic.store_decision(
        domain="sales", summary="New pricing tier", department="sales",
        db_path=episodic.DB_PATH,
    )
    assert needs_check_in(_finance(), datetime.now(UTC)) is not None


def test_needs_check_in_uses_the_oldest_review(audit_db: AuditLogger) -> None:
    """A Goal last graded before the newest activity still gets re-graded."""
    _add_goal()
    _add_goal()
    first, second = _finance().goals
    assert first.id is not None and second.id is not None
    now = datetime.now(UTC)
    dept_store.record_goal_review(
        first.id, status="on_track", last_reviewed_at=(now - timedelta(days=3)).isoformat(),
    )
    audit_db.log("tool_invocation", "finance note", actor="executive", department="finance")
    dept_store.record_goal_review(
        second.id, status="on_track", last_reviewed_at=(now + timedelta(seconds=1)).isoformat(),
    )
    assert needs_check_in(_finance(), now) is None
