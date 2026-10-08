"""remind_me: a reminder sent to the person who asked, as plain text, when
it is due (``orchestrator.reminder_tools``).

Act as me's way to "remind me Friday" that is safe on a turn that read the
person's mail. ``schedule_followup`` is not: its row runs a whole Executive
turn with tools when it fires, and ``/scheduled`` shows it to everyone. A
reminder here:

- is stored as fixed text, cleaned once when it is set: one line, no links
  or addresses (a link in a DM is fetched for its preview, and the text may
  repeat something the mail planted), at most ``MAX_TEXT`` characters;
- goes only to the person who set it, through their own channel
  (``scheduler.runner.deliver_to_person``), never to anyone else;
- runs nothing when it fires: no model, no tool, the text as stored;
- is private: its own table, private audit rows, never on the activity rail.

The scheduler's tick claims each due reminder once before sending it
(``send_due``), so two workers never both send one and a send that failed
halfway is never repeated.
"""
from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from openexecutive.delegation.schema import REMINDERS_TABLE, ensure_schema

logger = logging.getLogger(__name__)

MAX_TEXT = 300
MAX_PENDING = 25
HORIZON = timedelta(days=366)
SCAN_EVERY = timedelta(minutes=1)
# A reminder missed by more than this (the API was down) is dropped, not sent late.
STALE_AFTER = timedelta(days=2)

# Anything a chat app could turn into a link: a URL of any scheme, an email
# address, a domain-like word (``evil.example``, ``x.evil.example?d=1``), a
# host with a port, an IPv4 address. Each with whatever path or query follows.
_URL_RE = re.compile(
    r"(?i)\b[a-z][\w+.-]*://\S+"
    r"|\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"
    r"|\b\d{1,3}(?:\.\d{1,3}){3}\b(?::\d+)?(?:[/?#]\S*)?"
    # The last label starts with any letter, so a non-ASCII TLD (.рф) counts too.
    r"|\b[\w-]+(?:\.[\w-]+)*\.[^\W\d_][\w-]+\b(?::\d+)?(?:[/?#]\S*)?"
    r"|\b[a-z][\w-]*:\d{2,5}(?:[/?#]\S*)?"
)

_last_scan_at: datetime | None = None
_task: asyncio.Task[int] | None = None


@dataclass(frozen=True)
class Reminder:
    id: int
    person_id: int
    text: str
    due_at: str
    created_at: str


def clean_text(text: str) -> str:
    """The reminder as it will be sent: one line, links and email addresses
    replaced by "[link]", cut to ``MAX_TEXT``."""
    from openexecutive.utils.prompt_blocks import plain

    flat = " ".join(plain(text or "").split())
    return _URL_RE.sub("[link]", flat)[:MAX_TEXT].strip()


def _db(db_path: Path | None) -> Path:
    if db_path is not None:
        return db_path
    from openexecutive.memory import episodic

    return Path(episodic.DB_PATH)


def _connect(db_path: Path | None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db(db_path)))
    ensure_schema(conn)
    return conn


def pending_count(person_id: int, *, db_path: Path | None = None) -> int:
    """Reminders ``person_id`` has set that are still to send. Raises on a
    read error: the cap must not fail open."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            f"SELECT COUNT(*) FROM {REMINDERS_TABLE} "  # noqa: S608 — constant table name
            "WHERE person_id = ? AND claimed_at IS NULL AND cancelled_at IS NULL",
            (person_id,),
        ).fetchone()
        return int(row[0] if row else 0)
    finally:
        conn.close()


def add(person_id: int, text: str, due_at: datetime, *, now: datetime, db_path: Path | None = None) -> int:
    """Store a reminder for ``person_id``; returns its id. ``text`` must
    already be ``clean_text``'s and ``due_at`` timezone-aware."""
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            f"INSERT INTO {REMINDERS_TABLE} (person_id, text, due_at, created_at) "  # noqa: S608
            "VALUES (?, ?, ?, ?)",
            # UTC, so ``_due``'s string comparison orders them by time.
            (person_id, text, due_at.astimezone(UTC).isoformat(), now.astimezone(UTC).isoformat()),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def _due(now: datetime, db_path: Path | None) -> list[Reminder]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT id, person_id, text, due_at, created_at FROM {REMINDERS_TABLE} "  # noqa: S608
            "WHERE claimed_at IS NULL AND cancelled_at IS NULL AND due_at <= ? ORDER BY due_at LIMIT 50",
            (now.astimezone(UTC).isoformat(),),
        ).fetchall()
    finally:
        conn.close()
    return [Reminder(int(r[0]), int(r[1]), str(r[2]), str(r[3]), str(r[4])) for r in rows]


