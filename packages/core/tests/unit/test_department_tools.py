"""Unit tests for openexecutive.orchestrator.department_tools.

These tools let the Executive update department Goal status and progress
from inside a chat turn — closing the loop that Phase A's check-in
workflow opened. Mirrors the shape of test_people_tools.py.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from openexecutive.departments import registry as dept_registry
from openexecutive.departments import store as dept_store
from openexecutive.memory import episodic as episodic_module
from openexecutive.orchestrator.department_tools import (
    handle_list_department_goals,
    handle_update_department_goal,
)


@pytest.fixture(autouse=True)
def shared_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated SQLite for each test, shared by all stores like in prod."""
    db_path = tmp_path / "episodic.db"
    monkeypatch.setattr(dept_store, "DB_PATH", db_path)
    monkeypatch.setattr(episodic_module, "DB_PATH", db_path)
    dept_store.initialize_db()
    dept_registry.invalidate()
    return db_path


def _call(coro_fn, payload: dict[str, Any]) -> dict[str, Any]:
    return json.loads(asyncio.run(coro_fn(payload)))


def _seed_engineering_goal(
    *,
    status: str = "on_track",
    current: str = "0% done",
    key_result: str = "Ship billing migration",
) -> int:
    dept_store.create_department("Engineering")
    return dept_store.insert_goal(
        "engineering",
        period_type="quarter",
        period_value="Q2 2026",
        key_result=key_result,
        target="Cutover by Jun 30",
        current=current,
        status=status,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- #
# list_department_goals
# --------------------------------------------------------------------------- #

def test_list_department_goals_returns_compact_shape() -> None:
    goal_id = _seed_engineering_goal()
    result = _call(handle_list_department_goals, {"department_slug": "engineering"})
    assert result["department_slug"] == "engineering"
    assert result["count"] == 1
    g = result["goals"][0]
    assert g["id"] == goal_id
    assert g["key_result"] == "Ship billing migration"
    assert g["target"] == "Cutover by Jun 30"
    assert g["status"] == "on_track"
    assert g["last_reviewed_at"] == ""
    assert "quarter" in g["period"]


def test_list_department_goals_empty_dept() -> None:
    dept_store.create_department("Empty")
    result = _call(handle_list_department_goals, {"department_slug": "empty"})
    assert result["count"] == 0
    assert result["goals"] == []


def test_list_department_goals_missing_slug() -> None:
    result = _call(handle_list_department_goals, {})
    assert "error" in result


# --------------------------------------------------------------------------- #
# update_department_goal — happy path
# --------------------------------------------------------------------------- #

def test_update_department_goal_happy_path_status_change() -> None:
    goal_id = _seed_engineering_goal(status="on_track")
    result = _call(handle_update_department_goal, {
        "department_slug": "engineering",
        "goal_id": goal_id,
        "status": "at_risk",
        "rationale": "deals slipping into Q3 per pipeline review",
    })
    assert result["status"] == "ok"
    assert result["from_status"] == "on_track"
    assert result["to_status"] == "at_risk"
    assert result["current_updated"] is False

    after = dept_store.get_goal(goal_id)
    assert after is not None
    assert after.status == "at_risk"
    assert after.current == "0% done"  # unchanged
    assert after.last_reviewed_at != ""


def test_update_department_goal_happy_path_current_only() -> None:
    """Bumping `current` without changing `status` is the
    'we made progress, still on track' case."""
    goal_id = _seed_engineering_goal(status="on_track", current="0% done")
    result = _call(handle_update_department_goal, {
        "department_slug": "engineering",
        "goal_id": goal_id,
        "current": "cutover complete",
        "rationale": "user reported the migration shipped this morning",
    })
    assert result["status"] == "ok"
    assert result["from_status"] == "on_track"
    assert result["to_status"] == "on_track"
    assert result["current_updated"] is True

    after = dept_store.get_goal(goal_id)
    assert after is not None
    assert after.current == "cutover complete"
    assert after.status == "on_track"
    assert after.last_reviewed_at != ""


def test_update_department_goal_status_and_current_together() -> None:
    goal_id = _seed_engineering_goal(status="on_track", current="50%")
    result = _call(handle_update_department_goal, {
        "department_slug": "engineering",
        "goal_id": goal_id,
        "status": "off_track",
        "current": "stalled on regulatory review",
        "rationale": "external blocker per legal counsel",
    })
    assert result["status"] == "ok"
    after = dept_store.get_goal(goal_id)
    assert after is not None
    assert after.status == "off_track"
    assert after.current == "stalled on regulatory review"


# --------------------------------------------------------------------------- #
# update_department_goal — validation errors
# --------------------------------------------------------------------------- #

def test_update_department_goal_missing_rationale_rejected() -> None:
    goal_id = _seed_engineering_goal()
    result = _call(handle_update_department_goal, {
        "department_slug": "engineering",
        "goal_id": goal_id,
        "status": "at_risk",
    })
    assert "error" in result
    assert "rationale" in result["error"]
    # Row unchanged.
    assert dept_store.get_goal(goal_id).status == "on_track"  # type: ignore[union-attr]


def test_update_department_goal_neither_status_nor_current_rejected() -> None:
    goal_id = _seed_engineering_goal()
    result = _call(handle_update_department_goal, {
        "department_slug": "engineering",
        "goal_id": goal_id,
        "rationale": "wanted to call the tool just to call it",
    })
    assert "error" in result
    assert "status" in result["error"] and "current" in result["error"]
    assert dept_store.get_goal(goal_id).status == "on_track"  # type: ignore[union-attr]


def test_update_department_goal_invalid_status_rejected() -> None:
    goal_id = _seed_engineering_goal()
    result = _call(handle_update_department_goal, {
        "department_slug": "engineering",
        "goal_id": goal_id,
        "status": "in_progress",  # not a valid enum value
        "rationale": "made-up status",
    })
    assert "error" in result
    assert dept_store.get_goal(goal_id).status == "on_track"  # type: ignore[union-attr]


def test_update_department_goal_missing_goal_rejected() -> None:
    result = _call(handle_update_department_goal, {
        "department_slug": "engineering",
        "goal_id": 99999,
        "status": "at_risk",
        "rationale": "ghost goal",
    })
    assert "error" in result
    assert "not found" in result["error"]


def test_update_department_goal_cross_department_id_blocked() -> None:
    """Goal id from engineering must NOT be writable as 'finance'."""
    eng_goal = _seed_engineering_goal()
    dept_store.create_department("Finance")
    result = _call(handle_update_department_goal, {
        "department_slug": "finance",
        "goal_id": eng_goal,
        "status": "off_track",
        "rationale": "trying to mutate the wrong dept",
    })
    assert "error" in result
    assert "engineering" in result["error"]
    # Engineering row unchanged.
    after = dept_store.get_goal(eng_goal)
    assert after is not None
    assert after.status == "on_track"


def test_update_department_goal_boolean_id_rejected() -> None:
    """`isinstance(True, int)` is True in Python — explicit reject."""
    _seed_engineering_goal()
    result = _call(handle_update_department_goal, {
        "department_slug": "engineering",
        "goal_id": True,
        "status": "at_risk",
        "rationale": "should fail",
    })
    assert "error" in result


def test_update_department_goal_empty_rationale_rejected() -> None:
    goal_id = _seed_engineering_goal()
    result = _call(handle_update_department_goal, {
        "department_slug": "engineering",
        "goal_id": goal_id,
        "status": "at_risk",
        "rationale": "   ",  # whitespace-only
    })
    assert "error" in result


# --------------------------------------------------------------------------- #
# Audit logging
# --------------------------------------------------------------------------- #

def test_update_department_goal_emits_tool_invocation_audit_row() -> None:
    goal_id = _seed_engineering_goal(status="on_track")
    fake_log = MagicMock()
    with patch("openexecutive.audit.log_event", fake_log):
        result = _call(handle_update_department_goal, {
            "department_slug": "engineering",
            "goal_id": goal_id,
            "status": "at_risk",
            "rationale": "deals slipping",
        })
    assert result["status"] == "ok"
    # The handler may call _audit multiple times (e.g. on different paths);
    # find the write success row.
    write_calls = [
        c for c in fake_log.call_args_list
        if c.args and c.args[0] == "tool_invocation"
        and c.kwargs.get("details", {}).get("tool") == "update_department_goal"
        and c.kwargs.get("details", {}).get("ok") is True
    ]
    assert len(write_calls) == 1
    details = write_calls[0].kwargs["details"]
    assert details["from_status"] == "on_track"
    assert details["to_status"] == "at_risk"
    assert details["goal_id"] == goal_id
    assert details["department_slug"] == "engineering"
    assert details["rationale"] == "deals slipping"
    assert write_calls[0].kwargs["actor"] == "executive"


def test_update_department_goal_audit_row_tagged_with_department() -> None:
    """The `department=` top-level kwarg on log_event lets dept-scoped
    audit queries (audit_logger.query(department=slug)) pick this row up
    — same plumbing the department_check_in workflow uses."""
    goal_id = _seed_engineering_goal(status="on_track")
    fake_log = MagicMock()
    with patch("openexecutive.audit.log_event", fake_log):
        _call(handle_update_department_goal, {
            "department_slug": "engineering",
            "goal_id": goal_id,
            "status": "at_risk",
            "rationale": "deal slipping",
        })
    write_calls = [
        c for c in fake_log.call_args_list
        if c.args and c.args[0] == "tool_invocation"
        and c.kwargs.get("details", {}).get("ok") is True
    ]
    assert len(write_calls) == 1
    assert write_calls[0].kwargs.get("department") == "engineering"


def test_update_department_goal_failed_input_audited_as_not_ok() -> None:
    fake_log = MagicMock()
    with patch("openexecutive.audit.log_event", fake_log):
        _call(handle_update_department_goal, {
            "department_slug": "engineering",
            "goal_id": "not-an-int",
            "rationale": "x",
        })
    bad_calls = [
        c for c in fake_log.call_args_list
        if c.args and c.args[0] == "tool_invocation"
        and c.kwargs.get("details", {}).get("tool") == "update_department_goal"
        and c.kwargs.get("details", {}).get("ok") is False
    ]
    assert len(bad_calls) >= 1


# --------------------------------------------------------------------------- #
# Tool definitions present in the executive registry
# --------------------------------------------------------------------------- #

def test_department_tools_wired_into_executive_registry() -> None:
    """Smoke: the Executive's tool registry includes both new tools, and
    the handlers are wired correctly."""
    from openexecutive.orchestrator.executive import (
        _ALL_SKILL_HANDLERS,
        _ALL_SKILL_TOOLS,
    )
    names = {t["name"] for t in _ALL_SKILL_TOOLS}
    assert "list_department_goals" in names
    assert "update_department_goal" in names
    assert _ALL_SKILL_HANDLERS["list_department_goals"] is handle_list_department_goals
    assert _ALL_SKILL_HANDLERS["update_department_goal"] is handle_update_department_goal


def test_tool_list_sorts_stably_by_name() -> None:
    """The Executive sorts tools by name before sending them to Anthropic —
    so prompt-cache layout stays stable across deploys. The sort must
    handle the new tools without surprises (alphabetical insertion)."""
    from openexecutive.orchestrator.executive import _ALL_SKILL_TOOLS
    sorted_names = [t["name"] for t in sorted(_ALL_SKILL_TOOLS, key=lambda t: t["name"])]
    # Pure assertion: sorted order is stable and includes our tools.
    assert sorted_names == sorted(sorted_names)
    assert "list_department_goals" in sorted_names
    assert "update_department_goal" in sorted_names


# --------------------------------------------------------------------------- #
# create_goal — a new goal the user states, filed under an area
# --------------------------------------------------------------------------- #

from openexecutive.audit.context import set_turn  # noqa: E402
from openexecutive.orchestrator import department_tools  # noqa: E402

_REAL_MAY_ADD_AREA = department_tools._may_add_area
from openexecutive.orchestrator.department_tools import (  # noqa: E402
    CREATE_GOAL_TOOL,
    default_period_value,
    handle_create_goal,
)


def _create(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "area": "Engineering",
        "key_result": "Ship the mobile app",
        "target": "Live in the App Store by Nov 15",
        "rationale": "User said the app ships by November 15.",
        **overrides,
    }
    return _call(handle_create_goal, payload)


@pytest.fixture(autouse=True)
def _fresh_turn_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(department_tools, "_goals_created_by_turn", {})
    # Most tests speak as the verified principal; the gate has its own tests.
    monkeypatch.setattr(department_tools, "_may_add_area", lambda: True)


def test_create_goal_resolves_area_by_slug_and_by_title() -> None:
    dept_store.create_department("Engineering")
    by_slug = _create(area="engineering")
    by_title = _create(area="  ENGINEERING ", key_result="Hire a contractor")
    assert by_slug["status"] == by_title["status"] == "ok"
    assert by_slug["area_slug"] == by_title["area_slug"] == "engineering"
    assert by_slug["area_created"] is False and by_title["area_created"] is False
    goals = dept_store.list_goals("engineering")
    assert [g.key_result for g in goals] == ["Ship the mobile app", "Hire a contractor"]
    assert goals[0].target == "Live in the App Store by Nov 15"
    assert goals[0].status == "on_track"
    assert goals[0].period_type == "quarter"
    assert len(dept_store.list_departments()) == 1


def test_create_goal_matches_a_title_that_does_not_slug_to_its_slug() -> None:
    # The seeded `hr` department is titled "People & Talent".
    dept_store.seed_default_departments()
    result = _create(area="People & Talent", key_result="Hire a designer")
    assert result["area_slug"] == "hr" and result["area_created"] is False


def test_create_goal_creates_a_missing_area_and_maps_obvious_specialists() -> None:
    sales = _create(area="Sales", key_result="Paying clients", target="20 by Dec 31")
    assert sales["status"] == "ok" and sales["area_created"] is True
    assert sales["area_slug"] == "sales" and sales["area_title"] == "Sales"
    assert dept_store.get_department("sales").config.specialist_key == "sales"  # type: ignore[union-attr]

    other = _create(area="Client work", key_result="Retainers", target="3 signed")
    assert other["area_slug"] == "client-work" and other["area_created"] is True
    # Not a specialist's domain: informational.
    assert dept_store.get_department("client-work").config.specialist_key is None  # type: ignore[union-attr]

    # A bare slug from the model reads as words.
    slugged = _create(area="customer_success", key_result="Churn", target="under 2%")
    assert slugged["area_title"] == "Customer Success"
    assert slugged["area_slug"] == "customer-success"


def test_create_goal_never_gives_a_second_department_the_same_specialist() -> None:
    dept_store.seed_default_departments()
    dept_store.delete_department("finance")
    # "Finance" was deleted, so a new Finance area gets the CFO back…
    first = _create(area="Finance", key_result="Runway", target="12 months")
    assert dept_store.get_department(first["area_slug"]).config.specialist_key == "cfo"  # type: ignore[union-attr]
    # …but a specialist already wired to a department stays with it: "People
    # Talent" matches no department yet slugs like the HR default's title.
    second = _create(area="People Talent", key_result="Hire a designer", target="by Q1")
    assert second["area_created"] is True and second["area_slug"] == "people-talent"
    assert dept_store.get_department("hr").config.specialist_key == "chro"  # type: ignore[union-attr]
    assert dept_store.get_department("people-talent").config.specialist_key is None  # type: ignore[union-attr]


def test_create_goal_honours_period_status_and_current() -> None:
    dept_store.create_department("Engineering")
    result = _create(
        period_type="month", period_value="November 2026",
        status="at_risk", current="Beta in TestFlight",
    )
    goal = dept_store.get_goal(result["goal_id"])
    assert goal is not None
    assert (goal.period_type, goal.period_value) == ("month", "November 2026")
    assert goal.status == "at_risk" and goal.current == "Beta in TestFlight"


def test_create_goal_defaults_the_period_to_the_current_one(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import date

    monkeypatch.setattr(department_tools, "_today_local", lambda: date(2026, 9, 25))
    result = _create(area="Engineering")
    goal = dept_store.get_goal(result["goal_id"])
    assert goal is not None and goal.period_value == "Q3 2026"
    today = date(2026, 9, 25)  # a Friday
    assert default_period_value("week", today) == "Week of Sep 21"
    assert default_period_value("month", today) == "September 2026"
    assert default_period_value("quarter", date(2026, 12, 31)) == "Q4 2026"
    assert default_period_value("year", today) == "2026"
    assert default_period_value("ongoing", today) == "Ongoing"


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"area": "  "}, "area is required"),
        ({"key_result": ""}, "key_result and target"),
        ({"target": None}, "key_result and target"),
        ({"rationale": " "}, "rationale is required"),
        ({"status": "done"}, "status must be one of"),
        ({"period_type": "decade"}, "period_type must be one of"),
        ({"area": "x" * 129}, "area must be at most"),
        ({"target": "x" * 513}, "target must be at most"),
        ({"period_value": "x" * 65}, "period_value must be at most"),
    ],
)
def test_create_goal_validates_input(overrides: dict[str, Any], fragment: str) -> None:
    result = _create(**overrides)
    assert fragment in result["error"]
    assert dept_store.list_departments() == []


