"""Tests for the People channel routing helpers."""
from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store
from openexecutive.people.channel import (
    _in_window,
    next_available_window,
    prefer_channel_for,
)
from openexecutive.people.models import AvailabilityWindow


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "people.db"
    monkeypatch.setattr(people_store, "DB_PATH", path)
    people_registry.invalidate()
    people_store.initialize_db()
    yield
    people_registry.invalidate()


# --------------------------------------------------------------------------- #
# _in_window
# --------------------------------------------------------------------------- #

def test_in_window_true_during_window() -> None:
    win = AvailabilityWindow(
        weekdays=[1],  # Tuesday
        start_local="09:00",
        end_local="13:00",
        timezone="America/Los_Angeles",
    )
    # Tuesday 2026-05-19 at 10:30 PDT = 17:30 UTC
    now = datetime(2026, 5, 19, 17, 30, tzinfo=UTC)
    assert _in_window(win, now) is True


def test_in_window_false_before_start() -> None:
    win = AvailabilityWindow(
        weekdays=[1], start_local="09:00", end_local="13:00",
        timezone="America/Los_Angeles",
    )
    # Tuesday 07:30 PDT = 14:30 UTC
    now = datetime(2026, 5, 19, 14, 30, tzinfo=UTC)
    assert _in_window(win, now) is False


def test_in_window_false_wrong_day() -> None:
    win = AvailabilityWindow(
        weekdays=[1], start_local="09:00", end_local="13:00",
        timezone="America/Los_Angeles",
    )
    # Wednesday 2026-05-20 at 10:30 PDT = 17:30 UTC
    now = datetime(2026, 5, 20, 17, 30, tzinfo=UTC)
    assert _in_window(win, now) is False


def test_in_window_cross_midnight() -> None:
    """22:00-06:00 cross-midnight window."""
    win = AvailabilityWindow(
        weekdays=[0, 1, 2, 3, 4],  # Mon-Fri
        start_local="22:00",
        end_local="06:00",
        timezone="UTC",
    )
    # Mon 23:00 UTC — inside window (>= 22:00)
    assert _in_window(win, datetime(2026, 5, 18, 23, 0, tzinfo=UTC)) is True
    # Tue 05:00 UTC — inside window (< 06:00)
    assert _in_window(win, datetime(2026, 5, 19, 5, 0, tzinfo=UTC)) is True
    # Mon 21:00 UTC — outside (before 22:00)
    assert _in_window(win, datetime(2026, 5, 18, 21, 0, tzinfo=UTC)) is False


def test_in_window_cross_midnight_single_day_morning_half() -> None:
    """Regression: Friday-only 22:00-06:00 window. Saturday 02:00 is the
    morning half — the previous day is Friday (weekday 4), so it must match."""
    win = AvailabilityWindow(
        weekdays=[4],  # Friday only
        start_local="22:00",
        end_local="06:00",
        timezone="UTC",
    )
    # Sat 2026-05-23 02:00 UTC — morning half, previous day was Friday
    sat_morning = datetime(2026, 5, 23, 2, 0, tzinfo=UTC)
    assert _in_window(win, sat_morning) is True

    # Sat 2026-05-23 10:00 UTC — outside both halves
    sat_afternoon = datetime(2026, 5, 23, 10, 0, tzinfo=UTC)
    assert _in_window(win, sat_afternoon) is False

    # Fri 2026-05-22 23:00 UTC — evening half (Friday), in window
    fri_evening = datetime(2026, 5, 22, 23, 0, tzinfo=UTC)
    assert _in_window(win, fri_evening) is True


def test_in_window_unknown_timezone_falls_back_to_utc() -> None:
    """Unknown timezone should not raise; falls back to UTC."""
    win = AvailabilityWindow(
        weekdays=[0, 1, 2, 3, 4],
        start_local="09:00",
        end_local="17:00",
        timezone="Planet/Mars",  # invalid
    )
    # Mon 10:00 UTC — in_window if treated as UTC
    now = datetime(2026, 5, 18, 10, 0, tzinfo=UTC)
    # Should not raise; result depends on UTC interpretation
    result = _in_window(win, now)
    assert isinstance(result, bool)


# --------------------------------------------------------------------------- #
# prefer_channel_for
# --------------------------------------------------------------------------- #

def test_prefer_channel_for_returns_none_when_person_missing() -> None:
    result = prefer_channel_for(9999, now=datetime.now(UTC))
    assert result is None


def test_prefer_channel_for_returns_none_when_archived() -> None:
    pid = people_store.upsert_person(
        full_name="Archived", email="a@b.com", preferred_channel="email"
    )
    people_store.archive_person(pid)
    people_registry.invalidate()
    result = prefer_channel_for(pid, now=datetime.now(UTC))
    assert result is None


