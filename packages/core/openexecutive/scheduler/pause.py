"""Global pause switch for the Executive's autonomous work.

One operator-level flag, persisted in the episodic DB, that every autonomous
loop checks at the top of each iteration:

- ``scheduler.runner.run_scheduler`` — claims nothing (briefs, nudges, alert
  review, monitoring, cadences, scheduled outbound messages all wait),
- ``integrations.email_poller.run_email_poller`` — leaves Gmail unread,
- ``workflows.resumer`` — applies no ``wait_for_human`` timeouts and resumes
  no answered runs.

Pausing holds work; it never cancels it. Due ``scheduled_actions`` stay
``pending`` and fire on the first tick after resume. Work already running
when the pause lands finishes. Inbound conversations (web chat, Slack,
Discord, Telegram, Google Chat) are deliberately NOT gated — pausing stops
what the Executive starts on its own, not its answers to people.

``is_paused`` fails open, like the rotation claim pause: a DB error reads as
"not paused" so a storage hiccup can never wedge every loop. It is also
read-only — it never creates the DB file or the table — so loops running
against an unconfigured or test DB leave no trace.

The table survives client-slot swaps (``clients.slots._GLOBAL_TABLES``): the
switch belongs to the operator, not to whichever client company is active.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

logger = logging.getLogger(__name__)

TABLE = "executive_control"

_CREATE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    paused INTEGER NOT NULL DEFAULT 0,
    paused_at TEXT,
    paused_by TEXT,
    reason TEXT,
    updated_at TEXT
)
"""


class PauseState(BaseModel):
    paused: bool = False
    paused_at: str | None = None
    paused_by: str | None = None
    reason: str | None = None


def _db_path() -> Path:
    # Resolved at call time so tests that monkeypatch episodic.DB_PATH (and
    # EPISODIC_DB_PATH deployments) see the same file as scheduled_actions.
    from openexecutive.memory import episodic

    return Path(episodic.DB_PATH)


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def initialize_pause_db(db_path: Path | None = None) -> None:
    """Create the control table if missing. Idempotent."""
    conn = _connect(db_path if db_path is not None else _db_path())
    try:
        conn.execute(_CREATE_SQL)
        conn.commit()
    finally:
        conn.close()


def get_pause_state() -> PauseState:
    """Current state. Raises on DB errors other than "nothing stored yet"."""
    path = _db_path()
    if not path.exists():
        return PauseState()
    conn = _connect(path)
    try:
        row = conn.execute(
            f"SELECT paused, paused_at, paused_by, reason FROM {TABLE} WHERE id = 1"  # noqa: S608 — constant table name
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return PauseState()
        raise
    finally:
        conn.close()
    if row is None or not row["paused"]:
        return PauseState()
    return PauseState(
        paused=True,
        paused_at=row["paused_at"],
        paused_by=row["paused_by"],
        reason=row["reason"],
    )


# Log a read failure once per failure streak, not once per poll per loop.
_read_failing = False


def is_paused() -> bool:
    """True while the operator has paused autonomous work. Never raises."""
    global _read_failing
    try:
        paused = get_pause_state().paused
    except Exception:
        if not _read_failing:
            logger.exception(
                "pause: could not read pause state — treating as NOT paused"
            )
            _read_failing = True
        return False
    _read_failing = False
    return paused


def pause(by: str, reason: str | None = None) -> PauseState:
    """Pause autonomous work. Idempotent: re-pausing keeps the original
    ``paused_at`` / ``paused_by`` so the UI keeps showing when it started."""
    now = datetime.now(UTC).isoformat()
    conn = _connect(_db_path())
    try:
        conn.execute(_CREATE_SQL)
        conn.execute(
            f"""
            INSERT INTO {TABLE} (id, paused, paused_at, paused_by, reason, updated_at)
            VALUES (1, 1, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                paused_at = CASE WHEN paused = 1 THEN paused_at ELSE excluded.paused_at END,
                paused_by = CASE WHEN paused = 1 THEN paused_by ELSE excluded.paused_by END,
                reason = CASE WHEN paused = 1 THEN reason ELSE excluded.reason END,
                paused = 1,
                updated_at = excluded.updated_at
            """,  # noqa: S608 — constant table name
            (now, by, reason, now),
        )
        conn.commit()
    finally:
        conn.close()
    logger.warning("pause: autonomous work PAUSED by %s (reason=%r)", by, reason)
    return get_pause_state()


def resume(by: str) -> PauseState:
    """Resume autonomous work. Idempotent."""
    now = datetime.now(UTC).isoformat()
    conn = _connect(_db_path())
    try:
        conn.execute(_CREATE_SQL)
        conn.execute(
            f"""
            INSERT INTO {TABLE} (id, paused, updated_at) VALUES (1, 0, ?)
            ON CONFLICT(id) DO UPDATE SET
                paused = 0, paused_at = NULL, paused_by = NULL, reason = NULL,
                updated_at = excluded.updated_at
            """,  # noqa: S608 — constant table name
            (now,),
        )
        conn.commit()
    finally:
        conn.close()
    logger.warning("pause: autonomous work RESUMED by %s", by)
    return get_pause_state()


def count_held_actions(now: datetime | None = None) -> int:
    """Pending scheduled actions already due — what fires on resume."""
    path = _db_path()
    if not path.exists():
        return 0
    at = (now or datetime.now(UTC)).isoformat()
    conn = _connect(path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM scheduled_actions WHERE status = 'pending' AND run_at <= ?",
            (at,),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return 0
        raise
    finally:
        conn.close()
    return int(row[0])