def test_create_goal_refuses_a_goal_the_area_already_tracks() -> None:
    goal_id = _seed_engineering_goal(key_result="Ship billing migration")
    result = _create(area="engineering", key_result="  ship billing-migration ")
    assert "already tracks" in result["error"]
    assert str(goal_id) in result["error"] and "update_department_goal" in result["error"]
    assert len(dept_store.list_goals("engineering")) == 1


def test_create_goal_refuses_a_sweep_within_one_turn() -> None:
    dept_store.create_department("Engineering")
    cap = department_tools._MAX_GOALS_PER_TURN
    with set_turn(session_id="s1", turn_id="t1"):
        results = [_create(key_result=f"Goal {i}") for i in range(cap + 1)]
    assert all(r.get("status") == "ok" for r in results[:cap])
    assert "refused" in results[cap]["error"]
    assert len(dept_store.list_goals("engineering")) == cap
    # A new turn starts a new count.
    with set_turn(session_id="s1", turn_id="t2"):
        assert _create(key_result="Next turn goal")["status"] == "ok"


def test_create_goal_audits_the_write_with_its_rationale() -> None:
    fake_log = MagicMock()
    with patch("openexecutive.audit.log_event", fake_log):
        result = _create(area="Sales", key_result="Paying clients", target="20 by Dec 31",
                         rationale="User set 20 paying clients by year end.")
    ok_rows = [
        c for c in fake_log.call_args_list
        if c.args and c.args[0] == "tool_invocation"
        and c.kwargs.get("details", {}).get("tool") == "create_goal"
        and c.kwargs.get("details", {}).get("ok") is True
    ]
    assert len(ok_rows) == 1
    details = ok_rows[0].kwargs["details"]
    assert details["goal_id"] == result["goal_id"]
    assert details["department_slug"] == "sales"
    assert details["area_created"] is True
    assert details["rationale"] == "User set 20 paying clients by year end."
    assert ok_rows[0].kwargs["department"] == "sales"
    assert ok_rows[0].kwargs["actor"] == "executive"


