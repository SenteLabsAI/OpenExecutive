"""Act as me: the threads of a person's own mailbox the Executive opened for
them (``read_my_email``), so a later conversation can open the same one
again (``my_email_read_before``) instead of asking them to search for it.

Where, never what. A conversation that reads someone's mail keeps their
words and the Executive's replies, never the mail (``memory.session_store``),
and nothing from it teaches memory. This list keeps that promise: a row is a
thread id, its subject and first sender as one line each, the mailbox it was
read from and when — no message text, attachment or recipient. The mail
itself is read again, from their mailbox, each time it is needed.

- **Theirs alone.** Rows are keyed on the person; ``my_email_read_before``
  lists only the speaker's own, and only from the mailbox open now.
- **Short-lived.** A row older than ``RETENTION`` is never listed and is
  deleted on the next write; at most ``MAX_ROWS`` are kept per person.
- **Forgotten with Act as me.** Turning it off deletes the person's rows
  (``api.routes.delegation``).
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from openexecutive.delegation.schema import MAIL_READ_TABLE, ensure_schema

logger = logging.getLogger(__name__)

RETENTION = timedelta(days=30)
MAX_ROWS = 200
MAX_LISTED = 20
_FIELD_CHARS = 160


@dataclass(frozen=True)
class MailRead:
    thread_id: str
    subject: str
    sender: str
    read_at: str


def _db(db_path: Path | None) -> Path:
    if db_path is not None:
        return db_path
    from openexecutive.memory import episodic

    return Path(episodic.DB_PATH)


def _connect(db_path: Path | None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db(db_path)))
    ensure_schema(conn)
    return conn


def _now(now: datetime | None) -> datetime:
    return (now or datetime.now(UTC)).astimezone(UTC)


def record(
    person_id: int,
    mailbox: str,
    thread_id: str,
    *,
    subject: str,
    sender: str,
    now: datetime | None = None,
    db_path: Path | None = None,
) -> None:
    """Note that ``thread_id`` of ``person_id``'s ``mailbox`` was opened for
    them. A thread read again moves to the top. Drops rows past
    ``RETENTION`` and beyond ``MAX_ROWS``."""
    from openexecutive.delegation.ghostwriter import one_line

    at = _now(now)
    conn = _connect(db_path)
    try:
        conn.execute(
            f"INSERT INTO {MAIL_READ_TABLE} (person_id, mailbox, thread_id, subject, sender, read_at) "  # noqa: S608
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(person_id, mailbox, thread_id) DO UPDATE SET "
            "subject = excluded.subject, sender = excluded.sender, read_at = excluded.read_at",
            (
                person_id,
                mailbox.strip().lower(),
                thread_id,
                one_line(subject, _FIELD_CHARS),
                one_line(sender, _FIELD_CHARS),
                at.isoformat(),
            ),
        )
        conn.execute(
            f"DELETE FROM {MAIL_READ_TABLE} WHERE read_at < ?",  # noqa: S608 — constant table name
            ((at - RETENTION).isoformat(),),
        )
        conn.execute(
            f"DELETE FROM {MAIL_READ_TABLE} WHERE person_id = ? AND id NOT IN ("  # noqa: S608
            f"  SELECT id FROM {MAIL_READ_TABLE} WHERE person_id = ? ORDER BY read_at DESC LIMIT ?"
            ")",
            (person_id, person_id, MAX_ROWS),
        )
        conn.commit()
    finally:
        conn.close()


def recent(
    person_id: int,
    mailbox: str,
    *,
    words: str = "",
    limit: int = MAX_LISTED,
    now: datetime | None = None,
    db_path: Path | None = None,
) -> list[MailRead]:
    """``person_id``'s threads of ``mailbox`` read within ``RETENTION``,
    newest first; with ``words``, only those whose subject or sender holds
    every one of them (case-insensitive)."""
    since = (_now(now) - RETENTION).isoformat()
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT thread_id, subject, sender, read_at FROM {MAIL_READ_TABLE} "  # noqa: S608
            "WHERE person_id = ? AND mailbox = ? AND read_at >= ? ORDER BY read_at DESC",
            (person_id, mailbox.strip().lower(), since),
        ).fetchall()
    finally:
        conn.close()
    wanted = [w for w in words.lower().split() if w]
    found: list[MailRead] = []
    for thread_id, subject, sender, read_at in rows:
        haystack = f"{subject} {sender}".lower()
        if all(w in haystack for w in wanted):
            found.append(MailRead(str(thread_id), str(subject), str(sender), str(read_at)))
        if len(found) >= limit:
            break
    return found


def forget(person_id: int, *, db_path: Path | None = None) -> int:
    """Delete every row of ``person_id``'s; returns how many."""
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            f"DELETE FROM {MAIL_READ_TABLE} WHERE person_id = ?",  # noqa: S608 — constant table name
            (person_id,),
        )
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()
