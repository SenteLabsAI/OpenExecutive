"""Every draft Act as me saved in someone's Gmail (``delegation_drafts``).

One row per draft, from chat (``ghostwrite_email``) or the inbox watcher. It
is what the daily limit counts (``delegation.caps``), and what "How I write"
leaves out when it learns from sent mail:

- a chat draft's whole thread, since the person asked for it there;
- an inbox draft only as the message it became once sent. The watcher drafts
  into so many threads that skipping them all would leave little of the
  person's own writing to learn from.

Metadata only: ids and times, never an address, a subject or any text.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from openexecutive.delegation.schema import DRAFTS_TABLE, ensure_schema

SOURCE_CHAT = "chat"
SOURCE_INBOX = "inbox"
_SOURCES = frozenset({SOURCE_CHAT, SOURCE_INBOX})


def _db(db_path: Path | None) -> Path:
    if db_path is not None:
        return db_path
    from openexecutive.memory import episodic

    return Path(episodic.DB_PATH)


def _connect(db_path: Path | None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db(db_path)))
    ensure_schema(conn)
    return conn


def record(
    person_id: int,
    *,
    source: str,
    thread_id: str | None,
    draft_id: str,
    message_id: str,
    now: datetime,
    db_path: Path | None = None,
) -> int:
    """Store a draft just saved in ``person_id``'s Gmail; returns its row id."""
    if source not in _SOURCES:
        raise ValueError(f"unknown draft source {source!r}")
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            f"INSERT INTO {DRAFTS_TABLE} "  # noqa: S608 — constant table name
            "(person_id, source, thread_id, draft_id, message_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (person_id, source, thread_id, draft_id, message_id, now.isoformat()),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def count_since(
    person_id: int, since: datetime, *, source: str | None = None, db_path: Path | None = None
) -> int:
    """Drafts saved as ``person_id`` at or after ``since``, from chat and the
    inbox alike, or from one ``source``. Raises on a read error: a cap must
    not fail open."""
    sql = f"SELECT COUNT(*) FROM {DRAFTS_TABLE} WHERE person_id = ? AND created_at >= ?"  # noqa: S608
    params: tuple[object, ...] = (person_id, since.isoformat())
    if source is not None:
        sql += " AND source = ?"
        params = (*params, source)
    conn = _connect(db_path)
    try:
        row = conn.execute(sql, params).fetchone()
    finally:
        conn.close()
    return int(row[0]) if row else 0


def mark_sent(
    person_id: int, draft_id: str, sent_message_id: str, *, db_path: Path | None = None
) -> bool:
    """Record the message a draft became when it was sent."""
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            f"UPDATE {DRAFTS_TABLE} SET sent_message_id = ? "  # noqa: S608 — constant table name
            "WHERE person_id = ? AND draft_id = ?",
            (sent_message_id, person_id, draft_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def chat_thread_ids(person_id: int, *, db_path: Path | None = None) -> set[str]:
    """The threads chat drafted into as ``person_id``."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT DISTINCT thread_id FROM {DRAFTS_TABLE} "  # noqa: S608 — constant table name
            "WHERE person_id = ? AND source = ? AND thread_id IS NOT NULL",
            (person_id, SOURCE_CHAT),
        ).fetchall()
    finally:
        conn.close()
    return {str(r[0]) for r in rows}


def sent_message_ids(person_id: int, *, db_path: Path | None = None) -> set[str]:
    """The messages drafts written as ``person_id`` became once sent."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT sent_message_id FROM {DRAFTS_TABLE} "  # noqa: S608 — constant table name
            "WHERE person_id = ? AND sent_message_id IS NOT NULL",
            (person_id,),
        ).fetchall()
    finally:
        conn.close()
    return {str(r[0]) for r in rows}


def usage_since(person_id: int, since: datetime, *, db_path: Path | None = None) -> tuple[int, int]:
    """``(drafts saved, of those sent)`` for ``person_id`` since ``since``:
    counts only, never what they said or to whom (the owner's view of a team
    member's use). Raises on a read error."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            f"SELECT COUNT(*), COUNT(sent_message_id) FROM {DRAFTS_TABLE} "  # noqa: S608 — constant table name
            "WHERE person_id = ? AND created_at >= ?",
            (person_id, since.isoformat()),
        ).fetchone()
    finally:
        conn.close()
    return int(row[0] or 0), int(row[1] or 0)
