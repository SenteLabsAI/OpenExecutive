"""Tests for Phase 6 WaitForHuman persistence helpers."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from openexecutive.workflows import persistence as wf_persistence


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated workflow_runs DB."""
    path = tmp_path / "wf.db"
    # persistence.py imports DB_PATH from episodic at module level;
    # monkeypatch it so initialize_runs_db uses the temp file.
    monkeypatch.setattr(wf_persistence, "DB_PATH", path)
    import openexecutive.memory.episodic as ep
    monkeypatch.setattr(ep, "DB_PATH", path)
    ep.initialize_db(path)
    wf_persistence.initialize_runs_db(path)
    return path


def _seed_run(db: Path, run_id: str = "run-001") -> None:
    wf_persistence.create_run(
        run_id,
        "department_check_in",
        "Test run",
        {"department_slug": "finance"},
        db_path=db,
    )


# ---------------------------------------------------------------------------
# initialize_runs_db — new columns present
# ---------------------------------------------------------------------------

def test_initialize_runs_db_creates_phase6_columns(db: Path) -> None:
    import sqlite3
    conn = sqlite3.connect(str(db))
    cols = {row[1] for row in conn.execute("PRAGMA table_info(workflow_runs)")}
    conn.close()
    assert "state_json" in cols
    assert "awaiting_person_id" in cols
    assert "awaiting_until" in cols
    assert "resolution_json" in cols


def test_initialize_runs_db_idempotent(db: Path) -> None:
    wf_persistence.initialize_runs_db(db)
    wf_persistence.initialize_runs_db(db)  # second call must not raise


def test_list_runs_status_filter_done_not_starved(db: Path) -> None:
    """The `status` filter is applied in SQL, so a `status='done'` caller is
    never starved by more-recent running rows that fill a small `limit`."""
    # Three running runs touched most recently, one older done run.
    for i in range(3):
        wf_persistence.create_run(f"run-r{i}", "research", f"Running {i}", {}, db_path=db)
    wf_persistence.create_run("run-done", "morning_brief", "Done one", {}, db_path=db)
    wf_persistence.complete_run("run-done", artifact="body", db_path=db)

    # Unfiltered, limit=2 returns the two most-recently-updated (the done run
    # was completed last, so it leads; the running ones follow).
    unfiltered = wf_persistence.list_runs(limit=2, db_path=db)
    assert len(unfiltered) == 2

    # status='done' returns only the done run regardless of the running backlog.
    done = wf_persistence.list_runs(status="done", limit=2, db_path=db)
    assert [r["run_id"] for r in done] == ["run-done"]
    assert all(r["status"] == "done" for r in done)


# ---------------------------------------------------------------------------
# save_checkpoint / load_checkpoint
# ---------------------------------------------------------------------------

def test_save_load_checkpoint_round_trip(db: Path) -> None:
    _seed_run(db)
    state = '{"on_timeout": "escalate", "channel": "slack", "question": "Approve?"}'
    until = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)

    wf_persistence.save_checkpoint("run-001", state, person_id := 42, until, db_path=db)

    result = wf_persistence.load_checkpoint("run-001", db_path=db)
    assert result is not None
    loaded_state, loaded_person, loaded_until = result
    assert loaded_state == state
    assert loaded_person == person_id
    assert loaded_until is not None
    assert loaded_until.replace(tzinfo=UTC) == until


def test_checkpoint_sets_awaiting_human_status(db: Path) -> None:
    _seed_run(db)
    wf_persistence.save_checkpoint("run-001", "{}", None, None, db_path=db)
    run = wf_persistence.get_run("run-001", db_path=db)
    assert run is not None
    assert run["status"] == "awaiting_human"


def test_checkpoint_awaiting_fields_stored(db: Path) -> None:
    _seed_run(db)
    until = datetime.now(UTC) + timedelta(hours=24)
    wf_persistence.save_checkpoint("run-001", '{"q": "ok?"}', 7, until, db_path=db)

    result = wf_persistence.load_checkpoint("run-001", db_path=db)
    assert result is not None
    _, pid, ts = result
    assert pid == 7
    assert ts is not None
    assert abs((ts - until).total_seconds()) < 2


def test_load_nonexistent_checkpoint_returns_none(db: Path) -> None:
    assert wf_persistence.load_checkpoint("nonexistent", db_path=db) is None


def test_load_checkpoint_null_state_json_returns_none(db: Path) -> None:
    _seed_run(db)
    # Never saved a checkpoint — state_json is NULL.
    assert wf_persistence.load_checkpoint("run-001", db_path=db) is None


# ---------------------------------------------------------------------------
# store_resolution / mark_timed_out
# ---------------------------------------------------------------------------

