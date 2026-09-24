"""SQLite persistence for workflow runs.

Reuses the same DB file as `memory/episodic.py` (so a single backup covers
everything). The table is initialized lazily on first write.

Phase 6 additions
-----------------
Four columns added via idempotent ALTER:
  state_json     TEXT     — checkpoint dict (on_timeout, channel, etc.)
  awaiting_person_id INTEGER — person we're waiting on
  awaiting_until TEXT     — ISO timestamp for timeout
  resolution_json TEXT    — serialized WaitForHumanResolution on match

Resume additions
----------------
Three more columns, same idempotent ALTER:
  resume_state_json TEXT    — serialized WorkflowResumeState; NULL means the
                              gate is pause-only and nothing will continue it
  resumed_at        TEXT    — ISO timestamp a resume was claimed at, so a
                              worker that dies mid-resume can be found again
  resume_attempts   INTEGER — bounded retries for that requeue, reset at each
                              new gate (a new gate is new work, not a retry)
  resume_claim      TEXT    — fencing token for the live claim; a worker's
                              terminal write is refused once it is superseded

New status values beyond running/done/error:
  awaiting_human — paused, waiting for a human reply
  resolved       — human replied, resolution stored in resolution_json.
                   NOT terminal when resume_state_json is set: the resumer
                   claims such a row (resolved -> running) and executes the
                   steps after the gate.
  timed_out      — awaiting_until passed, timeout policy applied
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openexecutive.memory.episodic import DB_PATH, _get_conn


def _resolve(db_path: Path | None) -> Path:
    """Dynamic DB_PATH resolution — lets tests monkeypatch persistence.DB_PATH."""
    return db_path if db_path is not None else DB_PATH


def initialize_runs_db(db_path: Path | None = None) -> None:
    """Create the workflow_runs table if it doesn't exist. Idempotent."""
    with _get_conn(_resolve(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS workflow_runs (
                run_id        TEXT PRIMARY KEY,
                workflow_name TEXT NOT NULL,
                title         TEXT NOT NULL,
                status        TEXT NOT NULL,
                inputs        TEXT NOT NULL,
                artifact      TEXT,
                error         TEXT,
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS workflow_runs_workflow_idx "
            "ON workflow_runs (workflow_name, updated_at DESC)"
        )
        # Phase 6 additive columns — idempotent via PRAGMA + try/except pattern.
        existing = {row[1] for row in conn.execute("PRAGMA table_info(workflow_runs)")}
        for col, ddl in (
            ("state_json", "TEXT"),
            ("awaiting_person_id", "INTEGER"),
            ("awaiting_until", "TEXT"),
            ("resolution_json", "TEXT"),
            # Artifacts gallery soft-delete: NULL = active, ISO ts = archived.
            ("archived_at", "TEXT"),
            # Resume: the engine payload, plus the claim bookkeeping that lets
            # a run stranded in 'running' by a crashed worker be requeued.
            ("resume_state_json", "TEXT"),
            ("resumed_at", "TEXT"),
            ("resume_attempts", "INTEGER NOT NULL DEFAULT 0"),
            # Fencing token for the current claim. A worker writes its
            # terminal row only if this still matches what it claimed with,
            # so a worker wrongly declared dead cannot overwrite the one that
            # replaced it.
            ("resume_claim", "TEXT"),
        ):
            if col not in existing:
                try:
                    conn.execute(f"ALTER TABLE workflow_runs ADD COLUMN {col} {ddl}")
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise


def create_run(
    run_id: str,
    workflow_name: str,
    title: str,
    inputs: dict[str, Any],
    db_path: Path | None = None,
) -> None:
    initialize_runs_db(db_path)  # _resolve happens inside
    now = datetime.now(UTC).isoformat()
    with _get_conn(_resolve(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO workflow_runs
                (run_id, workflow_name, title, status, inputs, created_at, updated_at)
            VALUES (?, ?, ?, 'running', ?, ?, ?)
            """,
            (run_id, workflow_name, title, json.dumps(inputs), now, now),
        )


def complete_run(
    run_id: str,
    artifact: str,
    db_path: Path | None = None,
) -> None:
    now = datetime.now(UTC).isoformat()
    with _get_conn(_resolve(db_path)) as conn:
        conn.execute(
            "UPDATE workflow_runs SET status = 'done', artifact = ?, updated_at = ? "
            "WHERE run_id = ?",
            (artifact, now, run_id),
        )


def fail_run(
    run_id: str,
    error: str,
    db_path: Path | None = None,
) -> None:
    now = datetime.now(UTC).isoformat()
    with _get_conn(_resolve(db_path)) as conn:
        conn.execute(
            "UPDATE workflow_runs SET status = 'error', error = ?, updated_at = ? "
            "WHERE run_id = ?",
            (error, now, run_id),
        )


def get_run(run_id: str, db_path: Path | None = None) -> dict[str, Any] | None:
    if not _resolve(db_path).exists():
        return None
    with _get_conn(_resolve(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM workflow_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
    if row is None:
        return None
    out = dict(row)
    try:
        out["inputs"] = json.loads(out["inputs"]) if out["inputs"] else {}
    except json.JSONDecodeError:
        out["inputs"] = {}
    return out


def list_runs(
    workflow_name: str | None = None,
    limit: int = 100,
    db_path: Path | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """Recent runs, newest-updated first. `workflow_name` and `status` are
    optional SQL filters — pushing `status` into the query (rather than letting
    callers filter the returned page) ensures a `status='done'` caller isn't
    starved when the most-recent `limit` rows are dominated by running/awaiting
    runs."""
    if not _resolve(db_path).exists():
        return []
    clauses: list[str] = []
    params: list[Any] = []
    if workflow_name:
        clauses.append("workflow_name = ?")
        params.append(workflow_name)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
    params.append(limit)
    with _get_conn(_resolve(db_path)) as conn:
        rows = conn.execute(
            "SELECT run_id, workflow_name, title, status, created_at, updated_at "
            f"FROM workflow_runs {where}ORDER BY updated_at DESC LIMIT ?",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def list_artifact_runs(
    limit: int = 200,
    db_path: Path | None = None,
    archived: bool = False,
) -> list[dict[str, Any]]:
    """Completed runs that produced an artifact, newest first.

    Powers the Executive Artifacts section. Mirrors `list_runs` — the heavy
    `artifact` body is intentionally excluded from the list query (fetch it
    per-run via `get_run`). Only `status='done'` runs with a non-empty
    artifact qualify; running/failed/awaiting runs have nothing to show. The
    `artifact != ''` guard keeps this list consistent with the artifacts
    detail route, which treats an empty body as "no artifact".

    `archived` selects which slice to return: the default (False) lists only
    active artifacts (`archived_at IS NULL`); True lists only archived ones,
    mirroring `alerts.store.list_artifact_alerts`.
    """
    if not _resolve(db_path).exists():
        return []
    archived_clause = (
        "AND archived_at IS NOT NULL" if archived else "AND archived_at IS NULL"
    )
    with _get_conn(_resolve(db_path)) as conn:
        rows = conn.execute(
            "SELECT run_id, workflow_name, title, status, created_at, updated_at, "
            "archived_at "
            "FROM workflow_runs "
            "WHERE artifact IS NOT NULL AND artifact != '' AND status = 'done' "
            f"{archived_clause} "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def set_run_archived(
    run_id: str, archived: bool, db_path: Path | None = None
) -> bool:
    """Archive (soft-hide) or restore a workflow-run artifact.

    Sets `archived_at` to the current time when archiving, or NULL when
    restoring. Returns True if a row was updated. Mirrors
    `alerts.store.set_alert_archived`.
    """
    if not _resolve(db_path).exists():
        return False
    now = datetime.now(UTC).isoformat() if archived else None
    with _get_conn(_resolve(db_path)) as conn:
        cur = conn.execute(
            "UPDATE workflow_runs SET archived_at = ? WHERE run_id = ?",
            (now, run_id),
        )
        return cur.rowcount > 0


def delete_run(run_id: str, db_path: Path | None = None) -> bool:
    if not _resolve(db_path).exists():
        return False
    with _get_conn(_resolve(db_path)) as conn:
        cur = conn.execute("DELETE FROM workflow_runs WHERE run_id = ?", (run_id,))
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Phase 6 — WaitForHuman checkpoint helpers
# ---------------------------------------------------------------------------

def save_checkpoint(
    run_id: str,
    state_json: str,
    awaiting_person_id: int | None,
    awaiting_until: datetime | None,
    db_path: Path | None = None,
    *,
    resume_state_json: str | None = None,
    expect_claim: str | None = None,
) -> bool:
    """Persist a WaitForHuman pause point and mark the run awaiting_human.

    ``resume_state_json`` is keyword-only and defaulted so every existing
    positional caller keeps working; NULL leaves the gate pause-only.

    ``resolution_json``, ``resumed_at``, ``resume_claim`` and
    ``resume_attempts`` are all CLEARED here, which is what makes a *second*
    gate correct. A run resumed past gate 1 still carries gate 1's resolution
    and claim, and leaving those would let the stale-resume sweep and the
    executor act on gate 1's answer at gate 2. ``resume_attempts`` resets
    because a new gate is a new unit of work, not a retry of the last one —
    counting gates against the crash-recovery budget would leave a three-gate
    workflow with none by its final gate.

    Gate 1's decision is not lost by the clear: the engine records it as the
    gate step's own output, so it travels on inside the new
    ``resume_state_json`` and lands in the artifact.

    ``expect_claim`` fences a SECOND-gate checkpoint: pass the token the
    caller claimed with and the write is refused if that claim has since been
    superseded, so a worker wrongly declared dead cannot park a run the live
    worker has already finished. Returns whether a row was written.
    """
    initialize_runs_db(db_path)  # _resolve happens inside
    now = datetime.now(UTC).isoformat()
    until_str = awaiting_until.isoformat() if awaiting_until else None
    clause = " AND resume_claim = ?" if expect_claim is not None else ""
    params: list[Any] = [
        state_json, awaiting_person_id, until_str, resume_state_json, now, run_id
    ]
    if expect_claim is not None:
        params.append(expect_claim)
    with _get_conn(_resolve(db_path)) as conn:
        cur = conn.execute(
            f"""
            UPDATE workflow_runs
               SET status = 'awaiting_human',
                   state_json = ?,
                   awaiting_person_id = ?,
                   awaiting_until = ?,
                   resume_state_json = ?,
                   resolution_json = NULL,
                   resumed_at = NULL,
                   resume_claim = NULL,
                   resume_attempts = 0,
                   updated_at = ?
             WHERE run_id = ?{clause}
            """,
            params,
        )
        return cur.rowcount > 0


def load_checkpoint(
    run_id: str,
    db_path: Path | None = None,
) -> tuple[str, int | None, datetime | None] | None:
    """Return (state_json, awaiting_person_id, awaiting_until) or None."""
    if not _resolve(db_path).exists():
        return None
    with _get_conn(_resolve(db_path)) as conn:
        row = conn.execute(
            "SELECT state_json, awaiting_person_id, awaiting_until "
            "FROM workflow_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    if row is None or row[0] is None:
        return None
    until: datetime | None = None
    if row[2]:
        try:
            until = datetime.fromisoformat(row[2])
            if until.tzinfo is None:
                until = until.replace(tzinfo=UTC)
        except ValueError:
            pass
    return (row[0], row[1], until)


def list_awaiting_runs(db_path: Path | None = None) -> list[dict[str, Any]]:
    """Return all runs with status='awaiting_human'."""
    if not _resolve(db_path).exists():
        return []
    with _get_conn(_resolve(db_path)) as conn:
        rows = conn.execute(
            "SELECT run_id, workflow_name, title, awaiting_person_id, "
            "awaiting_until, state_json, resolution_json, updated_at "
            "FROM workflow_runs WHERE status = 'awaiting_human' "
            "ORDER BY updated_at"
        ).fetchall()
    result = []
    for r in rows:
        d = dict(zip(
            ("run_id", "workflow_name", "title", "awaiting_person_id",
             "awaiting_until", "state_json", "resolution_json", "updated_at"),
            r,
            strict=False,
        ))
        result.append(d)
    return result


def store_resolution(
    run_id: str,
    resolution_json: str,
    db_path: Path | None = None,
) -> bool:
    """Store a resolution and mark the run resolved. Idempotent."""
    now = datetime.now(UTC).isoformat()
    with _get_conn(_resolve(db_path)) as conn:
        cur = conn.execute(
            """
            UPDATE workflow_runs
               SET status = 'resolved', resolution_json = ?, updated_at = ?
             WHERE run_id = ? AND status = 'awaiting_human'
            """,
            (resolution_json, now, run_id),
        )
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Resume — claim/execute helpers for the background executor
# ---------------------------------------------------------------------------

def list_resumable_runs(
    limit: int = 50,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Resolved runs that carry a resume payload, oldest-updated first.

    A `resolved` row WITHOUT `resume_state_json` is a pause-only gate and is
    deliberately excluded — its decision was the whole point, and claiming it
    would flip a finished run back to `running` forever.
    """
    if not _resolve(db_path).exists():
        return []
    cols = (
        "run_id", "workflow_name", "title", "inputs",
        "resume_state_json", "resolution_json", "updated_at",
    )
    with _get_conn(_resolve(db_path)) as conn:
        rows = conn.execute(
            f"SELECT {', '.join(cols)} FROM workflow_runs "
            "WHERE status = 'resolved' AND resume_state_json IS NOT NULL "
            "ORDER BY updated_at LIMIT ?",
            (limit,),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(zip(cols, r, strict=False))
        try:
            d["inputs"] = json.loads(d["inputs"]) if d["inputs"] else {}
        except json.JSONDecodeError:
            d["inputs"] = {}
        out.append(d)
    return out


def claim_run_for_resume(run_id: str, db_path: Path | None = None) -> str | None:
    """Atomically take ownership of a resolved run: resolved -> running.

    Returns a fencing token on success, or None if another worker won the race
    or the row moved on. This single guarded UPDATE is what prevents double
    execution between the resumer poll and the immediate post-resolution kick.

    The token matters because the claim is not a lease and cannot be: the
    stale sweep decides a worker is dead from a wall-clock guess, and a slow
    but ALIVE worker will eventually be declared dead and replaced. Terminal
    writes carry the token so the superseded worker's late `complete_run` (or
    worse, its `save_checkpoint` at a later gate) is refused instead of
    overwriting — or resurrecting — the run the replacement finished.
    """
    if not _resolve(db_path).exists():
        return None
    now = datetime.now(UTC).isoformat()
    token = uuid.uuid4().hex
    with _get_conn(_resolve(db_path)) as conn:
        cur = conn.execute(
            """
            UPDATE workflow_runs
               SET status = 'running',
                   resumed_at = ?,
                   resume_claim = ?,
                   resume_attempts = resume_attempts + 1,
                   updated_at = ?
             WHERE run_id = ?
               AND status = 'resolved'
               AND resume_state_json IS NOT NULL
            """,
            (now, token, now, run_id),
        )
        return token if cur.rowcount > 0 else None


def touch_resume_claim(run_id: str, claim: str, db_path: Path | None = None) -> bool:
    """Heartbeat a live resume: bump ``resumed_at`` while ``claim`` still holds.

    The stale sweep measures a claim's age from ``resumed_at``. A resume that
    runs workflow ``action`` steps can legitimately outlast any fixed window
    (tool calls, several steps), and a requeue while it is still alive would
    replay its external side effects on a second worker. The resumer calls this
    as events flow, so a live worker never looks dead. Returns False once the
    claim has been superseded — the caller must stop acting.
    """
    if not _resolve(db_path).exists():
        return False
    now = datetime.now(UTC).isoformat()
    with _get_conn(_resolve(db_path)) as conn:
        cur = conn.execute(
            """
            UPDATE workflow_runs
               SET resumed_at = ?
             WHERE run_id = ?
               AND resume_claim = ?
               AND status = 'running'
            """,
            (now, run_id, claim),
        )
        return cur.rowcount > 0


def finish_resumed_run(
    run_id: str,
    claim: str,
    *,
    artifact: str | None = None,
    error: str | None = None,
    db_path: Path | None = None,
) -> bool:
    """Write a resumed run's terminal row, but only if the claim still holds.

    The fenced counterpart to `complete_run` / `fail_run`, which have no status
    guard at all. Without the fence a worker the stale sweep replaced could
    land its result on top of the replacement's, or flip an already-`done` run
    back to `error`. Also clears the resume payload, so the row cannot be
    re-claimed. Returns False when the claim was superseded — the caller
    should do nothing further.
    """
    if not _resolve(db_path).exists():
        return False
    now = datetime.now(UTC).isoformat()
    with _get_conn(_resolve(db_path)) as conn:
        cur = conn.execute(
            """
            UPDATE workflow_runs
               SET status = ?,
                   artifact = COALESCE(?, artifact),
                   error = ?,
                   resume_state_json = NULL,
                   resumed_at = NULL,
                   resume_claim = NULL,
                   updated_at = ?
             WHERE run_id = ? AND status = 'running' AND resume_claim = ?
            """,
            (
                "done" if artifact is not None else "error",
                artifact,
                error,
                now,
                run_id,
                claim,
            ),
        )
        return cur.rowcount > 0


def clear_resume_state(run_id: str, db_path: Path | None = None) -> None:
    """Drop the resume payload once a run reaches a terminal state.

    Keeps a finished run from being re-claimed, and keeps the (potentially
    large) completed-step text out of rows nothing will replay.
    """
    if not _resolve(db_path).exists():
        return
    with _get_conn(_resolve(db_path)) as conn:
        conn.execute(
            "UPDATE workflow_runs SET resume_state_json = NULL, resumed_at = NULL, "
            "resume_claim = NULL WHERE run_id = ?",
            (run_id,),
        )


def list_stale_resuming_runs(
    older_than: datetime,
    max_attempts: int = 3,
    db_path: Path | None = None,
) -> list[str]:
    """Run ids whose resume was claimed but never finished.

    Crash recovery. `sweep_stale_awaiting` only looks at `awaiting_human`, so
    without this a process killed between the claim and the terminal write
    would leave the run in `running` with nobody coming back for it.

    A run legitimately in `running` has no `resume_state_json`, and a run
    parked at a later gate is `awaiting_human`, so neither is ever matched.
    """
    if not _resolve(db_path).exists():
        return []
    with _get_conn(_resolve(db_path)) as conn:
        rows = conn.execute(
            """
            SELECT run_id FROM workflow_runs
             WHERE status = 'running'
               AND resume_state_json IS NOT NULL
               AND resumed_at IS NOT NULL
               AND resumed_at < ?
               AND resume_attempts < ?
             ORDER BY resumed_at
            """,
            (older_than.isoformat(), max_attempts),
        ).fetchall()
    return [r[0] for r in rows]


def list_exhausted_resuming_runs(
    older_than: datetime,
    max_attempts: int = 3,
    db_path: Path | None = None,
) -> list[str]:
    """Stranded resumes that have used up their retry budget.

    The companion to `list_stale_resuming_runs`, and the reason the bound is
    not simply a filter: a run that exceeds it drops out of that query, but it
    is `running` with a payload, so `list_resumable_runs` (which wants
    `resolved`) never sees it either. Without this it would sit in `running`
    forever, showing as in-progress on /jobs, in no queue at all.
    """
    if not _resolve(db_path).exists():
        return []
    with _get_conn(_resolve(db_path)) as conn:
        rows = conn.execute(
            """
            SELECT run_id FROM workflow_runs
             WHERE status = 'running'
               AND resume_state_json IS NOT NULL
               AND resumed_at IS NOT NULL
               AND resumed_at < ?
               AND resume_attempts >= ?
             ORDER BY resumed_at
            """,
            (older_than.isoformat(), max_attempts),
        ).fetchall()
    return [r[0] for r in rows]


def requeue_run_for_resume(
    run_id: str,
    db_path: Path | None = None,
    *,
    stale_before: datetime | None = None,
) -> bool:
    """running -> resolved, so the next tick re-claims it. Guarded, idempotent.

    ``stale_before`` re-checks staleness in the same UPDATE: the sweep reads
    stale ids and then requeues them, and a live worker's heartbeat
    (``touch_resume_claim``) can land in between. With the cutoff in the
    WHERE clause, a fresh heartbeat wins and the run is left alone.

    `resume_attempts` is NOT reset here — it is the bound that stops a run
    which reliably kills its worker from being retried forever. It IS reset by
    `save_checkpoint`, because reaching a new gate is new work rather than
    another attempt at the last one.
    """
    if not _resolve(db_path).exists():
        return False
    now = datetime.now(UTC).isoformat()
    sql = (
        "UPDATE workflow_runs SET status = 'resolved', resumed_at = NULL, "
        # Breaking the claim is the point: the worker we just declared
        # dead may in fact be alive, and this is what stops its late
        # write from landing on the replacement's work.
        "resume_claim = NULL, updated_at = ? "
        "WHERE run_id = ? AND status = 'running' "
        "AND resume_state_json IS NOT NULL"
    )
    params: list[str] = [now, run_id]
    if stale_before is not None:
        sql += " AND resumed_at IS NOT NULL AND resumed_at < ?"
        params.append(stale_before.isoformat())
    with _get_conn(_resolve(db_path)) as conn:
        cur = conn.execute(sql, params)
        return cur.rowcount > 0


def mark_timed_out(run_id: str, db_path: Path | None = None) -> bool:
    """Transition awaiting_human → timed_out. Returns True if updated."""
    now = datetime.now(UTC).isoformat()
    with _get_conn(_resolve(db_path)) as conn:
        cur = conn.execute(
            "UPDATE workflow_runs SET status = 'timed_out', updated_at = ? "
            "WHERE run_id = ? AND status = 'awaiting_human'",
            (now, run_id),
        )
        return cur.rowcount > 0
