"""The scheduler's throttled alert expiry sweep (scheduler/runner.py)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from openexecutive.alerts import lifecycle
from openexecutive.memory import history
from openexecutive.monitoring.research import watch_policy
from openexecutive.scheduler import runner


@pytest.fixture(autouse=True)
def _reset_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "_last_alert_sweep_at", None)
    monkeypatch.setattr(watch_policy, "sweep", lambda now, db_path=None: {"expired": 0, "disabled": 0, "nudged": 0})
    monkeypatch.setattr(history, "sweep_expired", lambda now=None, db_path=None: 0)


def test_maybe_sweep_throttles_to_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[datetime] = []

    def fake_expire(now: datetime, db_path=None) -> int:  # noqa: ARG001
        calls.append(now)
        return 2

    monkeypatch.setattr(lifecycle, "expire_stale_alerts", fake_expire)
    t0 = datetime.now(UTC)
    assert runner._maybe_sweep_alerts(t0) == 2
    assert runner._maybe_sweep_alerts(t0 + timedelta(minutes=5)) == 0
    assert runner._maybe_sweep_alerts(t0 + timedelta(minutes=14)) == 0
    assert runner._maybe_sweep_alerts(t0 + timedelta(minutes=15)) == 2
    assert calls == [t0, t0 + timedelta(minutes=15)]


def test_maybe_sweep_swallows_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(now: datetime, db_path=None) -> int:  # noqa: ARG001
        raise RuntimeError("db locked")

    monkeypatch.setattr(lifecycle, "expire_stale_alerts", boom)
    assert runner._maybe_sweep_alerts(datetime.now(UTC)) == 0
    # The failed attempt still consumed the interval slot (no hot retry loop).
    assert runner._last_alert_sweep_at is not None


def test_maybe_sweep_runs_the_watchlist_sweep_on_the_same_throttle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[datetime] = []
    monkeypatch.setattr(lifecycle, "expire_stale_alerts", lambda now, db_path=None: 0)
    monkeypatch.setattr(
        watch_policy, "sweep",
        lambda now, db_path=None: (seen.append(now), {"expired": 1, "disabled": 0, "nudged": 0})[1],
    )
    t0 = datetime.now(UTC)
    runner._maybe_sweep_alerts(t0)
    runner._maybe_sweep_alerts(t0 + timedelta(minutes=5))
    assert seen == [t0]


def test_watchlist_sweep_failure_never_breaks_the_alert_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lifecycle, "expire_stale_alerts", lambda now, db_path=None: 3)

    def boom(now: datetime, db_path=None) -> dict[str, int]:  # noqa: ARG001
        raise RuntimeError("watchlist db locked")

    monkeypatch.setattr(watch_policy, "sweep", boom)
    assert runner._maybe_sweep_alerts(datetime.now(UTC)) == 3


def test_expired_notes_are_swept_on_the_same_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[datetime] = []
    monkeypatch.setattr(lifecycle, "expire_stale_alerts", lambda now, db_path=None: 0)
    monkeypatch.setattr(history, "sweep_expired", lambda now=None, db_path=None: (seen.append(now), 4)[1])
    t0 = datetime.now(UTC)
    runner._maybe_sweep_alerts(t0)
    runner._maybe_sweep_alerts(t0 + timedelta(minutes=5))
    assert seen == [t0]


def test_a_note_sweep_failure_never_breaks_the_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lifecycle, "expire_stale_alerts", lambda now, db_path=None: 1)

    def boom(now: datetime | None = None, db_path: object = None) -> int:
        raise RuntimeError("notes db locked")

    monkeypatch.setattr(history, "sweep_expired", boom)
    assert runner._maybe_sweep_alerts(datetime.now(UTC)) == 1
