from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openexecutive.memory.conversation_ids import is_rolling
from openexecutive.memory.drive_reads import delete_session_drive_memory
from openexecutive.memory.episodic import DB_PATH, _get_conn


def search_conversation_id(conversation_id: str) -> str:
    """One persisted opaque search UUID per full runtime conversation.

    Chat adapters reconstruct Session objects on follow-ups. Their private
    channel/thread identifiers stay in SQLite, never in search requests. An
    atomic insert handles overlapping turns and different worker processes.
    The table is lazy so disabled search never writes search state.
    """
    with _get_conn() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS search_conversations "
            "(conversation_id TEXT PRIMARY KEY, search_id TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO search_conversations (conversation_id, search_id) VALUES (?, ?)",
            (conversation_id, str(uuid.uuid4())),
        )
        row = conn.execute(
            "SELECT search_id FROM search_conversations WHERE conversation_id = ?", (conversation_id,)
        ).fetchone()
        return str(row["search_id"])


def create_session(
    session_id: str,
    title: str,
    created_at: str,
    caller_person_id: int | None = None,
    db_path: Path = DB_PATH,
) -> None:
    with _get_conn(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO sessions (session_id, title, created_at, updated_at, caller_person_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, title, created_at, created_at, caller_person_id),
        )
        # Bind the owner late if turn 1 landed without a resolved caller
        # (e.g. principal not yet seeded, or DB lookup transiently failed)
        # and turn 2 succeeded in resolving one. INSERT OR IGNORE would
        # otherwise leave the row orphaned with caller_person_id = NULL,
        # invisible to its real owner forever.
        if caller_person_id is not None:
            conn.execute(
                "UPDATE sessions SET caller_person_id = ? "
                "WHERE session_id = ? AND caller_person_id IS NULL",
                (caller_person_id, session_id),
            )


def update_session_title(session_id: str, title: str, db_path: Path = DB_PATH) -> None:
    """The automatic title (a placeholder, then a generated one). Never
    replaces a name the owner gave the chat (``rename_session``)."""
    with _get_conn(db_path) as conn:
        conn.execute(
            "UPDATE sessions SET title = ? WHERE session_id = ? AND title_locked = 0",
            (title, session_id),
        )


def rename_session(session_id: str, title: str, db_path: Path = DB_PATH) -> bool:
    """The owner's own name for a chat, kept from then on. False when there
    is no such chat."""
    with _get_conn(db_path) as conn:
        cur = conn.execute(
            "UPDATE sessions SET title = ?, title_locked = 1 WHERE session_id = ?",
            (title, session_id),
        )
        return cur.rowcount > 0


def set_pinned(session_id: str, pinned: bool, db_path: Path = DB_PATH) -> bool:
    """Pin a chat to the top of its owner's list, or unpin it. Pinning again
    keeps the first pin's time. False when there is no such chat."""
    now = datetime.now(UTC).isoformat()
    with _get_conn(db_path) as conn:
        cur = conn.execute(
            "UPDATE sessions SET pinned_at = CASE WHEN ? THEN COALESCE(pinned_at, ?) END "
            "WHERE session_id = ?",
            (1 if pinned else 0, now, session_id),
        )
        return cur.rowcount > 0


def update_session_timestamp(session_id: str, db_path: Path = DB_PATH) -> None:
    now = datetime.now(UTC).isoformat()
    with _get_conn(db_path) as conn:
        conn.execute("UPDATE sessions SET updated_at = ? WHERE session_id = ?", (now, session_id))