def test_create_goal_bad_input_is_audited_as_not_ok() -> None:
    fake_log = MagicMock()
    with patch("openexecutive.audit.log_event", fake_log):
        _create(rationale="")
    assert any(
        c.kwargs.get("details", {}).get("tool") == "create_goal"
        and c.kwargs.get("details", {}).get("ok") is False
        for c in fake_log.call_args_list
    )


def test_create_goal_invalidates_the_registry_so_the_next_turn_sees_it() -> None:
    dept_store.create_department("Engineering")
    assert dept_registry.get_state("engineering").goals == []  # type: ignore[union-attr]
    _create()
    assert [g.key_result for g in dept_registry.get_state("engineering").goals] == [  # type: ignore[union-attr]
        "Ship the mobile app"
    ]


def test_create_goal_schema_is_static_and_the_tool_list_sorted() -> None:
    """The cached tool prefix must not move when areas change: no enum of
    current departments in the schema, and the registry sorts cleanly."""
    from openexecutive.orchestrator.executive import _ALL_SKILL_HANDLERS, _ALL_SKILL_TOOLS

    before = json.dumps(CREATE_GOAL_TOOL, sort_keys=True)
    _create(area="Brand New Area")
    assert json.dumps(CREATE_GOAL_TOOL, sort_keys=True) == before
    assert "enum" not in CREATE_GOAL_TOOL["input_schema"]["properties"]["area"]

    names = [t["name"] for t in _ALL_SKILL_TOOLS]
    assert "create_goal" in names and len(names) == len(set(names))
    assert _ALL_SKILL_HANDLERS["create_goal"] is handle_create_goal
    ordered = [t["name"] for t in sorted(_ALL_SKILL_TOOLS, key=lambda t: t["name"])]
    assert ordered == sorted(names)