def test_store_resolution_marks_resolved(db: Path) -> None:
    _seed_run(db)
    wf_persistence.save_checkpoint("run-001", "{}", None, None, db_path=db)

    ok = wf_persistence.store_resolution("run-001", '{"decision":"approve"}', db_path=db)
    assert ok is True

    run = wf_persistence.get_run("run-001", db_path=db)
    assert run is not None
    assert run["status"] == "resolved"


def test_store_resolution_idempotent(db: Path) -> None:
    _seed_run(db)
    wf_persistence.save_checkpoint("run-001", "{}", None, None, db_path=db)
    wf_persistence.store_resolution("run-001", '{"decision":"approve"}', db_path=db)
    # Second call on already-resolved run returns False (not awaiting_human).
    ok2 = wf_persistence.store_resolution("run-001", '{"decision":"approve"}', db_path=db)
    assert ok2 is False


def test_mark_timed_out_transitions_status(db: Path) -> None:
    _seed_run(db)
    wf_persistence.save_checkpoint("run-001", "{}", None, None, db_path=db)
    ok = wf_persistence.mark_timed_out("run-001", db_path=db)
    assert ok is True
    run = wf_persistence.get_run("run-001", db_path=db)
    assert run is not None
    assert run["status"] == "timed_out"


def test_mark_timed_out_only_from_awaiting_human(db: Path) -> None:
    _seed_run(db)
    # run is in 'running' status — should not be timed out.
    ok = wf_persistence.mark_timed_out("run-001", db_path=db)
    assert ok is False


# ---------------------------------------------------------------------------
# list_awaiting_runs
# ---------------------------------------------------------------------------

def test_list_awaiting_runs_filters_correctly(db: Path) -> None:
    wf_persistence.create_run("r1", "wf", "T1", {}, db_path=db)
    wf_persistence.create_run("r2", "wf", "T2", {}, db_path=db)
    wf_persistence.save_checkpoint("r1", "{}", 1, None, db_path=db)
    # r2 stays in running

    awaiting = wf_persistence.list_awaiting_runs(db_path=db)
    run_ids = [r["run_id"] for r in awaiting]
    assert "r1" in run_ids
    assert "r2" not in run_ids


# ---------------------------------------------------------------------------
# Resume — columns, claim, crash recovery
# ---------------------------------------------------------------------------

def test_initialize_runs_db_creates_resume_columns(db: Path) -> None:
    import sqlite3
    conn = sqlite3.connect(str(db))
    cols = {row[1] for row in conn.execute("PRAGMA table_info(workflow_runs)")}
    conn.close()
    assert "resume_state_json" in cols
    assert "resumed_at" in cols
    assert "resume_attempts" in cols


def _checkpoint(db: Path, run_id: str, *, resume: str | None) -> None:
    wf_persistence.save_checkpoint(
        run_id,
        '{"person_id": 7, "question": "ok?"}',
        7,
        datetime.now(UTC) + timedelta(hours=24),
        db_path=db,
        resume_state_json=resume,
    )


def _row(db: Path, run_id: str) -> dict:
    import sqlite3
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM workflow_runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    conn.close()
    return dict(row)


def test_save_checkpoint_stores_resume_payload(db: Path) -> None:
    _seed_run(db)
    _checkpoint(db, "run-001", resume='{"workflow_name": "wf"}')
    assert _row(db, "run-001")["resume_state_json"] == '{"workflow_name": "wf"}'


def test_save_checkpoint_without_payload_leaves_it_null(db: Path) -> None:
    """A pause-only gate must not look resumable."""
    _seed_run(db)
    _checkpoint(db, "run-001", resume=None)
    assert _row(db, "run-001")["resume_state_json"] is None


def test_second_checkpoint_clears_the_previous_resolution(db: Path) -> None:
    """The second-gate case. A run resumed past gate 1 still carries gate 1's
    resolution and claim stamp; leaving them would let the executor act on the
    old answer at the new gate, and the stale sweep treat a fresh pause as an
    abandoned resume."""
    _seed_run(db)
    _checkpoint(db, "run-001", resume='{"gate": 1}')
    wf_persistence.store_resolution("run-001", '{"decision": "approve"}', db_path=db)
    assert wf_persistence.claim_run_for_resume("run-001", db_path=db) is not None
    assert _row(db, "run-001")["resumed_at"] is not None

    _checkpoint(db, "run-001", resume='{"gate": 2}')
    row = _row(db, "run-001")
    assert row["status"] == "awaiting_human"
    assert row["resume_state_json"] == '{"gate": 2}'
    assert row["resolution_json"] is None
    assert row["resumed_at"] is None