def save_message(
    session_id: str,
    role: str,
    content: str | list[dict[str, Any]],
    db_path: Path = DB_PATH,
    action_chips: str | None = None,
    stopped: bool = False,
    sender_person_id: int | None = None,
    sources: str | None = None,
) -> int:
    """Persist one chat message and return its row id. ``action_chips`` is a
    JSON-encoded list of the assistant turn's action-chip dicts (or None), so
    reopening a saved session restores the ✓ tool-action pills instead of bare
    prose. ``stopped`` marks an assistant message the user halted mid-stream,
    so the reply is not read back as a complete one. ``sender_person_id`` is
    the rostered Person who actually wrote a user message — pass the resolved
    sender, never the session owner's principal fallback, so per-person
    learning never reads an outsider's words as someone on the roster.
    ``sources`` is the JSON ``TurnSources.payload()`` of an assistant reply —
    what it looked at and which areas it had to leave out — or None."""
    text = content if isinstance(content, str) else str(content)
    now = datetime.now(UTC).isoformat()
    with _get_conn(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO chat_messages "
            "(session_id, role, content, created_at, action_chips, stopped, sender_person_id, "
            "sources) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id, role, text, now, action_chips, 1 if stopped else 0,
                sender_person_id, sources,
            ),
        )
        return int(cur.lastrowid or 0)


FEEDBACK_VALUES = frozenset({"up", "down"})
_FEEDBACK_NOTE_MAX = 500


def set_message_feedback(
    session_id: str,
    message_id: int,
    feedback: str | None,
    note: str | None = None,
    db_path: Path = DB_PATH,
    by_person_id: int | None = None,
) -> bool:
    """Record 👍/👎 (or clear it with ``None``) on one assistant message.

    Scoped by ``session_id`` as well as the id so a caller cannot rate a
    message in a session it does not own by guessing ids. Returns False when
    no assistant message matched. ``by_person_id`` is who left it (the
    resolved caller), so per-person learning reads only a person's own
    reactions."""
    if feedback is not None and feedback not in FEEDBACK_VALUES:
        raise ValueError(f"feedback must be one of {sorted(FEEDBACK_VALUES)} or None")
    clean_note = (note or "").strip()[:_FEEDBACK_NOTE_MAX] or None
    with _get_conn(db_path) as conn:
        cur = conn.execute(
            "UPDATE chat_messages SET feedback = ?, feedback_note = ?, feedback_by_person_id = ? "
            "WHERE id = ? AND session_id = ? AND role = 'assistant'",
            (feedback, clean_note if feedback is not None else None,
             by_person_id if feedback is not None else None, message_id, session_id),
        )
        return cur.rowcount > 0


def load_messages(
    session_id: str, db_path: Path = DB_PATH, *, with_senders: bool = False
) -> list[dict[str, Any]]:
    """The conversation, oldest first. ``with_senders`` adds who wrote each
    user message (``sender_person_id``, when known), for a chat more than one
    person writes in (``shared_with``)."""
    if not db_path.exists():
        return []
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT id, role, content, action_chips, stopped, feedback, sources, sender_person_id "
            "FROM chat_messages "
            "WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        msg: dict[str, Any] = {"role": row["role"], "content": row["content"]}
        # The row id lets the UI attach 👍/👎 to a reloaded reply, and name
        # the turn it takes back when the person edits their message or asks
        # again (``rewind_last_turn``). Only on assistant rows, alongside
        # `feedback`, for the same reason as `actions` below: untouched user
        # rows keep their exact shape.
        if row["role"] == "assistant":
            msg["id"] = row["id"]
            if row["feedback"]:
                msg["feedback"] = row["feedback"]
        elif with_senders and row["sender_person_id"] is not None:
            msg["sender_person_id"] = int(row["sender_person_id"])
        raw = row["action_chips"]
        if raw:
            try:
                chips = json.loads(raw)
            except (ValueError, TypeError):
                chips = None
            if chips:
                msg["actions"] = chips
        # Attached only when true, like `actions` above, so untouched rows keep
        # the exact dict shape every existing consumer expects. Safe to ride
        # along into `Session.conversation_history`: the Executive rebuilds each
        # history turn as {"role", "content"}, so extra keys never reach the
        # Anthropic payload.
        if row["stopped"]:
            msg["stopped"] = True
        # What the reply looked at, for the web chat to show under it. Same
        # attach-only-when-present rule as `actions`.
        if row["role"] == "assistant" and row["sources"]:
            try:
                sources = json.loads(row["sources"])
            except (ValueError, TypeError):
                sources = None
            if isinstance(sources, dict):
                msg["sources"] = sources
        out.append(msg)
    return out


