"""``_parse_cadence_spec(spec, after, tz)`` — local wall-clock cadences.

The principal's rhythm fires at a local time in the user's zone; department
and workflow cadences keep calling the parser with the UTC default. US DST in
2026: clocks spring forward on Sun 8 March (02:00 → 03:00) and fall back on
Sun 1 November (02:00 → 01:00) in America/New_York.
"""
from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from openexecutive.departments.cadence import _parse_cadence_spec, is_valid_cadence_spec

NY = ZoneInfo("America/New_York")


def _local(dt: datetime) -> tuple[int, int, int, int, int]:
    loc = dt.astimezone(NY)
    return (loc.year, loc.month, loc.day, loc.hour, loc.minute)


# --------------------------------------------------------------------------- #
# Daily across both transitions
# --------------------------------------------------------------------------- #


def test_daily_across_spring_forward_keeps_local_time() -> None:
    # Sat 7 March, 09:00 EST (14:00 UTC) — just after the 08:00 brief.
    after = datetime(2026, 3, 7, 14, 0, tzinfo=UTC)
    nxt = _parse_cadence_spec("daily@08:00", after, NY)
    assert nxt == datetime(2026, 3, 8, 12, 0, tzinfo=UTC)  # 08:00 EDT = 12:00 UTC
    assert _local(nxt) == (2026, 3, 8, 8, 0)
    # And the day before, still standard time: 08:00 EST = 13:00 UTC.
    before = _parse_cadence_spec("daily@08:00", datetime(2026, 3, 6, 14, 0, tzinfo=UTC), NY)
    assert before == datetime(2026, 3, 7, 13, 0, tzinfo=UTC)


def test_daily_across_fall_back_keeps_local_time() -> None:
    # Sat 31 October, 19:00 EDT (23:00 UTC) — after the 18:00 digest.
    after = datetime(2026, 10, 31, 23, 0, tzinfo=UTC)
    nxt = _parse_cadence_spec("daily@18:00", after, NY)
    assert nxt == datetime(2026, 11, 1, 23, 0, tzinfo=UTC)  # 18:00 EST = 23:00 UTC
    assert _local(nxt) == (2026, 11, 1, 18, 0)


def test_daily_time_in_the_spring_gap_fires_once_after_the_jump() -> None:
    # 02:30 does not exist on 8 March in New York; it resolves to 03:30 EDT.
    after = datetime(2026, 3, 8, 5, 0, tzinfo=UTC)  # 00:00 EST
    nxt = _parse_cadence_spec("daily@02:30", after, NY)
    assert nxt == datetime(2026, 3, 8, 7, 30, tzinfo=UTC)
    assert _local(nxt) == (2026, 3, 8, 3, 30)
    # The next day is back to a normal 02:30 EDT.
    again = _parse_cadence_spec("daily@02:30", nxt, NY)
    assert _local(again) == (2026, 3, 9, 2, 30)


def test_daily_time_repeated_at_fall_back_fires_once() -> None:
    # 01:30 happens twice on 1 November; the first occurrence (EDT) is used,
    # and a caller just past it does not get the repeat an hour later.
    after = datetime(2026, 11, 1, 4, 0, tzinfo=UTC)  # 00:00 EDT
    first = _parse_cadence_spec("daily@01:30", after, NY)
    assert first == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)  # 01:30 EDT
    nxt = _parse_cadence_spec("daily@01:30", first, NY)
    assert nxt == datetime(2026, 11, 2, 6, 30, tzinfo=UTC)  # 01:30 EST next day
    # Even from inside the repeated hour (01:10 EST), not today's second 01:30.
    inside = datetime(2026, 11, 1, 6, 10, tzinfo=UTC)
    assert _parse_cadence_spec("daily@01:30", inside, NY) == nxt


def test_daily_uses_the_local_date_not_the_utc_date() -> None:
    # 22:00 EST on 10 Jan is already 03:00 UTC on 11 Jan; the next 08:00
    # local is 11 Jan, not 12 Jan.
    after = datetime(2026, 1, 11, 3, 0, tzinfo=UTC)
    assert _local(_parse_cadence_spec("daily@08:00", after, NY)) == (2026, 1, 11, 8, 0)