def test_claim_run_for_resume_succeeds_once(db: Path) -> None:
    """The guard against double execution: the poll loop and the immediate
    kick both call this, and only one may win."""
    _seed_run(db)
    _checkpoint(db, "run-001", resume='{"gate": 1}')
    wf_persistence.store_resolution("run-001", "{}", db_path=db)

    first = wf_persistence.claim_run_for_resume("run-001", db_path=db)
    assert first is not None
    assert wf_persistence.claim_run_for_resume("run-001", db_path=db) is None
    row = _row(db, "run-001")
    assert row["status"] == "running"
    assert row["resume_attempts"] == 1
    assert row["resume_claim"] == first


def test_claim_ignores_a_resolved_run_with_no_payload(db: Path) -> None:
    """A pause-only gate is finished at `resolved`. Claiming it would flip a
    completed run back to `running` with nothing to execute."""
    _seed_run(db)
    _checkpoint(db, "run-001", resume=None)
    wf_persistence.store_resolution("run-001", "{}", db_path=db)

    assert wf_persistence.claim_run_for_resume("run-001", db_path=db) is None
    assert _row(db, "run-001")["status"] == "resolved"


def test_list_resumable_runs_selects_only_claimable_rows(db: Path) -> None:
    for rid, resume, resolve in (
        ("ready", '{"gate": 1}', True),      # resolved + payload -> yes
        ("pause-only", None, True),          # resolved, no payload -> no
        ("still-waiting", '{"gate": 1}', False),  # awaiting_human -> no
    ):
        wf_persistence.create_run(rid, "wf", rid, {"a": "b"}, db_path=db)
        _checkpoint(db, rid, resume=resume)
        if resolve:
            wf_persistence.store_resolution(rid, "{}", db_path=db)

    ids = [r["run_id"] for r in wf_persistence.list_resumable_runs(db_path=db)]
    assert ids == ["ready"]


def test_list_resumable_runs_parses_inputs(db: Path) -> None:
    wf_persistence.create_run("r", "wf", "t", {"topic": "pricing"}, db_path=db)
    _checkpoint(db, "r", resume='{"gate": 1}')
    wf_persistence.store_resolution("r", "{}", db_path=db)
    assert wf_persistence.list_resumable_runs(db_path=db)[0]["inputs"] == {
        "topic": "pricing"
    }


def test_clear_resume_state_makes_a_run_unclaimable(db: Path) -> None:
    _seed_run(db)
    _checkpoint(db, "run-001", resume='{"gate": 1}')
    wf_persistence.store_resolution("run-001", "{}", db_path=db)
    wf_persistence.clear_resume_state("run-001", db_path=db)

    assert wf_persistence.list_resumable_runs(db_path=db) == []
    assert wf_persistence.claim_run_for_resume("run-001", db_path=db) is None


def test_stale_resuming_run_is_found_and_requeued(db: Path) -> None:
    """Crash recovery. `sweep_stale_awaiting` only looks at awaiting_human, so
    a worker that dies after claiming would otherwise strand the run in
    `running` with nobody coming back for it."""
    _seed_run(db)
    _checkpoint(db, "run-001", resume='{"gate": 1}')
    wf_persistence.store_resolution("run-001", "{}", db_path=db)
    wf_persistence.claim_run_for_resume("run-001", db_path=db)

    future = datetime.now(UTC) + timedelta(minutes=1)
    assert wf_persistence.list_stale_resuming_runs(future, db_path=db) == ["run-001"]
    assert wf_persistence.requeue_run_for_resume("run-001", db_path=db) is True
    row = _row(db, "run-001")
    assert row["status"] == "resolved"
    # The old claim is broken, which is what stops a worker we wrongly
    # declared dead from writing over its replacement.
    assert row["resume_claim"] is None
    # Requeued rows are claimable again, and the attempt count keeps rising.
    assert wf_persistence.claim_run_for_resume("run-001", db_path=db) is not None
    assert _row(db, "run-001")["resume_attempts"] == 2


def test_a_fresh_claim_is_not_treated_as_stale(db: Path) -> None:
    _seed_run(db)
    _checkpoint(db, "run-001", resume='{"gate": 1}')
    wf_persistence.store_resolution("run-001", "{}", db_path=db)
    wf_persistence.claim_run_for_resume("run-001", db_path=db)

    past = datetime.now(UTC) - timedelta(minutes=30)
    assert wf_persistence.list_stale_resuming_runs(past, db_path=db) == []


