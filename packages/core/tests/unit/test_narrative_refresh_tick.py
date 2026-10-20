"""scheduler.runner._maybe_refresh_narrative: the /today header's precompute."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from openexecutive.scheduler import runner


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []

    async def _refresh() -> bool:
        calls.append(1)
        return True

    from openexecutive.api.routes import today

    monkeypatch.setattr(today, "refresh_principal_narrative", _refresh)
    monkeypatch.setattr(runner, "_last_narrative_refresh_at", None)
    monkeypatch.setattr(runner, "_narrative_task", None)
    monkeypatch.setattr(
        "openexecutive.memory.workspace_settings.get_user_timezone", lambda *a, **k: UTC
    )
    return calls


def _settings(monkeypatch: pytest.MonkeyPatch, minutes: int = 10) -> None:
    fake = SimpleNamespace(
        briefing_narrative_refresh_minutes=minutes,
        briefing_narrative_refresh_start_hour=6,
        briefing_narrative_refresh_end_hour=22,
    )
    monkeypatch.setattr("openexecutive.config.get_settings", lambda: fake)


async def test_refresh_is_throttled_and_daytime_only(
    started: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings(monkeypatch)
    noon = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)

    assert runner._maybe_refresh_narrative(noon) is True
    await asyncio.sleep(0)
    assert runner._maybe_refresh_narrative(noon + timedelta(minutes=5)) is False
    assert runner._maybe_refresh_narrative(noon + timedelta(minutes=11)) is True
    await asyncio.sleep(0)
    assert len(started) == 2

    monkeypatch.setattr(runner, "_last_narrative_refresh_at", None)
    night = datetime(2026, 9, 28, 23, 30, tzinfo=UTC)
    assert runner._maybe_refresh_narrative(night) is False


async def test_refresh_off_when_minutes_is_zero(
    started: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings(monkeypatch, minutes=0)
    assert runner._maybe_refresh_narrative(datetime(2026, 9, 28, 12, tzinfo=UTC)) is False
    assert started == []


async def test_a_running_refresh_is_not_doubled(
    started: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings(monkeypatch, minutes=1)
    gate = asyncio.Event()

    async def _slow() -> bool:
        await gate.wait()
        return True

    from openexecutive.api.routes import today

    monkeypatch.setattr(today, "refresh_principal_narrative", _slow)
    noon = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
    assert runner._maybe_refresh_narrative(noon) is True
    assert runner._maybe_refresh_narrative(noon + timedelta(minutes=5)) is False
    gate.set()
    task: Any = runner._narrative_task
    await task
