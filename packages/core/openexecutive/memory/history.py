"""Always in the loop: dated notes of what happened, that the Executive can
recall later.

A note is an *event*, never an instruction: "Oct 3: told Dana Lee the Q4
price list comes Friday", with the exact words it came from (``quote``), when
it happened, where (``channel``, ``conversation_key``), who it was with
(``counterpart``) and how far it is trusted (``trust``). Code sets every one
of those but the summary; a model only writes that sentence, and the quote it
rests on is checked word for word before anything is stored
(``memory.history_notes``).

**What writes notes.** For now only one thing: a reply the Executive drafted
in someone's own mailbox (Act as me), after that person approved and sent it
(``delegation.reply_send``). The note-taker reads only the words they sent,
so nothing another person wrote can become a note this way. That is their
own word, so ``trust`` is ``high``.

**Who reads them.** A note's ``visibility`` is ``private``: its own person
alone, never the principal, and only in a conversation nobody else can read
(``orchestrator.history_tools``). Each person sees, corrects, pins and
forgets their own notes in Memories → History (``/memories/history``).

**Switches.** Each person turns it on for themselves (``reply_notes``,
absent means off). The owner sets how long notes last for the company
(``retention_days``: 30, 90, 365, or until forgotten; 90 when unset), and a
person may shorten it for their own notes, never lengthen it. A pinned note
never expires. "Don't remember this" forgets a conversation's notes and
keeps it from being noted again (``history_excluded``).

Every function here is per company (the episodic DB), never raises on a
read, and logs codes, never note text.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from openexecutive.memory.history_schema import (
    COMPANY_TABLE,
    EXCLUDED_TABLE,
    NOTES_TABLE,
    PERSON_TABLE,
    ensure_schema,
)

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 90
# What the owner and each person may pick. None is "until forgotten".
RETENTION_CHOICES: tuple[int | None, ...] = (30, 90, 365, None)

KINDS: tuple[str, ...] = ("promised", "agreed", "declined", "answered", "asked", "shared")
TRUST_LEVELS: tuple[str, ...] = ("high", "medium", "low")
SOURCE_APPROVED_REPLY = "approved_reply"
CHANNEL_EMAIL = "email"

MAX_SUMMARY_CHARS = 240
MAX_QUOTE_CHARS = 400
MAX_CORRECTION_CHARS = 400
MAX_LIST = 200


@dataclass(frozen=True)
class NewNote:
    kind: str
    summary: str
    quote: str
    due_date: str | None = None


@dataclass(frozen=True)
class Note:
    id: int
    person_id: int
    visibility: str
    source: str
    channel: str
    conversation_key: str
    counterpart: str
    subject: str
    kind: str
    summary: str
    quote: str
    due_date: str | None
    trust: str
    occurred_at: str
    created_at: str
    expires_at: str | None
    pinned: bool
    correction: str | None
    corrected_at: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "channel": self.channel,
            "conversation_key": self.conversation_key,
            "counterpart": self.counterpart,
            "subject": self.subject,
            "kind": self.kind,
            "summary": self.summary,
            "quote": self.quote,
            "due_date": self.due_date,
            "trust": self.trust,
            "occurred_at": self.occurred_at,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "pinned": self.pinned,
            "correction": self.correction,
            "corrected_at": self.corrected_at,
        }


@dataclass(frozen=True)
class PersonSettings:
    reply_notes: bool = False
    retention_days: int | None = None


class SettingError(ValueError):
    """A setting that isn't allowed, with a message for the person."""


_COLUMNS = (
    "id, person_id, visibility, source, channel, conversation_key, counterpart, subject, kind, "
    "summary, quote, due_date, trust, occurred_at, created_at, expires_at, pinned, correction, corrected_at"
)


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


def _db_path(db_path: Path | None) -> Path:
    if db_path is not None:
        return db_path
    from openexecutive.memory import episodic

    return Path(episodic.DB_PATH)


def _connect(db_path: Path | None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path(db_path)))
    ensure_schema(conn)
    return conn


