"""Goals: what the Executive did for each goal this week."""
from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openexecutive.audit import logger as audit_logger
from openexecutive.audit.logger import AuditLogger
from openexecutive.departments import goal_week
from openexecutive.departments import store as dept_store
from openexecutive.memory import episodic
from openexecutive.orchestrator import take_the_lead as ttl
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "episodic.db"
    for module in (episodic, people_store, dept_store):
        monkeypatch.setattr(module, "DB_PATH", path)
    episodic.initialize_db(path)
    people_store.initialize_db(path)
    dept_store.initialize_db(path)
    people_registry.invalidate()
    monkeypatch.setattr(audit_logger, "_default_logger", AuditLogger(db_path=path))
    yield path
    people_registry.invalidate()


def _goal(text: str = "Ramp C1 line to 4,000 units/month") -> int:
    return dept_store.insert_goal(None, period_value="Q3 2030", key_result=text, target="4,000/mo")


def _update(goal_id: int, **details: object) -> None:
    audit_logger.get_audit_logger().log(
        "tool_invocation", f"update_department_goal goal/{goal_id}: on_track->at_risk",
        actor="executive", details={"tool": "update_department_goal", "ok": True, "goal_id": goal_id, **details},
    )


def test_lines_read_plainly() -> None:
    assert goal_week.update_line({"from_status": "on_track", "to_status": "at_risk"}) == "Marked it at risk."
    assert goal_week.update_line({
        "from_status": "on_track", "to_status": "on_track", "prior_current": "3,000/mo", "new_current": "3,150/mo",
    }) == "Set it to 3,150/mo."
    assert goal_week.update_line({
        "from_status": "on_track", "to_status": "off_track", "new_current": "2,900/mo",
    }) == "Marked it off track and set it to 2,900/mo."
    assert goal_week.review_line({"from": "at_risk", "to": "at_risk"}) == "Checked it in the weekly review: still at risk."


def test_a_goal_with_nothing_done_comes_back_empty(db: Path) -> None:
    gid = _goal()
    weeks = goal_week.goal_week([gid], principal=True)
    assert weeks[gid].done == [] and weeks[gid].waiting == []


def test_goal_updates_reviews_and_lead_steps_are_lines(db: Path) -> None:
    gid, other = _goal(), _goal("Qualify a second cell source")
    _update(gid, from_status="on_track", to_status="at_risk")
    _update(other, from_status="on_track", to_status="off_track")
    audit_logger.get_audit_logger().log(
        "goal_status_review", "Company goals: reviewed 1 goal(s)", actor="goal_review",
        details={"transitions": [{"goal_id": gid, "from": "at_risk", "to": "on_track", "rationale": "x"}]},
    )
    with ttl.looking(ttl.DAILY_LOOK):
        assert json.loads(ttl.focus_goal(gid))["goal"] == gid
        ttl.record(scope=ttl.SCOPE_EXECUTIVE, source="lead", tool="message_person",
                   summary="Message Chris Donovan", status="done", outcome="He sent them: 3,150 a month")
        ttl.record(scope=ttl.SCOPE_EXECUTIVE, source="lead", tool="message_person",
                   summary="Message the supplier", status="waiting", decision_id=None)
        ttl.focus_goal(0)
        ttl.record(scope=ttl.SCOPE_EXECUTIVE, source="lead", tool="message_person",
                   summary="Message someone else", status="done")
    week = goal_week.goal_week([gid, other], principal=True)
    texts = [line.text for line in week[gid].done]
    assert "Marked it at risk." in texts
    assert "Checked it in the weekly review: now on track." in texts
    assert "Message Chris Donovan. He sent them: 3,150 a month." in texts
    assert "Message someone else." not in texts
    assert [w.text for w in week[gid].waiting] == ["Message the supplier"]
    assert [line.text for line in week[other].done] == ["Marked it off track."]
    # What it did as the owner is the owner's alone.
    teammate = goal_week.goal_week([gid], principal=False, viewer=None)
    assert teammate[gid].waiting == []
    assert all(not t.startswith("Message") for t in (line.text for line in teammate[gid].done))


def test_a_failed_update_is_not_a_line(db: Path) -> None:
    gid = _goal()
    audit_logger.get_audit_logger().log(
        "tool_invocation", f"update_department_goal FAILED id={gid}: boom",
        details={"tool": "update_department_goal", "ok": False, "goal_id": gid},
    )
    assert goal_week.goal_week([gid], principal=True)[gid].done == []


