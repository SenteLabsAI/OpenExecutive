"""Proactive DMs held back by a person's notification settings, and their
release (``people.notifications``).

The outbound guard stores a held DM here instead of sending it
(``orchestrator.schedule_tools._guard_outbound``). Each row is either:

- **waiting for quiet hours to end** (``release_at`` set): the scheduler's
  tick (``maybe_release``) sends everything due to that person in one message
  once ``release_at`` passes;
- **waiting for their brief** (``release_at`` null, "In my briefs"): the
  principal's morning or end-of-day brief carries them as one "While you were
  away" block (``scheduler.runner._run_principal_brief``). Anyone else who
  chose briefs gets that block on its own when the briefs go out
  (``schedule_digest_release``), since only the principal has a brief.

A row is sent once: it is marked sent only after the delivery went, and left
held when it didn't (the next pass retries). Rows nobody could deliver for
``GIVE_UP_AFTER`` are dropped, so the table can't grow without end. A digest
row that has waited ``DIGEST_FALLBACK_AFTER`` with no brief to carry it (the
briefs not going out) is sent on its own.

The text is private to the person it was for: it is never listed anywhere,
only counted (``held_count``) on their own Notifications page, and release
audit rows are private to them.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path

logger = logging.getLogger(__name__)

TABLE = "held_messages"

# The most text one held row keeps.
MAX_TEXT = 4000
# The most rows one person can have waiting; past it the oldest go first.
MAX_HELD_PER_PERSON = 50
GIVE_UP_AFTER = timedelta(days=7)
DIGEST_FALLBACK_AFTER = timedelta(hours=36)
RELEASE_EVERY = timedelta(minutes=1)
HEADING = "While you were away"

_last_release_at: datetime | None = None
_task: asyncio.Task[int] | None = None


@dataclass(frozen=True)
class HeldMessage:
    id: int
    person_id: int
    source: str
    text: str
    created_at: str
    release_at: str | None


def _db(db_path: Path | None) -> Path:
    if db_path is not None:
        return db_path
    from openexecutive.memory import episodic

    return Path(episodic.DB_PATH)


def _connect(db_path: Path | None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db(db_path)))
    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id INTEGER NOT NULL,
            source TEXT NOT NULL DEFAULT '',
            text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            release_at TEXT,
            sent_at TEXT,
            dropped_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_held_messages_person
            ON {TABLE}(person_id) WHERE sent_at IS NULL AND dropped_at IS NULL;
        """
    )
    return conn


def _iso(dt: datetime) -> str:
    # UTC, so string comparison orders them by time.
    return dt.astimezone(UTC).isoformat()


def _rows(cur: sqlite3.Cursor) -> list[HeldMessage]:
    return [
        HeldMessage(int(r[0]), int(r[1]), str(r[2]), str(r[3]), str(r[4]), r[5])
        for r in cur.fetchall()
    ]


_COLUMNS = "id, person_id, source, text, created_at, release_at"
_OPEN = "sent_at IS NULL AND dropped_at IS NULL"


def hold(
    person_id: int,
    text: str,
    *,
    source: str,
    release_at: datetime | None,
    now: datetime | None = None,
    db_path: Path | None = None,
) -> int:
    """Store a DM for ``person_id`` until ``release_at`` (None: their next
    brief). Returns its id. Raises on a write error, so the caller can send
    instead of losing the message."""
    now = now or datetime.now(UTC)
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            f"INSERT INTO {TABLE} (person_id, source, text, created_at, release_at) "  # noqa: S608
            "VALUES (?, ?, ?, ?, ?)",
            (
                person_id,
                source[:64],
                text[:MAX_TEXT],
                _iso(now),
                _iso(release_at) if release_at is not None else None,
            ),
        )
        held_id = int(cur.lastrowid or 0)
        # Cap what one person can have waiting: drop the oldest past it.
        conn.execute(
            f"UPDATE {TABLE} SET dropped_at = ? WHERE person_id = ? AND {_OPEN} "  # noqa: S608
            f"AND id NOT IN (SELECT id FROM {TABLE} WHERE person_id = ? AND {_OPEN} "
            "ORDER BY id DESC LIMIT ?)",
            (_iso(now), person_id, person_id, MAX_HELD_PER_PERSON),
        )
        conn.commit()
        return held_id
    finally:
        conn.close()