def list_sessions(
    caller_person_id: int,
    db_path: Path = DB_PATH,
) -> list[dict[str, Any]]:
    """List sessions owned by `caller_person_id`: pinned ones first (newest
    pin first, with ``pinned_at``), then the rest newest first.

    Legacy rows with caller_person_id IS NULL (created before this column
    existed) are excluded — the comparison `NULL = ?` never matches in
    SQLite. Only the principal can still open them by id (see
    `api.routes.chat._session_access`).

    Each row adds ``preview``: the last message as one line (``_preview``).
    """
    if not db_path.exists():
        return []
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT s.session_id, s.title, s.created_at, s.updated_at, s.pinned_at,
                   s.topic_id, COUNT(m.id) AS message_count, MAX(m.id) AS last_id
            FROM sessions s
            LEFT JOIN chat_messages m ON m.session_id = s.session_id
            WHERE s.caller_person_id = ?
            GROUP BY s.session_id
            ORDER BY s.pinned_at IS NULL, s.pinned_at DESC, s.updated_at DESC
            """,
            (caller_person_id,),
        ).fetchall()
        # Each chat's last message, by its id (the primary key), so the
        # preview costs no second pass over the messages.
        last_ids = [row["last_id"] for row in rows if row["last_id"] is not None]
        last: dict[int, Any] = {}
        for at in range(0, len(last_ids), _IN_BATCH):
            batch = last_ids[at:at + _IN_BATCH]
            marks = ",".join("?" * len(batch))
            for msg in conn.execute(
                "SELECT id, role, substr(content, 1, ?) AS content, sender_person_id FROM chat_messages "
                f"WHERE id IN ({marks})",
                [_PREVIEW_READ_CHARS, *batch],
            ):
                last[msg["id"]] = msg
    # A chat-app conversation started with /new has a row before it has a
    # word in it; it shows once the first turn lands.
    out: list[dict[str, Any]] = []
    for row in rows:
        if not row["message_count"] and is_rolling(str(row["session_id"])):
            continue
        item = {
            k: row[k]
            for k in ("session_id", "title", "created_at", "updated_at", "message_count")
        }
        if row["pinned_at"]:
            item["pinned_at"] = row["pinned_at"]
        if row["topic_id"] is not None:
            item["topic_id"] = row["topic_id"]
        msg = last.get(row["last_id"]) if row["last_id"] is not None else None
        # In a shared chat the last message may be a teammate's: no "You: ".
        mine = msg is not None and msg["sender_person_id"] in (None, caller_person_id)
        item["preview"] = _preview(msg["role"], msg["content"], mine=mine) if msg is not None else None
        out.append(item)
    return out


_PREVIEW_CHARS = 120
# How much of a last message the list reads: enough to fill the preview once
# Markdown marks are gone, without loading a long answer in full per chat.
_PREVIEW_READ_CHARS = 2000
# Ids per IN (...) query, under SQLite's bound-parameter limit.
_IN_BATCH = 500
# Markdown's marks, which read as noise in a one-line preview: a link keeps
# its text; emphasis, code ticks, and a heading's or quote's lead go. An
# underscore inside a word (snake_case) and a # mid-line (C#) stay.
_MARKDOWN_MARKS = re.compile(
    r"\[([^\]]*)\]\([^)]*\)|`+|\*+|(?<!\w)_+|_+(?!\w)|^[ \t]*#+[ \t]|^[ \t]*>[ \t]?",
    re.MULTILINE,
)


def _preview(role: str | None, content: str | None, *, mine: bool = True) -> str | None:
    """The chat's last message as one plain line, about ``_PREVIEW_CHARS``
    long, with "You: " before the person's own words (``mine``: not someone
    else's in a shared chat); None for no message."""
    if content is None:
        return None
    text = _MARKDOWN_MARKS.sub(lambda m: m.group(1) or " ", str(content))
    text = " ".join(text.split())
    if not text:
        return None
    if len(text) > _PREVIEW_CHARS:
        cut = text.rfind(" ", 0, _PREVIEW_CHARS)
        text = text[: cut if cut > _PREVIEW_CHARS // 2 else _PREVIEW_CHARS].rstrip(" ,.;:") + "…"
    return f"You: {text}" if role == "user" and mine else text


_SEARCH_LIMIT = 50
_SNIPPET_CHARS = 160


def _like_pattern(query: str) -> str:
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _snippet(content: str, query: str) -> str:
    """About ``_SNIPPET_CHARS`` of ``content`` around the first match of
    ``query``, on one line, with … where it was cut."""
    text = " ".join(str(content or "").split())
    at = text.lower().find(query.lower())
    if at < 0:
        return text[:_SNIPPET_CHARS] + ("…" if len(text) > _SNIPPET_CHARS else "")
    lead = max(0, (_SNIPPET_CHARS - len(query)) // 3)
    start = max(0, at - lead)
    if start:
        space = text.find(" ", start)
        start = space + 1 if 0 <= space < at else start
    end = min(len(text), start + _SNIPPET_CHARS)
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


def search_sessions(
    caller_person_id: int,
    query: str,
    db_path: Path = DB_PATH,
) -> list[dict[str, Any]]:
    """The caller's own chats whose title or messages contain ``query``
    (case-insensitive), newest first, at most ``_SEARCH_LIMIT``.

    Scoped exactly like ``list_sessions`` (the caller's own rows), so nobody
    else's conversation can match. Each row adds ``match_count`` (how many
    messages contain the words; 0 when only the title does) and ``snippet``
    (the newest matching message around the match, prefixed with who said
    it), or None for a title-only match."""
    q = " ".join(query.split())
    if not q or not db_path.exists():
        return []
    pattern = _like_pattern(q)
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT s.session_id, s.title, s.created_at, s.updated_at, s.topic_id,
                   (SELECT COUNT(*) FROM chat_messages m
                     WHERE m.session_id = s.session_id) AS message_count,
                   (SELECT COUNT(*) FROM chat_messages m
                     WHERE m.session_id = s.session_id
                       AND m.content LIKE ? ESCAPE '\\') AS match_count,
                   s.title LIKE ? ESCAPE '\\' AS title_match
            FROM sessions s
            WHERE s.caller_person_id = ?
            ORDER BY s.updated_at DESC
            """,
            (pattern, pattern, caller_person_id),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            if not row["message_count"] and is_rolling(str(row["session_id"])):
                continue
            if not row["match_count"] and not row["title_match"]:
                continue
            item = {
                k: row[k]
                for k in ("session_id", "title", "created_at", "updated_at",
                          "message_count", "match_count")
            }
            if row["topic_id"] is not None:
                item["topic_id"] = row["topic_id"]
            item["snippet"] = None
            if row["match_count"]:
                hit = conn.execute(
                    "SELECT role, content FROM chat_messages "
                    "WHERE session_id = ? AND content LIKE ? ESCAPE '\\' "
                    "ORDER BY id DESC LIMIT 1",
                    (row["session_id"], pattern),
                ).fetchone()
                if hit is not None:
                    who = "You" if hit["role"] == "user" else "Executive"
                    item["snippet"] = f"{who}: {_snippet(hit['content'], q)}"
            out.append(item)
            if len(out) >= _SEARCH_LIMIT:
                break
    return out


def delete_session(session_id: str, db_path: Path = DB_PATH) -> bool:
    if not db_path.exists():
        return False
    with _get_conn(db_path) as conn:
        conn.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM session_shares WHERE session_id = ?", (session_id,))
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='search_conversations'").fetchone():
            conn.execute("DELETE FROM search_conversations WHERE conversation_id = ?", (session_id,))
        cur = conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        deleted = cur.rowcount > 0
    delete_session_drive_memory(session_id, db_path)
    from openexecutive.memory.chat_review import forget_session

    forget_session(session_id, db_path=db_path)
    return deleted


def get_session_metadata(session_id: str, db_path: Path = DB_PATH) -> dict[str, Any] | None:
    if not db_path.exists():
        return None
    with _get_conn(db_path) as conn:
        row = conn.execute(
            """
            SELECT s.session_id, s.title, s.created_at, s.updated_at, s.pinned_at,
                   s.topic_id, COUNT(m.id) AS message_count
            FROM sessions s
            LEFT JOIN chat_messages m ON m.session_id = s.session_id
            WHERE s.session_id = ?
            GROUP BY s.session_id
            """,
            (session_id,),
        ).fetchone()
    if row is None:
        return None
    meta = dict(row)
    if not meta.get("pinned_at"):
        meta.pop("pinned_at", None)
    if meta.get("topic_id") is None:
        meta.pop("topic_id", None)
    return meta


def get_session_owner(session_id: str, db_path: Path = DB_PATH) -> tuple[bool, int | None]:
    """``(exists, caller_person_id)`` for one session."""
    if not db_path.exists():
        return False, None
    with _get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT caller_person_id FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
    if row is None:
        return False, None
    owner = row["caller_person_id"]
    return True, (int(owner) if owner is not None else None)


def own_messages_since(
    person_id: int,
    since: str,
    *,
    limit: int = 30,
    max_chars: int = 300,
    db_path: Path = DB_PATH,
) -> list[dict[str, str]]:
    """What ``person_id`` wrote in chat after ``since`` (ISO 8601), oldest
    first: the newest ``limit`` of their own messages, each as one line of at
    most ``max_chars``. Only their words, never a reply or what someone else
    wrote in a chat they share; a message from before senders were recorded
    counts when the conversation is theirs."""
    if not db_path.exists():
        return []
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT m.created_at, m.content FROM chat_messages m "
            "JOIN sessions s ON s.session_id = m.session_id "
            "WHERE m.role = 'user' AND m.created_at > ? AND (m.sender_person_id = ? "
            "OR (m.sender_person_id IS NULL AND s.caller_person_id = ?)) "
            "ORDER BY m.created_at DESC, m.id DESC LIMIT ?",
            (since, person_id, person_id, limit),
        ).fetchall()
    out: list[dict[str, str]] = []
    for row in reversed(rows):
        text = " ".join(str(row["content"] or "").split())
        if len(text) > max_chars:
            text = text[: max_chars - 1].rstrip() + "…"
        if text:
            out.append({"at": str(row["created_at"]), "text": text})
    return out


def own_conversation_since(
    person_id: int,
    since: str,
    *,
    limit: int = 24,
    max_chars: int = 280,
    db_path: Path = DB_PATH,
) -> list[dict[str, str]]:
    """``person_id``'s own conversations with the Executive after ``since``
    (ISO 8601), oldest first: the newest ``limit`` messages, theirs and the
    Executive's replies, as ``{"at", "role", "text"}`` with ``text`` one line
    of at most ``max_chars``. Only conversations they own, so nobody else's
    words or a reply written to someone else ever reach it. The end-of-day
    digest reads it: what moved today is mostly said in chat."""
    if not db_path.exists():
        return []
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT m.created_at, m.role, m.content FROM chat_messages m "
            "JOIN sessions s ON s.session_id = m.session_id "
            "WHERE s.caller_person_id = ? AND m.created_at > ? "
            "AND m.role IN ('user', 'assistant') "
            "AND (m.role = 'assistant' OR m.sender_person_id IS NULL "
            "OR m.sender_person_id = ?) "
            "ORDER BY m.created_at DESC, m.id DESC LIMIT ?",
            (person_id, since, person_id, limit),
        ).fetchall()
    out: list[dict[str, str]] = []
    for row in reversed(rows):
        text = " ".join(str(row["content"] or "").split())
        if len(text) > max_chars:
            text = text[: max_chars - 1].rstrip() + "…"
        if text:
            out.append({"at": str(row["created_at"]), "role": str(row["role"]), "text": text})
    return out


def mark_mail_private(
    session_id: str,
    owner_person_id: int | None = None,
    db_path: Path = DB_PATH,
    *,
    history_len: int | None = None,
) -> int | None:
    """Make ``session_id`` its owner's alone from now on: a turn in it read or
    drafted in the speaker's own mailbox (Act as me), and its replies quote
    other people's mail. The principal no longer reads it either
    (``api.routes.chat._session_access``). Never undone. Returns the
    conversation's owner as stored.

    A channel adapter writes its session row only after the turn, so a
    missing row is created here, owned by ``owner_person_id`` (the speaker);
    the adapter's ``create_session`` then leaves it as it is. A row without an
    owner gets this one. A row owned by someone else is left exactly as it
    is, unmarked: the caller sees its owner returned and reads nothing.

    ``history_len`` is how many history messages the conversation held before
    this turn (``mail_read_at``); None stores unknown, which carries the
    lockdown for good."""
    now = datetime.now(UTC).isoformat()
    with _get_conn(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT caller_person_id FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        stored = row["caller_person_id"] if row is not None else None
        if stored is not None and stored != owner_person_id:
            return int(stored)
        conn.execute(
            "INSERT INTO sessions (session_id, title, created_at, updated_at, caller_person_id, mail_private, mail_read_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET mail_private = 1, mail_read_at = excluded.mail_read_at, "
            "caller_person_id = COALESCE(caller_person_id, excluded.caller_person_id)",
            (session_id, session_id, now, now, owner_person_id, history_len),
        )
    return owner_person_id


def mail_read_at(session_id: str, db_path: Path = DB_PATH) -> int | None:
    """How many history messages ``session_id`` held before the last turn
    that read its owner's mail (``mark_mail_private``), or None when unknown
    or never read."""
    if not db_path.exists():
        return None
    with _get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT mail_read_at FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
    if row is None or row["mail_read_at"] is None:
        return None
    return int(row["mail_read_at"])


def session_mail_private(session_id: str, db_path: Path = DB_PATH) -> bool:
    """Whether ``session_id`` is its owner's alone (``mark_mail_private``)."""
    if not db_path.exists():
        return False
    import sqlite3

    try:
        with _get_conn(db_path) as conn:
            row = conn.execute(
                "SELECT mail_private FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
    except sqlite3.OperationalError as exc:
        # A DB from before the column (or before the table) never marked one.
        if "no such column" in str(exc) or "no such table" in str(exc):
            return False
        raise
    return bool(row is not None and row["mail_private"])


def rewind_last_turn(
    session_id: str, last_message_id: int, db_path: Path = DB_PATH
) -> list[str] | None:
    """Remove the conversation's last turn: its last reply and the person's
    messages it answered (everything after the reply before it). Returns
    those messages' words, oldest first, for the turn that replaces it.

    ``last_message_id`` must be the id of the conversation's last row and that
    row a reply, so a stale page (another tab already moved the chat on)
    removes nothing. None when it isn't."""
    if not db_path.exists():
        return None
    with _get_conn(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        last = conn.execute(
            "SELECT id, role FROM chat_messages WHERE session_id = ? ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if last is None or last["id"] != last_message_id or last["role"] != "assistant":
            return None
        before = conn.execute(
            "SELECT MAX(id) AS id FROM chat_messages "
            "WHERE session_id = ? AND role = 'assistant' AND id < ?",
            (session_id, last_message_id),
        ).fetchone()
        since = int(before["id"]) if before is not None and before["id"] is not None else 0
        asked = conn.execute(
            "SELECT content FROM chat_messages "
            "WHERE session_id = ? AND role = 'user' AND id > ? AND id < ? ORDER BY id",
            (session_id, since, last_message_id),
        ).fetchall()
        conn.execute(
            "DELETE FROM chat_messages WHERE session_id = ? AND id > ? AND id <= ?",
            (session_id, since, last_message_id),
        )
    return [str(row["content"]) for row in asked]


# Sharing: a chat's owner adds named teammates to it. They read it and write
# in it with the owner, each turn answered as whoever sent it; only the owner
# renames, pins, shares or deletes it. An Act as me conversation is never
# shared (``session_mail_private``; the routes refuse it).


def shared_with(session_id: str, db_path: Path = DB_PATH) -> list[dict[str, Any]]:
    """Who ``session_id`` is shared with: ``person_id`` and ``shared_at``,
    oldest share first."""
    if not db_path.exists():
        return []
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT person_id, created_at FROM session_shares WHERE session_id = ? "
            "ORDER BY created_at, person_id",
            (session_id,),
        ).fetchall()
    return [{"person_id": int(r["person_id"]), "shared_at": r["created_at"]} for r in rows]


def is_shared_with(session_id: str, person_id: int, db_path: Path = DB_PATH) -> bool:
    if not db_path.exists():
        return False
    with _get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM session_shares WHERE session_id = ? AND person_id = ?",
            (session_id, person_id),
        ).fetchone()
    return row is not None


def set_shares(
    session_id: str,
    person_ids: list[int],
    shared_by_person_id: int | None,
    db_path: Path = DB_PATH,
) -> tuple[list[int], list[int]]:
    """Share ``session_id`` with exactly ``person_ids``. Returns the ids added
    and the ids removed; someone already on the list keeps their share time."""
    now = datetime.now(UTC).isoformat()
    wanted = set(person_ids)
    with _get_conn(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        have = {
            int(r["person_id"])
            for r in conn.execute(
                "SELECT person_id FROM session_shares WHERE session_id = ?", (session_id,)
            )
        }
        added = sorted(wanted - have)
        removed = sorted(have - wanted)
        for pid in removed:
            conn.execute(
                "DELETE FROM session_shares WHERE session_id = ? AND person_id = ?",
                (session_id, pid),
            )
        for pid in added:
            conn.execute(
                "INSERT INTO session_shares (session_id, person_id, shared_by_person_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (session_id, pid, shared_by_person_id, now),
            )
    return added, removed


def leave_share(session_id: str, person_id: int, db_path: Path = DB_PATH) -> bool:
    """Take ``person_id`` out of a chat shared with them. False when it wasn't."""
    if not db_path.exists():
        return False
    with _get_conn(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM session_shares WHERE session_id = ? AND person_id = ?",
            (session_id, person_id),
        )
        return cur.rowcount > 0


def last_turn_sender(session_id: str, db_path: Path = DB_PATH) -> int | None:
    """Who wrote the conversation's newest message from a person, or None."""
    if not db_path.exists():
        return None
    with _get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT sender_person_id FROM chat_messages WHERE session_id = ? AND role = 'user' "
            "ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
    if row is None or row["sender_person_id"] is None:
        return None
    return int(row["sender_person_id"])


def list_shared_with_person(person_id: int, db_path: Path = DB_PATH) -> list[dict[str, Any]]:
    """The chats shared with ``person_id``, newest activity first, each with
    its owner (``owner_person_id``) and when it was shared (``shared_at``).
    An Act as me conversation is left out even if it was shared before it
    read mail."""
    if not db_path.exists():
        return []
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT s.session_id, s.title, s.created_at, s.updated_at,
                   s.caller_person_id AS owner_person_id, sh.created_at AS shared_at,
                   (SELECT COUNT(*) FROM chat_messages m
                     WHERE m.session_id = s.session_id) AS message_count
            FROM session_shares sh
            JOIN sessions s ON s.session_id = sh.session_id
            WHERE sh.person_id = ? AND s.mail_private = 0
            ORDER BY s.updated_at DESC
            """,
            (person_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def export_rows(session_id: str, db_path: Path = DB_PATH) -> list[dict[str, Any]]:
    """Every message of ``session_id`` with who wrote it, for an export:
    ``role``, ``content``, ``created_at``, ``sender_person_id``, ``stopped``
    and the reply's ``sources`` (decoded, or None)."""
    if not db_path.exists():
        return []
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT role, content, created_at, sender_person_id, stopped, sources "
            "FROM chat_messages WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        sources = None
        if row["sources"]:
            try:
                sources = json.loads(row["sources"])
            except (ValueError, TypeError):
                sources = None
        out.append({
            "role": row["role"],
            "content": row["content"],
            "created_at": row["created_at"],
            "sender_person_id": row["sender_person_id"],
            "stopped": bool(row["stopped"]),
            "sources": sources if isinstance(sources, dict) else None,
        })
    return out
