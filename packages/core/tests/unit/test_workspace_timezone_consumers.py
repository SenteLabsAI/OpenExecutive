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
    assert "The user's timezone: America/Halifax. Cadences are in UTC." in (
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
