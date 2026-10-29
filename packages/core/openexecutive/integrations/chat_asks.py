"""Messages in a team chat that ask something of the principal, until they
answer.

A Slack channel the bot is in carries messages that mention the principal
by name: ``@Olivia, can you sign off on this?``. The bot never answers those, since they are not to it,
and nothing used to keep them. Now the adapter notes each one
(:func:`note_ask`), and notes every message the principal writes in a place
(:func:`note_spoke`): one in the same thread, or a new message in the same
place, answers the asks there before it.

Right now (``briefing.header_context``) shows an ask that has gone
unanswered ``UNANSWERED_AFTER`` and is younger than ``KEEP_FOR``, with a
link to the message, so it sits next to the emails that wait on them. Once
answered it no longer counts, and the next rewrite of Right now drops it.

Only the principal's asks are kept: Right now's mail and calendar points
are theirs alone too. What a message says is kept as written, cut short, and
shown only to them. Never raises.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

UNANSWERED_AFTER = timedelta(minutes=30)
KEEP_FOR = timedelta(hours=24)
TEXT_MAX = 300
# At most this many asks reach Right now.
SHOWN_MAX = 5

_TABLE = """
    CREATE TABLE IF NOT EXISTS chat_asks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel TEXT NOT NULL,
        place TEXT NOT NULL,
        place_name TEXT NOT NULL DEFAULT '',
        thread TEXT NOT NULL,
        message_ref TEXT NOT NULL,
        person_id INTEGER NOT NULL,
        from_name TEXT NOT NULL DEFAULT '',
        text TEXT NOT NULL DEFAULT '',
        link TEXT NOT NULL DEFAULT '',
        asked_at TEXT NOT NULL,
        answered_at TEXT,
        UNIQUE (channel, message_ref, person_id)
    )
"""

# Slack notes asks here (``slack_bot.note_chat_ask``); the other names are
# for an adapter that reads every message of a place, which Google Chat's
# webhook app (mentions of it only) does not.
APP_NAMES = {"slack": "Slack", "google_chat": "Google Chat"}


@dataclass(frozen=True)
class ChatAsk:
    id: int
    channel: str
    place_name: str
    from_name: str
    text: str
    link: str
    asked_at: str


def _connect(db_path: Path | None) -> sqlite3.Connection:
    from openexecutive.memory import episodic

    conn = sqlite3.connect(str(db_path or episodic.DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute(_TABLE)
    return conn


def _principal_id() -> int | None:
    from openexecutive.people.store import find_principal_person

    principal = find_principal_person()
    return principal.id if principal is not None else None


def note_ask(
    channel: str, *, place: str, thread: str, message_ref: str, person_id: int, from_name: str, text: str,
    link: str = "", place_name: str = "", at: datetime | None = None, db_path: Path | None = None,
) -> bool:
    """Keep a message in ``place`` that asks something of ``person_id``, when
    that is the principal. ``thread`` is the thread it belongs to (its own
    ref when it starts one). True when it was new."""
    try:
        if person_id != _principal_id():
            return False
        when = (at or datetime.now(UTC)).isoformat()
        conn = _connect(db_path)
        try:
            cur = conn.execute(
                "INSERT OR IGNORE INTO chat_asks (channel, place, place_name, thread, message_ref, person_id,"
                " from_name, text, link, asked_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (channel, place, place_name[:80], thread, message_ref, person_id, from_name[:80],
                 " ".join(text.split())[:TEXT_MAX], link[:500], when),
            )
            conn.commit()
            return cur.rowcount == 1
        finally:
            conn.close()
    except Exception:
        logger.warning("chat_asks: couldn't keep an ask", exc_info=True)
        return False


def note_spoke(
    channel: str, *, place: str, thread: str, top_level: bool, person_id: int,
    at: datetime | None = None, db_path: Path | None = None,
) -> int:
    """``person_id`` wrote in ``place``: in ``thread``, or as a new message
    there (``top_level``). Marks the asks it answers; returns how many."""
    try:
        when = (at or datetime.now(UTC)).isoformat()
        conn = _connect(db_path)
        try:
            if top_level:
                cur = conn.execute(
                    "UPDATE chat_asks SET answered_at = ? WHERE channel = ? AND place = ? AND person_id = ?"
                    " AND answered_at IS NULL AND asked_at <= ?",
                    (when, channel, place, person_id, when),
                )
            else:
                cur = conn.execute(
                    "UPDATE chat_asks SET answered_at = ? WHERE channel = ? AND place = ? AND thread = ?"
                    " AND person_id = ? AND answered_at IS NULL",
                    (when, channel, place, thread, person_id),
                )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()
    except Exception:
        logger.warning("chat_asks: couldn't note a reply", exc_info=True)
        return 0


def waiting(person_id: int, now: datetime | None = None, *, db_path: Path | None = None) -> list[ChatAsk]:
    """``person_id``'s asks unanswered for ``UNANSWERED_AFTER`` and younger
    than ``KEEP_FOR``, oldest first, at most ``SHOWN_MAX``."""
    now = now or datetime.now(UTC)
    try:
        conn = _connect(db_path)
        try:
            rows = conn.execute(
                "SELECT * FROM chat_asks WHERE person_id = ? AND answered_at IS NULL"
                " AND asked_at >= ? AND asked_at <= ? ORDER BY asked_at LIMIT ?",
                (person_id, (now - KEEP_FOR).isoformat(), (now - UNANSWERED_AFTER).isoformat(), SHOWN_MAX),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        logger.warning("chat_asks: couldn't read the asks", exc_info=True)
        return []
    return [
        ChatAsk(id=int(r["id"]), channel=r["channel"], place_name=r["place_name"], from_name=r["from_name"],
                text=r["text"], link=r["link"], asked_at=r["asked_at"])
        for r in rows
    ]


def where(ask: ChatAsk) -> str:
    """"Slack #launch", "Google Chat" and so on."""
    app = APP_NAMES.get(ask.channel, ask.channel)
    return f"{app} {ask.place_name}".strip()


__all__ = ["ChatAsk", "KEEP_FOR", "UNANSWERED_AFTER", "note_ask", "note_spoke", "waiting", "where"]
