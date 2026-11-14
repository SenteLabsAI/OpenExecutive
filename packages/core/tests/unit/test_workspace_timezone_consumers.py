"""Everything that used to read the static USER_TIMEZONE now follows the
workspace zone: the Executive's block-0 timezone line, open-loop due dates,
the workflow designer / action-step context, and alert quiet hours."""
from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from openexecutive.alerts.models import UserPreferences
from openexecutive.alerts.preferences import _in_quiet_hours
from openexecutive.memory import episodic
from openexecutive.memory import workspace_settings as ws


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(episodic, "DB_PATH", tmp_path / "episodic.db")
    episodic.initialize_db(tmp_path / "episodic.db")
    monkeypatch.setattr(ws, "_configured_timezone", lambda: ZoneInfo("UTC"))


def _set_zone(zone: str | None) -> None:
    ws.restore_workspace_settings(ws.WorkspaceSettings(timezone=zone))


def _tz_line(text: str) -> str:
    (line,) = [ln for ln in text.splitlines() if ln.startswith("The user's local timezone is")]
    return line


def test_block0_timezone_line_follows_the_workspace_zone() -> None:
    from openexecutive.prompts.cache_manager import build_system_blocks

    assert _tz_line(build_system_blocks()[0]["text"]).startswith(
        "The user's local timezone is UTC (IANA)."
    )
    _set_zone("America/Phoenix")
    block0 = build_system_blocks()[0]
    assert _tz_line(block0["text"]).startswith("The user's local timezone is America/Phoenix (IANA).")
    # Still the 1h cached block — the zone is stable between changes.
    assert block0["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    # Byte-identical across builds while the zone is unchanged (cache stays warm).
    assert build_system_blocks()[0]["text"] == block0["text"]


def test_block0_falls_back_to_user_timezone_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.prompts.cache_manager import build_system_blocks

    monkeypatch.setattr(ws, "_configured_timezone", lambda: ZoneInfo("Europe/Vienna"))
    assert "The user's local timezone is Europe/Vienna (IANA)." in build_system_blocks()[0]["text"]


def test_open_loop_due_dates_use_the_workspace_zone() -> None:
    from openexecutive.attunement import open_loops

    _set_zone("Pacific/Auckland")
    assert open_loops._user_tz() == ZoneInfo("Pacific/Auckland")
    due = open_loops._resolve_due("2026-07-01", today=date(2026, 6, 30), default_days=2)
    local = due.astimezone(ZoneInfo("Pacific/Auckland"))
    assert (local.date(), local.hour) == (date(2026, 7, 1), 17)


def test_designer_context_names_the_workspace_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.workflows import designer

    monkeypatch.setattr("openexecutive.people.store.list_people", lambda: [])
    monkeypatch.setattr(
        "openexecutive.workflows.dynamic_store.list_definitions", lambda active_only=True: []
    )
    _set_zone("America/Halifax")
    assert "The user's timezone: America/Halifax. Cadences are in that timezone." in (
        designer.build_context_block()
    )


def test_action_step_turn_names_the_workspace_zone() -> None:
    from openexecutive.workflows import action_step
    from openexecutive.workflows.dynamic_models import ActionStepSpec

    _set_zone("Africa/Nairobi")
    step = ActionStepSpec(id="s1", title="Draft", goal="Draft it.")
    text = action_step._user_turn(
        step, workflow_title="W", goal="G", values={}, company_block="", prior_outputs={},
    )
    assert "The user's timezone: Africa/Nairobi." in text


# --------------------------------------------------------------------------- #
# Quiet hours
# --------------------------------------------------------------------------- #

# 23:00 UTC is 19:00 in New York (EDT) — outside a 22:00→07:00 window there,
# inside it in UTC.
_NOW = datetime(2030, 6, 1, 23, 0, tzinfo=UTC)


def _prefs(tz: str) -> UserPreferences:
    return UserPreferences(quiet_hours_start="22:00", quiet_hours_end="07:00", quiet_hours_tz=tz)


@pytest.mark.parametrize("stored", ["UTC", "", "   "])
def test_default_quiet_hours_zone_follows_the_workspace_zone(stored: str) -> None:
    """"UTC" is the historical model default and the column DEFAULT, and
    nothing in the app writes the column — so a stored "UTC" (or nothing)
    means "the user's zone", not a choice of UTC."""
    assert UserPreferences().quiet_hours_tz == "UTC"
    assert _in_quiet_hours(_prefs(stored), now=_NOW)  # no zone anywhere: UTC, as before
    _set_zone("America/New_York")
    assert not _in_quiet_hours(_prefs(stored), now=_NOW)  # 19:00 in New York


def test_explicit_quiet_hours_zone_is_kept() -> None:
    _set_zone("America/New_York")
    # 23:00 UTC is 08:00 in Tokyo — outside the window there.
    assert not _in_quiet_hours(_prefs("Asia/Tokyo"), now=_NOW)
    # 23:00 UTC is 00:00 in London (BST) — inside.
    assert _in_quiet_hours(_prefs("Europe/London"), now=_NOW)


@pytest.mark.parametrize("stored", ["Nowhere/Land", "America", "Europe/", "../etc"])
def test_unloadable_stored_quiet_hours_zone_falls_back_to_the_user_zone(stored: str) -> None:
    _set_zone("America/New_York")
    assert not _in_quiet_hours(_prefs(stored), now=_NOW)


def test_existing_utc_row_follows_the_user_zone(tmp_path: Path) -> None:
    """An alert-preferences row as it exists on every install today — the
    column DEFAULT 'UTC' — follows the workspace zone."""
    import sqlite3

    from openexecutive.alerts import store as alert_store
    from openexecutive.alerts.preferences import get_preferences

    db = tmp_path / "alerts.db"
    alert_store.initialize_db(db)
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO user_preferences (id, quiet_hours_start, quiet_hours_end, updated_at) "
            "VALUES (1, '22:00', '07:00', '2026-01-01T00:00:00+00:00')"
        )
    loaded = get_preferences(db_path=db)
    assert loaded.quiet_hours_tz == "UTC"
    assert _in_quiet_hours(loaded, now=_NOW)
    _set_zone("America/New_York")
    assert not _in_quiet_hours(loaded, now=_NOW)


