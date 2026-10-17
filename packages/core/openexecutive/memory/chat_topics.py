"""Topics that group one person's chats on their Chats page.

A chat's topic is chosen by the model when the chat is named
(``utils.chat_topics``), and the owner can move a chat, rename, pin, merge or
ungroup a topic. What the owner does sticks: a chat they filed
(``topic_source = 'owner'``) is never retagged, and a topic they touched
(``kept``) is never merged away by the tidy.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openexecutive.memory.episodic import DB_PATH, _get_conn

# Longest topic name kept; the model is asked for two to four words.
MAX_TOPIC_CHARS = 40
# Topics shown to the model when it picks one: the ones used most recently.
PROMPT_TOPICS = 60


def clean_topic_name(raw: str | None) -> str | None:
    """One line, no quotes or end punctuation, capped. None when nothing is
    left, or the model said no topic fits."""
    if not raw:
        return None
    name = re.sub(r"\s+", " ", raw).strip().strip("\"'").rstrip(".!?,:;").strip()
    name = "".join(c for c in name if c.isprintable())[:MAX_TOPIC_CHARS].strip()
    if not name or name.lower() in {"none", "-", "n/a", "other"}:
        return None
    return name


def _now() -> str:
    return datetime.now(UTC).isoformat()


def topic_names(owner: int, db_path: Path = DB_PATH) -> list[str]:
    """The owner's topic names, most recently used first, for the prompt."""
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT t.name FROM chat_topics t
            LEFT JOIN sessions s ON s.topic_id = t.id
            WHERE t.owner_person_id = ?
            GROUP BY t.id
            ORDER BY MAX(COALESCE(s.updated_at, t.created_at)) DESC
            LIMIT ?
            """,
            (owner, PROMPT_TOPICS),
        ).fetchall()
    return [str(r["name"]) for r in rows]


def _topic_id(conn: Any, owner: int, name: str, kept: bool) -> tuple[int, bool]:
    # OR IGNORE: the backfill and a new chat's naming may make the same
    # topic at once; the second just finds the first's.
    made = conn.execute(
        "INSERT OR IGNORE INTO chat_topics (owner_person_id, name, kept, created_at) "
        "VALUES (?, ?, ?, ?)",
        (owner, name, 1 if kept else 0, _now()),
    ).rowcount > 0
    row = conn.execute(
        "SELECT id FROM chat_topics WHERE owner_person_id = ? AND name = ?",
        (owner, name),
    ).fetchone()
    if kept and not made:
        conn.execute("UPDATE chat_topics SET kept = 1 WHERE id = ?", (row["id"],))
    return int(row["id"]), made


def set_auto_topic(
    session_id: str, owner: int, name: str | None, db_path: Path = DB_PATH
) -> tuple[int | None, bool]:
    """File a chat under the model's topic (made if new; None = none fits).
    A chat the owner filed is left as it is. Returns the topic id and whether
    the topic was just made; (None, False) when nothing changed or none fits.
    One transaction, so a list running meanwhile never drops the new topic
    as empty."""
    with _get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT topic_source FROM sessions WHERE session_id = ? AND caller_person_id = ?",
            (session_id, owner),
        ).fetchone()
        if row is None or row["topic_source"] == "owner":
            return None, False
        topic_id, made = _topic_id(conn, owner, name, False) if name else (None, False)
        conn.execute(
            "UPDATE sessions SET topic_id = ?, topic_source = 'auto' WHERE session_id = ?",
            (topic_id, session_id),
        )
        return topic_id, made


def file_session(
    session_id: str,
    owner: int,
    *,
    topic_id: int | None = None,
    name: str | None = None,
    db_path: Path = DB_PATH,
) -> bool:
    """The owner puts their chat under one of their topics (by id, or by a
    name, made if new) or under none. Kept so. False when the topic id isn't
    theirs."""
    with _get_conn(db_path) as conn:
        if name:
            topic_id, _ = _topic_id(conn, owner, name, True)
        elif topic_id is not None:
            if conn.execute(
                "SELECT 1 FROM chat_topics WHERE id = ? AND owner_person_id = ?",
                (topic_id, owner),
            ).fetchone() is None:
                return False
            conn.execute("UPDATE chat_topics SET kept = 1 WHERE id = ?", (topic_id,))
        conn.execute(
            "UPDATE sessions SET topic_id = ?, topic_source = 'owner' WHERE session_id = ?",
            (topic_id, session_id),
        )
        return True


def topic_id_by_name(owner: int, name: str, db_path: Path = DB_PATH) -> int | None:
    with _get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM chat_topics WHERE owner_person_id = ? AND name = ?",
            (owner, name),
        ).fetchone()
    return int(row["id"]) if row is not None else None


def owned_topic(owner: int, topic_id: int, db_path: Path = DB_PATH) -> dict[str, Any] | None:
    with _get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT id, name, kept, pinned_at FROM chat_topics WHERE id = ? AND owner_person_id = ?",
            (topic_id, owner),
        ).fetchone()
    return dict(row) if row is not None else None


def list_topics(owner: int, db_path: Path = DB_PATH) -> list[dict[str, Any]]:
    """The owner's topics that hold a chat: pinned first, then by their
    newest chat. Topics left empty (their chats deleted or moved) go."""
    with _get_conn(db_path) as conn:
        conn.execute(
            "DELETE FROM chat_topics WHERE owner_person_id = ? AND id NOT IN "
            "(SELECT topic_id FROM sessions WHERE topic_id IS NOT NULL)",
            (owner,),
        )
        rows = conn.execute(
            """
            SELECT t.id, t.name, t.kept, t.pinned_at, COUNT(s.session_id) AS chat_count,
                   MAX(s.updated_at) AS updated_at
            FROM chat_topics t JOIN sessions s ON s.topic_id = t.id
            WHERE t.owner_person_id = ? AND s.caller_person_id = ?
            GROUP BY t.id
            ORDER BY t.pinned_at IS NULL, t.pinned_at DESC, updated_at DESC
            """,
            (owner, owner),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        item: dict[str, Any] = {
            "id": int(r["id"]),
            "name": str(r["name"]),
            "kept": bool(r["kept"]),
            "chat_count": int(r["chat_count"]),
        }
        if r["pinned_at"]:
            item["pinned_at"] = r["pinned_at"]
        out.append(item)
    return out


def rename_topic(owner: int, topic_id: int, name: str, db_path: Path = DB_PATH) -> int | None:
    """Rename the owner's topic. A name another of their topics has merges
    this one into it. Returns the topic the chats are under now, None when
    there is no such topic."""
    if owned_topic(owner, topic_id, db_path) is None:
        return None
    with _get_conn(db_path) as conn:
        other = conn.execute(
            "SELECT id FROM chat_topics WHERE owner_person_id = ? AND name = ? AND id != ?",
            (owner, name, topic_id),
        ).fetchone()
    if other is not None:
        merge_topics(owner, topic_id, int(other["id"]), db_path)
        with _get_conn(db_path) as conn:
            conn.execute("UPDATE chat_topics SET kept = 1 WHERE id = ?", (other["id"],))
        return int(other["id"])
    with _get_conn(db_path) as conn:
        conn.execute(
            "UPDATE chat_topics SET name = ?, kept = 1 WHERE id = ?", (name, topic_id)
        )
    return topic_id


def keep_topic(owner: int, topic_id: int, db_path: Path = DB_PATH) -> None:
    with _get_conn(db_path) as conn:
        conn.execute(
            "UPDATE chat_topics SET kept = 1 WHERE id = ? AND owner_person_id = ?",
            (topic_id, owner),
        )


def pin_topic(owner: int, topic_id: int, pinned: bool, db_path: Path = DB_PATH) -> bool:
    with _get_conn(db_path) as conn:
        cur = conn.execute(
            "UPDATE chat_topics SET pinned_at = CASE WHEN ? THEN COALESCE(pinned_at, ?) END, "
            "kept = CASE WHEN ? THEN 1 ELSE kept END "
            "WHERE id = ? AND owner_person_id = ?",
            (1 if pinned else 0, _now(), 1 if pinned else 0, topic_id, owner),
        )
        return cur.rowcount > 0


def merge_topics(owner: int, source_id: int, into_id: int, db_path: Path = DB_PATH) -> bool:
    """Move every chat under ``source_id`` to ``into_id`` and drop the
    source. Both must be the owner's. A chat keeps who filed it."""
    if source_id == into_id:
        return owned_topic(owner, into_id, db_path) is not None
    if owned_topic(owner, source_id, db_path) is None or owned_topic(owner, into_id, db_path) is None:
        return False
    with _get_conn(db_path) as conn:
        conn.execute("UPDATE sessions SET topic_id = ? WHERE topic_id = ?", (into_id, source_id))
        conn.execute("DELETE FROM chat_topics WHERE id = ?", (source_id,))
    return True


