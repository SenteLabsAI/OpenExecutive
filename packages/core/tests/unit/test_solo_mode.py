"""Solo mode — one person using Open Executive just for themselves: no
department check-ins, wherever they would otherwise be scheduled or fired."""
from __future__ import annotations

import ast
import asyncio
import inspect
import sqlite3
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from openexecutive.alerts import store as alert_store
from openexecutive.cli import fixture_loader
from openexecutive.departments import registry as dept_registry
from openexecutive.departments import store as dept_store
from openexecutive.departments.cadence import bootstrap_cadences
from openexecutive.departments.models import AuthorityLevel
from openexecutive.knowledge.store import ChromaDBStore
from openexecutive.memory import episodic
from openexecutive.memory import workspace_settings as ws
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "solo.db"
    for mod in (episodic, dept_store, people_store, alert_store):
        monkeypatch.setattr(mod, "DB_PATH", db)
    monkeypatch.setattr(ws, "_configured_timezone", lambda: ws.ZoneInfo("UTC"))
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **k: None)
    dept_registry.invalidate()
    people_registry.invalidate()
    episodic.initialize_db(db)
    dept_store.initialize_db(db)
    people_store.initialize_db(db)
    alert_store.initialize_db(db)
    dept_store.seed_default_departments(db_path=db)
    dept_registry.invalidate()
    yield db
    dept_registry.invalidate()
    people_registry.invalidate()


def _count(db: Path, kind: str, status: str = "pending") -> int:
    with sqlite3.connect(str(db)) as conn:
        return int(conn.execute(
            "SELECT COUNT(*) FROM scheduled_actions WHERE kind = ? AND status = ?",
            (kind, status),
        ).fetchone()[0])


# --------------------------------------------------------------------------- #
# bootstrap_cadences
# --------------------------------------------------------------------------- #


def test_bootstrap_cadences_is_a_no_op_in_solo(_isolated: Path) -> None:
    ws.restore_workspace_settings(ws.WorkspaceSettings(mode="solo"))
    assert bootstrap_cadences() == 0
    assert bootstrap_cadences(db_path=_isolated) == 0
    assert _count(_isolated, "dept_cadence") == 0


def test_bootstrap_cadences_runs_in_team(_isolated: Path) -> None:
    assert bootstrap_cadences() == 8


def _settings_stub(tmp_path: Path) -> Any:
    profile_path = tmp_path / "company" / "profile.yaml"
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text("name: ''\n")
    return type("S", (), {
        "vector_store_path": tmp_path / "chroma",
        "company_profile_path": profile_path,
        "honcho_workspace_id": "openexec",
    })()


@pytest.fixture()
def _reset_harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _isolated: Path) -> Any:
    from openexecutive.audit.logger import AuditLogger
    from openexecutive.evals.persistence import (
        initialize_eval_runs_db,
        initialize_user_scenarios_db,
    )
    from openexecutive.knowledge import review_store as rs_mod
    from openexecutive.memory import honcho_client
    from openexecutive.workflows.persistence import initialize_runs_db

    monkeypatch.setattr(rs_mod, "DB_PATH", _isolated)
    initialize_runs_db(_isolated)
    initialize_eval_runs_db(_isolated)
    initialize_user_scenarios_db(_isolated)
    AuditLogger(db_path=_isolated)

    async def _noop(workspace_id: str | None = None) -> None:
        return None

    monkeypatch.setattr(honcho_client, "delete_workspace_and_reset_client", _noop)
    return _settings_stub(tmp_path)


def _run_reset(settings: Any) -> None:
    with patch.object(ChromaDBStore, "delete_company_docs", lambda self: None), \
         patch.object(ChromaDBStore, "delete_documents", lambda self, **kw: None):
        asyncio.run(fixture_loader.reset_all_state(settings))


def test_reset_returns_to_team_defaults_and_rebootstraps(_reset_harness: Any, _isolated: Path) -> None:
    ws.restore_workspace_settings(ws.WorkspaceSettings(mode="solo", timezone="Asia/Seoul"))
    _run_reset(_reset_harness)
    assert ws.get_workspace() == ws.WorkspaceSettings()
    assert _count(_isolated, "dept_cadence") == 8


