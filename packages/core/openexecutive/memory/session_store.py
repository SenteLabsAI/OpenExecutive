from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openexecutive.memory.episodic import DB_PATH, _get_conn


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
    with _get_conn(db_path) as conn:
        conn.execute("UPDATE sessions SET title = ? WHERE session_id = ?", (title, session_id))


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
) -> int:
    """Persist one chat message and return its row id. ``action_chips`` is a
    JSON-encoded list of the assistant turn's action-chip dicts (or None), so
    reopening a saved session restores the ✓ tool-action pills instead of bare
    prose. ``stopped`` marks an assistant message the user halted mid-stream,
    so the reply is not read back as a complete one. ``sender_person_id`` is
    the rostered Person who actually wrote a user message — pass the resolved
    sender, never the session owner's principal fallback, so per-person
    learning never reads an outsider's words as someone on the roster."""
    text = content if isinstance(content, str) else str(content)
    now = datetime.now(UTC).isoformat()
    with _get_conn(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO chat_messages "
            "(session_id, role, content, created_at, action_chips, stopped, sender_person_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, role, text, now, action_chips, 1 if stopped else 0, sender_person_id),
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


def load_messages(session_id: str, db_path: Path = DB_PATH) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT id, role, content, action_chips, stopped, feedback FROM chat_messages "
            "WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        msg: dict[str, Any] = {"role": row["role"], "content": row["content"]}
        # The row id lets the UI attach 👍/👎 to a reloaded reply. Only on
        # assistant rows, alongside `feedback`, for the same reason as
        # `actions` below: untouched user rows keep their exact shape.
        if row["role"] == "assistant":
            msg["id"] = row["id"]
            if row["feedback"]:
                msg["feedback"] = row["feedback"]
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
        out.append(msg)
    return out


def list_sessions(
    caller_person_id: int,
    db_path: Path = DB_PATH,
) -> list[dict[str, Any]]:
    """List sessions owned by `caller_person_id`, newest first.

    Legacy rows with caller_person_id IS NULL (created before this column
    existed) are excluded — the comparison `NULL = ?` never matches in
    SQLite. Only the principal can still open them by id (see
    `api.routes.chat._session_access`).
    """
    if not db_path.exists():
        return []
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT s.session_id, s.title, s.created_at, s.updated_at,
                   COUNT(m.id) AS message_count
            FROM sessions s
            LEFT JOIN chat_messages m ON m.session_id = s.session_id
            WHERE s.caller_person_id = ?
            GROUP BY s.session_id
            ORDER BY s.updated_at DESC
            """,
            (caller_person_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def delete_session(session_id: str, db_path: Path = DB_PATH) -> bool:
    if not db_path.exists():
        return False
    with _get_conn(db_path) as conn:
        conn.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
        cur = conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        return cur.rowcount > 0


def get_session_metadata(session_id: str, db_path: Path = DB_PATH) -> dict[str, Any] | None:
    if not db_path.exists():
        return None
    with _get_conn(db_path) as conn:
        row = conn.execute(
            """
            SELECT s.session_id, s.title, s.created_at, s.updated_at,
                   COUNT(m.id) AS message_count
            FROM sessions s
            LEFT JOIN chat_messages m ON m.session_id = s.session_id
            WHERE s.session_id = ?
            GROUP BY s.session_id
            """,
            (session_id,),
        ).fetchone()
    return dict(row) if row else None


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
