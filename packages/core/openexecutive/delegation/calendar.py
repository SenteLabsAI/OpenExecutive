"""Act as me: Read my calendar.

A person who has Act as me can also let the Executive read their own
calendar, read-only: ``calendar.readonly`` on Google, ``Calendars.Read`` on
Microsoft, granted on the same per-person sign-in as their mailbox when they
connect it with ``--calendar`` (``scripts/connect-own-gmail.py``,
``scripts/connect-own-outlook.py``). A sign-in made without it keeps working
for mail; only the calendar read says to connect again.

**The switch.** One row per person in ``delegation_calendar``; absent,
unreadable or off all mean off. Only they turn it on (``PUT
/delegation/calendar``), only while Act as me is on and their calendar
opens; turning Act as me off turns it off too.

**What reads it.** ``my_calendar`` (``orchestrator.mail_read_tools``), on
the same turns and with the same fence as their mail: a read makes the turn
private and locks it down (``delegation.lockdown``), since an invite's title
is anyone's words. And the first week's private brief lists today's meetings
(``briefing.first_week``). Nothing is kept: each read asks the calendar again.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from openexecutive.delegation.schema import CALENDAR_TABLE, ensure_schema

logger = logging.getLogger(__name__)

MAX_DAYS = 31

NOT_GRANTED = (
    "Your saved sign-in doesn't include your calendar. Add it under Read my calendar "
    "in Delegate → Act as me, then turn Read my calendar on."
)
OFF = "Read my calendar is off. They can turn it on in Settings → Act as me."


def _db(db_path: Path | None) -> Path:
    if db_path is not None:
        return db_path
    from openexecutive.memory import episodic

    return Path(episodic.DB_PATH)


def _connect(db_path: Path | None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db(db_path)))
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def enabled(person_id: int | None, *, db_path: Path | None = None) -> bool:
    """Whether ``person_id`` turned Read my calendar on. Never raises."""
    if person_id is None:
        return False
    try:
        conn = _connect(db_path)
        try:
            row = conn.execute(
                f"SELECT enabled FROM {CALENDAR_TABLE} WHERE person_id = ?",  # noqa: S608 — constant table name
                (person_id,),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        logger.warning("delegation.calendar: couldn't read the switch — treating it as off", exc_info=True)
        return False
    return bool(row is not None and row["enabled"])


def set_enabled(person_id: int, on: bool, *, updated_by: str, db_path: Path | None = None) -> None:
    """Turn it on or off (callers authorize first)."""
    conn = _connect(db_path)
    try:
        conn.execute(
            f"INSERT INTO {CALENDAR_TABLE} (person_id, enabled, updated_at, updated_by) "  # noqa: S608
            "VALUES (?, ?, ?, ?) ON CONFLICT(person_id) DO UPDATE SET enabled = excluded.enabled, "
            "updated_at = excluded.updated_at, updated_by = excluded.updated_by",
            (person_id, int(on), datetime.now(UTC).isoformat(), updated_by),
        )
        conn.commit()
    finally:
        conn.close()


def day_window(day: date, days: int, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """From the start of ``day`` for ``days`` days, in ``tz``."""
    start = datetime.combine(day, time.min, tzinfo=tz)
    return start, start + timedelta(days=max(1, min(days, MAX_DAYS)))


def local_when(value: str, all_day: bool, tz: ZoneInfo) -> str:
    """An event's start or end as the person reads it: ``2026-10-14 09:30``
    in their time zone, or the date alone for an all-day event."""
    if all_day or not value:
        return value[:10]
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value[:25]
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(tz).strftime("%Y-%m-%d %H:%M")


def last_day(start: str, end: str) -> str:
    """An all-day event's last day: its ``end`` is the day after (both
    Google and Graph), so a one-day event ends on its start."""
    try:
        first = date.fromisoformat(start[:10])
        after = date.fromisoformat(end[:10])
    except ValueError:
        return start[:10]
    return max(first, after - timedelta(days=1)).isoformat()


async def events_for(mailbox: Any, start: datetime, end: datetime, *, max_results: int = 100) -> list[Any]:
    """``mailbox.list_events``: their own calendar, through the client for
    their mailbox (Gmail or Outlook)."""
    events: list[Any] = await mailbox.list_events(start, end, max_results=max_results)
    return events


__all__ = ["MAX_DAYS", "NOT_GRANTED", "OFF", "day_window", "enabled", "events_for", "local_when", "set_enabled"]
