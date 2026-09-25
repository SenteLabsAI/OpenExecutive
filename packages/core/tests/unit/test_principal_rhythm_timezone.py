"""The principal's rhythm (morning brief, end-of-day digest, reflection) in the
user's time zone — scheduler/runner.py."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
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


# --------------------------------------------------------------------------- #
# Zone change: re-time pending rows in place (reschedule_principal_rhythm)
# --------------------------------------------------------------------------- #


def _row(kind: str, run_at: datetime, status: str = "pending") -> int:
    aid = episodic.insert_scheduled_action(
        run_at=run_at.isoformat(), channel="__internal__", channel_ref="principal",
        intent_text="brief", kind=kind,
    )
    if status != "pending":
        with sqlite3.connect(str(episodic.DB_PATH)) as conn:
            conn.execute("UPDATE scheduled_actions SET status = ? WHERE id = ?", (status, aid))
    return aid


def _snapshot() -> list[tuple[int, str, str, str]]:
    with sqlite3.connect(str(episodic.DB_PATH)) as conn:
        return sorted(conn.execute("SELECT id, kind, status, run_at FROM scheduled_actions"))


def _at(aid: int) -> datetime:
    row = episodic.get_scheduled_action(aid)
    assert row is not None
    return datetime.fromisoformat(row.run_at)


def _zone(name: str) -> None:
    ws.restore_workspace_settings(ws.WorkspaceSettings(timezone=name))


def _utc(*a: int) -> datetime:
    return datetime(*a, tzinfo=UTC)


def test_zone_change_retimes_each_pending_row_in_place() -> None:
    morning = _row("principal_brief_morning", _utc(2026, 7, 2, 8, 0))
    eod = _row("principal_brief_eod", _utc(2026, 7, 1, 18, 0))
    refl = _row("executive_reflection", _utc(2026, 7, 2, 7, 30))
    before = {r[0] for r in _snapshot()}

    _zone("America/Chicago")
    assert runner.reschedule_principal_rhythm(now=_utc(2026, 7, 1, 10, 0)) == 3

    # Same rows, still pending — nothing inserted or cancelled.
    assert {r[0] for r in _snapshot()} == before
    assert {r[2] for r in _snapshot()} == {"pending"}
    assert _at(morning) == _utc(2026, 7, 1, 13, 0)  # 08:00 CDT
    assert _at(eod) == _utc(2026, 7, 1, 23, 0)  # 18:00 CDT
    assert _at(refl) == _utc(2026, 7, 1, 12, 30)  # 07:30 CDT


def test_zone_change_just_before_the_brief_keeps_todays_brief() -> None:
    """At 07:59Z the 08:00Z brief is a minute away. In Europe/London (BST)
    08:00 is already past, so the new zone's next 08:00 is tomorrow — moving
    there would skip today's brief. It keeps 08:00Z once; the chain then
    lands on 08:00 BST."""
    morning = _row("principal_brief_morning", _utc(2026, 7, 1, 8, 0))
    _zone("Europe/London")
    runner.reschedule_principal_rhythm(now=_utc(2026, 7, 1, 7, 59))
    assert _at(morning) == _utc(2026, 7, 1, 8, 0)

    nxt = runner._enqueue_next_principal_brief(
        "principal_brief_morning", after=_utc(2026, 7, 1, 20, 0)
    )
    assert nxt is not None and _at(nxt) == _utc(2026, 7, 2, 7, 0)  # 08:00 BST


def test_zone_change_after_todays_brief_sends_no_second_one_today() -> None:
    """The brief fired at 08:00Z; at 09:00Z the user moves to Los Angeles,
    where 08:00 PDT (15:00Z) is still ahead today. The pending row must not
    move there — that is a second brief 7h after the first — but to 08:00
    PDT tomorrow."""
    _row("principal_brief_morning", _utc(2026, 7, 1, 8, 0), status="done")
    pending = _row("principal_brief_morning", _utc(2026, 7, 2, 8, 0))
    _zone("America/Los_Angeles")
    assert runner.reschedule_principal_rhythm(now=_utc(2026, 7, 1, 9, 0)) == 1

    new = _at(pending)
    assert new == _utc(2026, 7, 2, 15, 0)  # 08:00 PDT tomorrow
    assert new - _utc(2026, 7, 1, 8, 0) >= timedelta(hours=12)
    assert len(_pending("principal_brief_morning")) == 1


def test_zone_change_east_moves_earlier_but_not_within_12h_of_the_last_run() -> None:
    _row("principal_brief_morning", _utc(2026, 7, 1, 8, 0), status="done")
    pending = _row("principal_brief_morning", _utc(2026, 7, 2, 8, 0))
    _zone("Asia/Tokyo")
    runner.reschedule_principal_rhythm(now=_utc(2026, 7, 1, 10, 0))
    # 08:00 JST = 23:00Z. Today's (07-01 23:00Z) is 15h after the last run.
    assert _at(pending) == _utc(2026, 7, 1, 23, 0)


def test_zone_change_leaves_running_rows_and_inserts_nothing() -> None:
    running = _row("principal_brief_morning", _utc(2026, 7, 1, 8, 0), status="running")
    _row("principal_brief_eod", _utc(2026, 7, 1, 18, 0))
    _zone("Europe/Warsaw")
    runner.reschedule_principal_rhythm(now=_utc(2026, 7, 1, 8, 1))
    row = episodic.get_scheduled_action(running)
    assert row is not None and row.status == "running"
    assert _at(running) == _utc(2026, 7, 1, 8, 0)
    # No pending morning row appears: the running one chains its successor.
    assert _pending("principal_brief_morning") == []
    assert len(_snapshot()) == 2


def test_zone_change_keeps_a_due_brief_held_by_a_pause() -> None:
    """A brief that is already due but not yet claimed (paused Executive, no
    company profile yet) must still go out — re-timing it to tomorrow would
    silently drop today's."""
    due = _row("principal_brief_morning", _utc(2020, 1, 1, 8, 0))
    _zone("America/Bogota")
    assert runner.reschedule_principal_rhythm() == 0
    assert _at(due) == _utc(2020, 1, 1, 8, 0)
    assert len(_snapshot()) == 1


