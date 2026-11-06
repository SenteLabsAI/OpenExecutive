"""What the Executive already wrote back on an email thread.

An email turn knows the thread only through what the sender quoted below
their message, and people trim that. So the answers the poller mails itself
(a mail source's answer) are kept here per thread, and the next turn on that thread from the same person
is shown the last few (``block``). Without it, a reply could say "there was
no draft" right after the previous one sent the draft.

Only what was sent to that same address on that same thread is shown, so a
turn never sees mail that went to someone else. Kept in the episodic
database, trimmed to the last ``KEEP`` per thread. Plain code.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

TABLE = "email_thread_replies"
# Replies shown to the next turn, and how long each may be.
SHOWN = 3
MAX_CHARS = 2000
# Replies kept per thread.
KEEP = 10
# Tests point this at a temporary file (tests/conftest.py).
DB_PATH_OVERRIDE: Path | None = None


def _db(db_path: Path | None) -> Path:
    if db_path is not None:
        return db_path
    if DB_PATH_OVERRIDE is not None:
        return DB_PATH_OVERRIDE
    from openexecutive.memory import episodic

    return Path(episodic.DB_PATH)


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db(db_path)))
    conn.row_factory = sqlite3.Row
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {TABLE} ("  # noqa: S608 - constant table name
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  thread_key TEXT NOT NULL,"
        "  to_addr TEXT NOT NULL,"
        "  body TEXT NOT NULL,"
        "  sent_at TEXT NOT NULL"
        ")"
    )
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_thread ON {TABLE} (thread_key, id)")
    return conn


def record(thread_key: str, to: str, body: str, *, db_path: Path | None = None) -> None:
    """Keep ``body``, sent to ``to`` on ``thread_key``. Never raises."""
    key, addr, text = thread_key.strip(), to.strip().lower(), body.strip()
    if not key or not addr or not text:
        return
    try:
        conn = _connect(db_path)
        try:
            with conn:
                conn.execute(
                    f"INSERT INTO {TABLE} (thread_key, to_addr, body, sent_at) VALUES (?, ?, ?, ?)",  # noqa: S608
                    (key, addr, text[:MAX_CHARS * 2], datetime.now(UTC).isoformat()),
                )
                conn.execute(
                    f"DELETE FROM {TABLE} WHERE thread_key = ? AND id NOT IN "  # noqa: S608
                    f"(SELECT id FROM {TABLE} WHERE thread_key = ? ORDER BY id DESC LIMIT ?)",
                    (key, key, KEEP),
                )
        finally:
            conn.close()
    except Exception:
        logger.exception("email: could not keep the reply sent on thread %s", key)


def recent(thread_key: str, to: str, *, limit: int = SHOWN, db_path: Path | None = None) -> list[tuple[str, str]]:
    """The last ``limit`` replies sent to ``to`` on ``thread_key``, oldest
    first, as ``(sent_at, body)``. Empty on any error."""
    key, addr = thread_key.strip(), to.strip().lower()
    if not key or not addr:
        return []
    try:
        conn = _connect(db_path)
        try:
            rows = conn.execute(
                f"SELECT sent_at, body FROM {TABLE} WHERE thread_key = ? AND to_addr = ? "  # noqa: S608
                "ORDER BY id DESC LIMIT ?",
                (key, addr, limit),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        logger.exception("email: could not read the replies sent on thread %s", key)
        return []
    return [(str(r["sent_at"]), str(r["body"])) for r in reversed(rows)]


def block(thread_key: str, to: str, *, db_path: Path | None = None) -> str:
    """The turn's note of what it already wrote to ``to`` on this thread, or
    "" when nothing was. Goes in the user turn, never a cached block."""
    from openexecutive.orchestrator.content_trust import wrap_untrusted

    sent = recent(thread_key, to, db_path=db_path)
    if not sent:
        return ""
    parts = []
    for when, body in sent:
        text = body if len(body) <= MAX_CHARS else body[:MAX_CHARS] + "\n[…]"
        parts.append(f"[{when[:16].replace('T', ' ')} UTC]\n{text}")
    # What was sent can quote the sender's own mail, so it is content, not
    # instructions, like the email itself.
    return (
        "--- YOUR EARLIER REPLIES ON THIS THREAD ---\n"
        "What you already wrote back to this person here, oldest first. Build on it: "
        "don't say something doesn't exist if you sent it, and don't repeat it unless asked.\n"
        + wrap_untrusted("\n\n".join(parts), source="earlier_reply", author="you")
        + "\n--- END OF EARLIER REPLIES ---"
    )
