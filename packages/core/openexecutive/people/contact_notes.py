"""What the Executive remembers about each of the principal's contacts and
team members (everyone but the principal): a
short note on what they deal with each other about, and when they last
emailed, heard from and met each other.

``people.contact_keeper`` keeps it, on the same pass that adds contacts:

- **Dates and counts** come from mail headers and calendar dates, in code.
- **The note** (two or three sentences, and a few topics) is written by one
  small model call from the principal's **own** sent mail to that contact
  (only what they wrote, without the quoted mail below it) and the titles of
  meetings they organized. Nobody else's words go in, so the note is the
  principal's own account, like their sent mail in ``read_my_email``. It is
  rewritten only when there is new sent mail or a new meeting, at most
  ``NOTES_PER_RUN`` a pass and ``NOTES_PER_DAY`` a day.

It is read by the Executive on the principal's own turns (``list_people``,
and ``ask_about_person`` for a contact) and shown on the person's page to the
principal. The notes are the principal's alone, a teammate's included: they
come from the principal's own mailbox, so the route answers only the
principal and no other turn sees them. A teammate's own mail to the
Executive (``teammate_mail``) and their peer memory are not read here.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

NOTES_PER_RUN = 5
NOTES_PER_DAY = 20
NOTE_MAX = 400
TOPIC_MAX = 40
TOPICS_MAX = 6
# What the model reads: the newest sent messages, each cut to this length.
MESSAGES_TO_READ = 8
_MESSAGE_CHARS = 700
_MAX_TOKENS = 300

_SCHEMA = """
CREATE TABLE IF NOT EXISTS contact_notes (
    person_id INTEGER PRIMARY KEY,
    note TEXT NOT NULL DEFAULT '',
    topics TEXT NOT NULL DEFAULT '[]',
    last_emailed_at TEXT,
    last_heard_at TEXT,
    last_met_at TEXT,
    next_meeting_at TEXT,
    next_meeting_title TEXT NOT NULL DEFAULT '',
    emailed_count INTEGER NOT NULL DEFAULT 0,
    met_count INTEGER NOT NULL DEFAULT 0,
    activity_key TEXT NOT NULL DEFAULT '',
    note_written_at TEXT,
    updated_at TEXT NOT NULL
);
"""

_BANNED = re.compile(r"https?:|www\.|\b(ignore|instruction|prompt|system|assistant)\b", re.IGNORECASE)


class ContactNote(BaseModel):
    person_id: int
    note: str = ""
    topics: list[str] = Field(default_factory=list)
    last_emailed_at: str | None = None
    last_heard_at: str | None = None
    last_met_at: str | None = None
    next_meeting_at: str | None = None
    next_meeting_title: str = ""
    # In the last 60 days (emails) and 30 days (meetings), as the pass reads them.
    emailed_count: int = 0
    met_count: int = 0
    note_written_at: str | None = None
    updated_at: str = ""


def _db(db_path: Path | None) -> Path:
    if db_path is not None:
        return db_path
    from openexecutive.people import store as people_store

    return people_store.DB_PATH


@contextmanager
def _conn(db_path: Path | None = None) -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(str(_db(db_path)))
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _row(row: sqlite3.Row) -> ContactNote:
    try:
        topics = [str(t) for t in json.loads(row["topics"] or "[]") if isinstance(t, str)]
    except ValueError:
        topics = []
    return ContactNote(
        person_id=row["person_id"], note=row["note"], topics=topics,
        last_emailed_at=row["last_emailed_at"], last_heard_at=row["last_heard_at"],
        last_met_at=row["last_met_at"], next_meeting_at=row["next_meeting_at"],
        next_meeting_title=row["next_meeting_title"], emailed_count=row["emailed_count"],
        met_count=row["met_count"], note_written_at=row["note_written_at"], updated_at=row["updated_at"],
    )


def get(person_id: int, db_path: Path | None = None) -> ContactNote | None:
    with _conn(db_path) as conn:
        row = conn.execute("SELECT * FROM contact_notes WHERE person_id = ?", (person_id,)).fetchone()
    return _row(row) if row is not None else None


def get_many(person_ids: Iterable[int], db_path: Path | None = None) -> dict[int, ContactNote]:
    """Notes by person id. Never raises: an unreadable table reads as none."""
    ids = sorted({int(i) for i in person_ids})
    if not ids:
        return {}
    try:
        with _conn(db_path) as conn:
            rows = conn.execute(
                f"SELECT * FROM contact_notes WHERE person_id IN ({','.join('?' * len(ids))})", ids,
            ).fetchall()
    except Exception:
        logger.warning("contact_notes: couldn't read the notes", exc_info=True)
        return {}
    return {row["person_id"]: _row(row) for row in rows}


def activity_key(person_id: int, db_path: Path | None = None) -> str:
    with _conn(db_path) as conn:
        row = conn.execute("SELECT activity_key FROM contact_notes WHERE person_id = ?", (person_id,)).fetchone()
    return row["activity_key"] if row is not None else ""


def put_activity(
    person_id: int, *, last_emailed_at: str | None, last_met_at: str | None, next_meeting_at: str | None,
    next_meeting_title: str, emailed_count: int, met_count: int, last_heard_at: str | None = None,
    db_path: Path | None = None,
) -> None:
    """Dates and counts, kept as they are when the pass doesn't know
    (``last_heard_at`` None leaves the stored one)."""
    now = datetime.now(UTC).isoformat()
    with _conn(db_path) as conn:
        conn.execute(
            "INSERT INTO contact_notes (person_id, last_emailed_at, last_heard_at, last_met_at, next_meeting_at, "
            "next_meeting_title, emailed_count, met_count, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(person_id) DO UPDATE SET "
            "last_emailed_at = COALESCE(excluded.last_emailed_at, contact_notes.last_emailed_at), "
            "last_heard_at = COALESCE(excluded.last_heard_at, contact_notes.last_heard_at), "
            "last_met_at = COALESCE(excluded.last_met_at, contact_notes.last_met_at), "
            "next_meeting_at = excluded.next_meeting_at, next_meeting_title = excluded.next_meeting_title, "
            "emailed_count = excluded.emailed_count, met_count = excluded.met_count, "
            "updated_at = excluded.updated_at",
            (person_id, last_emailed_at, last_heard_at, last_met_at, next_meeting_at,
             next_meeting_title[:TOPIC_MAX * 2], emailed_count, met_count, now),
        )


def put_note(person_id: int, note: str, topics: list[str], key: str, db_path: Path | None = None) -> None:
    now = datetime.now(UTC).isoformat()
    with _conn(db_path) as conn:
        conn.execute(
            "INSERT INTO contact_notes (person_id, note, topics, activity_key, note_written_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(person_id) DO UPDATE SET note = excluded.note, "
            "topics = excluded.topics, activity_key = excluded.activity_key, "
            "note_written_at = excluded.note_written_at, updated_at = excluded.updated_at",
            (person_id, note, json.dumps(topics), key, now, now),
        )


def set_key(person_id: int, key: str, db_path: Path | None = None) -> None:
    """Mark ``key`` as read without a new note (nothing to write it from)."""
    with _conn(db_path) as conn:
        conn.execute("UPDATE contact_notes SET activity_key = ? WHERE person_id = ?", (key, person_id))


def forget(person_id: int, db_path: Path | None = None) -> None:
    """Drop what is known about ``person_id`` (they were removed)."""
    with _conn(db_path) as conn:
        conn.execute("DELETE FROM contact_notes WHERE person_id = ?", (person_id,))


def notes_written_since(moment: datetime, db_path: Path | None = None) -> int:
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM contact_notes WHERE note_written_at >= ?", (moment.isoformat(),),
        ).fetchone()
    return int(row["n"]) if row is not None else 0


def key_for(message_ids: Iterable[str], meeting_ids: Iterable[str]) -> str:
    """What the note was written from: it is rewritten when this changes."""
    raw = "|".join(sorted(message_ids)) + "#" + "|".join(sorted(meeting_ids))
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


# --------------------------------------------------------------------------- #
# For the Executive
# --------------------------------------------------------------------------- #


def _day(iso: str | None) -> str | None:
    return iso[:10] if iso else None


def for_model(note: ContactNote | None) -> dict[str, Any]:
    """What ``list_people`` adds for a contact: only what is known."""
    if note is None:
        return {}
    out: dict[str, Any] = {}
    if note.note:
        out["what_you_deal_with"] = note.note
    if note.topics:
        out["topics"] = note.topics
    activity = {
        "last_emailed": _day(note.last_emailed_at),
        "last_heard_from": _day(note.last_heard_at),
        "last_met": _day(note.last_met_at),
        "next_meeting": _day(note.next_meeting_at),
        "next_meeting_title": note.next_meeting_title or None,
        "emails_last_60_days": note.emailed_count or None,
        "meetings_last_30_days": note.met_count or None,
    }
    activity = {k: v for k, v in activity.items() if v}
    if activity:
        out["recent_activity"] = activity
    return out


# --------------------------------------------------------------------------- #
# Writing a note
# --------------------------------------------------------------------------- #


_NOTE_TOOL: dict[str, Any] = {
    "name": "record_contact_note",
    "description": "Record what the owner deals with this contact about.",
    "input_schema": {
        "type": "object",
        "properties": {
            "note": {
                "type": "string",
                "description": (
                    "Two or three plain sentences, at most 400 characters: what the owner and this "
                    "contact work on together, what the contact handles or knows about, and anything "
                    "still open. Written to the owner as \"you\". Empty when the mail says nothing useful."
                ),
            },
            "topics": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Up to 6 short topics (1 to 4 words each) this contact is useful for.",
            },
        },
        "required": ["note", "topics"],
    },
}

NOTE_PROMPT = (
    "You keep a short private note for a busy executive about one of their contacts. You read "
    "emails the executive wrote to that contact and the titles of meetings the executive set up "
    "with them. Say what they work on together, what the contact handles or knows about, and what "
    "is still open, so the executive could later think of asking this contact. Use only what the "
    "mail says; never guess. No links, no email addresses, no phone numbers."
)


def clean_note(raw: str) -> str:
    text = " ".join((raw or "").split()).strip()
    if not text or _BANNED.search(text) or "@" in text or "<" in text:
        return ""
    return text[:NOTE_MAX].rstrip()


def clean_topics(raw: Any) -> list[str]:
    out: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        topic = " ".join(str(item).split()).strip(" ,.;")
        if (
            not topic or len(topic) > TOPIC_MAX or len(topic.split()) > 4 or _BANNED.search(topic)
            or "@" in topic or "<" in topic or topic.casefold() in {t.casefold() for t in out}
        ):
            continue
        out.append(topic)
        if len(out) >= TOPICS_MAX:
            break
    return out


async def write_note(
    name: str, messages: list[tuple[str, str, str]], meetings: list[tuple[str, str]], *, model: str | None = None,
) -> tuple[str, list[str]] | None:
    """A note from ``messages`` (date, subject, what the principal wrote) and
    ``meetings`` (date, title), or None when the model call fails. Never
    raises."""
    from openexecutive.audit.usage import log_model_usage
    from openexecutive.config import get_settings
    from openexecutive.providers import get_provider
    from openexecutive.utils.prompt_blocks import no_tags

    parts = [f"Contact: {no_tags(name)}"]
    for when, subject, text in messages[:MESSAGES_TO_READ]:
        parts.append(
            f"<sent_email date=\"{when[:10]}\">\nSubject: {no_tags(subject)[:150]}\n"
            f"{no_tags(text)[:_MESSAGE_CHARS]}\n</sent_email>"
        )
    for when, title in meetings[:MESSAGES_TO_READ]:
        parts.append(f"<meeting date=\"{when[:10]}\">{no_tags(title)[:150]}</meeting>")
    chosen = model or get_settings().routing_model
    try:
        response = await get_provider(chosen).messages_create(
            model=chosen,
            max_tokens=_MAX_TOKENS,
            system=NOTE_PROMPT,
            tools=[_NOTE_TOOL],
            tool_choice={"type": "tool", "name": _NOTE_TOOL["name"]},
            messages=[{"role": "user", "content": "\n\n".join(parts)}],
        )
        log_model_usage(response, model=chosen, actor="contact_keeper")
    except Exception:
        logger.warning("contact_notes: writing a note failed", exc_info=True)
        return None
    for block in response.content:
        if getattr(block, "type", "") == "tool_use" and isinstance(getattr(block, "input", None), dict):
            return clean_note(str(block.input.get("note") or "")), clean_topics(block.input.get("topics"))
    return None
