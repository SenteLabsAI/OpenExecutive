"""Approvals of Today cards that are not backed by a decision.

A decision-backed card (a meeting booking, a held action) is approved at
``/decisions/{id}/approve``, which records its resolver and carries the
decision out. Every other proposal card (one the alert review or a
department's authority gate filed) is approved at ``/alerts/{id}/approve``:
this module records who approved it and when, claims the card so it is
carried out once, and keeps what came of it.

One row per approval in ``alert_approvals``:

- ``running`` while the work goes on (a chat turn on the server, or the
  scheduled action it held being queued again);
- ``done`` or ``failed`` once it has finished, with a one-line ``outcome``
  and, for a chat turn, the ``session_id`` of the chat that shows the work.

A card whose last approval failed can be approved again; any other card is
claimed once, so a double click or a second approver never runs it twice.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from openexecutive.alerts.store import _get_conn

STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

# Alert statuses a card can be approved from: still in the queue.
_OPEN_ALERT_STATUSES = ("unread", "read")

# A turn that stopped may already have sent something, so a failed row says
# to look before approving again.
RESTARTED_OUTCOME = (
    "It stopped when the server restarted. It may have done part of it, so check its chat "
    "before trying again."
)

# Bounds on what a row keeps.
_OUTCOME_MAX = 500
EDITED_BODY_MAX = 8000


class AlertApproval(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    alert_id: int
    approved_by_person_id: int | None = None
    approved_at: str
    edited_body: str | None = None
    status: str = STATUS_RUNNING
    outcome: str = ""
    session_id: str | None = None
    finished_at: str | None = None


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS alert_approvals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_id INTEGER NOT NULL,
            approved_by_person_id INTEGER,
            approved_at TEXT NOT NULL,
            edited_body TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            outcome TEXT NOT NULL DEFAULT '',
            session_id TEXT,
            finished_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_alert_approvals_alert
            ON alert_approvals(alert_id, id DESC);
        CREATE TABLE IF NOT EXISTS alert_held_actions (
            alert_id INTEGER PRIMARY KEY,
            action_id INTEGER NOT NULL
        );
    """)


def _row(row: sqlite3.Row) -> AlertApproval:
    return AlertApproval(**dict(row))


def claim(
    alert_id: int,
    *,
    approved_by_person_id: int | None,
    edited_body: str | None = None,
    db_path: Path | None = None,
) -> AlertApproval | None:
    """Mark the card approved and record who approved it, in one go.

    Returns the new approval, or None when the card can't be approved now:
    it has left the queue, it is already being carried out, or it was
    carried out. A card whose last approval failed can be approved again.
    """
    now = datetime.now(UTC).isoformat()
    with _get_conn(db_path) as conn:
        ensure_schema(conn)
        # BEGIN IMMEDIATE takes the write lock before the reads, so two
        # approvals of the same card can't both pass the checks below.
        conn.execute("BEGIN IMMEDIATE")
        status_row = conn.execute("SELECT status FROM alerts WHERE id = ?", (alert_id,)).fetchone()
        if status_row is None:
            return None
        last = conn.execute(
            "SELECT status FROM alert_approvals WHERE alert_id = ? ORDER BY id DESC LIMIT 1",
            (alert_id,),
        ).fetchone()
        if status_row["status"] in _OPEN_ALERT_STATUSES:
            if last is not None and last["status"] == STATUS_RUNNING:
                return None
        elif not (status_row["status"] == "ack" and last is not None and last["status"] == STATUS_FAILED):
            return None
        conn.execute("UPDATE alerts SET status = 'ack' WHERE id = ?", (alert_id,))
        cursor = conn.execute(
            "INSERT INTO alert_approvals (alert_id, approved_by_person_id, approved_at, edited_body, status) "
            "VALUES (?, ?, ?, ?, ?)",
            (alert_id, approved_by_person_id, now, edited_body, STATUS_RUNNING),
        )
        row = conn.execute(
            "SELECT * FROM alert_approvals WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return _row(row)


def record_held_action(alert_id: int, action_id: int, *, db_path: Path | None = None) -> None:
    """Note that this card holds that department-gated scheduled action, so
    approving the card queues it again. Only the scheduler writes this, as it
    files the card: nothing on the card itself (tags, body, source) can pick
    which action runs, since a model reading inbound mail can set those."""
    with _get_conn(db_path) as conn:
        ensure_schema(conn)
        conn.execute(
            "INSERT OR REPLACE INTO alert_held_actions (alert_id, action_id) VALUES (?, ?)",
            (alert_id, action_id),
        )


def held_action_id(alert_id: int, *, db_path: Path | None = None) -> int | None:
    """The scheduled action this card holds, or None."""
    with _get_conn(db_path) as conn:
        ensure_schema(conn)
        row = conn.execute(
            "SELECT action_id FROM alert_held_actions WHERE alert_id = ?", (alert_id,)
        ).fetchone()
        return int(row["action_id"]) if row is not None else None


def set_session(approval_id: int, session_id: str, *, db_path: Path | None = None) -> None:
    with _get_conn(db_path) as conn:
        ensure_schema(conn)
        conn.execute(
            "UPDATE alert_approvals SET session_id = ? WHERE id = ?", (session_id, approval_id)
        )


def finish(
    approval_id: int, status: str, outcome: str = "", *, db_path: Path | None = None,
) -> AlertApproval | None:
    """Record how a running approval ended (``done`` or ``failed``)."""
    if status not in (STATUS_DONE, STATUS_FAILED):
        raise ValueError(f"an approval finishes done or failed, not {status!r}")
    now = datetime.now(UTC).isoformat()
    with _get_conn(db_path) as conn:
        ensure_schema(conn)
        conn.execute(
            "UPDATE alert_approvals SET status = ?, outcome = ?, finished_at = ? "
            "WHERE id = ? AND status = ?",
            (status, outcome[:_OUTCOME_MAX], now, approval_id, STATUS_RUNNING),
        )
        row = conn.execute("SELECT * FROM alert_approvals WHERE id = ?", (approval_id,)).fetchone()
        return _row(row) if row is not None else None


def get(approval_id: int, *, db_path: Path | None = None) -> AlertApproval | None:
    with _get_conn(db_path) as conn:
        ensure_schema(conn)
        row = conn.execute("SELECT * FROM alert_approvals WHERE id = ?", (approval_id,)).fetchone()
        return _row(row) if row is not None else None


def latest_for(alert_id: int, *, db_path: Path | None = None) -> AlertApproval | None:
    with _get_conn(db_path) as conn:
        ensure_schema(conn)
        row = conn.execute(
            "SELECT * FROM alert_approvals WHERE alert_id = ? ORDER BY id DESC LIMIT 1",
            (alert_id,),
        ).fetchone()
        return _row(row) if row is not None else None


def fail_stale_running(*, db_path: Path | None = None) -> int:
    """At startup: an approval still ``running`` was cut off by a restart.
    Mark it failed so its card can be approved again."""
    now = datetime.now(UTC).isoformat()
    with _get_conn(db_path) as conn:
        ensure_schema(conn)
        cursor = conn.execute(
            "UPDATE alert_approvals SET status = ?, outcome = ?, finished_at = ? WHERE status = ?",
            (STATUS_FAILED, RESTARTED_OUTCOME, now, STATUS_RUNNING),
        )
        return cursor.rowcount