def test_a_run_past_its_retry_budget_is_reported_not_dropped(db: Path) -> None:
    """A run that reliably kills its worker must stop being retried — and must
    then be ENDED. Falling out of the stale query isn't enough: the row is
    `running` with a payload, so the resumable query (which wants `resolved`)
    never sees it either, and it would show as in-progress forever in no queue
    at all. `list_exhausted_resuming_runs` is what finds it."""
    _seed_run(db)
    _checkpoint(db, "run-001", resume='{"gate": 1}')
    wf_persistence.store_resolution("run-001", "{}", db_path=db)
    future = datetime.now(UTC) + timedelta(minutes=1)

    for _ in range(3):
        wf_persistence.claim_run_for_resume("run-001", db_path=db)
        wf_persistence.requeue_run_for_resume("run-001", db_path=db)
    wf_persistence.claim_run_for_resume("run-001", db_path=db)

    assert _row(db, "run-001")["resume_attempts"] == 4
    # Out of the retry queue...
    assert wf_persistence.list_stale_resuming_runs(
        future, max_attempts=3, db_path=db
    ) == []
    # ...but findable, so something can end it.
    assert wf_persistence.list_exhausted_resuming_runs(
        future, max_attempts=3, db_path=db
    ) == ["run-001"]


def test_reaching_a_new_gate_resets_the_retry_budget(db: Path) -> None:
    """A new gate is new work, not another attempt at the last one. Counting
    gates against the budget would leave a three-gate workflow with no
    crash-recovery budget by its final gate, all without a single failure."""
    _seed_run(db)
    _checkpoint(db, "run-001", resume='{"gate": 1}')
    wf_persistence.store_resolution("run-001", "{}", db_path=db)
    wf_persistence.claim_run_for_resume("run-001", db_path=db)
    assert _row(db, "run-001")["resume_attempts"] == 1

    _checkpoint(db, "run-001", resume='{"gate": 2}')

    row = _row(db, "run-001")
    assert row["resume_attempts"] == 0
    assert row["resume_claim"] is None


def test_a_superseded_worker_cannot_write_its_result(db: Path) -> None:
    """The fence. The stale sweep decides a worker is dead from a wall-clock
    guess, so a slow-but-alive worker WILL eventually be replaced. Its late
    write must be refused, not land on top of the replacement's."""
    _seed_run(db)
    _checkpoint(db, "run-001", resume='{"gate": 1}')
    wf_persistence.store_resolution("run-001", "{}", db_path=db)
    stale_claim = wf_persistence.claim_run_for_resume("run-001", db_path=db)
    assert stale_claim is not None

    # Declared dead and handed to someone else.
    wf_persistence.requeue_run_for_resume("run-001", db_path=db)
    live_claim = wf_persistence.claim_run_for_resume("run-001", db_path=db)
    assert live_claim is not None and live_claim != stale_claim

    # The replacement finishes.
    assert wf_persistence.finish_resumed_run(
        "run-001", live_claim, artifact="# Real result", db_path=db
    ) is True
    # The original wakes up and tries to write. Refused.
    assert wf_persistence.finish_resumed_run(
        "run-001", stale_claim, error="stale failure", db_path=db
    ) is False

    row = _row(db, "run-001")
    assert row["status"] == "done"
    assert row["artifact"] == "# Real result"
    assert row["error"] is None


def test_a_superseded_worker_cannot_park_the_run_at_a_later_gate(db: Path) -> None:
    """The nastier half of the same race: a fenced checkpoint stops a
    superseded worker resurrecting an already-finished run into
    `awaiting_human` and asking the approver a second time."""
    _seed_run(db)
    _checkpoint(db, "run-001", resume='{"gate": 1}')
    wf_persistence.store_resolution("run-001", "{}", db_path=db)
    stale_claim = wf_persistence.claim_run_for_resume("run-001", db_path=db)
    wf_persistence.requeue_run_for_resume("run-001", db_path=db)
    live_claim = wf_persistence.claim_run_for_resume("run-001", db_path=db)
    assert live_claim is not None
    wf_persistence.finish_resumed_run(
        "run-001", live_claim, artifact="# Real result", db_path=db
    )

    parked = wf_persistence.save_checkpoint(
        "run-001", "{}", 7, datetime.now(UTC) + timedelta(hours=1),
        db_path=db, resume_state_json='{"gate": 2}', expect_claim=stale_claim,
    )

    assert parked is False
    assert _row(db, "run-001")["status"] == "done"


def test_an_ordinary_running_run_is_never_requeued(db: Path) -> None:
    """A normal in-flight run has no payload and must be left alone."""
    _seed_run(db)  # create_run leaves status='running', resume_state_json NULL
    future = datetime.now(UTC) + timedelta(hours=1)
    assert wf_persistence.list_stale_resuming_runs(future, db_path=db) == []
    assert wf_persistence.requeue_run_for_resume("run-001", db_path=db) is False