# --------------------------------------------------------------------------- #
# Weekly across both transitions
# --------------------------------------------------------------------------- #


def test_weekly_across_spring_forward() -> None:
    # Mon 2 March 2026, 10:00 EST — after this week's 09:00 check-in.
    after = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)
    nxt = _parse_cadence_spec("weekly@mon@09:00", after, NY)
    assert _local(nxt) == (2026, 3, 9, 9, 0)
    assert nxt == datetime(2026, 3, 9, 13, 0, tzinfo=UTC)  # EDT now: 13:00 UTC, not 14:00


def test_weekly_across_fall_back() -> None:
    # Fri 30 October 2026, 18:00 EDT — after this week's 17:00 slot.
    after = datetime(2026, 10, 30, 22, 0, tzinfo=UTC)
    nxt = _parse_cadence_spec("weekly@fri-17:00", after, NY)
    assert _local(nxt) == (2026, 11, 6, 17, 0)
    assert nxt == datetime(2026, 11, 6, 22, 0, tzinfo=UTC)  # EST now: 22:00 UTC


def test_weekly_same_day_later_today_local() -> None:
    after = datetime(2026, 3, 9, 12, 0, tzinfo=UTC)  # Mon 08:00 EDT
    assert _local(_parse_cadence_spec("weekly@mon@09:00", after, NY)) == (2026, 3, 9, 9, 0)


def test_quarterly_in_a_zone() -> None:
    after = datetime(2026, 2, 1, tzinfo=UTC)
    nxt = _parse_cadence_spec("quarterly@01-09:00", after, NY)
    assert _local(nxt) == (2026, 4, 1, 9, 0)
    assert nxt == datetime(2026, 4, 1, 13, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# The UTC default is unchanged
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("spec", "after", "expected"),
    [
        ("daily@09:00", datetime(2026, 5, 20, 8, 0, tzinfo=UTC), datetime(2026, 5, 20, 9, 0, tzinfo=UTC)),
        ("daily@09:00", datetime(2026, 5, 20, 9, 0, tzinfo=UTC), datetime(2026, 5, 21, 9, 0, tzinfo=UTC)),
        # DST dates are ordinary days in UTC.
        ("daily@08:00", datetime(2026, 3, 7, 14, 0, tzinfo=UTC), datetime(2026, 3, 8, 8, 0, tzinfo=UTC)),
        ("weekly@mon@09:00", datetime(2026, 3, 2, 15, 0, tzinfo=UTC), datetime(2026, 3, 9, 9, 0, tzinfo=UTC)),
        ("quarterly@01-09:00", datetime(2026, 2, 1, tzinfo=UTC), datetime(2026, 4, 1, 9, 0, tzinfo=UTC)),
        # A naive `after` is still read as UTC.
        ("daily@09:00", datetime(2026, 5, 20, 8, 0), datetime(2026, 5, 20, 9, 0, tzinfo=UTC)),
    ],
)
def test_utc_default_unchanged(spec: str, after: datetime, expected: datetime) -> None:
    assert _parse_cadence_spec(spec, after) == expected
    assert _parse_cadence_spec(spec, after, UTC) == expected


def test_after_in_another_zone_is_the_same_instant() -> None:
    after_ny = datetime(2026, 5, 20, 4, 0, tzinfo=NY)  # 08:00 UTC
    assert _parse_cadence_spec("daily@09:00", after_ny) == datetime(2026, 5, 20, 9, 0, tzinfo=UTC)


def test_out_of_range_times_still_invalid() -> None:
    assert not is_valid_cadence_spec("daily@25:00")
    assert not is_valid_cadence_spec("weekly@mon@09:61")
    assert not is_valid_cadence_spec("quarterly@01-24:00")
    with pytest.raises(ValueError):
        _parse_cadence_spec("daily@24:00", datetime(2026, 1, 1, tzinfo=UTC), NY)