def conversation_key(channel: str, ref: str) -> str:
    """A stable key for one conversation (a mail thread id), so its notes can
    be found and forgotten together without keeping the raw id twice."""
    return hashlib.sha256(f"{channel}\x00{ref}".encode()).hexdigest()[:32]


def _row_note(row: tuple[Any, ...]) -> Note:
    return Note(
        id=int(row[0]), person_id=int(row[1]), visibility=str(row[2]), source=str(row[3]),
        channel=str(row[4]), conversation_key=str(row[5]), counterpart=str(row[6] or ""),
        subject=str(row[7] or ""), kind=str(row[8]), summary=str(row[9]), quote=str(row[10]),
        due_date=row[11], trust=str(row[12]), occurred_at=str(row[13]), created_at=str(row[14]),
        expires_at=row[15], pinned=bool(row[16]), correction=row[17], corrected_at=row[18],
    )


# --- Settings ---------------------------------------------------------------


def _valid_retention(days: Any) -> int | None:
    if days is None:
        return None
    if isinstance(days, bool) or not isinstance(days, int) or days not in RETENTION_CHOICES:
        raise SettingError("Choose 30, 90 or 365 days, or keep notes until they're forgotten.")
    return days


def company_retention(*, db_path: Path | None = None) -> int | None:
    """How long notes last, in days, for the company (None: until
    forgotten). The built-in default when the owner never set it, or on any
    read error."""
    if not _db_path(db_path).exists():
        return DEFAULT_RETENTION_DAYS
    try:
        conn = _connect(db_path)
        try:
            row = conn.execute(f"SELECT retention_days FROM {COMPANY_TABLE} WHERE id = 1").fetchone()  # noqa: S608 — constant table name
        finally:
            conn.close()
    except Exception:
        logger.warning("history: couldn't read the company retention — using the default", exc_info=True)
        return DEFAULT_RETENTION_DAYS
    if row is None:
        return DEFAULT_RETENTION_DAYS
    return int(row[0]) if row[0] is not None else None


def valid_retention(days: Any) -> int | None:
    """``days`` as a company retention, or SettingError."""
    return _valid_retention(days)


def valid_person_retention(days: Any, company: int | None) -> int | None:
    """``days`` as a person's own retention under the company's ``company``:
    one of the choices, never longer. SettingError otherwise."""
    value = _valid_retention(days)
    if value is not None and company is not None and value > company:
        raise SettingError(f"Your notes can't last longer than the company's {company} days.")
    return value


def set_company_retention(days: Any, *, by: str, db_path: Path | None = None, now: datetime | None = None) -> int | None:
    """Set the company default. Shortening it shortens everyone's stored
    notes too (a person's own setting can only be shorter). Returns it."""
    value = _valid_retention(days)
    moment = _now(now).isoformat()
    conn = _connect(db_path)
    try:
        conn.execute(
            f"INSERT INTO {COMPANY_TABLE} (id, retention_days, updated_at, updated_by) VALUES (1, ?, ?, ?) "  # noqa: S608 — constant table name
            "ON CONFLICT(id) DO UPDATE SET retention_days = excluded.retention_days, "
            "updated_at = excluded.updated_at, updated_by = excluded.updated_by",
            (value, moment, by),
        )
        # A person's own choice may now be longer than the company's: clamp it.
        if value is not None:
            conn.execute(
                f"UPDATE {PERSON_TABLE} SET retention_days = ? "  # noqa: S608 — constant table name
                "WHERE retention_days IS NOT NULL AND retention_days > ?",
                (value, value),
            )
        conn.commit()
    finally:
        conn.close()
    _reapply_expiry(None, db_path=db_path)
    return value