def test_a_workflow_schedule_runs_on_the_companys_clock_all_year(monkeypatch: pytest.MonkeyPatch) -> None:
    """"Every Monday at 9am" is 9 AM in the company's zone, winter and
    summer alike, never 9 AM UTC."""
    from openexecutive.workflows import dynamic_cadence
    from openexecutive.workflows.dynamic_models import DynamicWorkflowDef

    _set_zone("America/New_York")
    queued: list[str] = []
    monkeypatch.setattr(
        "openexecutive.memory.episodic.insert_scheduled_action",
        lambda **kw: queued.append(kw["run_at"]) or 1,
    )
    defn = DynamicWorkflowDef.model_construct(name="w", title="W", cadence="weekly@mon@09:00", cadence_person_id=1)
    dynamic_cadence.schedule_dynamic_workflow_cadence(defn, after=datetime(2026, 11, 13, 12, tzinfo=UTC))
    dynamic_cadence.schedule_dynamic_workflow_cadence(defn, after=datetime(2026, 7, 1, 12, tzinfo=UTC))
    assert queued == ["2026-11-16T14:00:00+00:00", "2026-07-06T13:00:00+00:00"]


def test_a_cadence_saved_in_utc_moves_to_the_companys_clock_once(tmp_path) -> None:
    """Cadences saved while they were UTC keep their moment: 14:00 UTC on a
    November Monday becomes 9 AM New York, once per database."""
    import json

    from openexecutive.memory.episodic import _get_conn
    from openexecutive.workflows import dynamic_cadence, dynamic_store

    _set_zone("America/New_York")
    nov = datetime(2026, 11, 13, 12, tzinfo=UTC)
    assert dynamic_cadence.from_utc("weekly@mon@14:00", dynamic_cadence._zone(), nov) == "weekly@mon@09:00"
    assert dynamic_cadence.from_utc("weekly@tue@02:00", dynamic_cadence._zone(), nov) == "weekly@mon@21:00"
    assert dynamic_cadence.from_utc("daily@13:30", dynamic_cadence._zone(), nov) == "daily@08:30"
    assert dynamic_cadence.from_utc("quarterly@15-14:00", dynamic_cadence._zone(), nov) == "quarterly@15-09:00"
    assert dynamic_cadence.from_utc("quarterly@01-02:00", dynamic_cadence._zone(), nov) == "quarterly@01-21:00"
    assert dynamic_cadence.from_utc("whenever", dynamic_cadence._zone(), nov) == "whenever"

    db = tmp_path / "episodic.db"
    body = {
        "name": "w", "title": "W", "cadence": "daily@14:00", "cadence_person_id": 1,
        "steps": [
            {"kind": "specialist", "id": "r", "title": "R", "specialist": "cso", "goal": "Go."},
            {"kind": "synthesis", "id": "a", "title": "A"},
        ],
    }
    with _get_conn(db) as conn:
        conn.execute(
            "CREATE TABLE dynamic_workflows (name TEXT PRIMARY KEY, definition TEXT NOT NULL, "
            "is_active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO dynamic_workflows VALUES ('w', ?, 1, 'then', 'then')", (json.dumps(body),)
        )
    dynamic_store.initialize_dynamic_workflows_db(db)
    moved = dynamic_store.get_definition("w", db_path=db)
    assert moved is not None and moved.cadence in ("daily@09:00", "daily@10:00")  # EST, or EDT in summer
    assert moved.updated_at == "then"
    dynamic_store.initialize_dynamic_workflows_db(db)
    again = dynamic_store.get_definition("w", db_path=db)
    assert again is not None and again.cadence == moved.cadence