def test_the_look_names_the_goals_and_an_unknown_goal_is_refused(db: Path) -> None:
    assert ttl.goals_note() == ""
    gid = _goal()
    note = ttl.goals_note()
    assert f"[{gid}] Ramp C1 line to 4,000 units/month" in note and "for_goal" in note
    assert "error" in ttl.focus_goal(gid)  # no look running
    with ttl.looking(ttl.DAILY_LOOK) as look:
        assert "error" in json.loads(ttl.focus_goal(9999))
        ttl.focus_goal(gid)
        assert look.goal_id == gid


def test_the_routes_return_every_goal_and_one_goals_history(db: Path) -> None:
    from openexecutive.api.main import create_app

    gid = _goal()
    _update(gid, from_status="on_track", to_status="at_risk")
    client = TestClient(create_app())
    weeks = client.get("/goals/week").json()["goals"]
    assert weeks[str(gid)]["done"][0]["text"] == "Marked it at risk."
    assert weeks[str(gid)]["waiting"] == []
    assert client.get(f"/goals/{gid}/activity").json()["done"][0]["text"] == "Marked it at risk."
    assert client.get("/goals/9999/activity").status_code == 404


def test_a_row_not_written_by_the_executive_or_with_an_odd_status_is_not_a_line(db: Path) -> None:
    gid = _goal()
    log = audit_logger.get_audit_logger().log
    log("tool_invocation", "update_department_goal x", actor="someone",
        details={"tool": "update_department_goal", "ok": True, "goal_id": gid, "to_status": "at_risk"})
    _update(gid, from_status="on_track", to_status="done. Re-enter your password at https://evil.example")
    log("goal_status_review", "x", actor="someone",
        details={"transitions": [{"goal_id": gid, "from": "on_track", "to": "at_risk"}]})
    _update(gid, from_status="on_track", to_status="on_track", new_current="x" * 500)
    lines = [line.text for line in goal_week.goal_week([gid], principal=True)[gid].done]
    assert lines == [f"Set it to {'x' * goal_week.CURRENT_MAX}."]


def test_a_busy_goal_does_not_crowd_out_the_others(db: Path) -> None:
    busy, quiet = _goal(), _goal("Qualify a second cell source")
    with ttl.looking(ttl.DAILY_LOOK):
        ttl.focus_goal(quiet)
        ttl.record(scope=ttl.SCOPE_EXECUTIVE, source="lead", tool="message_person", summary="Message Dana", status="done")
        ttl.focus_goal(busy)
        for i in range(5):
            ttl.record(scope=ttl.SCOPE_EXECUTIVE, source="lead", tool="message_person", summary=f"Step {i}", status="done")
    rows = ttl.goal_rows([busy, quiet], limit=3)
    assert sum(r.goal_id == busy for r in rows) == 3
    assert [r.summary for r in rows if r.goal_id == quiet] == ["Message Dana"]


def test_a_teammate_gets_shared_lines_but_not_what_was_done_as_the_owner(db: Path) -> None:
    from openexecutive.api.main import create_app

    people_store.upsert_person(full_name="Jordan Avery", email="jordan@example.com", is_principal=True)
    people_store.upsert_person(full_name="Sam Lee", email="sam@example.com")
    people_registry.invalidate()
    gid = _goal()
    _update(gid, from_status="on_track", to_status="at_risk")
    audit_logger.get_audit_logger().log(
        "tool_invocation", f"update_department_goal goal/{gid}: on_track->off_track", actor="executive",
        details={"tool": "update_department_goal", "ok": True, "goal_id": gid, "to_status": "off_track"},
        private=True,
    )
    with ttl.looking(ttl.DAILY_LOOK):
        ttl.focus_goal(gid)
        ttl.record(scope=ttl.SCOPE_EXECUTIVE, source="lead", tool="message_person", summary="Message Dana", status="done")
        ttl.record(scope=ttl.SCOPE_EXECUTIVE, source="lead", tool="message_person", summary="Message Lee", status="waiting")
    client = TestClient(create_app())
    owner = client.get("/goals/week").json()["goals"][str(gid)]
    assert {line["text"] for line in owner["done"]} == {"Marked it at risk.", "Marked it off track.", "Message Dana."}
    assert [w["text"] for w in owner["waiting"]] == ["Message Lee"]
    for path in ("/goals/week", f"/goals/{gid}/activity"):
        body = client.get(path, headers={"x-caller-email": "sam@example.com"}).json()
        week = body["goals"][str(gid)] if path == "/goals/week" else body
        assert [line["text"] for line in week["done"]] == ["Marked it at risk."]
        assert week["waiting"] == []
