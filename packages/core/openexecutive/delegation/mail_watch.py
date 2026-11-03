"""watch_for_email: tell the person when an email they are waiting for lands
in their own mailbox (``orchestrator.reminder_tools``).

"Let me know when Dana's email comes in" used to have no real answer: the
watchlist reads public sources, ``schedule_followup`` runs a whole turn and
is refused on a turn that read mail, and a reminder only tells the person to
look. A mail watch looks for them:

- one row per watch, the person's own (``delegation_mail_watches``), with
  the sender's address and, optionally, a word the subject must contain;
- every ``CHECK_EVERY`` the scheduler's tick asks the person's own mailbox
  (``gmail.gmail_for``, Gmail or Outlook) for mail from that address since the
  watch was set, and the first match is claimed once and sent to the person
  alone through their own channel (``scheduler.runner.deliver_to_person``);
- the message names the sender and the subject, cleaned like a reminder (one
  line, no links or addresses) and never the email's text, so nothing the
  mail says reaches a chat app or a model turn;
- a watch nothing answered by ``expires_at`` ends with one line saying so;
- private: its own table, private audit rows, never an alert (alerts feed chat
  turns, and would carry other people's mail into memory).
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from openexecutive.delegation.schema import MAIL_WATCH_TABLE, ensure_schema

logger = logging.getLogger(__name__)

MAX_PENDING = 10
DEFAULT_DAYS = 7
MAX_DAYS = 30
CHECK_EVERY = timedelta(minutes=5)
MAX_ABOUT = 60
# Mail read per check: the newest few from the sender are enough to find one
# that arrived since the watch was set.
_RECENT_LIMIT = 5
_RECENT_LIMIT_ABOUT = 25  # the most recent_from returns
# How long after a watch ends it keeps retrying a message it could not deliver.
_DELIVERY_GRACE = timedelta(days=1)

_last_scan_at: datetime | None = None
_task: asyncio.Task[int] | None = None


@dataclass(frozen=True)
class MailWatch:
    id: int
    person_id: int
    sender: str
    about: str
    created_at: str
    expires_at: str


def _db(db_path: Path | None) -> Path:
    if db_path is not None:
        return db_path
    from openexecutive.memory import episodic

    return Path(episodic.DB_PATH)


def _connect(db_path: Path | None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db(db_path)))
    ensure_schema(conn)
    return conn


def clean_about(text: str) -> str:
    """The subject word(s) to match, as stored: one line, no links."""
    from openexecutive.delegation.reminders import clean_text

    return clean_text(text)[:MAX_ABOUT].replace("[link]", "").strip()


def pending_count(person_id: int, *, db_path: Path | None = None) -> int:
    conn = _connect(db_path)
    try:
        row = conn.execute(
            f"SELECT COUNT(*) FROM {MAIL_WATCH_TABLE} "  # noqa: S608
            "WHERE person_id = ? AND claimed_at IS NULL AND cancelled_at IS NULL",
            (person_id,),
        ).fetchone()
    finally:
        conn.close()
    return int(row[0] if row else 0)


def add(
    person_id: int, sender: str, about: str, expires_at: datetime, *,
    now: datetime, db_path: Path | None = None,
) -> int:
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            f"INSERT INTO {MAIL_WATCH_TABLE} "  # noqa: S608
            "(person_id, sender, about, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (
                person_id, sender, about, now.astimezone(UTC).isoformat(),
                expires_at.astimezone(UTC).isoformat(),
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def _open(db_path: Path | None) -> list[MailWatch]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT id, person_id, sender, about, created_at, expires_at FROM {MAIL_WATCH_TABLE} "  # noqa: S608
            "WHERE claimed_at IS NULL AND cancelled_at IS NULL ORDER BY id",
        ).fetchall()
    finally:
        conn.close()
    return [MailWatch(int(r[0]), int(r[1]), str(r[2]), str(r[3]), str(r[4]), str(r[5])) for r in rows]


def _claim(watch_id: int, outcome: str, now: datetime, db_path: Path | None) -> bool:
    """End a watch, once (``found`` or ``expired``). True when this caller did."""
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            f"UPDATE {MAIL_WATCH_TABLE} SET claimed_at = ?, outcome = ? "  # noqa: S608
            "WHERE id = ? AND claimed_at IS NULL AND cancelled_at IS NULL",
            (now.isoformat(), outcome, watch_id),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def _release(watch_id: int, db_path: Path | None) -> None:
    """Reopen a watch whose message couldn't be delivered, so the next pass tries again."""
    conn = _connect(db_path)
    try:
        conn.execute(
            f"UPDATE {MAIL_WATCH_TABLE} SET claimed_at = NULL, outcome = NULL "  # noqa: S608
            "WHERE id = ? AND cancelled_at IS NULL",
            (watch_id,),
        )
        conn.commit()
    finally:
        conn.close()


