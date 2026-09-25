"""memory/workspace_settings.py — the solo/team mode and the user's zone."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openexecutive.departments import registry as dept_registry
from openexecutive.departments import store as dept_store
from openexecutive.memory import episodic
from openexecutive.memory import workspace_settings as ws
from openexecutive.orchestrator.session import Session

# The real USER_TIMEZONE fallback, captured before the autouse fixture pins it.
_REAL_CONFIGURED_TZ = ws._configured_timezone


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", db)
    monkeypatch.setattr(dept_store, "DB_PATH", db)
    # Pin the USER_TIMEZONE fallback so a developer's .env cannot leak in.
    monkeypatch.setattr(ws, "_configured_timezone", lambda: ws.ZoneInfo("UTC"))
    dept_registry.invalidate()
    episodic.initialize_db(db)
    dept_store.initialize_db(db)
    yield db
    dept_registry.invalidate()


def _kinds(db: Path, status: str = "pending") -> list[str]:
    with sqlite3.connect(str(db)) as conn:
        return sorted(
            r[0]
            for r in conn.execute("SELECT kind FROM scheduled_actions WHERE status = ?", (status,))
        )


# --------------------------------------------------------------------------- #
# Defaults, reads, validation
# --------------------------------------------------------------------------- #


def test_defaults_with_no_db_and_reads_create_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "nope" / "missing.db"
    monkeypatch.setattr(episodic, "DB_PATH", missing)
    assert ws.get_workspace() == ws.WorkspaceSettings(mode="team", timezone=None)
    assert ws.get_user_timezone().key == "UTC"
    assert ws.effective_workspace_mode() == "team"
    ws.reset_workspace_settings()
    assert not missing.exists()


def test_defaults_with_db_but_no_table(_isolated: Path) -> None:
    assert ws.get_workspace() == ws.WorkspaceSettings()
    with sqlite3.connect(str(_isolated)) as conn:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'workspace_settings'"
        ).fetchone() is None


def test_init_is_idempotent_and_row_defaults(_isolated: Path) -> None:
    ws.init_workspace_settings_db()
    ws.init_workspace_settings_db()
    assert ws.get_workspace() == ws.WorkspaceSettings()


def test_set_and_get_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openexecutive.scheduler.runner.reschedule_principal_rhythm", lambda: 0)
    out = ws.set_timezone("  America/New_York ")
    assert out.timezone == "America/New_York"
    assert ws.get_workspace().timezone == "America/New_York"
    assert ws.get_user_timezone().key == "America/New_York"
    # Mode is untouched by a zone write.
    assert ws.get_workspace().mode == "team"


@pytest.mark.parametrize("bad", ["Mars/Olympus_Mons", "not a zone", "../etc/passwd", "x" * 65])
def test_invalid_timezone_rejected(bad: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openexecutive.scheduler.runner.reschedule_principal_rhythm", lambda: 0)
    ws.set_timezone("Europe/Paris")
    with pytest.raises(ValueError):
        ws.set_timezone(bad)
    assert ws.get_workspace().timezone == "Europe/Paris"


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_blank_timezone_clears(blank: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openexecutive.scheduler.runner.reschedule_principal_rhythm", lambda: 0)
    ws.set_timezone("Asia/Tokyo")
    ws.set_timezone(blank)
    assert ws.get_workspace().timezone is None
    assert ws.get_user_timezone().key == "UTC"


def test_user_timezone_falls_back_to_setting_then_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive import config

    monkeypatch.setattr(
        config, "get_settings", lambda: SimpleNamespace(user_timezone="Europe/Berlin")
    )
    assert _REAL_CONFIGURED_TZ().key == "Europe/Berlin"

    def _boom() -> Any:
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(config, "get_settings", _boom)
    assert _REAL_CONFIGURED_TZ().key == "UTC"


def test_stored_zone_wins_over_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ws, "_configured_timezone", lambda: ws.ZoneInfo("Europe/Berlin"))
    monkeypatch.setattr("openexecutive.scheduler.runner.reschedule_principal_rhythm", lambda: 0)
    assert ws.get_user_timezone().key == "Europe/Berlin"
    ws.set_timezone("Australia/Sydney")
    assert ws.get_user_timezone().key == "Australia/Sydney"


def test_bad_stored_values_read_as_defaults(_isolated: Path) -> None:
    ws.init_workspace_settings_db()
    with sqlite3.connect(str(_isolated)) as conn:
        conn.execute(
            "INSERT INTO workspace_settings (id, mode, timezone) VALUES (1, 'party', 'Nowhere/Land')"
        )
    assert ws.get_workspace() == ws.WorkspaceSettings()


def test_read_error_reads_as_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"this is not a sqlite database" * 100)
    monkeypatch.setattr(episodic, "DB_PATH", garbage)
    assert ws.get_workspace() == ws.WorkspaceSettings()


# --------------------------------------------------------------------------- #
# effective_workspace_mode
# --------------------------------------------------------------------------- #


def test_effective_mode_without_session_follows_workspace() -> None:
    assert ws.effective_workspace_mode() == "team"
    assert ws.effective_workspace_mode(Session()) == "team"
    ws.set_workspace_mode("solo")
    assert ws.effective_workspace_mode() == "solo"
    assert ws.effective_workspace_mode(Session()) == "solo"


def test_session_override_wins_both_ways() -> None:
    assert ws.effective_workspace_mode(Session(workspace_mode="solo")) == "solo"
    ws.set_workspace_mode("solo")
    assert ws.effective_workspace_mode(Session(workspace_mode="team")) == "team"
    # A garbage override is ignored rather than trusted.
    assert ws.effective_workspace_mode(Session(workspace_mode="bogus")) == "solo"


# --------------------------------------------------------------------------- #
# set_workspace_mode side effects
# --------------------------------------------------------------------------- #


def test_invalid_mode_rejected() -> None:
    with pytest.raises(ValueError):
        ws.set_workspace_mode("enterprise")
    assert ws.get_workspace().mode == "team"


def test_switch_to_solo_cancels_only_dept_cadence_rows(_isolated: Path) -> None:
    from openexecutive.departments.cadence import bootstrap_cadences

    dept_store.seed_default_departments(db_path=_isolated)
    dept_registry.invalidate()
    assert bootstrap_cadences() == 8
    soon = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    for kind in ("ad_hoc", "principal_brief_morning", "nudge_scan"):
        episodic.insert_scheduled_action(
            run_at=soon, channel="__internal__", channel_ref="x", intent_text="t", kind=kind,
        )
    # A running check-in is left for the runner to retire.
    running = episodic.insert_scheduled_action(
        run_at=soon, channel="__internal__", channel_ref="finance", intent_text="t",
        department="finance", kind="dept_cadence",
    )
    with sqlite3.connect(str(_isolated)) as conn:
        conn.execute("UPDATE scheduled_actions SET status = 'running' WHERE id = ?", (running,))

    ws.set_workspace_mode("solo")

    assert ws.get_workspace().mode == "solo"
    assert _kinds(_isolated) == ["ad_hoc", "nudge_scan", "principal_brief_morning"]
    assert _kinds(_isolated, "cancelled") == ["dept_cadence"] * 8
    assert _kinds(_isolated, "running") == ["dept_cadence"]
    # Department rows stay, so switching back is instant.
    assert len(dept_store.list_departments()) == 8


def test_switch_back_to_team_rebootstraps(_isolated: Path) -> None:
    dept_store.seed_default_departments(db_path=_isolated)
    dept_registry.invalidate()
    ws.set_workspace_mode("solo")
    assert "dept_cadence" not in _kinds(_isolated)

    ws.set_workspace_mode("team")

    assert ws.get_workspace().mode == "team"
    assert _kinds(_isolated).count("dept_cadence") == 8


# --------------------------------------------------------------------------- #
# set_timezone re-times the principal's rhythm only on a real change
# --------------------------------------------------------------------------- #


def test_timezone_change_reschedules_only_when_effective_zone_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(
        "openexecutive.scheduler.runner.reschedule_principal_rhythm",
        lambda: calls.append(1) or 0,
    )
    ws.set_timezone("America/Chicago")  # UTC fallback → Chicago
    assert len(calls) == 1
    ws.set_timezone("America/Chicago")  # same zone: nothing to re-time
    assert len(calls) == 1
    ws.set_timezone("UTC")  # Chicago → UTC
    assert len(calls) == 2
    # Clearing the stored UTC falls back to USER_TIMEZONE, pinned to UTC here:
    # the zone in effect does not move, so the briefs are left alone.
    ws.set_timezone(None)
    assert len(calls) == 2
    assert ws.get_workspace().timezone is None


# --------------------------------------------------------------------------- #
# restore / reset (no side effects)
# --------------------------------------------------------------------------- #


def test_restore_and_reset_have_no_scheduler_side_effects(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fail(*_a: object, **_k: object) -> int:
        raise AssertionError("no scheduler side effects expected")

    monkeypatch.setattr("openexecutive.scheduler.runner.reschedule_principal_rhythm", _fail)
    monkeypatch.setattr("openexecutive.departments.cadence.cancel_pending_cadences", _fail)
    monkeypatch.setattr("openexecutive.departments.cadence.bootstrap_cadences", _fail)

    ws.restore_workspace_settings(ws.WorkspaceSettings(mode="solo", timezone="Europe/Lisbon"))
    assert ws.get_workspace() == ws.WorkspaceSettings(mode="solo", timezone="Europe/Lisbon")
    ws.reset_workspace_settings()
    assert ws.get_workspace() == ws.WorkspaceSettings()


def test_side_effect_failures_never_roll_back_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: object, **_k: object) -> int:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("openexecutive.scheduler.runner.reschedule_principal_rhythm", _boom)
    monkeypatch.setattr("openexecutive.departments.cadence.cancel_pending_cadences", _boom)
    assert ws.set_timezone("Europe/Athens").timezone == "Europe/Athens"
    assert ws.set_workspace_mode("solo").mode == "solo"
    assert ws.get_workspace() == ws.WorkspaceSettings(mode="solo", timezone="Europe/Athens")
