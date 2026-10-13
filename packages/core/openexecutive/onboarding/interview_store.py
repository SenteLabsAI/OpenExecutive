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


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_path()))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS onboarding_interviews ("
        " session_id TEXT PRIMARY KEY,"
        " payload TEXT NOT NULL,"
        " updated_at REAL NOT NULL)"
    )
    return conn


def to_payload(session: InterviewSession) -> dict[str, Any]:
    return {
        "transcript": [t.model_dump() for t in session.transcript],
        "questions_asked": session.questions_asked,
        "draft": session.draft.model_dump(mode="json") if session.draft is not None else None,
        "last_hint": session.last_hint,
        "saved": session.saved,
    }


def from_payload(payload: dict[str, Any]) -> InterviewSession:
    from openexecutive.api.routes.onboarding import InterviewSession
    from openexecutive.onboarding.interview import CompanyDraft, Turn

    draft = payload.get("draft")
    return InterviewSession(
        transcript=[Turn.model_validate(t) for t in payload.get("transcript", [])],
        questions_asked=int(payload.get("questions_asked", 0)),
        draft=CompanyDraft.model_validate(draft) if draft else None,
        last_hint=str(payload.get("last_hint", "")),
        saved=bool(payload.get("saved", False)),
    )


def save(session_id: str, session: InterviewSession) -> None:
    """Write the session through; a failure only costs the resume."""
    now = time.time()
    try:
        with _conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO onboarding_interviews (session_id, payload, updated_at) "
                "VALUES (?, ?, ?)",
                (session_id, json.dumps(to_payload(session)), now),
            )
            conn.execute(
                "DELETE FROM onboarding_interviews WHERE updated_at < ?", (now - TTL_SECONDS,)
            )
            conn.execute(
                "DELETE FROM onboarding_interviews WHERE session_id NOT IN ("
                " SELECT session_id FROM onboarding_interviews ORDER BY updated_at DESC LIMIT ?)",
                (MAX_ROWS,),
            )
    except (OSError, sqlite3.Error) as exc:
        logger.warning("onboarding interview: could not persist (%s)", type(exc).__name__)


def load(session_id: str) -> InterviewSession | None:
    """The stored session, or None when there is none or it has expired."""
    try:
        with _conn() as conn:
            row = conn.execute(
                "SELECT payload, updated_at FROM onboarding_interviews WHERE session_id = ?",
                (session_id,),
            ).fetchone()
    except (OSError, sqlite3.Error) as exc:
        logger.warning("onboarding interview: could not read (%s)", type(exc).__name__)
        return None
    if row is None or time.time() - float(row[1]) > TTL_SECONDS:
        return None
    try:
        return from_payload(json.loads(row[0]))
    except (ValueError, TypeError) as exc:
        logger.warning("onboarding interview: stored row unreadable (%s)", type(exc).__name__)
        return None


def delete(session_id: str) -> None:
    try:
        with _conn() as conn:
            conn.execute("DELETE FROM onboarding_interviews WHERE session_id = ?", (session_id,))
    except (OSError, sqlite3.Error) as exc:
        logger.warning("onboarding interview: could not delete (%s)", type(exc).__name__)