def test_reset_path_bootstrap_honours_solo(
    _reset_harness: Any, _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The solo check lives inside bootstrap_cadences, so the reset's own call
    honours it too (here the workspace is held at solo through the reset)."""
    ws.restore_workspace_settings(ws.WorkspaceSettings(mode="solo"))
    monkeypatch.setattr(ws, "reset_workspace_settings", lambda db_path=None: None)
    _run_reset(_reset_harness)
    assert ws.get_workspace().mode == "solo"
    assert _count(_isolated, "dept_cadence") == 0
    # The principal's own rhythm is still seeded.
    assert _count(_isolated, "principal_brief_morning") == 1


# --------------------------------------------------------------------------- #
# The runner retires a dept_cadence row in solo
# --------------------------------------------------------------------------- #


def _claim_dept_cadence(slug: str = "finance") -> episodic.ScheduledAction:
    episodic.insert_scheduled_action(
        run_at=(datetime.now(UTC) - timedelta(seconds=5)).isoformat(),
        channel="__internal__", channel_ref=slug, intent_text=f"Department check-in: {slug}",
        department=slug, kind="dept_cadence",
    )
    (action,) = episodic.claim_due_actions(datetime.now(UTC))
    return action


def test_solo_dept_cadence_row_is_retired_without_running_or_chaining(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.scheduler import runner

    # propose_only would otherwise turn the check-in into an approval card.
    dept_store.update_department("finance", authority_level=AuthorityLevel.PROPOSE_ONLY)
    people_store.upsert_person(full_name="Solo Sam", is_principal=True)
    dept_registry.invalidate()
    people_registry.invalidate()
    ws.restore_workspace_settings(ws.WorkspaceSettings(mode="solo"))

    def _fail(*_a: object, **_k: object) -> Any:
        raise AssertionError("a solo workspace must not run or chain a check-in")

    monkeypatch.setattr("openexecutive.departments.cadence.enqueue_next", _fail)
    monkeypatch.setattr("openexecutive.departments.authority.gate_action", _fail)
    monkeypatch.setattr(
        "openexecutive.workflows.department_check_in.DepartmentCheckInWorkflow.run", _fail
    )

    action = _claim_dept_cadence()
    asyncio.run(runner._execute_action(action, None))

    row = episodic.get_scheduled_action(action.id or 0)
    # Cancelled, not done: it never ran, so it must not read as a recent
    # department pulse to the nudge engine (or as a run in the Pulse history).
    assert row is not None and row.status == "cancelled"
    assert "solo" in row.last_error
    assert _count(_isolated, "dept_cadence") == 0  # nothing chained
    assert alert_store.list_alerts(limit=10) == []  # and no approval card
    from openexecutive.scheduler.nudge_engine import _dept_cadence_recent

    assert not _dept_cadence_recent("finance", datetime.now(UTC) - timedelta(days=1))


def test_a_check_in_running_across_the_switch_does_not_chain(_isolated: Path) -> None:
    """A check-in that was already running when the install switched to solo
    finishes, but enqueue_next schedules no successor."""
    from openexecutive.departments.cadence import enqueue_next

    assert enqueue_next("finance") is not None  # team: chains
    ws.restore_workspace_settings(ws.WorkspaceSettings(mode="solo"))
    before = _count(_isolated, "dept_cadence")
    assert enqueue_next("finance") is None
    assert _count(_isolated, "dept_cadence") == before


def test_team_dept_cadence_row_still_reaches_the_gate(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: in team mode the same row goes through the authority gate."""
    from openexecutive.scheduler import runner

    seen: list[str] = []

    class _Stop(Exception):
        pass

    def _gate(dept: str, *_a: object, **_k: object) -> Any:
        seen.append(dept)
        raise _Stop

    monkeypatch.setattr("openexecutive.departments.authority.gate_action", _gate)
    action = _claim_dept_cadence()
    with pytest.raises(_Stop):
        asyncio.run(runner._execute_action(action, None))
    assert seen == ["finance"]


# --------------------------------------------------------------------------- #
# Fixture load / snapshot: workspace.yaml
# --------------------------------------------------------------------------- #


def _apply_file(path: Path, *, keep_when_missing: bool = False) -> dict[str, Any]:
    return fixture_loader._apply_workspace(
        fixture_loader._read_workspace_file(path), keep_when_missing=keep_when_missing
    )


def test_workspace_file_applied_after_reset(tmp_path: Path) -> None:
    ws.restore_workspace_settings(ws.WorkspaceSettings(mode="solo", timezone="Asia/Seoul"))
    f = tmp_path / "workspace.yaml"
    f.write_text("mode: solo\ntimezone: America/Chicago\n")
    out = _apply_file(f)
    assert out == {"mode": "solo", "timezone": "America/Chicago"}
    assert ws.get_workspace() == ws.WorkspaceSettings(mode="solo", timezone="America/Chicago")


def test_missing_workspace_file_means_defaults_for_a_fixture(tmp_path: Path) -> None:
    ws.restore_workspace_settings(ws.WorkspaceSettings(mode="solo", timezone="Asia/Seoul"))
    assert fixture_loader._read_workspace_file(tmp_path / "workspace.yaml") is None
    out = _apply_file(tmp_path / "workspace.yaml")
    assert out == {"mode": "team", "timezone": None}


def test_missing_workspace_file_keeps_settings_for_a_legacy_backup(tmp_path: Path) -> None:
    """Unloading from a backup that predates workspace.yaml must not reset the
    user's settings to team / no zone."""
    ws.restore_workspace_settings(ws.WorkspaceSettings(mode="solo", timezone="Asia/Seoul"))
    out = _apply_file(tmp_path / "workspace.yaml", keep_when_missing=True)
    assert out == {"mode": "solo", "timezone": "Asia/Seoul"}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("mode: enterprise\ntimezone: Europe/Oslo\n", {"mode": "team", "timezone": "Europe/Oslo"}),
        ("mode: solo\ntimezone: Mars/Base\n", {"mode": "solo", "timezone": None}),
        # zoneinfo raises IsADirectoryError / ValueError for these, not
        # ZoneInfoNotFoundError — none may abort the load.
        ("mode: solo\ntimezone: America\n", {"mode": "solo", "timezone": None}),
        ("timezone: Europe/\n", {"mode": "team", "timezone": None}),
        ("timezone: ../etc\n", {"mode": "team", "timezone": None}),
        ("timezone: 42\n", {"mode": "team", "timezone": None}),
        ("mode: [solo]\n", {"mode": "team", "timezone": None}),
        ("- just\n- a list\n", {"mode": "team", "timezone": None}),
        ("mode: [unclosed\n", {"mode": "team", "timezone": None}),
    ],
)
def test_bad_workspace_file_values_are_skipped(
    tmp_path: Path, text: str, expected: dict[str, Any]
) -> None:
    f = tmp_path / "workspace.yaml"
    f.write_text(text)
    assert _apply_file(f) == expected


