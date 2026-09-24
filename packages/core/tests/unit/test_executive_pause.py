"""Global pause switch (scheduler/pause.py) and the loops it gates."""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from openexecutive.memory import episodic
from openexecutive.scheduler import pause as pause_store
from openexecutive.workflows import persistence as wf_persistence


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", db)
    monkeypatch.setattr(wf_persistence, "DB_PATH", db)
    episodic.initialize_db(db)
    wf_persistence.initialize_runs_db(db)
    # Audited paths (apply_resolution) must not leak rows into ./episodic_memory.db.
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **k: None)
    monkeypatch.setattr(pause_store, "_read_failing", False)
    return db


def _insert_action(run_at: datetime, status: str = "pending") -> int:
    return episodic.insert_scheduled_action(
        run_at=run_at.isoformat(),
        channel="telegram",
        channel_ref="42",
        intent_text="say hi",
        status=status,
    )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def test_default_state_is_running_and_reads_create_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "nope.db"
    monkeypatch.setattr(episodic, "DB_PATH", missing)
    assert pause_store.get_pause_state().paused is False
    assert pause_store.is_paused() is False
    assert pause_store.count_held_actions() == 0
    assert not missing.exists(), "read path must never create the DB file"


def test_pause_resume_round_trip() -> None:
    state = pause_store.pause("ceo@example.com", "board offsite")
    assert state.paused is True
    assert state.paused_by == "ceo@example.com"
    assert state.reason == "board offsite"
    assert state.paused_at is not None
    assert pause_store.is_paused() is True

    state = pause_store.resume("ceo@example.com")
    assert state.paused is False
    assert state.paused_at is None and state.paused_by is None and state.reason is None
    assert pause_store.is_paused() is False


def test_repause_keeps_original_start_and_reason() -> None:
    first = pause_store.pause("a@example.com", "first")
    second = pause_store.pause("b@example.com", "second")
    assert second.paused_at == first.paused_at
    assert second.paused_by == "a@example.com"
    assert second.reason == "first"


def test_pause_after_resume_starts_fresh() -> None:
    pause_store.pause("a@example.com", "first")
    pause_store.resume("a@example.com")
    again = pause_store.pause("b@example.com", None)
    assert again.paused_by == "b@example.com"
    assert again.reason is None


def test_is_paused_fails_closed_on_unreadable_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"this is not a sqlite database" * 100)
    monkeypatch.setattr(episodic, "DB_PATH", garbage)
    with pytest.raises(sqlite3.DatabaseError):
        pause_store.get_pause_state()
    # A brake that can't be read holds work rather than silently releasing it.
    assert pause_store.is_paused() is True


def test_count_held_actions_counts_only_due_pending_rows() -> None:
    now = datetime.now(UTC)
    _insert_action(now - timedelta(minutes=5))
    _insert_action(now - timedelta(minutes=1))
    _insert_action(now + timedelta(hours=1))  # not yet due
    _insert_action(now - timedelta(minutes=5), status="done")
    assert pause_store.count_held_actions(now) == 2


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


