"""Where a setup interview lives between requests.

The interview used to be held only in the API process, so a restart (a
deploy, a crash) or a second worker lost it, and the person started setup
again from the first question. Each session is now written through to the
episodic SQLite database after every turn and read back when the process
does not hold it. Rows carry the same TTL and cap the in-memory table has,
and a saved session's row is deleted at once: the transcript is unedited
free text with the company's financials in it, and the profile it produced
is the artifact worth keeping.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from openexecutive.memory import episodic

if TYPE_CHECKING:
    from openexecutive.api.routes.onboarding import InterviewSession

logger = logging.getLogger(__name__)

# Tests point this at a temp file; None means the episodic database.
DB_PATH: Path | None = None

TTL_SECONDS = 2 * 3600
MAX_ROWS = 50


def _path() -> Path:
    return DB_PATH if DB_PATH is not None else episodic.DB_PATH


def _conn() -> contextlib.closing[sqlite3.Connection]:
    """A connection that commits on success and is closed either way: the
    sqlite3 context manager alone only commits or rolls back."""
    conn = sqlite3.connect(str(_path()))
    # Deleted transcripts are zeroed, not left in free pages of the file.
    conn.execute("PRAGMA secure_delete=ON")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS onboarding_interviews ("
        " session_id TEXT PRIMARY KEY,"
        " payload TEXT NOT NULL,"
        " updated_at REAL NOT NULL)"
    )
    return contextlib.closing(conn)


def _sweep(conn: sqlite3.Connection, now: float) -> None:
    """Drop expired rows and everything past the cap. Runs on every call, so
    an abandoned interview goes even when no other setup ever starts."""
    conn.execute("DELETE FROM onboarding_interviews WHERE updated_at < ?", (now - TTL_SECONDS,))
    conn.execute(
        "DELETE FROM onboarding_interviews WHERE session_id NOT IN ("
        " SELECT session_id FROM onboarding_interviews ORDER BY updated_at DESC LIMIT ?)",
        (MAX_ROWS,),
    )


def to_payload(session: InterviewSession) -> dict[str, Any]:
    return {
        "owner_key": session.owner_key,
        "transcript": [t.model_dump() for t in session.transcript],
        "questions_asked": session.questions_asked,
        "draft": session.draft.model_dump(mode="json") if session.draft is not None else None,
        "last_hint": session.last_hint,
        "last_options": list(session.last_options),
        "saved": session.saved,
    }


def from_payload(payload: dict[str, Any]) -> InterviewSession:
    from openexecutive.api.routes.onboarding import InterviewSession
    from openexecutive.onboarding.interview import CompanyDraft, Turn

    draft = payload.get("draft")
    return InterviewSession(
        owner_key=str(payload.get("owner_key", "")),
        transcript=[Turn.model_validate(t) for t in payload.get("transcript", [])],
        questions_asked=int(payload.get("questions_asked", 0)),
        draft=CompanyDraft.model_validate(draft) if draft else None,
        last_hint=str(payload.get("last_hint", "")),
        last_options=[str(o) for o in payload.get("last_options") or [] if isinstance(o, str)],
        saved=bool(payload.get("saved", False)),
    )


def save(session_id: str, session: InterviewSession) -> None:
    """Write the session through; a failure only costs the resume."""
    now = time.time()
    session.stored_at = now
    try:
        with _conn() as conn, conn:
            conn.execute(
                "INSERT OR REPLACE INTO onboarding_interviews (session_id, payload, updated_at) "
                "VALUES (?, ?, ?)",
                (session_id, json.dumps(to_payload(session)), now),
            )
            _sweep(conn, now)
    except (OSError, sqlite3.Error) as exc:
        logger.warning("onboarding interview: could not persist (%s)", type(exc).__name__)


def load(session_id: str, *, newer_than: float | None = None) -> InterviewSession | None:
    """The stored session, or None when there is none or it has expired.

    With ``newer_than``, only a row written after that time (a save by
    another process) comes back, so a caller holding its own copy learns
    whether it is stale without paying for the row otherwise.
    """
    try:
        with _conn() as conn, conn:
            _sweep(conn, time.time())
            row = conn.execute(
                "SELECT payload, updated_at FROM onboarding_interviews WHERE session_id = ?",
                (session_id,),
            ).fetchone()
    except (OSError, sqlite3.Error) as exc:
        logger.warning("onboarding interview: could not read (%s)", type(exc).__name__)
        return None
    if row is None or time.time() - float(row[1]) > TTL_SECONDS:
        return None
    if newer_than is not None and float(row[1]) <= newer_than:
        return None
    try:
        session = from_payload(json.loads(row[0]))
        session.stored_at = float(row[1])
        return session
    except (ValueError, TypeError, AttributeError) as exc:
        logger.warning("onboarding interview: stored row unreadable (%s)", type(exc).__name__)
        return None


def delete(session_id: str) -> None:
    try:
        with _conn() as conn, conn:
            conn.execute("DELETE FROM onboarding_interviews WHERE session_id = ?", (session_id,))
            _sweep(conn, time.time())
    except (OSError, sqlite3.Error) as exc:
        logger.warning("onboarding interview: could not delete (%s)", type(exc).__name__)
