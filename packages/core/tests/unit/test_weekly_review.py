"""The weekly review: the workflow (workflows/weekly_review.py), the decisions
it revisits (memory/episodic.py), and its solo-only weekly schedule and
delivery (scheduler/runner.py, memory/workspace_settings.py)."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from openexecutive.audit import AuditLogger, set_audit_logger
from openexecutive.departments import registry as dept_registry
from openexecutive.departments import store as dept_store
from openexecutive.memory import episodic
from openexecutive.memory import workspace_settings as ws
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store
from openexecutive.scheduler import runner
from openexecutive.workflows import WORKFLOW_REGISTRY
from openexecutive.workflows.weekly_review import (
    REVIEW_ACTOR,
    WEEKLY_TOP_THREE_SYSTEM,
    WeeklyReviewInput,
    WeeklyReviewWorkflow,
    decisions_to_revisit,
)

KIND = runner.WEEKLY_REVIEW_KIND


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "weekly.db"
    for mod in (episodic, dept_store, people_store):
        monkeypatch.setattr(mod, "DB_PATH", db)
    monkeypatch.setattr(ws, "_configured_timezone", lambda: ZoneInfo("UTC"))
    monkeypatch.delenv("PRINCIPAL_WEEKLY_REVIEW_TIME", raising=False)
    # Audit rows go to the temp DB, never ./episodic_memory.db.
    set_audit_logger(AuditLogger(db_path=db))
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **k: None)
    episodic.initialize_db(db)
    dept_store.initialize_db(db)
    people_store.initialize_db(db)
    dept_registry.invalidate()
    people_registry.invalidate()
    yield db
    set_audit_logger(None)
    dept_registry.invalidate()
    people_registry.invalidate()


def _solo() -> None:
    ws.restore_workspace_settings(ws.WorkspaceSettings(mode="solo"))


def _decision(db: Path, *, days_ago: float, summary: str, outcome: str = "") -> int:
    ts = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    with sqlite3.connect(str(db)) as conn:
        cur = conn.execute(
            "INSERT INTO decisions (timestamp, domain, summary, outcome) VALUES (?, ?, ?, ?)",
            (ts, "strategy", summary, outcome),
        )
        return int(cur.lastrowid or 0)


def _initiative(db: Path, *, title: str, status: str, days_ago: float) -> int:
    ts = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    with sqlite3.connect(str(db)) as conn:
        cur = conn.execute(
            "INSERT INTO initiatives (title, status, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (title, status, ts, ts),
        )
        return int(cur.lastrowid or 0)


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #


def test_registered_as_a_background_workflow_with_defaulted_inputs() -> None:
    wf = WORKFLOW_REGISTRY["weekly_review"]
    assert isinstance(wf, WeeklyReviewWorkflow)
    meta = wf.meta()
    assert meta.background is True
    assert meta.playbooks == ["weekly-review"]
    assert [s.id for s in meta.steps] == [
        "goals", "commitments", "projects", "decisions", "revisit", "assemble",
    ]
    WeeklyReviewInput()  # every input is optional
    assert not meta.input_schema.get("required")


def test_prompts_and_playbook_are_role_neutral() -> None:
    from openexecutive.knowledge import skills_index

    playbook = next(skills_index.BUILTIN_SKILLS_PATH.rglob("weekly-review.md")).read_text()
    for text in (WEEKLY_TOP_THREE_SYSTEM, playbook):
        assert "founder" not in text.lower()
        assert "lead a function inside a larger organisation" in text


# --------------------------------------------------------------------------- #
# Decision selection
# --------------------------------------------------------------------------- #


def test_decisions_to_revisit_are_old_unanswered_and_at_most_three(_isolated: Path) -> None:
    old = [_decision(_isolated, days_ago=d, summary=f"old {d}") for d in (31, 40, 50, 60)]
    _decision(_isolated, days_ago=45, summary="answered", outcome="worked")
    _decision(_isolated, days_ago=45, summary="blank outcome", outcome="   ")
    _decision(_isolated, days_ago=29, summary="too recent")

    picked = decisions_to_revisit(datetime.now(UTC))

    assert len(picked) == 3
    # Newest first; whitespace counts as no outcome; answered and recent are out.
    assert [d.summary for d in picked] == ["old 31", "old 40", "blank outcome"]
    assert old[0] == picked[0].id


def test_decisions_since_is_this_weeks_newest_first(_isolated: Path) -> None:
    _decision(_isolated, days_ago=1, summary="yesterday")
    _decision(_isolated, days_ago=3, summary="midweek")
    _decision(_isolated, days_ago=8, summary="last week")
    week = episodic.decisions_since(datetime.now(UTC) - timedelta(days=7))
    assert [d.summary for d in week] == ["yesterday", "midweek"]


# --------------------------------------------------------------------------- #
# The workflow, with stubbed specialists and model
# --------------------------------------------------------------------------- #


def _seed_areas() -> dict[str, int]:
    dept_store.create_department("Finance", specialist_key="cfo")
    dept_store.create_department("Client work")  # no specialist
    dept_store.create_department("Marketing", specialist_key="cmo")  # no goals
    ids = {
        "buffer": dept_store.insert_goal(
            "finance", period_value="Q4 2026", key_result="Build a three-month cash buffer",
            target="3 months", current="2.1 months", status="on_track",
        ),
        "invoices": dept_store.insert_goal(
            "finance", period_value="Q4 2026", key_result="Collect invoices within 30 days",
            target="0 late", current="1 late", status="on_track",
        ),
        "client": dept_store.insert_goal(
            "client-work", period_value="Q4 2026", key_result="Ship two sprints",
            target="2", current="1", status="at_risk",
        ),
    }
    dept_registry.invalidate()
    return ids


def _stub_models(
    monkeypatch: pytest.MonkeyPatch,
    *,
    verdicts: dict[str, Any] | None = None,
    top_three: str | Exception = "1. Close the buffer gap — at risk\n2. Chase the late invoice — due",
) -> dict[str, list[Any]]:
    calls: dict[str, list[Any]] = {"specialist": [], "fast": []}

    async def _route(specialist_name: str, query: str, **kw: Any) -> str:
        calls["specialist"].append((specialist_name, query))
        return json.dumps(verdicts or {})

    class _P:
        async def messages_create(self, **kw: Any) -> Any:
            calls["fast"].append(kw)
            if isinstance(top_three, Exception):
                raise top_three
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=top_three)])

    monkeypatch.setattr("openexecutive.orchestrator.router.route_to_specialist", _route)
    monkeypatch.setattr("openexecutive.providers.get_provider", lambda _m: _P())
    monkeypatch.setattr("openexecutive.agents.utility_fast.get_fast_model", lambda: "claude-test")
    return calls


def _run(inputs: WeeklyReviewInput | None = None) -> list[Any]:
    async def _go() -> list[Any]:
        wf = WeeklyReviewWorkflow()
        return [e async for e in wf.run(inputs or WeeklyReviewInput(), store=None)]  # type: ignore[arg-type]

    return asyncio.run(_go())


def _artifact(events: list[Any]) -> str:
    return next(e.content for e in events if e.type == "artifact")


def test_review_grades_areas_saves_verdicts_and_lists_ungraded(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _solo()
    ids = _seed_areas()
    calls = _stub_models(monkeypatch, verdicts={
        "verdicts": [
            {"goal_id": ids["buffer"], "status": "at_risk", "rationale": "2.1 of 3 months."},
            {"goal_id": 999, "status": "off_track", "rationale": "hallucinated"},
        ],
        "narrative": "Cash is behind the buffer plan.",
    })

    events = _run(WeeklyReviewInput(period_label="Week of Sep 21"))
    artifact = _artifact(events)

    # Only the area with goals AND a specialist is graded, by its specialist.
    assert [c[0] for c in calls["specialist"]] == ["cfo"]
    assert "Finance area" in calls["specialist"][0][1]
    buffer = dept_store.get_goal(ids["buffer"])
    assert buffer is not None and buffer.status == "at_risk" and buffer.last_reviewed_at
    invoices = dept_store.get_goal(ids["invoices"])
    assert invoices is not None and invoices.status == "on_track" and not invoices.last_reviewed_at
    # The ungraded area is listed, not dropped; the goalless one is skipped.
    assert "### Client work _(not graded — no specialist covers this area)_" in artifact
    assert "Ship two sprints** — at risk" in artifact
    assert "Marketing" not in artifact
    assert "**Build a three-month cash buffer** — at risk (was on track)" in artifact
    assert "_Cash is behind the buffer plan._" in artifact
    assert artifact.startswith("# Weekly review — Week of Sep 21")
    for heading in (
        "## Goals by area", "## Commitments due", "## Projects that went quiet",
        "## This week's decisions", "## How did these turn out?", "## Next week's top 3",
    ):
        assert heading in artifact
    assert "1. Close the buffer gap — at risk" in artifact
    result = next(e for e in events if e.type == "result").data
    assert result["areas_graded"] == 1 and result["areas_ungraded"] == ["Client work"]
    # The system prompt is the constant; the playbook rides in the user turn.
    assert calls["fast"][0]["system"] == WEEKLY_TOP_THREE_SYSTEM
    assert [e.step_id for e in events if e.type == "step_done"] == [
        "goals", "commitments", "projects", "decisions", "revisit", "assemble",
    ]


def test_review_audits_its_own_grading(_isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.audit.logger import get_audit_logger

    ids = _seed_areas()
    _stub_models(monkeypatch, verdicts={
        "verdicts": [{"goal_id": ids["buffer"], "status": "off_track", "rationale": "r"}],
        "narrative": "",
    })
    _run()
    rows = get_audit_logger().query(event_type="goal_status_review", limit=10)
    assert [(r.actor, r.department) for r in rows] == [(REVIEW_ACTOR, "finance")]


def test_review_lists_due_quiet_week_and_revisit(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _solo()
    _stub_models(monkeypatch)
    monkeypatch.setattr(
        "openexecutive.attunement.open_loops.principal_due_soon",
        lambda **kw: [
            {"loop_id": 4, "description": "Send the revised invoice", "due_at": "",
             "due_date": "2026-09-23", "state": "overdue"},
            {"loop_id": 5, "description": "Draft the proposal", "due_at": "",
             "due_date": "2026-09-28", "state": "soon"},
        ],
    )
    quiet_id = _initiative(_isolated, title="Template kit launch", status="active", days_ago=12)
    _initiative(_isolated, title="Fresh project", status="active", days_ago=2)
    _initiative(_isolated, title="Paused project", status="paused", days_ago=30)
    _decision(_isolated, days_ago=2, summary="Raise the sprint price")
    revisit_id = _decision(_isolated, days_ago=40, summary="Drop hourly billing")

    events = _run()
    artifact = _artifact(events)

    assert "- OVERDUE (was due 2026-09-23): Send the revised invoice" in artifact
    assert "- due 2026-09-28: Draft the proposal" in artifact
    assert "**Template kit launch** — no update in 12 days" in artifact
    assert "Fresh project" not in artifact and "Paused project" not in artifact
    assert "Raise the sprint price" in artifact
    assert f"[decision {revisit_id}]" in artifact and "Drop hourly billing" in artifact
    result = next(e for e in events if e.type == "result").data
    assert result["quiet_projects"] == [quiet_id]
    assert result["revisit_decision_ids"] == [revisit_id]


def test_review_with_nothing_keeps_its_shape(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_models(monkeypatch, top_three="")
    artifact = _artifact(_run())
    assert "_No goals tracked yet._" in artifact
    assert "_Nothing overdue or due in the next week._" in artifact
    assert "_No decisions logged this week._" in artifact
    assert "## Next week's top 3" in artifact


def test_top_three_falls_back_to_the_ranked_pick_when_the_model_fails(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = _seed_areas()
    _stub_models(monkeypatch, verdicts={
        "verdicts": [{"goal_id": ids["buffer"], "status": "off_track", "rationale": "r"}],
    }, top_three=RuntimeError("model down"))
    artifact = _artifact(_run())
    top = artifact.split("## Next week's top 3", 1)[1]
    assert "1. Finance: Build a three-month cash buffer — off track" in top
    assert "2. Client work: Ship two sprints — at risk" in top


def test_top_three_keeps_only_three_list_items(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_models(monkeypatch, top_three=(
        "Here you go:\n## Top 3\n- one — a\n- two — b\n3) three — c\n4. four — d\nThanks!"
    ))
    top = _artifact(_run()).split("## Next week's top 3", 1)[1]
    assert "1. one — a\n2. two — b\n3. three — c" in top
    assert "four" not in top and "Thanks" not in top and "Here you go" not in top


def test_a_failing_specialist_leaves_the_area_ungraded(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_areas()
    _stub_models(monkeypatch)

    async def _boom(**kw: Any) -> str:
        raise RuntimeError("provider down")

    monkeypatch.setattr("openexecutive.orchestrator.router.route_to_specialist", _boom)
    artifact = _artifact(_run())
    assert "### Finance _(couldn't be graded this week" in artifact


def test_an_unparseable_grade_leaves_the_area_ungraded(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = _seed_areas()
    _stub_models(monkeypatch)

    async def _prose(**kw: Any) -> str:
        return "The buffer looks fine to me."

    monkeypatch.setattr("openexecutive.orchestrator.router.route_to_specialist", _prose)
    events = _run()
    artifact = _artifact(events)
    assert "### Finance _(couldn't be graded this week" in artifact
    goal = dept_store.get_goal(ids["buffer"])
    assert goal is not None and goal.status == "on_track" and not goal.last_reviewed_at
    assert next(e for e in events if e.type == "result").data["areas_graded"] == 0


def test_team_mode_says_department(_isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_areas()
    calls = _stub_models(monkeypatch)
    artifact = _artifact(_run())
    assert "## Goals by department" in artifact
    assert "Finance department" in calls["specialist"][0][1]


# --------------------------------------------------------------------------- #
# Scheduling: solo only, weekly in the user's zone
# --------------------------------------------------------------------------- #


def _pending(kind: str = KIND) -> list[episodic.ScheduledAction]:
    return [a for a in episodic.list_scheduled_actions(status="pending", limit=50) if a.kind == kind]


def _local(run_at: str, zone: str) -> datetime:
    return datetime.fromisoformat(run_at).astimezone(ZoneInfo(zone))


def test_seeded_only_in_solo() -> None:
    assert runner.seed_principal_briefs() == 3
    assert _pending() == []
    _solo()
    assert runner.seed_principal_briefs() == 1  # only the weekly review was missing
    (row,) = _pending()
    assert row.channel == "__internal__" and row.channel_ref == "principal"
    assert runner.seed_principal_briefs() == 0  # idempotent


def test_default_is_friday_1600_in_the_users_zone() -> None:
    ws.restore_workspace_settings(ws.WorkspaceSettings(mode="solo", timezone="America/New_York"))
    runner.seed_principal_briefs()
    (row,) = _pending()
    local = _local(row.run_at, "America/New_York")
    assert (local.strftime("%a"), local.hour, local.minute) == ("Fri", 16, 0)


def test_weekly_time_is_dst_safe() -> None:
    ws.restore_workspace_settings(ws.WorkspaceSettings(mode="solo", timezone="America/New_York"))
    # Fri 30 Oct 2026 16:00 EDT (20:00 UTC) fired; DST ends Sun 1 Nov, so the
    # next Friday 16:00 EST is 21:00 UTC.
    after = datetime(2026, 10, 30, 20, 5, tzinfo=UTC)
    nxt = runner._next_principal_run_at(KIND, after)
    assert nxt == datetime(2026, 11, 6, 21, 0, tzinfo=UTC)


def test_explicit_env_spec_is_read_as_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    ws.restore_workspace_settings(ws.WorkspaceSettings(mode="solo", timezone="Asia/Tokyo"))
    monkeypatch.setenv("PRINCIPAL_WEEKLY_REVIEW_TIME", "weekly@MON@09:30")
    nxt = runner._next_principal_run_at(KIND, datetime(2026, 9, 25, 12, 0, tzinfo=UTC))
    assert nxt == datetime(2026, 9, 28, 9, 30, tzinfo=UTC)


@pytest.mark.parametrize("raw", ["16:00", "weekly@fri@25:00", "weekly@xyz@16:00", "daily@16:00"])
def test_invalid_env_spec_falls_back_to_the_default(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws.restore_workspace_settings(ws.WorkspaceSettings(mode="solo", timezone="Europe/Berlin"))
    monkeypatch.setenv("PRINCIPAL_WEEKLY_REVIEW_TIME", raw)
    nxt = runner._next_principal_run_at(KIND, datetime(2026, 9, 21, 12, 0, tzinfo=UTC))
    assert nxt is not None
    local = _local(nxt.isoformat(), "Europe/Berlin")
    assert (local.strftime("%a"), local.hour) == ("Fri", 16)


def test_switching_modes_seeds_and_cancels() -> None:
    ws.set_workspace_mode("solo")
    (row,) = _pending()
    ws.set_workspace_mode("team")
    assert _pending() == []
    cancelled = episodic.get_scheduled_action(row.id or 0)
    assert cancelled is not None and cancelled.status == "cancelled"
    ws.set_workspace_mode("solo")
    assert len(_pending()) == 1


def test_zone_change_retimes_the_weekly_review_in_place() -> None:
    ws.restore_workspace_settings(ws.WorkspaceSettings(mode="solo", timezone="UTC"))
    runner.seed_principal_briefs()
    ws.set_timezone("America/Los_Angeles")
    (row,) = _pending()
    local = _local(row.run_at, "America/Los_Angeles")
    assert (local.strftime("%a"), local.hour, local.minute) == ("Fri", 16, 0)


def test_chain_never_lands_within_half_a_week() -> None:
    fired = datetime(2026, 9, 25, 16, 0, tzinfo=UTC)
    action = episodic.ScheduledAction(
        created_at=fired.isoformat(), run_at=fired.isoformat(), channel="__internal__",
        channel_ref="principal", intent_text="x", kind=KIND,
    )
    assert runner._chain_after(action) >= fired + timedelta(days=3, hours=12)


# --------------------------------------------------------------------------- #
# The runner: run, store, deliver, record, chain
# --------------------------------------------------------------------------- #


def _fake_review(artifact: str = "REVIEW") -> Any:
    from openexecutive.workflows.base import WorkflowEvent

    class _Fake(WeeklyReviewWorkflow):
        async def run(self, inputs: Any, store: Any):  # type: ignore[override]
            yield WorkflowEvent(type="artifact", content=artifact)
            yield WorkflowEvent(type="done")

    return _Fake()


def _fire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, deliver_ok: bool = True
) -> tuple[int, list[tuple[str, str]], list[str]]:
    from openexecutive.briefing import narrative_cache
    from openexecutive.workflows import persistence as wf_persistence

    monkeypatch.setattr(narrative_cache, "DB_PATH", tmp_path / "cache.db")
    monkeypatch.setattr(wf_persistence, "DB_PATH", episodic.DB_PATH)
    monkeypatch.setitem(WORKFLOW_REGISTRY, "weekly_review", _fake_review())
    sent: list[tuple[str, str]] = []

    async def _deliver(text: str, *, label: str = "") -> runner.PrincipalDelivery:
        sent.append((text, label))
        if deliver_ok:
            return runner.PrincipalDelivery(True, "telegram → 5", "delivered", "telegram")
        return runner.PrincipalDelivery(False, "no channel", "no_channel")

    chained: list[str] = []
    monkeypatch.setattr(runner, "_deliver_to_principal", _deliver)
    monkeypatch.setattr(
        runner, "_enqueue_next_principal_brief", lambda kind, after: chained.append(kind)
    )

    class _Store:
        def __init__(self, **kw: Any) -> None: ...

    monkeypatch.setattr("openexecutive.knowledge.store.ChromaDBStore", _Store)
    action_id = episodic.insert_scheduled_action(
        run_at=datetime.now(UTC).isoformat(), channel="__internal__",
        channel_ref="principal", intent_text="weekly", kind=KIND,
    )
    action = episodic.get_scheduled_action(action_id)
    assert action is not None
    asyncio.run(runner._execute_action(action, None))
    return action_id, sent, chained


def test_runner_runs_stores_delivers_and_chains_in_solo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.briefing import brief_state
    from openexecutive.workflows.persistence import get_run, list_runs

    _solo()
    action_id, sent, chained = _fire(tmp_path, monkeypatch)
    assert sent == [("REVIEW", "Weekly Review")]
    assert chained == [KIND]
    row = episodic.get_scheduled_action(action_id)
    assert row is not None and row.status == "done"
    (run,) = list_runs(workflow_name="weekly_review")
    stored = get_run(run["run_id"])
    assert stored is not None and stored["artifact"] == "REVIEW" and stored["status"] == "done"
    outcome = brief_state.last_delivery_outcome()
    assert outcome is not None
    assert (outcome.kind, outcome.reason, outcome.channel) == (KIND, "delivered", "telegram")
    assert brief_state.brief_name(KIND) == "weekly review"


def test_runner_records_a_review_that_was_not_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.briefing import brief_state

    _solo()
    _fire(tmp_path, monkeypatch, deliver_ok=False)
    outcome = brief_state.last_delivery_outcome()
    assert outcome is not None and (outcome.kind, outcome.reason) == (KIND, "no_channel")


def test_runner_retires_a_review_that_fires_in_team(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    action_id, sent, chained = _fire(tmp_path, monkeypatch)
    assert sent == [] and chained == []
    row = episodic.get_scheduled_action(action_id)
    assert row is not None and row.status == "cancelled" and "solo" in row.last_error