def held_count(person_id: int, *, db_path: Path | None = None) -> int:
    """How many messages are waiting for ``person_id``. 0 on a read error."""
    try:
        conn = _connect(db_path)
        try:
            row = conn.execute(
                f"SELECT COUNT(*) FROM {TABLE} WHERE person_id = ? AND {_OPEN}",  # noqa: S608
                (person_id,),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        logger.warning("held_messages: couldn't count person %s's", person_id, exc_info=True)
        return 0
    return int(row[0] if row else 0)


def for_brief(person_id: int, *, db_path: Path | None = None) -> list[HeldMessage]:
    """Everything waiting for ``person_id``'s brief, oldest first."""
    conn = _connect(db_path)
    try:
        return _rows(conn.execute(
            f"SELECT {_COLUMNS} FROM {TABLE} WHERE person_id = ? AND {_OPEN} "  # noqa: S608
            "AND release_at IS NULL ORDER BY id",
            (person_id,),
        ))
    finally:
        conn.close()


def mark_sent(ids: list[int], *, now: datetime | None = None, db_path: Path | None = None) -> None:
    if not ids:
        return
    now = now or datetime.now(UTC)
    conn = _connect(db_path)
    try:
        conn.executemany(
            f"UPDATE {TABLE} SET sent_at = ? WHERE id = ? AND {_OPEN}",  # noqa: S608
            [(_iso(now), i) for i in ids],
        )
        conn.commit()
    finally:
        conn.close()


def release_digest_now(person_id: int, *, now: datetime, db_path: Path | None = None) -> int:
    """Turn ``person_id``'s brief-held messages into ones due at ``now``
    (they leave with the next release pass). Returns how many."""
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            f"UPDATE {TABLE} SET release_at = ? WHERE person_id = ? AND {_OPEN} "  # noqa: S608
            "AND release_at IS NULL",
            (_iso(now), person_id),
        )
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()


def schedule_digest_release(
    now: datetime, *, except_person_id: int | None, db_path: Path | None = None
) -> int:
    """When the briefs go out: everyone but ``except_person_id`` (the
    principal, whose brief carries theirs) gets their brief-held messages on
    their own. Returns how many rows were scheduled. Never raises."""
    try:
        conn = _connect(db_path)
        try:
            people = [
                int(r[0]) for r in conn.execute(
                    f"SELECT DISTINCT person_id FROM {TABLE} WHERE {_OPEN} "  # noqa: S608
                    "AND release_at IS NULL"
                ).fetchall()
            ]
        finally:
            conn.close()
        return sum(
            release_digest_now(pid, now=now, db_path=db_path)
            for pid in people
            if pid != except_person_id
        )
    except Exception:
        logger.warning("held_messages: couldn't schedule digest releases", exc_info=True)
        return 0


def _due(now: datetime, db_path: Path | None) -> dict[int, list[HeldMessage]]:
    conn = _connect(db_path)
    try:
        rows = _rows(conn.execute(
            f"SELECT {_COLUMNS} FROM {TABLE} WHERE {_OPEN} AND ("  # noqa: S608
            "(release_at IS NOT NULL AND release_at <= ?) "
            "OR (release_at IS NULL AND created_at <= ?)) ORDER BY id",
            (_iso(now), _iso(now - DIGEST_FALLBACK_AFTER)),
        ))
    finally:
        conn.close()
    by_person: dict[int, list[HeldMessage]] = {}
    for row in rows:
        by_person.setdefault(row.person_id, []).append(row)
    return by_person


def _drop(ids: list[int], now: datetime, db_path: Path | None) -> None:
    conn = _connect(db_path)
    try:
        conn.executemany(
            f"UPDATE {TABLE} SET dropped_at = ? WHERE id = ? AND {_OPEN}",  # noqa: S608
            [(_iso(now), i) for i in ids],
        )
        conn.commit()
    finally:
        conn.close()