def ungroup_topic(owner: int, topic_id: int, db_path: Path = DB_PATH) -> bool:
    """Drop the owner's topic; its chats go under no topic and stay there."""
    if owned_topic(owner, topic_id, db_path) is None:
        return False
    with _get_conn(db_path) as conn:
        conn.execute(
            "UPDATE sessions SET topic_id = NULL, topic_source = 'owner' WHERE topic_id = ?",
            (topic_id,),
        )
        conn.execute("DELETE FROM chat_topics WHERE id = ?", (topic_id,))
    return True


def owner_only(session_id: str, owner: int, db_path: Path = DB_PATH) -> bool:
    """Whether only the owner has written in this chat and it is shared with
    no one, so a prompt may carry the owner's topic names and its title may
    show them: anyone else could steer the call into echoing them."""
    with _get_conn(db_path) as conn:
        if conn.execute(
            "SELECT 1 FROM session_shares WHERE session_id = ? LIMIT 1", (session_id,)
        ).fetchone() is not None:
            return False
        other = conn.execute(
            "SELECT 1 FROM chat_messages WHERE session_id = ? AND role = 'user' "
            "AND sender_person_id IS NOT NULL AND sender_person_id != ? LIMIT 1",
            (session_id, owner),
        ).fetchone()
    return other is None


def has_untagged(owner: int, db_path: Path = DB_PATH) -> bool:
    """Whether any of the owner's chats with a message waits for a topic."""
    with _get_conn(db_path) as conn:
        row = conn.execute(
            """
            SELECT 1 FROM sessions s
            WHERE s.caller_person_id = ? AND s.topic_source IS NULL
              AND EXISTS (SELECT 1 FROM chat_messages m WHERE m.session_id = s.session_id)
            LIMIT 1
            """,
            (owner,),
        ).fetchone()
    return row is not None


def untagged_sessions(owner: int, db_path: Path = DB_PATH) -> list[dict[str, str]]:
    """The owner's chats no one has looked at for a topic yet, newest first,
    leaving out ones with no message (a chat-app /new not used yet)."""
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT s.session_id, s.title FROM sessions s
            WHERE s.caller_person_id = ? AND s.topic_source IS NULL
              AND EXISTS (SELECT 1 FROM chat_messages m WHERE m.session_id = s.session_id)
            ORDER BY s.updated_at DESC
            """,
            (owner,),
        ).fetchall()
    return [{"session_id": str(r["session_id"]), "title": str(r["title"])} for r in rows]