def test_zone_change_with_no_rows_inserts_nothing() -> None:
    _zone("Africa/Cairo")
    assert runner.reschedule_principal_rhythm() == 0
    assert _snapshot() == []


def test_zone_change_via_set_timezone_moves_rows_without_inserting() -> None:
    runner.seed_principal_briefs()
    before = {(r[0], r[1], r[2]) for r in _snapshot()}
    ws.set_timezone("Pacific/Auckland")
    assert {(r[0], r[1], r[2]) for r in _snapshot()} == before


def test_a_pinned_time_does_not_move(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRINCIPAL_BRIEF_MORNING_TIME", "06:30")
    pinned = _row("principal_brief_morning", _utc(2026, 7, 2, 6, 30))
    _zone("Asia/Kolkata")
    assert runner.reschedule_principal_rhythm(now=_utc(2026, 7, 1, 10, 0)) == 0
    assert _at(pinned) == _utc(2026, 7, 2, 6, 30)


def test_a_row_claimed_mid_retime_is_not_moved(monkeypatch: pytest.MonkeyPatch) -> None:
    """The UPDATE is guarded on the row being unchanged since it was read, so
    a claim (pending → running) in between wins."""
    morning = _row("principal_brief_morning", _utc(2026, 7, 2, 8, 0))
    real = runner._next_principal_run_at

    def _claim_then_compute(kind: str, after: datetime) -> datetime | None:
        with sqlite3.connect(str(episodic.DB_PATH)) as conn:
            conn.execute("UPDATE scheduled_actions SET status = 'running' WHERE id = ?", (morning,))
        return real(kind, after)

    monkeypatch.setattr(runner, "_next_principal_run_at", _claim_then_compute)
    _zone("America/Chicago")
    assert runner.reschedule_principal_rhythm(now=_utc(2026, 7, 1, 10, 0)) == 0
    assert _at(morning) == _utc(2026, 7, 2, 8, 0)


# --------------------------------------------------------------------------- #
# The chain keeps the same 12h floor
# --------------------------------------------------------------------------- #


def _action(run_at: datetime) -> episodic.ScheduledAction:
    return episodic.ScheduledAction(
        id=1, created_at=run_at.isoformat(), run_at=run_at.isoformat(), channel="__internal__",
        channel_ref="principal", intent_text="brief", kind="principal_brief_morning",
    )


def test_chain_after_is_12h_past_the_fired_occurrence() -> None:
    fired = datetime.now(UTC) - timedelta(minutes=5)
    assert runner._chain_after(_action(fired)) == fired + timedelta(hours=12)


def test_chain_after_a_long_held_brief_is_now() -> None:
    before = datetime.now(UTC)
    after = runner._chain_after(_action(before - timedelta(days=2)))
    assert before <= after <= datetime.now(UTC)


def test_chain_after_zone_change_mid_run_skips_to_tomorrow() -> None:
    """Fired at 08:00Z, the zone became Los Angeles while it ran: the chain
    lands on 08:00 PDT tomorrow, not 15:00Z today."""
    _zone("America/Los_Angeles")
    fired = _utc(2026, 7, 1, 8, 0)
    after = max(_utc(2026, 7, 1, 8, 5), fired + timedelta(hours=12))
    aid = runner._enqueue_next_principal_brief("principal_brief_morning", after=after)
    assert aid is not None and _at(aid) == _utc(2026, 7, 2, 15, 0)


def test_rotation_next_occurrence_stays_utc() -> None:
    """`_next_occurrence` (client rotation) is unchanged: plain UTC."""
    base = datetime(2026, 3, 7, 13, 0, tzinfo=UTC)
    assert runner._next_occurrence(base, 8, 0) == datetime(2026, 3, 8, 8, 0, tzinfo=UTC)