def person_settings(person_id: int | None, *, db_path: Path | None = None) -> PersonSettings:
    """A person's own switches. Off, and the company default, when unset or
    unreadable."""
    if person_id is None or not _db_path(db_path).exists():
        return PersonSettings()
    try:
        conn = _connect(db_path)
        try:
            row = conn.execute(
                f"SELECT reply_notes, retention_days FROM {PERSON_TABLE} WHERE person_id = ?",  # noqa: S608 — constant table name
                (person_id,),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        logger.warning("history: couldn't read a person's settings — treating them as off", exc_info=True)
        return PersonSettings()
    if row is None:
        return PersonSettings()
    return PersonSettings(reply_notes=bool(row[0]), retention_days=int(row[1]) if row[1] is not None else None)


def set_person_settings(
    person_id: int,
    *,
    by: str,
    reply_notes: bool | None = None,
    retention_days: Any = ...,
    db_path: Path | None = None,
    now: datetime | None = None,
) -> PersonSettings:
    """Change a person's own switches (only the fields given). Their
    retention may be shorter than the company's, never longer; None means
    "the company default". Returns the settings now in force."""
    current = person_settings(person_id, db_path=db_path)
    notes = current.reply_notes if reply_notes is None else bool(reply_notes)
    days = current.retention_days
    if retention_days is not ...:
        days = valid_person_retention(retention_days, company_retention(db_path=db_path))
    conn = _connect(db_path)
    try:
        conn.execute(
            f"INSERT INTO {PERSON_TABLE} (person_id, reply_notes, retention_days, updated_at, updated_by) "  # noqa: S608 — constant table name
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(person_id) DO UPDATE SET "
            "reply_notes = excluded.reply_notes, retention_days = excluded.retention_days, "
            "updated_at = excluded.updated_at, updated_by = excluded.updated_by",
            (person_id, int(notes), days, _now(now).isoformat(), by),
        )
        conn.commit()
    finally:
        conn.close()
    if retention_days is not ...:
        _reapply_expiry(person_id, db_path=db_path)
    return PersonSettings(reply_notes=notes, retention_days=days)


def effective_retention(person_id: int, *, db_path: Path | None = None) -> int | None:
    """The days a new note of this person's lasts: the shorter of theirs and
    the company's (None: until forgotten)."""
    company = company_retention(db_path=db_path)
    own = person_settings(person_id, db_path=db_path).retention_days
    choices = [d for d in (company, own) if d is not None]
    return min(choices) if choices else None


def _expiry(occurred_at: str, days: int | None) -> str | None:
    if days is None:
        return None
    try:
        start = datetime.fromisoformat(occurred_at)
    except ValueError:
        start = datetime.now(UTC)
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    return (start + timedelta(days=days)).isoformat()


def _reapply_expiry(person_id: int | None, *, db_path: Path | None = None) -> None:
    """Recompute ``expires_at`` on unpinned notes after a retention change
    (one person's, or everyone's), from when each note's clock started."""
    conn = _connect(db_path)
    try:
        if person_id is None:
            people = [int(r[0]) for r in conn.execute(f"SELECT DISTINCT person_id FROM {NOTES_TABLE}")]  # noqa: S608 — constant table name
        else:
            people = [person_id]
    finally:
        conn.close()
    for pid in people:
        days = effective_retention(pid, db_path=db_path)
        conn = _connect(db_path)
        try:
            rows = conn.execute(
                f"SELECT id, COALESCE(kept_from, occurred_at) FROM {NOTES_TABLE} WHERE person_id = ? AND pinned = 0",  # noqa: S608 — constant table name
                (pid,),
            ).fetchall()
            conn.executemany(
                f"UPDATE {NOTES_TABLE} SET expires_at = ? WHERE id = ?",  # noqa: S608 — constant table name
                [(_expiry(str(occurred), days), int(nid)) for nid, occurred in rows],
            )
            conn.commit()
        finally:
            conn.close()


# --- Exclusions -------------------------------------------------------------


def is_excluded(person_id: int, key: str, *, db_path: Path | None = None) -> bool:
    """Whether the person asked not to remember this conversation. Fails
    closed: a read error counts as excluded."""
    if not _db_path(db_path).exists():
        return False
    try:
        conn = _connect(db_path)
        try:
            row = conn.execute(
                f"SELECT 1 FROM {EXCLUDED_TABLE} WHERE person_id = ? AND conversation_key = ?",  # noqa: S608 — constant table name
                (person_id, key),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        logger.warning("history: couldn't read the exclusions — treating it as excluded", exc_info=True)
        return True
    return row is not None


def forget_conversation(person_id: int, key: str, *, db_path: Path | None = None, now: datetime | None = None) -> int:
    """"Don't remember this": forget the person's notes from one
    conversation and keep it from being noted again. Returns how many notes
    were forgotten."""
    conn = _connect(db_path)
    try:
        deleted = conn.execute(
            f"DELETE FROM {NOTES_TABLE} WHERE person_id = ? AND conversation_key = ?",  # noqa: S608 — constant table name
            (person_id, key),
        ).rowcount
        conn.execute(
            f"INSERT OR IGNORE INTO {EXCLUDED_TABLE} (person_id, conversation_key, created_at) VALUES (?, ?, ?)",  # noqa: S608 — constant table name
            (person_id, key, _now(now).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
    return int(deleted or 0)


# --- Notes ------------------------------------------------------------------


def add_notes(
    person_id: int,
    notes: list[NewNote],
    *,
    source: str,
    channel: str,
    conversation_ref: str,
    counterpart: str,
    subject: str,
    trust: str,
    occurred_at: datetime,
    db_path: Path | None = None,
    now: datetime | None = None,
) -> list[int]:
    """Store checked notes as the person's own private notes. Nothing is
    stored for an excluded conversation. Returns the new ids."""
    if trust not in TRUST_LEVELS:
        raise ValueError("unknown trust level")
    key = conversation_key(channel, conversation_ref)
    if not notes or is_excluded(person_id, key, db_path=db_path):
        return []
    when = occurred_at.astimezone(UTC).isoformat()
    expires = _expiry(when, effective_retention(person_id, db_path=db_path))
    created = _now(now).isoformat()
    ids: list[int] = []
    conn = _connect(db_path)
    try:
        for note in notes:
            if note.kind not in KINDS:
                raise ValueError("unknown kind")
            cur = conn.execute(
                f"INSERT INTO {NOTES_TABLE} (person_id, visibility, source, channel, conversation_key, "  # noqa: S608 — constant table name
                "counterpart, subject, kind, summary, quote, due_date, trust, occurred_at, created_at, expires_at) "
                "VALUES (?, 'private', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    person_id, source, channel, key, counterpart[:200], subject[:200], note.kind,
                    note.summary[:MAX_SUMMARY_CHARS], note.quote[:MAX_QUOTE_CHARS], note.due_date,
                    trust, when, created, expires,
                ),
            )
            ids.append(int(cur.lastrowid or 0))
        conn.commit()
    finally:
        conn.close()
    return ids


def _live_clause() -> str:
    return "(expires_at IS NULL OR expires_at > ?)"


def list_notes(
    person_id: int | None,
    *,
    query: str | None = None,
    limit: int = 50,
    db_path: Path | None = None,
    now: datetime | None = None,
) -> list[Note]:
    """A person's own notes that haven't expired, newest first. ``query``
    narrows them to notes whose summary, counterpart, subject or quote
    mention it. Empty on any read error."""
    if person_id is None or not _db_path(db_path).exists():
        return []
    sql = (
        f"SELECT {_COLUMNS} FROM {NOTES_TABLE} WHERE person_id = ? AND {_live_clause()}"  # noqa: S608 — constant table name and columns
    )
    params: list[Any] = [person_id, _now(now).isoformat()]
    words = [w for w in (query or "").lower().split() if len(w) > 1][:6]
    for word in words:
        like = f"%{word.replace('%', '').replace('_', '')}%"
        sql += (
            " AND (lower(summary) LIKE ? OR lower(counterpart) LIKE ? OR lower(subject) LIKE ?"
            " OR lower(quote) LIKE ? OR lower(coalesce(correction, '')) LIKE ?)"
        )
        params.extend([like] * 5)
    sql += " ORDER BY occurred_at DESC, id DESC LIMIT ?"
    params.append(max(1, min(int(limit), MAX_LIST)))
    try:
        conn = _connect(db_path)
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
    except Exception:
        logger.warning("history: couldn't read notes", exc_info=True)
        return []
    return [_row_note(r) for r in rows]


def get_note(person_id: int, note_id: int, *, db_path: Path | None = None, now: datetime | None = None) -> Note | None:
    """One of the person's own live notes, or None (another person's note is
    None too)."""
    if not _db_path(db_path).exists():
        return None
    conn = _connect(db_path)
    try:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM {NOTES_TABLE} WHERE id = ? AND person_id = ? AND {_live_clause()}",  # noqa: S608 — constant table name and columns
            (note_id, person_id, _now(now).isoformat()),
        ).fetchone()
    finally:
        conn.close()
    return _row_note(row) if row else None


def forget_note(person_id: int, note_id: int, *, db_path: Path | None = None) -> bool:
    """Delete one of the person's own notes. False when it isn't theirs or
    is gone."""
    conn = _connect(db_path)
    try:
        deleted = conn.execute(
            f"DELETE FROM {NOTES_TABLE} WHERE id = ? AND person_id = ?",  # noqa: S608 — constant table name
            (note_id, person_id),
        ).rowcount
        conn.commit()
    finally:
        conn.close()
    return bool(deleted)


def pin_note(
    person_id: int, note_id: int, pinned: bool, *, db_path: Path | None = None, now: datetime | None = None
) -> Note | None:
    """Pin a note (it never expires) or unpin it (it expires as the person's
    retention says, counted from the unpin when it happened earlier, so an
    old note isn't lost the moment its pin comes off)."""
    note = get_note(person_id, note_id, db_path=db_path, now=now)
    if note is None:
        return None
    conn = _connect(db_path)
    try:
        if pinned:
            conn.execute(
                f"UPDATE {NOTES_TABLE} SET pinned = 1, expires_at = NULL WHERE id = ? AND person_id = ?",  # noqa: S608 — constant table name
                (note_id, person_id),
            )
        else:
            start = max(_now(now).isoformat(), note.occurred_at)
            conn.execute(
                f"UPDATE {NOTES_TABLE} SET pinned = 0, kept_from = ?, expires_at = ? WHERE id = ? AND person_id = ?",  # noqa: S608 — constant table name
                (start, _expiry(start, effective_retention(person_id, db_path=db_path)), note_id, person_id),
            )
        conn.commit()
    finally:
        conn.close()
    return get_note(person_id, note_id, db_path=db_path, now=now)


def correct_note(
    person_id: int, note_id: int, text: str, *, db_path: Path | None = None, now: datetime | None = None
) -> Note | None:
    """Keep the note and add the person's own version, which recall shows in
    its place. An empty text removes the correction."""
    note = get_note(person_id, note_id, db_path=db_path, now=now)
    if note is None:
        return None
    from openexecutive.delegation.ghostwriter import one_line

    cleaned = one_line(text or "", MAX_CORRECTION_CHARS)
    conn = _connect(db_path)
    try:
        conn.execute(
            f"UPDATE {NOTES_TABLE} SET correction = ?, corrected_at = ? WHERE id = ? AND person_id = ?",  # noqa: S608 — constant table name
            (cleaned or None, _now(now).isoformat() if cleaned else None, note_id, person_id),
        )
        conn.commit()
    finally:
        conn.close()
    return get_note(person_id, note_id, db_path=db_path, now=now)


def sweep_expired(*, db_path: Path | None = None, now: datetime | None = None) -> int:
    """Delete notes past their expiry (reads already skip them). Returns how
    many. Never raises."""
    if not _db_path(db_path).exists():
        return 0
    try:
        conn = _connect(db_path)
        try:
            deleted = conn.execute(
                f"DELETE FROM {NOTES_TABLE} WHERE pinned = 0 AND expires_at IS NOT NULL AND expires_at <= ?",  # noqa: S608 — constant table name
                (_now(now).isoformat(),),
            ).rowcount
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.warning("history: sweeping expired notes failed", exc_info=True)
        return 0
    return int(deleted or 0)
