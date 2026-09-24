"""`workflows.gate` — the one place a gate becomes a persisted pause.

Three runners can pause (the SSE route, the chat tool, the resumer at a second
gate) and nine cannot. This module covers the shared helper both halves go
through, so the invariants live in one place instead of being re-proved at
each call site.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from openexecutive.workflows import persistence as wf_persistence
from openexecutive.workflows.base import WorkflowEvent
from openexecutive.workflows.gate import (
    UnsupportedGateError,
    checkpoint_gate,
    ensure_workflow_event,
)
from openexecutive.workflows.wait_for_human import (
    WaitForHumanEvent,
    WorkflowResumeState,
)


@pytest.fixture(autouse=True)
def _isolate_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep audit writes out of the default ./episodic_memory.db.

    `audit.log_event` writes to the module default unless patched, and the
    leaked rows only break *other* modules' assertions in a full-suite run.
    """
    import openexecutive.audit as audit
    monkeypatch.setattr(audit, "log_event", lambda *a, **k: None)


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "wf.db"
    monkeypatch.setattr(wf_persistence, "DB_PATH", path)
    import openexecutive.memory.episodic as ep
    monkeypatch.setattr(ep, "DB_PATH", path)
    ep.initialize_db(path)
    wf_persistence.initialize_runs_db(path)
    wf_persistence.create_run("r1", "weekly_watch", "Weekly Watch", {"topic": "x"},
                              db_path=path)
    return path


def _stub_delivery(
    monkeypatch: pytest.MonkeyPatch,
    status: str = "sent",
    **updates: Any,
) -> None:
    """Stand in for gate_delivery, which is covered by its own module."""
    async def _deliver(event: WaitForHumanEvent, **_: Any) -> tuple[WaitForHumanEvent, str]:
        return event.model_copy(update={"delivery": status, **updates}), status

    monkeypatch.setattr(
        "openexecutive.workflows.gate_delivery.deliver_gate_question", _deliver
    )


def _gate(**overrides: Any) -> WaitForHumanEvent:
    base: dict[str, Any] = {"person_id": 7, "question": "Approve?", "timeout_hours": 24}
    base.update(overrides)
    return WaitForHumanEvent.model_validate(base)


def _resume_state() -> WorkflowResumeState:
    return WorkflowResumeState(
        workflow_name="weekly_watch",
        gate_step_id="gate",
        gate_step_index=1,
        steps_fingerprint="deadbeef",
        outputs={"research": ("Research", "body")},
    )


def _row(db: Path) -> dict:
    import sqlite3
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM workflow_runs WHERE run_id = 'r1'").fetchone()
    conn.close()
    return dict(row)


# ---------------------------------------------------------------------------
# checkpoint_gate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_checkpoint_gate_parks_the_run(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_delivery(monkeypatch, channel="slack", channel_ref="U1", outbound_message_id="m1")

    pause = await checkpoint_gate(
        run_id="r1", event=_gate(resume_state=_resume_state()),
        workflow_title="Weekly Watch", db_path=db,
    )

    row = _row(db)
    assert row["status"] == "awaiting_human"
    assert row["awaiting_person_id"] == 7
    assert pause.delivery == "sent"
    assert pause.resumable is True
    assert pause.question == "Approve?"


@pytest.mark.asyncio
async def test_routing_fields_are_persisted_not_the_pre_delivery_event(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Delivery runs BEFORE the checkpoint so `state_json` carries what a reply
    is matched against. Persisting the original event would leave the gate
    unanswerable on every channel — the #136 failure."""
    _stub_delivery(monkeypatch, channel="slack", channel_ref="U1", outbound_message_id="m1")

    await checkpoint_gate(run_id="r1", event=_gate(), db_path=db)

    state = json.loads(_row(db)["state_json"])
    assert state["channel"] == "slack"
    assert state["channel_ref"] == "U1"
    assert state["outbound_message_id"] == "m1"


@pytest.mark.asyncio
async def test_resume_payload_goes_to_its_own_column_not_state_json(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The resolver branches on which keys `state_json` holds, so the payload
    must not appear there — and it must not be lost either."""
    _stub_delivery(monkeypatch)

    await checkpoint_gate(run_id="r1", event=_gate(resume_state=_resume_state()), db_path=db)

    row = _row(db)
    assert "resume_state" not in json.loads(row["state_json"])
    stored = json.loads(row["resume_state_json"])
    assert stored["gate_step_id"] == "gate"
    assert stored["gate_step_index"] == 1


@pytest.mark.asyncio
async def test_payload_survives_the_delivery_copy(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`deliver_gate_question` returns a `model_copy`. That it preserves
    unlisted fields is load-bearing and easy to break — a delivery path that
    rebuilt the event instead would silently make every gate pause-only."""
    _stub_delivery(monkeypatch, channel="slack", channel_ref="U1")

    pause = await checkpoint_gate(
        run_id="r1", event=_gate(resume_state=_resume_state()), db_path=db
    )

    assert pause.resumable is True
    assert _row(db)["resume_state_json"] is not None


@pytest.mark.asyncio
async def test_a_gate_without_a_payload_is_pause_only(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_delivery(monkeypatch)
    pause = await checkpoint_gate(run_id="r1", event=_gate(), db_path=db)
    assert pause.resumable is False
    assert _row(db)["resume_state_json"] is None


@pytest.mark.asyncio
async def test_undelivered_gate_still_checkpoints(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delivery failure must not lose the run — it is still parked, still
    times out, and the caller is told nobody was asked."""
    _stub_delivery(monkeypatch, status="failed")

    pause = await checkpoint_gate(run_id="r1", event=_gate(), db_path=db)

    assert pause.delivery == "failed"
    assert _row(db)["status"] == "awaiting_human"


@pytest.mark.asyncio
async def test_awaiting_until_follows_the_step_timeout(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_delivery(monkeypatch)
    now = datetime(2026, 1, 1, tzinfo=UTC)

    pause = await checkpoint_gate(
        run_id="r1", event=_gate(timeout_hours=6), now=now, db_path=db
    )

    assert pause.awaiting_until == now + timedelta(hours=6)


@pytest.mark.asyncio
async def test_a_persistence_failure_propagates(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller must never report `awaiting_human` for a run that was not
    checkpointed: the resumer and the inbound resolver would both be unable to
    find it, and it would sit in `running` forever."""
    _stub_delivery(monkeypatch)

    def _boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr("openexecutive.workflows.gate.save_checkpoint", _boom)

    with pytest.raises(RuntimeError, match="disk on fire"):
        await checkpoint_gate(run_id="r1", event=_gate(), db_path=db)


# ---------------------------------------------------------------------------
# ensure_workflow_event
# ---------------------------------------------------------------------------

def test_ensure_workflow_event_passes_ordinary_events_through() -> None:
    event = WorkflowEvent(type="step_done", step_id="s1")
    assert ensure_workflow_event(event, site="test") is event


def test_ensure_workflow_event_raises_on_a_gate() -> None:
    """These runners have no human to ask. Raising turns a gate into a named
    failure instead of a run that reports success having dropped every step
    after it."""
    with pytest.raises(UnsupportedGateError) as exc:
        ensure_workflow_event(_gate(), site="scheduler.dynamic_workflow")
    assert "scheduler.dynamic_workflow" in str(exc.value)
    assert "cannot pause" in str(exc.value)