def cancel(person_id: int, watch_id: int, *, now: datetime, db_path: Path | None = None) -> bool:
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            f"UPDATE {MAIL_WATCH_TABLE} SET cancelled_at = ? "  # noqa: S608
            "WHERE id = ? AND person_id = ? AND claimed_at IS NULL AND cancelled_at IS NULL",
            (now.isoformat(), watch_id, person_id),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def _parse(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _matches(watch: MailWatch, message: Any, since: datetime) -> bool:
    received = _parse(getattr(message, "received_at", "") or "")
    if received is None or received < since:
        return False
    subject = str(getattr(message, "subject", "") or "").lower()
    return not watch.about or watch.about.lower() in subject


def found_text(watch: MailWatch, message: Any) -> str:
    """What the person is told: who and the subject, cleaned, never the text."""
    from openexecutive.delegation.reminders import clean_text

    who = clean_text(str(getattr(message, "from_name", "") or "")) or "The sender you're waiting on"
    subject = clean_text(str(getattr(message, "subject", "") or ""))
    line = f"The email you were waiting for came in: {who}"
    return f"{line}, \"{subject}\"." if subject else f"{line}."


def expired_text(watch: MailWatch, *, unreadable: bool = False) -> str:
    from openexecutive.memory.workspace_settings import get_user_timezone

    created = _parse(watch.created_at)
    when = ""
    if created is not None:
        local = created.astimezone(get_user_timezone())
        when = f" since {local:%b} {local.day}"
    about = f" about \"{watch.about}\"" if watch.about else ""
    if unreadable:
        return (
            f"I couldn't read your mailbox, so I can't tell whether the email{about} you were waiting on came in. "
            "I've stopped watching for it."
        )
    return f"No email{about} from the sender you were waiting on came in{when}. I've stopped watching for it."


async def _check_one(watch: MailWatch, now: datetime, db_path: Path | None) -> str:
    """``found`` / ``expired`` when the watch ended (and the person was told),
    else ``waiting``. Raises on a mailbox failure, for the caller to log."""
    from openexecutive.audit import log_event
    from openexecutive.audit.context import private_rows, rows_for_person
    from openexecutive.delegation.gmail import gmail_for, gmail_status, normalize_email
    from openexecutive.people.store import get_person
    from openexecutive.scheduler.runner import deliver_to_person

    person = get_person(watch.person_id)
    if person is None or person.archived:
        _claim(watch.id, "person_gone", now, db_path)
        return "expired"
    since = _parse(watch.created_at) or now
    expires = _parse(watch.expires_at) or now
    rows = private_rows() if person.is_principal else rows_for_person(watch.person_id)
    with rows:
        message = None
        unreadable = True
        email = normalize_email(person.email)
        try:
            if await gmail_status(email) == "connected":
                days = max(1, (now - since).days + 1)
                client = gmail_for(email)
                # The subject word is matched here, not in the query, so read
                # the most the mailbox allows when one is set.
                limit = _RECENT_LIMIT_ABOUT if watch.about else _RECENT_LIMIT
                recent = await client.recent_from(watch.sender, days=days, limit=limit)
                message = next((m for m in recent if _matches(watch, m, since)), None)
                unreadable = False
        except Exception:
            if now < expires:
                raise
            # A mailbox that stays broken still ends the watch on time.
            logger.warning("mail_watch: mailbox read failed for expiring watch %s", watch.id, exc_info=True)
        if message is not None:
            text, outcome = found_text(watch, message), "found"
        elif now >= expires:
            text, outcome = expired_text(watch, unreadable=unreadable), "expired"
        else:
            return "waiting"
        if not _claim(watch.id, outcome, now, db_path):
            return "waiting"  # another worker ended it
        delivery = await deliver_to_person(person, text, label="Email watch")
        if not delivery.ok and now < expires + _DELIVERY_GRACE:
            # Couldn't reach them: reopen it so the next pass tells them.
            _release(watch.id, db_path)
            logger.warning("mail_watch: couldn't deliver watch %s (%s)", watch.id, delivery.reason)
            return "waiting"
        log_event(
            "delegation_mail_watch_found" if outcome == "found" else "delegation_mail_watch_expired",
            f"Told person {watch.person_id} about a mail watch ({outcome})",
            actor="scheduler",
            details={
                "person_id": watch.person_id, "watch_id": watch.id,
                "delivered": bool(delivery.ok), "channel": delivery.channel,
            },
            private=True, private_to_person=watch.person_id,
        )
        return outcome


async def check_due(now: datetime, *, db_path: Path | None = None) -> int:
    """Check every open watch once. Returns how many ended. Never raises."""
    try:
        watches = _open(db_path)
    except Exception:
        logger.warning("mail_watch: couldn't read watches", exc_info=True)
        return 0
    ended = 0
    for watch in watches:
        try:
            if await _check_one(watch, now, db_path) != "waiting":
                ended += 1
        except Exception:
            # A mailbox hiccup: the watch stays open and is checked next time.
            logger.warning("mail_watch: checking watch %s failed", watch.id, exc_info=True)
    return ended


def maybe_check(now: datetime) -> bool:
    """Start a pass over open watches when the interval has passed and none
    is running. True when one started. Never raises."""
    global _last_scan_at, _task
    try:
        if _task is not None and not _task.done():
            return False
        if _last_scan_at is not None and now - _last_scan_at < CHECK_EVERY:
            return False
        _last_scan_at = now
        _task = asyncio.get_running_loop().create_task(check_due(now))
        return True
    except Exception:
        logger.warning("mail_watch: couldn't start a pass", exc_info=True)
        return False
