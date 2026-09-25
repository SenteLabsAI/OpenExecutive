"""The principal's rhythm (morning brief, end-of-day digest, reflection) in the
user's time zone — scheduler/runner.py."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from openexecutive.memory import episodic
from openexecutive.memory import workspace_settings as ws
from openexecutive.scheduler import runner

BRIEF_KINDS = ("principal_brief_morning", "principal_brief_eod", "executive_reflection")
_ENV = ("PRINCIPAL_BRIEF_MORNING_TIME", "PRINCIPAL_BRIEF_EOD_TIME", "PRINCIPAL_REFLECTION_TIME")


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "rhythm.db"
    monkeypatch.setattr(episodic, "DB_PATH", db)
    monkeypatch.setattr(ws, "_configured_timezone", lambda: ZoneInfo("UTC"))
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    episodic.initialize_db(db)
    return db


def _pending(kind: str) -> list[episodic.ScheduledAction]:
    return [
        a for a in episodic.list_scheduled_actions(status="pending", limit=50) if a.kind == kind
    ]


def _local_hhmm(run_at: str, zone: str) -> tuple[int, int]:
    dt = datetime.fromisoformat(run_at).astimezone(ZoneInfo(zone))
    return dt.hour, dt.minute


def test_default_times_follow_the_workspace_zone() -> None:
    ws.restore_workspace_settings(ws.WorkspaceSettings(timezone="America/Los_Angeles"))
    assert runner.seed_principal_briefs() == 3
    expected = {
        "principal_brief_morning": (8, 0),
        "principal_brief_eod": (18, 0),
        "executive_reflection": (7, 30),
    }
    for kind, hhmm in expected.items():
        (row,) = _pending(kind)
        assert _local_hhmm(row.run_at, "America/Los_Angeles") == hhmm


def test_default_times_follow_user_timezone_setting_when_no_workspace_zone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ws, "_configured_timezone", lambda: ZoneInfo("Asia/Tokyo"))
    runner.seed_principal_briefs()
    (row,) = _pending("principal_brief_morning")
    assert _local_hhmm(row.run_at, "Asia/Tokyo") == (8, 0)


def test_default_is_utc_with_no_zone_anywhere() -> None:
    runner.seed_principal_briefs()
    (row,) = _pending("executive_reflection")
    assert _local_hhmm(row.run_at, "UTC") == (7, 30)


def test_explicit_env_var_is_still_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator who set a time before zones existed keeps it, in UTC."""
    ws.restore_workspace_settings(ws.WorkspaceSettings(timezone="America/New_York"))
    monkeypatch.setenv("PRINCIPAL_BRIEF_MORNING_TIME", "06:30")
    runner.seed_principal_briefs()
    (morning,) = _pending("principal_brief_morning")
    assert _local_hhmm(morning.run_at, "UTC") == (6, 30)
    # The kinds without an override use the workspace zone.
    (eod,) = _pending("principal_brief_eod")
    assert _local_hhmm(eod.run_at, "America/New_York") == (18, 0)


@pytest.mark.parametrize("raw", ["not-a-time", "7:30am", "25:00", "   "])
def test_malformed_or_blank_env_var_is_ignored_like_an_unset_one(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a valid pinned time is read as UTC; anything else falls back to
    the default in the user's zone rather than to midnight-ish UTC."""
    ws.restore_workspace_settings(ws.WorkspaceSettings(timezone="America/New_York"))
    monkeypatch.setenv("PRINCIPAL_REFLECTION_TIME", raw)
    runner.seed_principal_briefs()
    (row,) = _pending("executive_reflection")
    assert _local_hhmm(row.run_at, "America/New_York") == (7, 30)


def test_chain_uses_the_zone_across_a_dst_change() -> None:
    ws.restore_workspace_settings(ws.WorkspaceSettings(timezone="America/New_York"))
    # The morning brief fired Sat 7 March 2026 at 08:00 EST (13:00 UTC); the
    # next link is Sunday 08:00 EDT, which is 12:00 UTC.
    aid = runner._enqueue_next_principal_brief(
        "principal_brief_morning", after=datetime(2026, 3, 7, 13, 5, tzinfo=UTC)
    )
    assert aid is not None
    row = episodic.get_scheduled_action(aid)
    assert row is not None
    assert datetime.fromisoformat(row.run_at) == datetime(2026, 3, 8, 12, 0, tzinfo=UTC)


def test_timezone_change_cancels_and_reseeds_all_three(_isolated: Path) -> None:
    runner.seed_principal_briefs()
    old_ids = {kind: _pending(kind)[0].id for kind in BRIEF_KINDS}
    # An unrelated pending row must survive the re-time.
    other = episodic.insert_scheduled_action(
        run_at=datetime.now(UTC).isoformat(), channel="email", channel_ref="a@b.co",
        intent_text="follow up", kind="ad_hoc",
    )

    ws.set_timezone("Australia/Sydney")

    for kind in BRIEF_KINDS:
        (row,) = _pending(kind)
        assert row.id != old_ids[kind]
        old = episodic.get_scheduled_action(old_ids[kind])
        assert old is not None and old.status == "cancelled"
    (morning,) = _pending("principal_brief_morning")
    assert _local_hhmm(morning.run_at, "Australia/Sydney") == (8, 0)
    kept = episodic.get_scheduled_action(other)
    assert kept is not None and kept.status == "pending"


def test_timezone_change_leaves_a_running_row_alone(_isolated: Path) -> None:
    runner.seed_principal_briefs()
    running_id = _pending("principal_brief_morning")[0].id
    with sqlite3.connect(str(_isolated)) as conn:
        conn.execute("UPDATE scheduled_actions SET status = 'running' WHERE id = ?", (running_id,))

    ws.set_timezone("Europe/Warsaw")

    running = episodic.get_scheduled_action(running_id)
    assert running is not None and running.status == "running"
    # No second morning row: the running one chains its own successor.
    assert _pending("principal_brief_morning") == []
    # The other two were re-timed.
    for kind in ("principal_brief_eod", "executive_reflection"):
        assert len(_pending(kind)) == 1


def test_timezone_change_keeps_a_due_brief_held_by_a_pause(_isolated: Path) -> None:
    """A brief that is already due but not yet claimed (paused Executive, no
    company profile yet) must still go out — re-timing it to tomorrow would
    silently drop today's."""
    due = episodic.insert_scheduled_action(
        run_at=datetime(2020, 1, 1, 8, 0, tzinfo=UTC).isoformat(),
        channel="__internal__", channel_ref="principal", intent_text="brief",
        kind="principal_brief_morning",
    )
    ws.set_timezone("America/Bogota")
    held = episodic.get_scheduled_action(due)
    assert held is not None and held.status == "pending"
    assert [r.id for r in _pending("principal_brief_morning")] == [due]
    # The other kinds were seeded in the new zone.
    (eod,) = _pending("principal_brief_eod")
    assert _local_hhmm(eod.run_at, "America/Bogota") == (18, 0)


def test_reschedule_on_empty_db_just_seeds() -> None:
    assert runner.reschedule_principal_rhythm() == 3
    assert runner.reschedule_principal_rhythm() == 3  # re-times, never duplicates
    for kind in BRIEF_KINDS:
        assert len(_pending(kind)) == 1


def test_rotation_next_occurrence_stays_utc() -> None:
    """`_next_occurrence` (client rotation) is unchanged: plain UTC."""
    base = datetime(2026, 3, 7, 13, 0, tzinfo=UTC)
    assert runner._next_occurrence(base, 8, 0) == datetime(2026, 3, 8, 8, 0, tzinfo=UTC)
