"""Tell the person once when their own mailbox stops taking its sign-in.

Act as me reads and drafts in the person's own mailbox (Gmail or Outlook)
with a saved sign-in. When the mail service stops accepting it (revoked,
expired, a password change), every mail and calendar step fails until they
connect it again, and before this they only found out mid-question, when a
search or a draft failed.

So each time a mailbox status check (``gmail.gmail_status``, the same for
Gmail and Outlook) finds ``needs_reconnect``, :func:`record` hands it to
:func:`note` in the background, which records the break off the event loop;
the first time for a break it sends the person one message, the way their
reminders reach them (``scheduler.runner.deliver_to_person``), saying how to
connect it again. A check that finds the mailbox connected clears the record,
so the next break is told again. Nothing goes to anyone else, and the row
holds only the address and two times.

Never raises: a notice that can't go is logged and tried again on the next
check that finds the break.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

TABLE = "delegation_sign_in_notices"
MESSAGE = (
    "Your mailbox stopped accepting the sign-in I use, so I can't read your mail "
    "or calendar, or save drafts in it, until you connect it again. You can do "
    "that in Delegate → As you."
)
# Notices being sent now, held so nothing collects one before it ends.
_tasks: set[asyncio.Task[bool]] = set()


def _db(db_path: Path | None) -> Path:
    if db_path is not None:
        return db_path
    from openexecutive.memory import episodic

    return Path(episodic.DB_PATH)


def _connect(db_path: Path | None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db(db_path)))
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {TABLE} ("  # noqa: S608 — constant table name
        "  email TEXT PRIMARY KEY,"
        "  broken_at TEXT NOT NULL,"
        "  noticed_at TEXT"
        ")"
    )
    return conn


def _claim(email: str, now: datetime, db_path: Path | None) -> bool:
    """Record the break for ``email``; True when no notice has gone for it
    yet (and it is now this caller's to send)."""
    conn = _connect(db_path)
    try:
        with conn:
            conn.execute(
                f"INSERT OR IGNORE INTO {TABLE} (email, broken_at) VALUES (?, ?)",  # noqa: S608
                (email, now.isoformat()),
            )
            cur = conn.execute(
                f"UPDATE {TABLE} SET noticed_at = ? WHERE email = ? AND noticed_at IS NULL",  # noqa: S608
                (now.isoformat(), email),
            )
            return cur.rowcount == 1
    finally:
        conn.close()


def _unclaim(email: str, db_path: Path | None) -> None:
    """The notice couldn't go: let the next check try again."""
    conn = _connect(db_path)
    try:
        with conn:
            conn.execute(f"UPDATE {TABLE} SET noticed_at = NULL WHERE email = ?", (email,))  # noqa: S608
    finally:
        conn.close()


def _clear(email: str, db_path: Path | None) -> None:
    conn = _connect(db_path)
    try:
        with conn:
            conn.execute(f"DELETE FROM {TABLE} WHERE email = ?", (email,))  # noqa: S608
    finally:
        conn.close()


async def _send(email: str, db_path: Path | None) -> None:
    from openexecutive.audit import log_event
    from openexecutive.audit.context import private_rows, rows_for_person
    from openexecutive.people.store import find_person_by_email
    from openexecutive.scheduler.runner import deliver_to_person

    try:
        person = find_person_by_email(email)
        if person is None or person.archived:
            return
        rows = private_rows() if person.is_principal else rows_for_person(person.id)
        with rows:
            delivery = await deliver_to_person(person, MESSAGE, label="Your mailbox")
    except Exception:
        logger.warning("sign_in_notice: couldn't send", exc_info=True)
        _unclaim(email, db_path)
        return
    if not delivery.ok:
        logger.info("sign_in_notice: not sent (%s)", delivery.reason)
        _unclaim(email, db_path)
        return
    log_event(
        "delegation_sign_in_notice_sent",
        f"Told person {person.id} their mailbox needs connecting again",
        actor="scheduler",
        details={"person_id": person.id, "channel": delivery.channel},
        private=True,
        private_to_person=person.id,
    )


async def note(
    email: str, status: str, *, now: datetime | None = None, db_path: Path | None = None,
) -> bool:
    """Record what a status check of ``email``'s own mailbox found. On a new
    break (``needs_reconnect``) send the one notice; on ``connected`` clear
    the break. The database work runs off the event loop. Returns True when
    a notice was tried. Never raises."""
    try:
        if not email:
            return False
        if status == "connected":
            await asyncio.to_thread(_clear, email, db_path)
            return False
        if status != "needs_reconnect":
            return False
        if not await asyncio.to_thread(_claim, email, now or datetime.now(UTC), db_path):
            return False
        await _send(email, db_path)
        return True
    except Exception:
        logger.warning("sign_in_notice: couldn't record the status", exc_info=True)
        return False


def record(email: str, status: str) -> None:
    """Start :func:`note` in the background, so the status check that found
    ``status`` never waits on it. Does nothing outside a running loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(note(email, status))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