def test_snapshot_round_trip_restores_the_users_settings(tmp_path: Path) -> None:
    ws.restore_workspace_settings(ws.WorkspaceSettings(mode="solo", timezone="Europe/Dublin"))
    f = tmp_path / "backup" / "workspace.yaml"
    f.parent.mkdir()
    fixture_loader._dump_workspace(f)
    ws.reset_workspace_settings()  # a fixture load in between
    _apply_file(f, keep_when_missing=True)  # unload applies the backup
    assert ws.get_workspace() == ws.WorkspaceSettings(mode="solo", timezone="Europe/Dublin")


def _top_level_calls(fn: Any) -> set[str]:
    """Names called by top-level statements of ``fn`` (not inside an if/for)."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    body = tree.body[0].body  # type: ignore[attr-defined]
    names: set[str] = set()
    for stmt in body:
        for node in ast.walk(stmt) if not isinstance(stmt, (ast.If, ast.For, ast.While)) else []:
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                names.add(node.func.id)
    return names


def test_fixture_load_path_always_applies_the_workspace_file() -> None:
    """Every load/unload reads the file before swapping anything and then
    applies it — a fixture with no workspace.yaml must not inherit the
    previous company's mode or zone."""
    calls = _top_level_calls(fixture_loader._apply_state_from_source)
    assert {"_read_workspace_file", "_apply_workspace"} <= calls
    src = inspect.getsource(fixture_loader._apply_state_from_source)
    assert src.index("_read_workspace_file(") < src.index("profile.save_to_yaml(")
    assert "_dump_workspace" in _top_level_calls(fixture_loader.snapshot_user_state)


def test_blank_client_slot_wipes_the_workspace_settings() -> None:
    from openexecutive.clients.slots import _BLANK_WIPE_TABLES, _GLOBAL_TABLES

    assert "workspace_settings" in _BLANK_WIPE_TABLES
    assert "workspace_settings" not in _GLOBAL_TABLES