def test_a_department_check_in_runs_on_the_companys_clock(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A check-in set for Monday 9:00 is 9 AM in the company's zone; one
    saved while check-ins were UTC keeps its moment, moved once."""
    import json

    from openexecutive.departments import cadence
    from openexecutive.departments import store as dept_store
    from openexecutive.memory.episodic import _get_conn

    _set_zone("America/New_York")
    db = tmp_path / "depts.db"
    monkeypatch.setattr(dept_store, "DB_PATH", db)
    dept_store.initialize_db(db)  # a new database: nothing to move, marked
    sales = dept_store.create_department("Sales", db_path=db)
    dept_store.update_department(sales.config.slug, cadences={"check_in": "weekly@mon@09:00"}, db_path=db)
    queued: list[str] = []
    monkeypatch.setattr(
        "openexecutive.memory.episodic.insert_scheduled_action",
        lambda **kw: queued.append(kw["run_at"]) or 1,
    )
    cadence.enqueue_next(sales.config.slug, after=datetime(2026, 11, 13, 12, tzinfo=UTC), db_path=db)
    assert queued == ["2026-11-16T14:00:00+00:00"]

    old = tmp_path / "old.db"
    with _get_conn(old) as conn:
        conn.execute(
            "CREATE TABLE departments (slug TEXT PRIMARY KEY, title TEXT NOT NULL, specialist_key TEXT, "
            "charter_mission TEXT NOT NULL DEFAULT '', charter_scope_json TEXT NOT NULL DEFAULT '[]', "
            "charter_out_of_scope_json TEXT NOT NULL DEFAULT '[]', authority_level TEXT NOT NULL "
            "DEFAULT 'propose_only', head_person_id INTEGER, head_persona_slug TEXT, cadences_json TEXT "
            "NOT NULL DEFAULT '{}', headcount INTEGER, budget_usd REAL, updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO departments (slug, title, cadences_json, updated_at) VALUES ('ops', 'Ops', ?, 'then')",
            (json.dumps({"check_in": "weekly@mon@14:00"}),),
        )
    dept_store.initialize_db(old)
    moved = dept_store.get_department("ops", db_path=old)
    assert moved is not None and moved.config.cadences["check_in"] in ("weekly@mon@09:00", "weekly@mon@10:00")
    dept_store.initialize_db(old)
    again = dept_store.get_department("ops", db_path=old)
    assert again is not None and again.config.cadences == moved.config.cadences


def test_changing_the_zone_moves_scheduled_workflow_runs_and_check_ins(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.departments import cadence
    from openexecutive.workflows import dynamic_cadence

    calls: list[str] = []
    monkeypatch.setattr(dynamic_cadence, "retime_pending", lambda *a, **k: calls.append("workflows") or 0)
    monkeypatch.setattr(cadence, "retime_pending", lambda *a, **k: calls.append("check-ins") or 0)
    monkeypatch.setattr("openexecutive.scheduler.runner.reschedule_principal_rhythm", lambda: None)
    _set_zone(None)
    ws.set_timezone("America/New_York")
    assert calls == ["workflows", "check-ins"]
    ws.set_timezone("America/New_York")  # same zone: nothing moves
    assert calls == ["workflows", "check-ins"]


def test_a_scheduled_workflow_run_moves_with_the_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.workflows import dynamic_cadence, dynamic_store
    from openexecutive.workflows.dynamic_models import DynamicWorkflowDef

    on = DynamicWorkflowDef.model_construct(name="on", title="On", cadence="daily@09:00", cadence_person_id=1, is_active=True)
    off = DynamicWorkflowDef.model_construct(name="off", title="Off", cadence="daily@09:00", cadence_person_id=1, is_active=False)
    monkeypatch.setattr(dynamic_store, "get_definition", lambda name, **_: {"on": on, "off": off}.get(name))
    episodic.insert_scheduled_action(run_at="2026-11-16T09:00:00+00:00", channel="__internal__", channel_ref="on",
                                     intent_text="x", kind="dynamic_workflow")
    episodic.insert_scheduled_action(run_at="2026-11-16T09:00:00+00:00", channel="__internal__", channel_ref="off",
                                     intent_text="x", kind="dynamic_workflow")
    _set_zone("America/New_York")
    assert dynamic_cadence.retime_pending() == 1
    with episodic._get_conn(episodic.DB_PATH) as conn:
        rows = conn.execute(
            "SELECT channel_ref, run_at FROM scheduled_actions WHERE kind = 'dynamic_workflow' AND status = 'pending'"
        ).fetchall()
    times = {r[0]: r[1] for r in rows}
    assert times["off"] == "2026-11-16T09:00:00+00:00"  # off: left alone
    assert times["on"].endswith(("14:00:00+00:00", "13:00:00+00:00"))  # 9 AM New York