def _claim(reminder_id: int, now: datetime, db_path: Path | None) -> bool:
    """Take a reminder for sending, once. True when this caller took it."""
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            f"UPDATE {REMINDERS_TABLE} SET claimed_at = ? "  # noqa: S608
            "WHERE id = ? AND claimed_at IS NULL AND cancelled_at IS NULL",
            (now.isoformat(), reminder_id),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def _mark_sent(reminder_id: int, now: datetime, db_path: Path | None) -> None:
    conn = _connect(db_path)
    try:
        conn.execute(
            f"UPDATE {REMINDERS_TABLE} SET sent_at = ? WHERE id = ?",  # noqa: S608
            (now.isoformat(), reminder_id),
        )
        conn.commit()
    finally:
        conn.close()


def message(text: str, created_at: str) -> str:
    """The reminder as sent, saying when they set it, so it reads as theirs."""
    from openexecutive.memory.workspace_settings import get_user_timezone

    try:
        set_at = datetime.fromisoformat(created_at).astimezone(get_user_timezone())
        when = f"{set_at:%b} {set_at.day}"
    except ValueError:
        return f"Reminder you set: {text}"
    return f"Reminder you set on {when}: {text}"


async def send_due(now: datetime, *, db_path: Path | None = None) -> int:
    """Send every due reminder to the person who set it. Returns how many
    went. Never raises."""
    from openexecutive.audit import log_event
    from openexecutive.audit.context import private_rows, rows_for_person
    from openexecutive.people.store import get_person
    from openexecutive.scheduler.runner import deliver_to_person

    try:
        due = _due(now, db_path)
    except Exception:
        logger.warning("reminders: couldn't read due reminders", exc_info=True)
        return 0
    sent = 0
    for reminder in due:
        try:
            if not _claim(reminder.id, now, db_path):
                continue
            due_at = datetime.fromisoformat(reminder.due_at)
            if now - due_at > STALE_AFTER:
                logger.info("reminders: dropped reminder %s, %s late", reminder.id, now - due_at)
                continue
            person = get_person(reminder.person_id)
            if person is None or person.archived:
                continue
            rows = private_rows() if person.is_principal else rows_for_person(reminder.person_id)
            with rows:
                text = message(reminder.text, reminder.created_at)
                delivery = await deliver_to_person(person, text, label="Reminder")
        except Exception:
            logger.warning("reminders: sending reminder %s failed", reminder.id, exc_info=True)
            continue
        if not delivery.ok:
            logger.info("reminders: reminder %s not sent (%s)", reminder.id, delivery.reason)
            continue
        try:
            _mark_sent(reminder.id, now, db_path)
        except Exception:
            logger.warning("reminders: couldn't mark reminder %s sent", reminder.id, exc_info=True)
        sent += 1
        log_event(
            "delegation_reminder_sent", f"Sent person {reminder.person_id} a reminder they set",
            actor="scheduler",
            details={"person_id": reminder.person_id, "reminder_id": reminder.id, "channel": delivery.channel},
            private=True, private_to_person=reminder.person_id,
        )
    return sent


def maybe_send(now: datetime) -> bool:
    """Start a pass over due reminders when the interval has passed and none
    is running. True when one started. Never raises."""
    global _last_scan_at, _task
    try:
        if _task is not None and not _task.done():
            return False
        if _last_scan_at is not None and now - _last_scan_at < SCAN_EVERY:
            return False
        _last_scan_at = now
        _task = asyncio.get_running_loop().create_task(send_due(now))
        return True
    except Exception:
        logger.warning("reminders: couldn't start a pass", exc_info=True)
        return False
