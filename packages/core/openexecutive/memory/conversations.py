"""Cut a chat app's endless stream into conversations.

The chat adapters name one stream per person (``memory.conversation_ids``).
Left whole, a Telegram chat shows on the Chats page as one chat that never
ends. ``current_conversation`` starts a new conversation when the stream has
been quiet for ``CONVERSATION_GAP`` or when the person asks for a fresh one
(``/new``), so each shows as its own chat with its own title, and the
Executive's replayed history starts clean.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from openexecutive.memory.conversation_ids import (
    base_session_id,
    conversation_id,
    conversation_number,
    family_clause,
    family_params,
    is_rolling,
)
from openexecutive.memory.episodic import DB_PATH, _get_conn

logger = logging.getLogger(__name__)

# Quiet time after which the next message starts a new conversation.
CONVERSATION_GAP = timedelta(hours=2)

# Title of a conversation started with /new before anything is said in it.
NEW_CONVERSATION_TITLE = "New conversation"

_SPLIT_MIGRATION = "2026-10-split-chat-app-conversations"
_TITLE_MAX = 60


def _parse(ts: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _latest(conn: Any, base_id: str) -> tuple[str, str, int] | None:
    """``(session_id, updated_at, message_count)`` of the stream's newest
    conversation, or None when it has none yet."""
    rows = conn.execute(
        "SELECT s.session_id, s.updated_at, "
        "(SELECT COUNT(*) FROM chat_messages m WHERE m.session_id = s.session_id) AS n "
        f"FROM sessions s WHERE {family_clause('s.session_id')}",
        family_params(base_id),
    ).fetchall()
    best: tuple[str, str, int] | None = None
    for row in rows:
        sid = str(row["session_id"])
        if base_session_id(sid) != base_id:
            continue
        if best is None or conversation_number(sid) > conversation_number(best[0]):
            best = (sid, str(row["updated_at"]), int(row["n"]))
    return best


def current_conversation(
    base_id: str,
    *,
    now: datetime | None = None,
    gap: timedelta = CONVERSATION_GAP,
    db_path: Path = DB_PATH,
) -> str:
    """The conversation a new message in ``base_id``'s stream belongs to.

    The newest one, unless the stream has been quiet for ``gap``: then the
    next one, which the adapter creates when it saves the turn. One with no
    messages yet (just started with /new) is always reused. Ids that are not
    a chat-app stream come back unchanged. Deterministic, so two messages
    racing across the gap land in the same new conversation.
    """
    if not is_rolling(base_id) or base_session_id(base_id) != base_id:
        return base_id
    if not db_path.exists():
        return base_id
    with _get_conn(db_path) as conn:
        latest = _latest(conn, base_id)
    if latest is None:
        return base_id
    sid, updated_at, count = latest
    if count == 0:
        return sid
    last = _parse(updated_at)
    when = now or datetime.now(UTC)
    if last is not None and when - last >= gap:
        return conversation_id(base_id, conversation_number(sid) + 1)
    return sid


def start_new_conversation(
    base_id: str,
    *,
    owner_person_id: int | None,
    db_path: Path = DB_PATH,
) -> tuple[str, str | None]:
    """Start a fresh conversation in ``base_id``'s stream (the person sent
    /new). Returns ``(new id, title of the one it ends)``; the title is None
    when there was nothing to end (the newest conversation is still empty).
    The new row has no messages, so it stays off the Chats page until the
    first turn lands in it."""
    if not is_rolling(base_id):
        raise ValueError(f"not a chat-app stream: {base_id!r}")
    base_id = base_session_id(base_id)
    now = datetime.now(UTC).isoformat()
    with _get_conn(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        latest = _latest(conn, base_id)
        if latest is not None and latest[2] == 0:
            return latest[0], None
        number = conversation_number(latest[0]) + 1 if latest is not None else 1
        new_id = conversation_id(base_id, number)
        ended = None
        if latest is not None:
            row = conn.execute(
                "SELECT title FROM sessions WHERE session_id = ?", (latest[0],)
            ).fetchone()
            ended = str(row["title"]) if row is not None else None
        conn.execute(
            "INSERT OR IGNORE INTO sessions (session_id, title, created_at, updated_at, caller_person_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (new_id, NEW_CONVERSATION_TITLE, now, now, owner_person_id),
        )
    return new_id, ended


def first_line_title(text: str) -> str:
    """A title from what the person said first, for conversations cut out of
    old history (no model call at boot)."""
    line = " ".join(str(text or "").split())
    if line.startswith("[") and "]: " in line[:80]:
        line = line.split("]: ", 1)[1]
    if len(line) <= _TITLE_MAX:
        return line
    cut = line[:_TITLE_MAX].rsplit(" ", 1)[0] or line[:_TITLE_MAX]
    return cut.rstrip(" ,.;:") + "…"


def split_existing_streams(
    *,
    gap: timedelta = CONVERSATION_GAP,
    db_path: Path | None = None,
    migration: str = _SPLIT_MIGRATION,
    only: Callable[[str], bool] | None = None,
) -> int:
    """One-shot: cut every chat-app stream saved before conversations existed
    by the same quiet-gap rule. Returns how many new conversations it made.

    An adapter that registers its streams later (``register_rolling``) cuts
    its own history once with its own ``migration`` name and an ``only``
    filter for its base ids, since this sweep may already have run without it.

    Messages move to their conversation's id; the first conversation keeps
    the base id. Nothing else moves: decisions and advice are read across the
    whole stream anyway (``conversation_ids.family_clause``), and the Drive
    files and searches a conversation remembers (``memory.drive_reads``) stay
    with the first one, as they would after a cut made live. Each takes its title from its first message, the stream's
    owner, and its read-mail lock (``mark_mail_private``, kept for good since
    the cut can't tell which part read the mail). Bounded by
    ``app_migrations`` like ``episodic.cancel_orphaned_talent_reminders``.
    """
    from openexecutive.memory.episodic import _resolve_db_path

    resolved = _resolve_db_path(db_path)
    made = 0
    with _get_conn(resolved) as conn:
        claimed = conn.execute(
            "INSERT OR IGNORE INTO app_migrations (name, applied_at) VALUES (?, ?)",
            (migration, datetime.now(UTC).isoformat()),
        )
        if claimed.rowcount == 0:
            return 0
        streams = conn.execute(
            "SELECT session_id, title, caller_person_id, mail_private FROM sessions"
        ).fetchall()
        for stream in streams:
            base = str(stream["session_id"])
            if not is_rolling(base) or base_session_id(base) != base:
                continue
            if only is not None and not only(base):
                continue
            messages = conn.execute(
                "SELECT id, role, content, created_at FROM chat_messages "
                "WHERE session_id = ? ORDER BY id",
                (base,),
            ).fetchall()
            parts: list[list[Any]] = []
            last: datetime | None = None
            for msg in messages:
                when = _parse(str(msg["created_at"]))
                if not parts or (
                    when is not None and last is not None and when - last >= gap
                    and msg["role"] == "user"
                ):
                    parts.append([])
                parts[-1].append(msg)
                if when is not None:
                    last = when
            if len(parts) < 2:
                continue
            for number, part in enumerate(parts, start=1):
                sid = conversation_id(base, number)
                first_user = next((m for m in part if m["role"] == "user"), part[0])
                title = first_line_title(str(first_user["content"])) or str(stream["title"])
                created = str(part[0]["created_at"])
                updated = str(part[-1]["created_at"])
                if number == 1:
                    conn.execute(
                        "UPDATE sessions SET title = ?, updated_at = ?, "
                        "mail_read_at = CASE WHEN mail_private THEN NULL ELSE mail_read_at END "
                        "WHERE session_id = ?",
                        (title, updated, base),
                    )
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO sessions "
                    "(session_id, title, created_at, updated_at, caller_person_id, mail_private, mail_read_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, NULL)",
                    (sid, title, created, updated, stream["caller_person_id"],
                     1 if stream["mail_private"] else 0),
                )
                ids = [int(m["id"]) for m in part]
                conn.executemany(
                    "UPDATE chat_messages SET session_id = ? WHERE id = ?",
                    [(sid, i) for i in ids],
                )
                made += 1
    if made:
        logger.info("conversations: cut old chat-app history into %d more conversations", made)
    return made