def test_create_goal_is_not_offered_to_the_unattended_passes() -> None:
    import inspect

    from openexecutive.workflows import executive_reflection
    from openexecutive.workflows.executive_research import _SYNTHESIS_EXCLUDED_TOOLS

    assert "create_goal" in _SYNTHESIS_EXCLUDED_TOOLS
    assert '"create_goal"' in inspect.getsource(executive_reflection.ExecutiveReflectionWorkflow.run)


def test_create_goal_chip_names_the_area_and_links_it() -> None:
    from openexecutive.orchestrator.action_chips import summarize_action

    chip = summarize_action(
        tool_name="create_goal",
        tool_input={"area": "sales", "key_result": "Paying clients"},
        tool_result=json.dumps({"status": "ok", "goal_id": 3, "area_slug": "sales",
                                "area_title": "Sales", "area_created": True}),
    )
    assert chip is not None
    assert chip["summary"] == "Added a new Sales area goal: Paying clients"
    assert chip["link"] == "/departments/sales"
    refused = summarize_action(
        tool_name="create_goal", tool_input={"area": "sales"},
        tool_result=json.dumps({"error": "refused"}),
    )
    assert refused is None


def test_solo_persona_teaches_create_goal_and_team_persona_does_not() -> None:
    from openexecutive.prompts.executive_persona import (
        EXECUTIVE_PERSONA_PROMPT,
        EXECUTIVE_PERSONA_SOLO_PROMPT,
    )

    assert "`create_goal`" in EXECUTIVE_PERSONA_SOLO_PROMPT
    # Team mode learns the tool from its description alone; the team persona
    # stays byte-identical (its sha256 is pinned elsewhere).
    assert "create_goal" not in EXECUTIVE_PERSONA_PROMPT
    assert "create_goal" in json.dumps(CREATE_GOAL_TOOL)


