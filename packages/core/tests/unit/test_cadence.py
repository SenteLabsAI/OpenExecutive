"""Tests for departments/cadence.py."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from openexecutive.departments import registry as dept_registry
from openexecutive.departments import store as dept_store
from openexecutive.departments.cadence import (
    _parse_cadence_spec,
    bootstrap_cadences,
    cancel_orphaned_cadences,
    enqueue_next,
    is_valid_cadence_spec,
)
from openexecutive.memory import episodic


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "test.db"
    monkeypatch.setattr(dept_store, "DB_PATH", db)
    monkeypatch.setattr(episodic, "DB_PATH", db)
    dept_registry.invalidate()
    episodic.initialize_db(db)
    dept_store.initialize_db(db)
    yield
    dept_registry.invalidate()


# --------------------------------------------------------------------------- #
# _parse_cadence_spec
# --------------------------------------------------------------------------- #

class TestParseCadenceSpec:
    def test_daily_after_time_today(self) -> None:
        """After 08:00 UTC, next daily@09:00 is today at 09:00."""
        after = datetime(2026, 5, 20, 8, 0, tzinfo=UTC)
        result = _parse_cadence_spec("daily@09:00", after)
        assert result is not None
        assert result.date() == after.date()
        assert result.hour == 9
        assert result.minute == 0

    def test_daily_at_exact_time_advances_one_day(self) -> None:
        """When after == target time, advance to tomorrow."""
        after = datetime(2026, 5, 20, 9, 0, tzinfo=UTC)
        result = _parse_cadence_spec("daily@09:00", after)
        assert result is not None
        assert result.date() == (after + timedelta(days=1)).date()

    def test_daily_after_time_advances_one_day(self) -> None:
        """After 10:00 UTC, next daily@09:00 is tomorrow."""
        after = datetime(2026, 5, 20, 10, 30, tzinfo=UTC)
        result = _parse_cadence_spec("daily@09:00", after)
        assert result is not None
        assert result.date() == (after + timedelta(days=1)).date()
        assert result.hour == 9

    def test_daily_result_is_utc(self) -> None:
        after = datetime(2026, 5, 20, 8, 0, tzinfo=UTC)
        result = _parse_cadence_spec("daily@09:00", after)
        assert result is not None
        assert result.tzinfo is not None

    def test_weekly_finds_correct_day(self) -> None:
        """2026-05-20 is a Wednesday (weekday=2). Next Tuesday should be 2026-05-26."""
        after = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)  # Wednesday
        result = _parse_cadence_spec("weekly@tue@09:00", after)
        assert result is not None
        assert result.weekday() == 1  # Tuesday
        assert result > after

    def test_weekly_same_day_before_time(self) -> None:
        """If today is Tuesday and time hasn't passed, return today."""
        after = datetime(2026, 5, 19, 8, 0, tzinfo=UTC)  # Tuesday 08:00
        result = _parse_cadence_spec("weekly@tue@09:00", after)
        assert result is not None
        assert result.weekday() == 1
        assert result.date() == after.date()
        assert result.hour == 9

    def test_weekly_same_day_after_time_advances_one_week(self) -> None:
        """If today is Tuesday and time has passed, return next Tuesday."""
        after = datetime(2026, 5, 19, 10, 0, tzinfo=UTC)  # Tuesday 10:00, after 09:00
        result = _parse_cadence_spec("weekly@tue@09:00", after)
        assert result is not None
        assert result.weekday() == 1
        assert result.date() > after.date()

    def test_weekly_mon(self) -> None:
        after = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)  # Wednesday
        result = _parse_cadence_spec("weekly@mon@09:00", after)
        assert result is not None
        assert result.weekday() == 0

    def test_unknown_spec_returns_none(self) -> None:
        after = datetime(2026, 5, 20, 8, 0, tzinfo=UTC)
        assert _parse_cadence_spec("hourly@30", after) is None
        assert _parse_cadence_spec("", after) is None
        assert _parse_cadence_spec("monthly@01@09:00", after) is None

    def test_unknown_dow_returns_none(self) -> None:
        after = datetime(2026, 5, 20, 8, 0, tzinfo=UTC)
        assert _parse_cadence_spec("weekly@xyz@09:00", after) is None

    def test_naive_after_treated_as_utc(self) -> None:
        """Naive datetimes should not raise."""
        after = datetime(2026, 5, 20, 8, 0)  # no tzinfo
        result = _parse_cadence_spec("daily@09:00", after)
        assert result is not None

    # ------------------------------------------------------------------ #
    # weekly with hyphen separator (fixture format: weekly@DOW-HH:MM)
    # ------------------------------------------------------------------ #

    def test_weekly_hyphen_separator(self) -> None:
        """weekly@thu-10:00 should parse identically to weekly@thu@10:00."""
        after = datetime(2026, 5, 20, 8, 0, tzinfo=UTC)  # Wednesday
        dash = _parse_cadence_spec("weekly@thu-10:00", after)
        at = _parse_cadence_spec("weekly@thu@10:00", after)
        assert dash is not None
        assert at is not None
        assert dash == at

    def test_weekly_hyphen_correct_day(self) -> None:
        after = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)  # Wednesday
        result = _parse_cadence_spec("weekly@thu-10:00", after)
        assert result is not None
        assert result.weekday() == 3  # Thursday
        assert result > after

    def test_weekly_hyphen_unknown_dow_returns_none(self) -> None:
        after = datetime(2026, 5, 20, 8, 0, tzinfo=UTC)
        assert _parse_cadence_spec("weekly@xyz-09:00", after) is None

    # ------------------------------------------------------------------ #
    # quarterly
    # ------------------------------------------------------------------ #

    def test_quarterly_advances_to_next_quarter(self) -> None:
        """2026-05-22 is Q2. Day 1 of Q2 (Apr 1) has passed → next is Jul 1 (Q3)."""
        after = datetime(2026, 5, 22, 10, 0, tzinfo=UTC)
        result = _parse_cadence_spec("quarterly@01-09:00", after)
        assert result is not None
        assert result.month == 7
        assert result.day == 1
        assert result.year == 2026
        assert result.hour == 9

    def test_quarterly_same_quarter_before_day(self) -> None:
        """2026-04-01 08:00 UTC — day 1 of Q2 hasn't fired yet today."""
        after = datetime(2026, 4, 1, 8, 0, tzinfo=UTC)
        result = _parse_cadence_spec("quarterly@01-09:00", after)
        assert result is not None
        assert result.month == 4
        assert result.day == 1
        assert result.hour == 9

    def test_quarterly_wraps_year(self) -> None:
        """2026-11-01 — Q4 day 1 has passed → next is Jan 1 2027."""
        after = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
        result = _parse_cadence_spec("quarterly@01-09:00", after)
        assert result is not None
        assert result.year == 2027
        assert result.month == 1
        assert result.day == 1

    def test_quarterly_result_is_utc(self) -> None:
        after = datetime(2026, 5, 22, 10, 0, tzinfo=UTC)
        result = _parse_cadence_spec("quarterly@01-09:00", after)
        assert result is not None
        assert result.tzinfo is not None

    def test_quarterly_result_strictly_after(self) -> None:
        after = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)  # exactly at Q3 fire time
        result = _parse_cadence_spec("quarterly@01-09:00", after)
        assert result is not None
        assert result > after
        assert result.month == 10  # Q4

    def test_quarterly_day_31_skips_invalid_months(self) -> None:
        """Day 31 is invalid for Apr/Jun/Sep/Nov — should skip to next valid quarter."""
        # 2026-01-01 10:00 — Q1 day 31 (Jan 31) has passed; Apr has no day 31;
        # Jul 31 is the first valid next occurrence.
        after = datetime(2026, 1, 31, 10, 0, tzinfo=UTC)
        result = _parse_cadence_spec("quarterly@31-09:00", after)
        assert result is not None
        assert result.month == 7  # July has day 31
        assert result.day == 31