def _postpone(ids: list[int], until: datetime, db_path: Path | None) -> None:
    conn = _connect(db_path)
    try:
        conn.executemany(
            f"UPDATE {TABLE} SET release_at = ? WHERE id = ? AND {_OPEN}",  # noqa: S608
            [(_iso(until), i) for i in ids],
        )
        conn.commit()
    finally:
        conn.close()


def render_block(messages: list[HeldMessage], *, zone: tzinfo) -> str:
    """The held messages as one "While you were away" block, oldest first,
    each with the time it would have gone."""
    lines = [f"**{HEADING}**", ""]
    for msg in messages:
        try:
            at = datetime.fromisoformat(msg.created_at).astimezone(zone)
            when = f"{at:%a} {at.hour % 12 or 12}:{at:%M} {'am' if at.hour < 12 else 'pm'}"
        except ValueError:
            when = ""
        text = msg.text.strip()
        lines.append(f"- {when}: {text}" if when else f"- {text}")
    return "\n".join(lines)


async def release_due(now: datetime, *, db_path: Path | None = None) -> int:
    """Send each person what is due to them, as one message. Returns how many
    people got one. Never raises."""
    from openexecutive.audit import log_event
    from openexecutive.audit.context import private_rows, rows_for_person
    from openexecutive.people.notifications import (
        get_notification_settings,
        in_quiet_hours,
        quiet_hours_end,
        zone_for,
    )
    from openexecutive.people.store import get_person
    from openexecutive.scheduler.runner import deliver_to_person

    try:
        due = _due(now, db_path)
    except Exception:
        logger.warning("held_messages: couldn't read what is due", exc_info=True)
        return 0
    delivered = 0
    for person_id, messages in due.items():
        ids = [m.id for m in messages]
        try:
            person = get_person(person_id)
            if person is None or person.archived:
                _drop(ids, now, db_path)
                continue
            oldest = datetime.fromisoformat(messages[0].created_at)
            if now - oldest > GIVE_UP_AFTER:
                logger.info("held_messages: dropped %d for person %s, too old", len(ids), person_id)
                _drop(ids, now, db_path)
                continue
            settings = get_notification_settings(person_id)
            if in_quiet_hours(settings, now):
                # A digest fallback, or quiet hours someone moved since: wait.
                end = quiet_hours_end(settings, now)
                if end is not None:
                    _postpone(ids, end, db_path)
                    continue
            text = render_block(messages, zone=zone_for(settings))
            rows = private_rows() if person.is_principal else rows_for_person(person_id)
            with rows:
                delivery = await deliver_to_person(person, text, label=HEADING)
        except Exception:
            logger.warning("held_messages: releasing person %s's failed", person_id, exc_info=True)
            continue
        if not delivery.ok:
            logger.info("held_messages: person %s's not sent (%s)", person_id, delivery.reason)
            continue
        try:
            mark_sent(ids, now=now, db_path=db_path)
        except Exception:
            logger.warning("held_messages: couldn't mark person %s's sent", person_id, exc_info=True)
        delivered += 1
        log_event(
            "held_messages_released",
            f"Sent person {person_id} {len(ids)} held message(s)",
            actor="scheduler",
            details={"person_id": person_id, "count": len(ids), "channel": delivery.channel},
            private=True,
            private_to_person=person_id,
        )
    return delivered


def maybe_release(now: datetime) -> bool:
    """Start a release pass when the interval has passed and none is running.
    True when one started. Never raises."""
    global _last_release_at, _task
    try:
        if _task is not None and not _task.done():
            return False
        if _last_release_at is not None and now - _last_release_at < RELEASE_EVERY:
            return False
        _last_release_at = now
        _task = asyncio.get_running_loop().create_task(release_due(now))
        return True
    except Exception:
        logger.warning("held_messages: couldn't start a release pass", exc_info=True)
        return False