def test_area_specialists_are_registered_and_obvious() -> None:
    from openexecutive.orchestrator.router import SPECIALIST_REGISTRY

    assert set(dept_store._AREA_SPECIALISTS.values()) <= set(SPECIALIST_REGISTRY)
    assert dept_store.specialist_key_for_area("Marketing") == "cmo"
    assert dept_store.specialist_key_for_area("product") == "cpo"
    assert dept_store.specialist_key_for_area("Sales") == "sales"
    assert dept_store.specialist_key_for_area("Client work") is None


def test_match_department_prefers_a_slug_match_over_a_title_match() -> None:
    dept_store.create_department("Growth")
    dept_store.create_department("Ops")
    dept_store.update_department("ops", title="growth")  # a title equal to another's slug
    configs = [d.config for d in dept_store.list_departments()]
    assert dept_store.match_department("growth", configs).slug == "growth"  # type: ignore[union-attr]
    assert dept_store.match_department("GROWTH ", configs).slug == "growth"  # type: ignore[union-attr]
    assert dept_store.match_department("", configs) is None
    assert dept_store.match_department("Legal", configs) is None


def test_create_goal_eval_scenario_is_shipped_and_valid() -> None:
    from openexecutive.evals.scenarios import validate_scenario_yaml
    from openexecutive.orchestrator import department_tools as mod

    path = Path(mod.__file__).parents[1] / "evals" / "_scenarios" / "department_goal_update_005.yaml"
    scenario = validate_scenario_yaml(path.read_text(encoding="utf-8"))
    assert scenario["expected_tool_calls"] == ["create_goal"]