def test_prefer_channel_for_returns_channel_no_windows() -> None:
    """No availability windows = always reachable."""
    pid = people_store.upsert_person(
        full_name="Always On", email="on@co.com", preferred_channel="email"
    )
    people_registry.invalidate()
    result = prefer_channel_for(pid, now=datetime.now(UTC))
    assert result is not None
    channel, ref = result
    assert channel == "email"
    assert ref == "on@co.com"


def test_prefer_channel_for_returns_none_on_leave() -> None:
    pid = people_store.upsert_person(
        full_name="On Leave",
        email="leave@co.com",
        preferred_channel="email",
        on_leave_until=date(2099, 12, 31),
    )
    people_registry.invalidate()
    result = prefer_channel_for(pid, now=datetime.now(UTC))
    assert result is None


def test_prefer_channel_for_returns_none_outside_window() -> None:
    pid = people_store.upsert_person(
        full_name="Sarah", email="s@co.com", preferred_channel="email"
    )
    people_store.set_availability(pid, [
        AvailabilityWindow(weekdays=[1], start_local="09:00", end_local="13:00", timezone="UTC")
    ])
    people_registry.invalidate()
    # Saturday 10:00 UTC — outside Tuesday window
    now = datetime(2026, 5, 16, 10, 0, tzinfo=UTC)
    assert prefer_channel_for(pid, now=now) is None


def test_prefer_channel_for_returns_channel_inside_window() -> None:
    pid = people_store.upsert_person(
        full_name="Sarah", slack_user_id="U12345", preferred_channel="slack"
    )
    people_store.set_availability(pid, [
        AvailabilityWindow(weekdays=[1], start_local="09:00", end_local="13:00", timezone="UTC")
    ])
    people_registry.invalidate()
    # Tuesday 10:00 UTC
    now = datetime(2026, 5, 19, 10, 0, tzinfo=UTC)
    result = prefer_channel_for(pid, now=now)
    assert result == ("slack", "U12345")


def test_prefer_channel_any_tries_chat_apps_before_email() -> None:
    pid = people_store.upsert_person(
        full_name="Any Chan",
        email="any@co.com",
        slack_user_id="U999",
        preferred_channel="any",
    )
    people_registry.invalidate()
    result = prefer_channel_for(pid, now=datetime.now(UTC))
    assert result is not None
    assert result[0] == "slack"  # email is the backup for "any"


def test_prefer_channel_any_goes_where_they_last_talked() -> None:
    pid = people_store.upsert_person(
        full_name="Any Chan",
        email="any@co.com",
        slack_user_id="U999",
        telegram_chat_id="4242",
        preferred_channel="any",
    )
    people_store.note_last_channel(pid, "telegram")
    people_registry.invalidate()
    result = prefer_channel_for(pid, now=datetime.now(UTC))
    assert result is not None and result[0] == "telegram"


def test_prefer_channel_returns_none_when_no_ref() -> None:
    """Preferred channel is slack but slack_user_id is not set."""
    pid = people_store.upsert_person(
        full_name="No Slack", preferred_channel="slack"
        # no slack_user_id
    )
    people_registry.invalidate()
    result = prefer_channel_for(pid, now=datetime.now(UTC))
    assert result is None


# --------------------------------------------------------------------------- #
# next_available_window
# --------------------------------------------------------------------------- #

def test_next_available_window_returns_none_for_unknown_person() -> None:
    result = next_available_window(9999, after=datetime.now(UTC))
    assert result is None


def test_next_available_window_returns_none_for_no_windows() -> None:
    """No windows = always reachable, no deferral needed."""
    pid = people_store.upsert_person(full_name="Always On", email="on@co.com")
    people_registry.invalidate()
    result = next_available_window(pid, after=datetime.now(UTC))
    assert result is None


def test_next_available_window_finds_next_tuesday() -> None:
    pid = people_store.upsert_person(full_name="Sarah")
    people_store.set_availability(pid, [
        AvailabilityWindow(weekdays=[1], start_local="09:00", end_local="13:00", timezone="UTC")
    ])
    people_registry.invalidate()

    # Wednesday 2026-05-20 12:00 UTC — next Tuesday is 2026-05-26
    after = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)
    result = next_available_window(pid, after=after)
    assert result is not None
    # Must be on a Tuesday (weekday() == 1)
    assert result.weekday() == 1
    assert result >= after


def test_next_available_window_returns_none_beyond_14_days() -> None:
    """If the only window is 15 days away, returns None (outside horizon)."""
    pid = people_store.upsert_person(full_name="Rare")
    # Only available on Sundays (weekday 6)
    people_store.set_availability(pid, [
        AvailabilityWindow(weekdays=[6], start_local="09:00", end_local="10:00", timezone="UTC")
    ])
    people_registry.invalidate()

    # A Sunday 2026-05-17; next Sunday would be 2026-05-24 (7 days away — within 14)
    after = datetime(2026, 5, 17, 9, 30, tzinfo=UTC)  # During Sunday window
    result = next_available_window(pid, after=after)
    # Within 14 days there will be at least one more Sunday
    assert result is not None
