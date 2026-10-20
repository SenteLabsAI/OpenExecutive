"""Integration tests for the proactive scheduler.

We stub out `Executive.chat` and `load_or_create_profile` to avoid hitting the
Anthropic API and YAML loading. The runner is exercised against a real SQLite
file via `claim_due_actions` so the atomic-claim guarantee is also covered.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from openexecutive.audit import logger as audit_logger
from openexecutive.memory.episodic import (
    ScheduledAction,
    claim_due_actions,
    get_scheduled_action,
    initialize_db,
    insert_scheduled_action,
)
from openexecutive.scheduler.runner import _execute_action, run_scheduler


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "episodic.db"
    initialize_db(db_path)
    monkeypatch.setattr("openexecutive.memory.episodic.DB_PATH", db_path)
    # Patching DB_PATH doesn't reach the audit logger, which keeps its own
    # path, so _execute_action's audit rows would otherwise land in
    # ./episodic_memory.db in the working directory (see CLAUDE.md, Testing).
    monkeypatch.setattr(audit_logger, "_default_logger", audit_logger.AuditLogger(db_path=db_path))
    return db_path


def _stub_executive_deps(monkeypatch: pytest.MonkeyPatch, raise_in_chat: bool = False) -> None:
    """Replace Executive + profile + retriever so _execute_action runs offline."""

    class _StubExec:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def chat(self, **_kwargs: object) -> str:
            if raise_in_chat:
                raise RuntimeError("simulated")
            return "ok"

    class _EmptyProfile:
        def is_empty(self) -> bool:
            return True

    monkeypatch.setattr(
        "openexecutive.orchestrator.executive.Executive", _StubExec
    )
    monkeypatch.setattr(
        "openexecutive.onboarding.profile_builder.load_or_create_profile",
        lambda: _EmptyProfile(),
    )
    monkeypatch.setattr(
        "openexecutive.knowledge.retriever.retrieve", lambda query: ""
    )


def test_execute_action_marks_done_on_success(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_executive_deps(monkeypatch)
    action_id = insert_scheduled_action(
        run_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        channel="telegram",
        channel_ref="42",
        intent_text="say hi",
    )
    claimed = claim_due_actions(datetime.now(UTC))
    assert len(claimed) == 1
    asyncio.run(_execute_action(claimed[0], gateway=None))

    stored = get_scheduled_action(action_id)
    assert stored is not None
    assert stored.status == "done"


def test_execute_action_retries_on_exception(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_executive_deps(monkeypatch, raise_in_chat=True)
    action_id = insert_scheduled_action(
        run_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        channel="telegram",
        channel_ref="42",
        intent_text="say hi",
    )
    claimed = claim_due_actions(datetime.now(UTC))
    asyncio.run(_execute_action(claimed[0], gateway=None))

    stored = get_scheduled_action(action_id)
    assert stored is not None
    # First failure → status flips back to pending with last_error set.
    assert stored.status == "pending"
    assert stored.last_error == "simulated"


def test_email_channel_without_gateway_short_circuits(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Email channel requires MCP — _execute_action must fail fast when gateway is None."""
    _stub_executive_deps(monkeypatch)

    action_id = insert_scheduled_action(
        run_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        channel="email",
        channel_ref="boss@example.com",
        intent_text="follow up",
    )
    claimed = claim_due_actions(datetime.now(UTC))
    asyncio.run(_execute_action(claimed[0], gateway=None))

    stored = get_scheduled_action(action_id)
    assert stored is not None
    assert "MCP gateway" in stored.last_error


def _set_profile_active(monkeypatch: pytest.MonkeyPatch, *, active: bool) -> None:
    """Force `_company_profile_active()` via a stub profile with the given state."""

    class _Profile:
        def is_empty(self) -> bool:
            return not active

    monkeypatch.setattr(
        "openexecutive.onboarding.profile_builder.load_or_create_profile",
        lambda: _Profile(),
    )


async def _run_scheduler_briefly(ready: asyncio.Event | None = None) -> None:
    """Start the loop, let it tick, then cancel cleanly.

    When `ready` is supplied, wait for it to be set (signalled by a dispatch
    stub) before cancelling so the assertion isn't racing the event loop;
    otherwise fall back to a short fixed tick.
    """
    task = asyncio.create_task(run_scheduler(gateway=None, poll_interval_seconds=60))
    if ready is not None:
        await asyncio.wait_for(ready.wait(), timeout=5)
    else:
        await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_scheduler_holds_due_actions_when_no_company_profile(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no active company profile, due actions are never claimed or run."""
    _set_profile_active(monkeypatch, active=False)
    dispatched: list[int | None] = []

    async def _recording_execute(action: ScheduledAction, gateway: object) -> None:
        dispatched.append(action.id)

    monkeypatch.setattr(
        "openexecutive.scheduler.runner._execute_action", _recording_execute
    )

    action_id = insert_scheduled_action(
        run_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        channel="telegram",
        channel_ref="42",
        intent_text="say hi",
    )

    asyncio.run(_run_scheduler_briefly())

    # Nothing dispatched, and the row stays pending so it fires after onboarding.
    assert dispatched == []
    stored = get_scheduled_action(action_id)
    assert stored is not None
    assert stored.status == "pending"


def test_scheduler_dispatches_due_actions_when_company_profile_active(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With an active company profile, the loop claims and dispatches due rows."""
    _set_profile_active(monkeypatch, active=True)
    dispatched: list[int | None] = []

    action_id = insert_scheduled_action(
        run_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        channel="telegram",
        channel_ref="42",
        intent_text="say hi",
    )

    async def _scenario() -> None:
        ready = asyncio.Event()

        async def _recording_execute(action: ScheduledAction, gateway: object) -> None:
            dispatched.append(action.id)
            ready.set()

        monkeypatch.setattr(
            "openexecutive.scheduler.runner._execute_action", _recording_execute
        )
        await _run_scheduler_briefly(ready=ready)

    asyncio.run(_scenario())

    assert action_id in dispatched


def test_runner_loop_cancellable(db: Path) -> None:
    """The loop must respect asyncio cancellation."""

    async def _runner() -> None:
        task = asyncio.create_task(
            run_scheduler(gateway=None, poll_interval_seconds=60)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_runner())