def test_only_the_verified_principal_may_add_an_area(
    shared_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A goal in an existing area is as open as update_department_goal, but a
    new department is org structure: only the principal, on a surface that
    confirms it is them, may add one (the roster tools' rule)."""
    from openexecutive.orchestrator.schedule_tools import set_session
    from openexecutive.orchestrator.session import Session
    from openexecutive.people import registry as people_registry
    from openexecutive.people import store as people_store

    # The real gate, not the autouse stand-in.
    monkeypatch.setattr(department_tools, "_may_add_area", _REAL_MAY_ADD_AREA)
    monkeypatch.setattr(people_store, "DB_PATH", shared_db)
    people_store.initialize_db(shared_db)
    people_registry.invalidate()
    principal = people_store.upsert_person(full_name="Pat Lee", is_principal=True)
    teammate = people_store.upsert_person(full_name="Sam Ortiz")
    dept_store.create_department("Engineering")

    def _as(session: Session | None) -> dict[str, Any]:
        with set_session(session):
            return _create(area="Partnerships", key_result="Signed partners", target="3 by Q1")

    # Unverified surfaces and anyone else: refused, nothing created.
    for session in (
        None,
        Session(caller_person_id=principal),  # e.g. an email turn
        Session(from_web_chat=True, caller_person_id=teammate),
    ):
        refused = _as(session)
        assert "refused" in refused["error"] and "principal" in refused["error"]
    assert dept_store.get_department("partnerships") is None
    # An existing area stays open to any speaker.
    with set_session(Session(caller_person_id=teammate)):
        assert _create(area="engineering", key_result="Faster CI")["status"] == "ok"
    # The principal in the web chat adds the area.
    added = _as(Session(from_web_chat=True, caller_person_id=principal))
    assert added.get("status") == "ok", added
    assert added["area_created"] is True
    people_registry.invalidate()
