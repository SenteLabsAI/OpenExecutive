"""Which conversations last night's look back reads.

Plain SQL, no model. A session qualifies when it has a message newer than the
watermark the last run reached, it is the owner's own (no user message from a
rostered teammate), and it is not a private-mail chat (``sessions.mail_private``,
a conversation that quoted other people's mail). Sessions carrying a signal the
product already records come first; the rest fill the remaining slots in a
random order so quiet failures get read too:

- a 👎 on a reply, or a reply the person stopped mid-stream;
- Try again / Edit (a ``session_rewound`` audit row);
- a tool call that failed (a ``tool_invocation`` audit row with ``ok`` false);
- a reply that named an area it could not answer from (``sources.unavailable``);
- a follow-up typed within two minutes of a reply (the shape of "no, I meant…").
"""
from __future__ import annotations

import json
import logging
import random
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from openexecutive.chat_review.store import _db

logger = logging.getLogger(__name__)

QUICK_FOLLOW_UP_SECONDS = 120


@dataclass
class Candidate:
    session_id: str
    title: str
    last_message_id: int
    message_count: int
    signals: list[str] = field(default_factory=list)

    @property
    def score(self) -> int:
        return len(self.signals)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _parse_ts(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _owner_sessions(
    conn: sqlite3.Connection, *, since_message_id: int, principal_id: int | None
) -> list[Candidate]:
    # A channel adapter writes its session row after the turn, so a
    # conversation may have messages and no row yet: start from the messages.
    cols = _columns(conn, "sessions")
    where: list[str] = []
    args: list[Any] = []
    if "mail_private" in cols:
        where.append("COALESCE(s.mail_private, 0) = 0")
    if "caller_person_id" in cols:
        # A session a teammate opened is theirs, even before its messages
        # carry a sender: excluded until they opt in.
        where.append("(s.caller_person_id IS NULL OR s.caller_person_id = ?)")
        args.append(principal_id if principal_id is not None else -1)
    args.append(since_message_id)
    rows = conn.execute(
        "SELECT m.session_id, s.title, MAX(m.id) AS last_id, COUNT(m.id) AS n "
        "FROM chat_messages m LEFT JOIN sessions s ON s.session_id = m.session_id"
        + (" WHERE " + " AND ".join(where) if where else "")
        + " GROUP BY m.session_id HAVING MAX(m.id) > ? ORDER BY MAX(m.id) DESC",
        args,
    ).fetchall()
    out: list[Candidate] = []
    msg_cols = _columns(conn, "chat_messages")
    for row in rows:
        sid = str(row["session_id"])
        if "sender_person_id" in msg_cols:
            # A teammate's conversation is theirs: excluded until they opt in.
            foreign = conn.execute(
                "SELECT 1 FROM chat_messages WHERE session_id = ? AND role = 'user' "
                "AND sender_person_id IS NOT NULL AND sender_person_id != ? LIMIT 1",
                (sid, principal_id if principal_id is not None else -1),
            ).fetchone()
            if foreign is not None:
                continue
        out.append(Candidate(
            session_id=sid,
            title=str(row["title"] or ""),
            last_message_id=int(row["last_id"]),
            message_count=int(row["n"]),
        ))
    return out


def _signals(conn: sqlite3.Connection, sid: str) -> list[str]:
    signals: list[str] = []
    msg_cols = _columns(conn, "chat_messages")
    select = ["id", "role", "created_at"]
    for optional in ("feedback", "stopped", "sources"):
        if optional in msg_cols:
            select.append(optional)
    rows = conn.execute(
        f"SELECT {', '.join(select)} FROM chat_messages WHERE session_id = ? ORDER BY id",
        (sid,),
    ).fetchall()
    last_reply_at: datetime | None = None
    for row in rows:
        keys = row.keys()
        if row["role"] == "assistant":
            if "feedback" in keys and row["feedback"] == "down" and "thumbs_down" not in signals:
                signals.append("thumbs_down")
            if "stopped" in keys and row["stopped"] and "stopped" not in signals:
                signals.append("stopped")
            if "sources" in keys and row["sources"]:
                try:
                    payload = json.loads(row["sources"])
                except json.JSONDecodeError:
                    payload = {}
                if isinstance(payload, dict) and payload.get("unavailable") and "unavailable" not in signals:
                    signals.append("unavailable")
            last_reply_at = _parse_ts(row["created_at"])
        elif row["role"] == "user" and last_reply_at is not None:
            asked_at = _parse_ts(row["created_at"])
            quick = (
                asked_at is not None
                and (asked_at - last_reply_at).total_seconds() <= QUICK_FOLLOW_UP_SECONDS
            )
            if quick and "quick_follow_up" not in signals:
                signals.append("quick_follow_up")
            last_reply_at = None
    if _table_exists(conn, "audit_log"):
        rewound = conn.execute(
            "SELECT 1 FROM audit_log WHERE session_id = ? AND event_type = 'session_rewound' LIMIT 1",
            (sid,),
        ).fetchone()
        if rewound is not None:
            signals.append("try_again")
        failed = conn.execute(
            "SELECT 1 FROM audit_log WHERE session_id = ? AND event_type = 'tool_invocation' "
            "AND details_json LIKE '%\"ok\": false%' LIMIT 1",
            (sid,),
        ).fetchone()
        if failed is not None:
            signals.append("tool_failed")
    return signals


def pick_sessions(
    *,
    since_message_id: int,
    limit: int,
    principal_id: int | None,
    db_path: Path | None = None,
    rng: random.Random | None = None,
) -> tuple[list[Candidate], int]:
    """``(chosen, considered)``: up to ``limit`` of the owner's conversations
    with new messages, signalled ones first, then a random sample of the rest.
    Never raises: a read failure picks nothing."""
    path = _db(db_path)
    if not path.exists():
        return [], 0
    try:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        try:
            if not (_table_exists(conn, "sessions") and _table_exists(conn, "chat_messages")):
                return [], 0
            candidates = _owner_sessions(conn, since_message_id=since_message_id, principal_id=principal_id)
            for c in candidates:
                c.signals = _signals(conn, c.session_id)
        finally:
            conn.close()
    except sqlite3.Error:
        logger.exception("chat_review: picking conversations failed")
        return [], 0
    # Only a conversation with at least one exchange can teach anything.
    candidates = [c for c in candidates if c.message_count >= 2]
    signalled = [c for c in candidates if c.signals]
    quiet = [c for c in candidates if not c.signals]
    (rng or random.Random()).shuffle(quiet)
    signalled.sort(key=lambda c: (-c.score, -c.last_message_id))
    chosen = (signalled + quiet)[: max(0, limit)]
    return chosen, len(candidates)
