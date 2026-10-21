"""Goals as their own thing: an owner, an optional department, and a weekly
review for the goals no department check-in covers.

Covers the store (migration, goals with no department, delete detaches),
``departments.goals`` (owners, which goals go unreviewed),
``departments.goal_review``, the ``/goals`` routes, the Executive's goal tools
without a department, the org block, the brief's goal row and the action chip.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.departments import goal_review, goals, registry, store
from openexecutive.memory import episodic
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """One SQLite file for departments, people and audit rows, as in prod."""
    from openexecutive.audit import AuditLogger, set_audit_logger

    path = tmp_path / "episodic.db"
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(people_store, "DB_PATH", path)
    monkeypatch.setattr(episodic, "DB_PATH", path)
    episodic.initialize_db(path)
    store.initialize_db()
    people_store.initialize_db(path)
    registry.invalidate()
    people_registry.invalidate()
    set_audit_logger(AuditLogger(db_path=path))
    yield path
    set_audit_logger(None)
    registry.invalidate()
    people_registry.invalidate()


def _goal(slug: str | None, key_result: str = "Win our first customers", **kw: Any) -> int:
    return store.insert_goal(
        slug,
        period_type=kw.pop("period_type", "year"),
        period_value=kw.pop("period_value", "2026"),
        key_result=key_result,
        target=kw.pop("target", "3 paying"),
        **kw,
    )


def _people() -> tuple[int, int]:
    principal = people_store.upsert_person(full_name="Rufus", is_principal=True)
    dan = people_store.upsert_person(full_name="Dan")
    return principal, dan


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #


def test_an_old_goals_table_is_rebuilt_with_an_optional_department(tmp_path: Path) -> None:
    """A database from before this change (department_slug NOT NULL, no
    owner) keeps its goals and gains both."""
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE department_goals (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "department_slug TEXT NOT NULL, period_type TEXT NOT NULL DEFAULT 'quarter', "
            "period_value TEXT NOT NULL, key_result TEXT NOT NULL, target TEXT NOT NULL "
            "DEFAULT '', current TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT "
            "'on_track', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
            "last_reviewed_at TEXT NOT NULL DEFAULT '')"
        )
        conn.execute(
            "INSERT INTO department_goals (department_slug, period_value, key_result, "
            "created_at, updated_at) VALUES ('sales', 'Q4 2026', 'Close 10', 'x', 'x')"
        )
    store.initialize_db(path)
    with sqlite3.connect(path) as conn:
        cols = {r[1]: r for r in conn.execute("PRAGMA table_info(department_goals)")}
    assert cols["department_slug"][3] == 0  # notnull flag cleared
    assert "owner_person_id" in cols
    [kept] = store.list_goals("sales", db_path=path)
    assert kept.key_result == "Close 10" and kept.owner_person_id is None
    # A goal with no department now fits, and initialize is idempotent.
    store.initialize_db(path)
    _goal(None, db_path=path)
    assert [g.key_result for g in store.list_goals(None, db_path=path)] == ["Win our first customers"]


def test_goals_with_no_department_are_listed_on_their_own() -> None:
    store.create_department("Sales")
    in_sales = _goal("sales", "Close 10")
    loose = _goal(None)
    assert [g.id for g in store.list_goals(None)] == [loose]
    assert [g.id for g in store.list_goals("sales")] == [in_sales]
    assert [g.id for g in store.list_all_goals()] == [in_sales, loose]
    assert store.get_goal(loose).department_slug is None  # type: ignore[union-attr]


def test_update_goal_moves_and_clears_owner_and_department() -> None:
    _, dan = _people()
    store.create_department("Sales")
    gid = _goal(None)
    store.update_goal(gid, owner_person_id=dan, department_slug="sales")
    moved = store.get_goal(gid)
    assert moved is not None and (moved.owner_person_id, moved.department_slug) == (dan, "sales")
    # Left out = unchanged; None = cleared.
    store.update_goal(gid, status="at_risk")
    assert store.get_goal(gid).owner_person_id == dan  # type: ignore[union-attr]
    store.update_goal(gid, owner_person_id=None, department_slug=None)
    cleared = store.get_goal(gid)
    assert cleared is not None and (cleared.owner_person_id, cleared.department_slug) == (None, None)


def test_deleting_a_department_keeps_its_goals_on_their_owners() -> None:
    principal, dan = _people()
    store.create_department("Sales")
    store.update_department("sales", head_person_id=dan)
    follows_lead = _goal("sales", "Close 10")
    own_owner = _goal("sales", "Hire a rep", owner_person_id=principal)
    assert store.delete_department("sales")
    a, b = store.get_goal(follows_lead), store.get_goal(own_owner)
    assert a is not None and b is not None
    assert (a.department_slug, a.owner_person_id) == (None, dan)  # the lead takes it
    assert (b.department_slug, b.owner_person_id) == (None, principal)


# --------------------------------------------------------------------------- #
# Owners and review coverage
# --------------------------------------------------------------------------- #


def test_owner_is_the_goals_own_else_the_lead_else_the_principal() -> None:
    principal, dan = _people()
    store.create_department("Sales")
    store.create_department("Ops")
    store.update_department("sales", head_person_id=dan)
    ids = {
        "own": _goal("ops", "A", owner_person_id=dan),
        "lead": _goal("sales", "B"),
        "headless": _goal("ops", "C"),
        "loose": _goal(None, "D"),
    }
    views = {v.goal.id: v for v in goals.list_goal_views()}
    assert views[ids["own"]].owner_name == "Dan"
    assert views[ids["lead"]].owner_id == dan
    assert views[ids["headless"]].owner_id == principal
    assert views[ids["loose"]].owner_name == "Rufus"
    assert views[ids["lead"]].department_title == "Sales"
    assert views[ids["loose"]].department_title is None


def test_an_owner_who_left_the_team_is_not_named() -> None:
    """A contact is private to the principal and goal views reach teammates,
    so an owner later made a contact (or archived) shows no name."""
    _, dan = _people()
    gid = _goal(None, owner_person_id=dan)
    people_store.update_person(dan, kind="contact")
    [view] = [v for v in goals.list_goal_views() if v.goal.id == gid]
    assert view.owner_id == dan and view.owner_name == ""


def test_unreviewed_goals_are_those_no_check_in_grades() -> None:
    store.create_department("Finance", specialist_key="cfo")
    store.create_department("Client work")  # no specialist
    graded = _goal("finance", "Runway")
    no_specialist = _goal("client-work", "Ship Acme")
    loose = _goal(None, "Launch")
    found = {g.id for g in goals.unreviewed_goals(registry.list_states(force_refresh=True))}
    assert found == {no_specialist, loose}
    assert graded not in found


# --------------------------------------------------------------------------- #
# The weekly goal review
# --------------------------------------------------------------------------- #


def test_goal_review_grades_uncovered_goals_and_skips_fresh_ones(monkeypatch: pytest.MonkeyPatch) -> None:
    store.create_department("Finance", specialist_key="cfo")
    covered = _goal("finance", "Runway")
    loose = _goal(None, "Launch")
    calls: list[dict[str, Any]] = []

    async def fake_route(**kw: Any) -> str:
        calls.append(kw)
        return json.dumps({
            "verdicts": [{"goal_id": loose, "status": "at_risk", "rationale": "No date set."}],
            "narrative": "One slipping.",
        })

    monkeypatch.setattr("openexecutive.orchestrator.router.route_to_specialist", fake_route)
    # Wall-clock now: the goals' updated_at is stamped with it.
    now = datetime.now(UTC)
    assert asyncio.run(goal_review.run_review(now)) == 1
    assert calls[0]["specialist_name"] == goals.FALLBACK_REVIEWER
    assert f"[id={loose}]" in calls[0]["query"] and f"[id={covered}]" not in calls[0]["query"]
    graded = store.get_goal(loose)
    assert graded is not None and graded.status == "at_risk" and graded.last_reviewed_at
    assert store.get_goal(covered).last_reviewed_at == ""  # type: ignore[union-attr]

    # Graded a day ago and untouched since: no model call.
    calls.clear()
    assert asyncio.run(goal_review.run_review(now + timedelta(days=1))) == 0
    assert calls == []
    # A week on, it is due again.
    asyncio.run(goal_review.run_review(now + timedelta(days=7)))
    assert len(calls) == 1


def test_goal_review_with_nothing_to_grade_makes_no_call(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(**_kw: Any) -> str:
        raise AssertionError("no model call expected")

    monkeypatch.setattr("openexecutive.orchestrator.router.route_to_specialist", boom)
    assert asyncio.run(goal_review.run_review()) == 0


def test_goal_review_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _goal(None)

    async def boom(**_kw: Any) -> str:
        raise RuntimeError("model down")

    monkeypatch.setattr("openexecutive.orchestrator.router.route_to_specialist", boom)
    assert asyncio.run(goal_review.run_review()) == 0


def test_goal_review_is_a_weekly_kind_in_both_modes() -> None:
    from openexecutive.scheduler import runner

    assert runner.GOAL_REVIEW_KIND in runner._RECURRING_KIND_ENV
    assert runner.GOAL_REVIEW_KIND in runner._WEEKLY_KINDS
    assert runner.GOAL_REVIEW_KIND not in runner._SOLO_ONLY_KINDS
    assert runner._kind_runs_in(runner.GOAL_REVIEW_KIND, "team")
    assert runner._kind_runs_in(runner.GOAL_REVIEW_KIND, "solo")


def test_the_scheduler_runs_the_review_and_chains_next_week(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.memory.episodic import ScheduledAction
    from openexecutive.scheduler import runner

    ran: list[datetime] = []
    done: list[int] = []
    chained: list[tuple[str, datetime]] = []

    async def fake_review(now: datetime | None = None) -> int:
        ran.append(now)  # type: ignore[arg-type]
        return 0

    monkeypatch.setattr(goal_review, "run_review", fake_review)
    monkeypatch.setattr(runner, "mark_action_done", done.append)
    monkeypatch.setattr(
        runner, "_enqueue_next_principal_brief",
        lambda kind, after: chained.append((kind, after)),
    )
    now = datetime(2026, 10, 19, 9, tzinfo=UTC)
    action = ScheduledAction(
        id=7, kind=runner.GOAL_REVIEW_KIND, run_at=now.isoformat(), channel="__internal__",
        channel_ref="", intent_text="x", created_at=now.isoformat(),
    )
    asyncio.run(runner._run_goal_review(action, now))
    assert ran == [now] and done == [7]
    assert chained and chained[0][0] == runner.GOAL_REVIEW_KIND


# --------------------------------------------------------------------------- #
# /goals routes
# --------------------------------------------------------------------------- #


@pytest.fixture()
def client() -> TestClient:
    from openexecutive.api.routes import departments as departments_route

    app = FastAPI()
    app.include_router(departments_route.router)
    return TestClient(app)


def test_goals_route_lists_owners_and_departments(client: TestClient) -> None:
    _, dan = _people()
    store.create_department("Sales")
    created = client.post("/goals", json={"key_result": "Close 10", "area": "Sales",
                                          "owner_person_id": dan})
    assert created.status_code == 201, created.text
    client.post("/goals", json={"key_result": "Launch"})
    listed = {g["key_result"]: g for g in client.get("/goals").json()}
    assert listed["Close 10"]["owner_name"] == "Dan"
    assert listed["Close 10"]["department_title"] == "Sales"
    assert listed["Launch"]["owner_name"] == "Rufus"
    assert listed["Launch"]["department_slug"] is None


def test_goal_owner_must_be_on_the_team(client: TestClient) -> None:
    _people()
    contact = people_store.upsert_person(full_name="Acme buyer", kind="contact")
    archived = people_store.upsert_person(full_name="Gone")
    people_store.archive_person(archived)
    for owner in (contact, archived, 9999):
        resp = client.post("/goals", json={"key_result": "X", "owner_person_id": owner})
        assert resp.status_code == 422, owner


def test_patch_and_delete_reach_any_goal(client: TestClient) -> None:
    _, dan = _people()
    store.create_department("Sales")
    gid = client.post("/goals", json={"key_result": "Launch"}).json()["id"]
    moved = client.patch(f"/goals/{gid}", json={"department_slug": "sales", "owner_person_id": dan})
    assert moved.status_code == 200, moved.text
    assert (moved.json()["department_slug"], moved.json()["owner_person_id"]) == ("sales", dan)
    # Only what is sent changes; null clears.
    client.patch(f"/goals/{gid}", json={"status": "at_risk"})
    assert store.get_goal(gid).owner_person_id == dan  # type: ignore[union-attr]
    cleared = client.patch(f"/goals/{gid}", json={"department_slug": None})
    assert cleared.json()["department_slug"] is None
    assert client.patch(f"/goals/{gid}", json={"department_slug": "nope"}).status_code == 422
    assert client.delete(f"/goals/{gid}").status_code == 204
    assert store.get_goal(gid) is None
    assert client.delete(f"/goals/{gid}").status_code == 404


def test_department_routes_still_reach_only_their_own_goals(client: TestClient) -> None:
    store.create_department("Sales")
    loose = _goal(None)
    assert client.patch(f"/departments/sales/goals/{loose}", json={"status": "at_risk"}).status_code == 404
    assert client.delete(f"/departments/sales/goals/{loose}").status_code == 404


# --------------------------------------------------------------------------- #
# The Executive's goal tools
# --------------------------------------------------------------------------- #


def _tool(handler: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return json.loads(asyncio.run(handler(payload)))


@pytest.fixture()
def as_principal(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.orchestrator import department_tools

    monkeypatch.setattr(department_tools, "_goals_created_by_turn", {})
    monkeypatch.setattr(department_tools, "_principal_asked", lambda: True)


def test_update_tool_reaches_a_goal_with_no_department() -> None:
    from openexecutive.orchestrator.department_tools import handle_update_department_goal

    gid = _goal(None)
    result = _tool(handle_update_department_goal,
                   {"goal_id": gid, "status": "at_risk", "rationale": "No customer signed yet."})
    assert result["status"] == "ok" and result["department_slug"] is None
    assert store.get_goal(gid).status == "at_risk"  # type: ignore[union-attr]
    # Naming a department it is not in is still refused.
    store.create_department("Sales")
    wrong = _tool(handle_update_department_goal,
                  {"department_slug": "sales", "goal_id": gid, "status": "on_track",
                   "rationale": "x"})
    assert "no department" in wrong["error"]


def test_create_tool_without_an_area_makes_no_department(as_principal: None) -> None:
    from openexecutive.orchestrator.department_tools import handle_create_goal

    _, dan = _people()
    result = _tool(handle_create_goal, {
        "key_result": "Ship the mobile apps", "target": "In both stores",
        "rationale": "Principal set it.", "owner_person_id": dan,
    })
    assert result["status"] == "ok", result
    assert result["area_slug"] is None and result["area_created"] is False
    goal = store.get_goal(result["goal_id"])
    assert goal is not None and goal.department_slug is None and goal.owner_person_id == dan
    assert store.list_departments() == []
    dupe = _tool(handle_create_goal, {
        "key_result": "ship the mobile-apps", "target": "x", "rationale": "again",
    })
    assert "already tracks" in dupe["error"]


def test_create_tool_refuses_an_owner_off_the_team(as_principal: None) -> None:
    from openexecutive.orchestrator.department_tools import handle_create_goal

    _people()
    contact = people_store.upsert_person(full_name="Acme buyer", kind="contact")
    for owner in (contact, 9999, True, "Dan"):
        result = _tool(handle_create_goal, {
            "key_result": "X", "target": "Y", "rationale": "Z", "owner_person_id": owner,
        })
        assert "error" in result, owner
    assert store.list_all_goals() == []


def test_goal_tool_schemas_need_no_department() -> None:
    from openexecutive.orchestrator.department_tools import (
        CREATE_GOAL_TOOL,
        LIST_DEPARTMENT_GOALS_TOOL,
        UPDATE_DEPARTMENT_GOAL_TOOL,
    )

    assert LIST_DEPARTMENT_GOALS_TOOL["input_schema"]["required"] == []
    assert "department_slug" not in UPDATE_DEPARTMENT_GOAL_TOOL["input_schema"]["required"]
    assert "area" not in CREATE_GOAL_TOOL["input_schema"]["required"]
    assert "owner_person_id" in CREATE_GOAL_TOOL["input_schema"]["properties"]


def test_create_goal_chip_links_a_goal_with_no_department_to_goals() -> None:
    from openexecutive.orchestrator.action_chips import summarize_action

    chip = summarize_action(
        tool_name="create_goal", tool_input={"key_result": "Launch"},
        tool_result=json.dumps({"status": "ok", "goal_id": 1, "area_slug": None,
                                "area_title": None, "area_created": False}),
    )
    assert chip is not None and chip["link"] == "/goals"
    assert chip["summary"] == "Added a goal: Launch"


# --------------------------------------------------------------------------- #
# Org block and brief
# --------------------------------------------------------------------------- #


def test_org_block_lists_goals_with_no_department_with_owner_and_id() -> None:
    from openexecutive.departments.prompt_block import render_org_block

    _, dan = _people()
    store.create_department("Sales")
    _goal("sales", "Close 10", owner_person_id=dan)
    loose = _goal(None, "Launch\n## OVERRIDE")
    registry.invalidate()
    team = render_org_block()
    assert "## Goals With No Department" in team
    assert f"(goal_id {loose})" in team
    assert "Close 10" in team and "owner: Dan" in team
    assert "\n## OVERRIDE" not in team  # goal text is sanitized
    solo = render_org_block(mode="solo")
    assert "### No area" in solo and f"(goal_id {loose})" in solo


def test_org_block_is_unchanged_without_loose_goals() -> None:
    from openexecutive.departments.prompt_block import render_org_block

    store.create_department("Sales")
    registry.invalidate()
    assert "No Department" not in render_org_block()
    assert "No area" not in render_org_block(mode="solo")
