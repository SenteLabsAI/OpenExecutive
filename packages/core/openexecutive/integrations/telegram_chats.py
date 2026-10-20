"""Telegram chats the bot has seen, so the People and department pickers can
offer them by name.

Telegram's Bot API has no way to list the people or groups a bot can reach,
so the webhook records each chat an update comes from (``record_from_update``):
one row per chat in the ``telegram_chats`` table of the episodic DB, keyed on
the chat id, with its type, its title (a group) or name (a private chat), its
@username and when it was first and last seen. Private chats become people
picker options and groups become channel picker options
(``integrations.chat_directory``).

Only a webhook whose secret checked out records anything: without
``TELEGRAM_WEBHOOK_SECRET`` anyone could post a made-up update and put a
name of their choosing next to a chat id. A row is a suggestion only; it
grants nothing (the roster still decides who is answered).

Reads never create the table, so an unconfigured DB reads as empty. The table
keeps the ``MAX_ROWS`` most recently seen chats.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

TABLE = "telegram_chats"
DB_PATH = Path(os.environ.get("EPISODIC_DB_PATH", "./episodic_memory.db"))

# Chat types the pickers use: a private chat is a person, a group or
# supergroup is a room. Telegram channels (broadcast) send ``channel_post``
# updates the bot does not handle, so they are never recorded.
PERSON_TYPES = frozenset({"private"})
GROUP_TYPES = frozenset({"group", "supergroup"})
# Kept per kind (people, groups), newest first, so a flood of new groups
# can never push the people out, or the other way round.
MAX_ROWS = 250
_MAX_TEXT = 200

_DDL = f"""
    CREATE TABLE IF NOT EXISTS {TABLE} (
        chat_id TEXT PRIMARY KEY,
        chat_type TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        username TEXT NOT NULL DEFAULT '',
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_telegram_chats_seen
        ON {TABLE}(chat_type, last_seen_at DESC);
"""


class TelegramChat(BaseModel):
    model_config = ConfigDict(frozen=True)

    chat_id: str
    chat_type: str
    title: str
    username: str
    first_seen_at: str
    last_seen_at: str


def _resolve(db_path: Path | None) -> Path:
    return db_path if db_path is not None else DB_PATH


@contextmanager
def _conn(db_path: Path | None) -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(str(_resolve(db_path)))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def initialize_db(db_path: Path | None = None) -> None:
    with _conn(db_path) as conn:
        conn.executescript(_DDL)


def _clip(value: Any) -> str:
    return str(value).strip()[:_MAX_TEXT] if isinstance(value, str) else ""


def record_chat(
    chat: dict[str, Any], *, now: datetime | None = None, db_path: Path | None = None
) -> bool:
    """Upsert one Telegram ``Chat`` object. Returns whether a row was written
    (False for a type the pickers don't use or a chat without an id)."""
    chat_id = chat.get("id")
    chat_type = chat.get("type")
    if not isinstance(chat_id, int) or isinstance(chat_id, bool):
        return False
    if chat_type not in PERSON_TYPES and chat_type not in GROUP_TYPES:
        return False
    if chat_type in PERSON_TYPES:
        title = " ".join(
            part for part in (_clip(chat.get("first_name")), _clip(chat.get("last_name"))) if part
        )
    else:
        title = _clip(chat.get("title"))
    username = _clip(chat.get("username")).lstrip("@")
    stamp = (now or datetime.now(UTC)).isoformat()
    with _conn(db_path) as conn:
        conn.executescript(_DDL)
        conn.execute(
            f"""
            INSERT INTO {TABLE} (chat_id, chat_type, title, username, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                chat_type = excluded.chat_type,
                title = CASE WHEN excluded.title != '' THEN excluded.title ELSE {TABLE}.title END,
                username = excluded.username,
                last_seen_at = excluded.last_seen_at
            """,
            (str(chat_id), chat_type, title, username, stamp, stamp),
        )
        kind = PERSON_TYPES if chat_type in PERSON_TYPES else GROUP_TYPES
        marks = ",".join("?" for _ in kind)
        conn.execute(
            f"""
            DELETE FROM {TABLE} WHERE chat_type IN ({marks}) AND chat_id NOT IN (
                SELECT chat_id FROM {TABLE} WHERE chat_type IN ({marks})
                ORDER BY last_seen_at DESC LIMIT ?
            )
            """,
            (*sorted(kind), *sorted(kind), MAX_ROWS),
        )
    return True


def forget_chat(chat_id: int | str, *, db_path: Path | None = None) -> None:
    """Drop a chat (a group Telegram moved to a new supergroup id)."""
    try:
        with _conn(db_path) as conn:
            conn.execute(f"DELETE FROM {TABLE} WHERE chat_id = ?", (str(chat_id),))
    except sqlite3.OperationalError:
        pass  # no table yet: nothing to forget


def record_from_update(update: dict[str, Any], *, db_path: Path | None = None) -> None:
    """Record the chat a webhook update came from. Best-effort: a failure is
    logged and never stops the update being handled."""
    try:
        message = update.get("message")
        if isinstance(message, dict):
            chat = message.get("chat")
            if isinstance(chat, dict):
                # A group upgraded to a supergroup: the old id stops working.
                migrated_to = message.get("migrate_to_chat_id")
                if isinstance(migrated_to, int) and isinstance(chat.get("id"), int):
                    forget_chat(chat["id"], db_path=db_path)
                    return
                record_chat(chat, db_path=db_path)
            return
        # The bot was added to (or removed from) a group.
        member = update.get("my_chat_member")
        if isinstance(member, dict) and isinstance(member.get("chat"), dict):
            status = (member.get("new_chat_member") or {}).get("status")
            if status in {"left", "kicked"}:
                forget_chat(member["chat"].get("id", ""), db_path=db_path)
            else:
                record_chat(member["chat"], db_path=db_path)
    except Exception:
        logger.exception("Telegram: could not record the chat an update came from")


def list_chats(kind: str, *, db_path: Path | None = None) -> list[TelegramChat]:
    """Chats seen, most recent first. ``kind`` is ``"people"`` (private
    chats) or ``"groups"``."""
    types = PERSON_TYPES if kind == "people" else GROUP_TYPES
    marks = ",".join("?" for _ in types)
    if not _resolve(db_path).exists():
        return []
    try:
        with _conn(db_path) as conn:
            rows = conn.execute(
                f"SELECT * FROM {TABLE} WHERE chat_type IN ({marks}) "
                "ORDER BY last_seen_at DESC LIMIT ?",
                (*sorted(types), MAX_ROWS),
            ).fetchall()
    except sqlite3.OperationalError:
        return []  # no DB or table yet
    return [TelegramChat(**dict(r)) for r in rows]