# --------------------------------------------------------------------------- #
# bootstrap_cadences
# --------------------------------------------------------------------------- #

class TestBootstrapCadences:
    def test_creates_actions_for_departments_with_cadence(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        dept_store.seed_default_departments(db_path=db)
        dept_registry.invalidate()

        count = bootstrap_cadences(db_path=db)
        # 8 departments, all seeded with DEFAULT_CHECK_IN_CADENCE "daily@09:00"
        assert count == 8

        actions = episodic.list_scheduled_actions(db_path=db)
        cadence_actions = [a for a in actions if a.kind == "dept_cadence"]
        assert len(cadence_actions) == 8

    def test_idempotent_no_double_insert(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        dept_store.seed_default_departments(db_path=db)
        dept_registry.invalidate()

        first = bootstrap_cadences(db_path=db)
        second = bootstrap_cadences(db_path=db)

        assert first == 8
        assert second == 0  # nothing new inserted

        actions = episodic.list_scheduled_actions(db_path=db)
        cadence_actions = [a for a in actions if a.kind == "dept_cadence"]
        assert len(cadence_actions) == 8

    def test_action_channel_and_ref(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        dept_store.seed_default_departments(db_path=db)
        dept_registry.invalidate()
        bootstrap_cadences(db_path=db)

        actions = episodic.list_scheduled_actions(db_path=db)
        finance_action = next(
            (a for a in actions if a.kind == "dept_cadence" and a.department == "finance"),
            None,
        )
        assert finance_action is not None
        assert finance_action.channel == "__internal__"
        assert finance_action.channel_ref == "finance"
        assert finance_action.status == "pending"

    def test_no_cadence_no_action(self, tmp_path: Path) -> None:
        """A department with no cadence spec should not get an action."""
        db = tmp_path / "test.db"
        dept_store.seed_default_departments(db_path=db)
        # Remove cadence from finance
        dept_store.update_department("finance", cadences={}, db_path=db)
        dept_registry.invalidate()

        count = bootstrap_cadences(db_path=db)
        assert count == 7  # 8 - 1 with empty cadence

        actions = episodic.list_scheduled_actions(db_path=db)
        finance_cadences = [
            a for a in actions if a.kind == "dept_cadence" and a.department == "finance"
        ]
        assert len(finance_cadences) == 0


# --------------------------------------------------------------------------- #
# enqueue_next
# --------------------------------------------------------------------------- #

class TestEnqueueNext:
    def test_creates_next_action(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        dept_store.seed_default_departments(db_path=db)
        dept_registry.invalidate()

        after = datetime(2026, 5, 20, 8, 0, tzinfo=UTC)
        action_id = enqueue_next("finance", after=after, db_path=db)
        assert action_id is not None
        assert action_id > 0

        action = episodic.get_scheduled_action(action_id, db_path=db)
        assert action is not None
        assert action.kind == "dept_cadence"
        assert action.department == "finance"
        assert action.channel == "__internal__"
        assert action.channel_ref == "finance"
        # run_at should be after the `after` timestamp
        run_at_dt = datetime.fromisoformat(action.run_at)
        if run_at_dt.tzinfo is None:
            run_at_dt = run_at_dt.replace(tzinfo=UTC)
        assert run_at_dt > after

    def test_run_at_respects_daily_spec(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        dept_store.seed_default_departments(db_path=db)
        # Finance uses "daily@09:00"
        dept_registry.invalidate()

        after = datetime(2026, 5, 20, 10, 0, tzinfo=UTC)  # after 09:00 today
        action_id = enqueue_next("finance", after=after, db_path=db)
        assert action_id is not None

        action = episodic.get_scheduled_action(action_id, db_path=db)
        assert action is not None
        run_at_dt = datetime.fromisoformat(action.run_at)
        # Should be 09:00 on 2026-05-21 (next day)
        assert run_at_dt.hour == 9
        assert run_at_dt.day == 21

    def test_unknown_department_returns_none(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        dept_store.seed_default_departments(db_path=db)
        dept_registry.invalidate()

        result = enqueue_next("nonexistent", db_path=db)
        assert result is None

    def test_no_cadence_returns_none(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        dept_store.seed_default_departments(db_path=db)
        dept_store.update_department("finance", cadences={}, db_path=db)
        dept_registry.invalidate()

        result = enqueue_next("finance", db_path=db)
        assert result is None


# --------------------------------------------------------------------------- #
# delete_department cadence cleanup
# --------------------------------------------------------------------------- #

def _pending_cadence_for(db: Path, slug: str) -> list[object]:
    return [
        a
        for a in episodic.list_scheduled_actions(db_path=db)
        if a.kind == "dept_cadence" and a.department == slug and a.status == "pending"
    ]


class TestDeleteCancelsCadence:
    def test_delete_cancels_pending_cadence(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        dept_store.seed_default_departments(db_path=db)
        dept_registry.invalidate()
        bootstrap_cadences(db_path=db)
        assert _pending_cadence_for(db, "finance")  # precondition

        assert dept_store.delete_department("finance", db_path=db) is True

        assert _pending_cadence_for(db, "finance") == []
        # Other departments' cadences are untouched.
        assert _pending_cadence_for(db, "marketing")

    def test_delete_without_scheduled_actions_table(self, tmp_path: Path) -> None:
        # delete_department must not blow up when the scheduled_actions table
        # has not been created (narrow store-only setups).
        db = tmp_path / "store_only.db"
        dept_store.initialize_db(db)
        dept_store.seed_default_departments(db_path=db)
        # No episodic.initialize_db → no scheduled_actions table.
        assert dept_store.delete_department("finance", db_path=db) is True


# --------------------------------------------------------------------------- #
# cancel_orphaned_cadences
# --------------------------------------------------------------------------- #

class TestCancelOrphanedCadences:
    def test_cancels_orphan_keeps_live(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        dept_store.seed_default_departments(db_path=db)
        dept_registry.invalidate()
        bootstrap_cadences(db_path=db)

        # Strand a cadence for a department that no longer exists.
        episodic.insert_scheduled_action(
            run_at=datetime.now(UTC).isoformat(),
            channel="__internal__",
            channel_ref="ghost",
            intent_text="Department check-in: Ghost",
            department="ghost",
            kind="dept_cadence",
            db_path=db,
        )

        cancelled = cancel_orphaned_cadences(db_path=db)
        assert cancelled == 1
        assert _pending_cadence_for(db, "ghost") == []
        # Live departments keep their pending cadence.
        assert _pending_cadence_for(db, "finance")

    def test_idempotent(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        dept_store.seed_default_departments(db_path=db)
        dept_registry.invalidate()
        bootstrap_cadences(db_path=db)
        episodic.insert_scheduled_action(
            run_at=datetime.now(UTC).isoformat(),
            channel="__internal__",
            channel_ref="ghost",
            intent_text="Department check-in: Ghost",
            department="ghost",
            kind="dept_cadence",
            db_path=db,
        )

        assert cancel_orphaned_cadences(db_path=db) == 1
        assert cancel_orphaned_cadences(db_path=db) == 0

    def test_no_op_when_all_live(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        dept_store.seed_default_departments(db_path=db)
        dept_registry.invalidate()
        bootstrap_cadences(db_path=db)

        assert cancel_orphaned_cadences(db_path=db) == 0


@pytest.mark.parametrize(
    ("spec", "valid"),
    [
        ("daily@09:00", True),
        ("weekly@mon@09:00", True),
        ("weekly@thu-17:30", True),
        ("quarterly@15-08:00", True),
        ("mondays@09:00", False),
        ("weekly@xyz@09:00", False),
        ("daily@24:00", False),
        ("daily@09:60", False),
        ("quarterly@32-09:00", False),
        ("", False),
    ],
)
def test_is_valid_cadence_spec(spec: str, valid: bool) -> None:
    assert is_valid_cadence_spec(spec) is valid