async def _run_briefly(coro: Any) -> None:
    task = asyncio.create_task(coro)
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def _patch_scheduler(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    calls = {"claim": 0, "sweep": 0}

    def _claim(now: datetime) -> list[Any]:
        calls["claim"] += 1
        return []

    def _sweep(now: datetime) -> int:
        calls["sweep"] += 1
        return 0

    monkeypatch.setattr("openexecutive.scheduler.runner.claim_due_actions", _claim)
    monkeypatch.setattr("openexecutive.scheduler.runner._maybe_sweep_alerts", _sweep)
    monkeypatch.setattr(
        "openexecutive.scheduler.runner._company_profile_active", lambda: True
    )
    monkeypatch.setattr("openexecutive.scheduler.runner.seed_principal_briefs", lambda: 0)
    return calls


def test_scheduler_claims_nothing_while_paused(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.scheduler.runner import run_scheduler

    calls = _patch_scheduler(monkeypatch)
    pause_store.pause("ceo@example.com")
    asyncio.run(_run_briefly(run_scheduler(gateway=None, poll_interval_seconds=60)))
    assert calls["claim"] == 0
    # Only the one boot-time sweep; the paused tick skips it.
    assert calls["sweep"] == 1


def test_scheduler_claims_when_running(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.scheduler.runner import run_scheduler

    calls = _patch_scheduler(monkeypatch)
    asyncio.run(_run_briefly(run_scheduler(gateway=None, poll_interval_seconds=60)))
    assert calls["claim"] == 1
    assert calls["sweep"] == 2  # boot + first tick


def test_email_poller_skips_gmail_while_paused(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.integrations import email_poller

    polled: list[str] = []

    async def _discover(gateway: Any) -> None:
        polled.append("discover")

    async def _poll(gateway: Any) -> None:
        polled.append("poll")

    monkeypatch.setattr(email_poller, "_discover_gmail_tools", _discover)
    monkeypatch.setattr(email_poller, "poll_once", _poll)

    pause_store.pause("ceo@example.com")
    asyncio.run(_run_briefly(email_poller.run_email_poller(object())))  # type: ignore[arg-type]
    assert polled == []

    pause_store.resume("ceo@example.com")
    asyncio.run(_run_briefly(email_poller.run_email_poller(object())))  # type: ignore[arg-type]
    assert polled == ["discover", "poll"]


def test_resumer_holds_startup_sweep_and_ticks_while_paused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.workflows import resumer

    calls: list[str] = []

    async def _startup() -> None:
        calls.append("startup")

    async def _tick(now: datetime) -> None:
        calls.append("tick")

    monkeypatch.setattr(resumer, "_startup_sweep", _startup)
    monkeypatch.setattr(resumer, "_tick", _tick)

    pause_store.pause("ceo@example.com")
    asyncio.run(_run_briefly(resumer.run_resumer(poll_interval_seconds=60)))
    assert calls == []

    pause_store.resume("ceo@example.com")
    asyncio.run(_run_briefly(resumer.run_resumer(poll_interval_seconds=60)))
    assert calls == ["startup", "tick"]


def test_resumer_runs_deferred_startup_sweep_once_on_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One loop that boots paused and is then resumed: the held startup sweep
    runs exactly once, before the first tick, and ticks continue after."""
    from openexecutive.workflows import resumer

    calls: list[str] = []

    async def _startup() -> None:
        calls.append("startup")

    async def _tick(now: datetime) -> None:
        calls.append("tick")

    monkeypatch.setattr(resumer, "_startup_sweep", _startup)
    monkeypatch.setattr(resumer, "_tick", _tick)
    states = iter([True, True, False, False, False])
    monkeypatch.setattr(pause_store, "is_paused", lambda: next(states, False))

    asyncio.run(_run_briefly(resumer.run_resumer(poll_interval_seconds=0)))
    assert calls[:3] == ["startup", "tick", "tick"]
    assert calls.count("startup") == 1


def _seed_resumable_run(run_id: str) -> None:
    import json

    wf_persistence.create_run(run_id, "test_wf", "Test run", {})
    state = json.dumps({
        "on_timeout": "escalate",
        "channel": "slack",
        "channel_ref": "U123",
        "expected_reply_shape": "approve_reject",
        "question": "Please approve.",
    })
    wf_persistence.save_checkpoint(
        run_id,
        state,
        5,
        datetime.now(UTC) + timedelta(hours=1),
        resume_state_json='{"workflow": "test_wf"}',
    )


@pytest.mark.parametrize("paused", [True, False])
def test_apply_resolution_defers_resume_while_paused(
    monkeypatch: pytest.MonkeyPatch, paused: bool
) -> None:
    from openexecutive.workflows import resumer
    from openexecutive.workflows.wait_for_human import WaitForHumanResolution

    kicked: list[str] = []
    monkeypatch.setattr(
        resumer, "_kick_resume", lambda run_id, db_path=None: kicked.append(run_id)
    )
    _seed_resumable_run("run-p")
    if paused:
        pause_store.pause("ceo@example.com")

    resolution = WaitForHumanResolution(
        run_id="run-p",
        reply_text="approved",
        source_channel="slack",
        source_message_id="m-1",
        parsed_decision={"decision": "approve", "note": ""},
        person_id=5,
    )
    assert asyncio.run(resumer.apply_resolution("run-p", resolution)) is True
    # The decision is always recorded; only the immediate kick is held.
    run = wf_persistence.get_run("run-p")
    assert run is not None and run["status"] == "resolved"
    assert kicked == ([] if paused else ["run-p"])
