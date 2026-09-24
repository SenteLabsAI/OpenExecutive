"""Department cadence scheduler helpers.

A *cadence* is a recurring proactive action tied to a department — e.g.
``"daily@09:00"`` fires a check-in workflow every day at 09:00 UTC.

Phase 5 surfaces two public entry points:

* ``bootstrap_cadences`` — called at startup to ensure every department
  that has a cadence spec already has a pending scheduled_action row.
* ``enqueue_next`` — called by the scheduler after a cadence fires, to
  chain the next occurrence forward.

Cadence specs are stored on the ``DepartmentConfig.cadences`` dict under
the key ``"check_in"``.  Three formats are supported:

* ``"daily@HH:MM"``            — fires every day at HH:MM UTC
* ``"weekly@DOW@HH:MM"``       — fires every week on DOW (3-letter,
  or ``"weekly@DOW-HH:MM"``      e.g. ``thu``) at HH:MM UTC;
                                  either ``@`` or ``-`` may separate DOW from time
* ``"quarterly@DD-HH:MM"``     — fires on day DD of each quarter-start
                                  month (Jan/Apr/Jul/Oct) at HH:MM UTC

Anything else is treated as unknown and silently skipped with a warning.
"""
from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

# ISO weekday (Monday=0) for each 3-letter DOW token.
_DOW_MAP: dict[str, int] = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}

_DAILY_RE = re.compile(r"^daily@(\d{2}):(\d{2})$")
_WEEKLY_RE = re.compile(r"^weekly@(\w{3})[@-](\d{2}):(\d{2})$")
_QUARTERLY_RE = re.compile(r"^quarterly@(\d{2})-(\d{2}):(\d{2})$")
_QUARTER_START_MONTHS = [1, 4, 7, 10]


def _parse_cadence_spec(spec: str, after: datetime) -> datetime | None:
    """Return the next occurrence of ``spec`` that is strictly *after* ``after``.

    Returns ``None`` for unrecognised specs.  ``after`` is treated as UTC;
    the returned datetime is always UTC-aware.
    """
    if after.tzinfo is None:
        after = after.replace(tzinfo=UTC)
    after_utc = after.astimezone(UTC)

    daily_m = _DAILY_RE.match(spec)
    if daily_m:
        hour, minute = int(daily_m.group(1)), int(daily_m.group(2))
        candidate = after_utc.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= after_utc:
            candidate += timedelta(days=1)
        return candidate

    weekly_m = _WEEKLY_RE.match(spec)
    if weekly_m:
        dow_str = weekly_m.group(1).lower()
        hour, minute = int(weekly_m.group(2)), int(weekly_m.group(3))
        target_dow = _DOW_MAP.get(dow_str)
        if target_dow is None:
            logger.warning("cadence: unknown DOW token %r in spec %r", dow_str, spec)
            return None
        candidate = after_utc.replace(hour=hour, minute=minute, second=0, microsecond=0)
        days_ahead = (target_dow - after_utc.weekday()) % 7
        candidate += timedelta(days=days_ahead)
        if candidate <= after_utc:
            candidate += timedelta(weeks=1)
        return candidate

    quarterly_m = _QUARTERLY_RE.match(spec)
    if quarterly_m:
        day = int(quarterly_m.group(1))
        hour, minute = int(quarterly_m.group(2)), int(quarterly_m.group(3))
        # Walk forward through up to 5 quarter-start months until we find a
        # valid date that is strictly after `after_utc`.
        year = after_utc.year
        start_q = (after_utc.month - 1) // 3  # 0-based index of current quarter
        for offset in range(5):
            q_idx = (start_q + offset) % 4
            q_year = year + (start_q + offset) // 4
            q_month = _QUARTER_START_MONTHS[q_idx]
            try:
                candidate = datetime(q_year, q_month, day, hour, minute, 0, tzinfo=UTC)
            except ValueError:
                # day out of range for this month (e.g. day=31 in a 30-day month)
                continue
            if candidate > after_utc:
                return candidate
        logger.warning("cadence: could not compute next quarterly occurrence for %r", spec)
        return None

    logger.warning("cadence: unrecognised spec %r", spec)
    return None


CADENCE_FORMATS_HINT = (
    "daily@HH:MM, weekly@DOW@HH:MM (e.g. weekly@mon@09:00) "
    "or quarterly@DD-HH:MM, times in UTC"
)


def is_valid_cadence_spec(spec: str) -> bool:
    """Return True if ``spec`` parses to a next occurrence.

    Used to reject bad specs at save time rather than letting the scheduler
    skip them silently. An out-of-range time (``daily@25:00``) makes
    ``datetime.replace`` raise, so that counts as invalid too.
    """
    try:
        return _parse_cadence_spec(spec, datetime.now(UTC)) is not None
    except ValueError:
        return False


def _has_pending_cadence(slug: str, conn: object) -> bool:  # type: ignore[type-arg]
    """Return True if a pending or running dept_cadence row exists for slug."""
    import sqlite3 as _sqlite3
    assert isinstance(conn, _sqlite3.Connection)
    row = conn.execute(
        "SELECT 1 FROM scheduled_actions "
        "WHERE kind = 'dept_cadence' AND department = ? "
        "  AND status IN ('pending', 'running') LIMIT 1",
        (slug,),
    ).fetchone()
    return row is not None


def bootstrap_cadences(db_path: Path | None = None) -> int:
    """Ensure every department with a cadence spec has a pending action row.

    Idempotent — skips departments that already have a pending or running
    ``dept_cadence`` action.  Returns the count of newly inserted rows.
    """
    from openexecutive.departments import store as dept_store
    from openexecutive.memory.episodic import _get_conn, insert_scheduled_action

    now = datetime.now(UTC)
    inserted = 0

    states = dept_store.list_departments(db_path=db_path)
    for state in states:
        spec = state.config.cadences.get("check_in", "")
        if not spec:
            continue
        slug = state.config.slug
        run_at = _parse_cadence_spec(spec, now)
        if run_at is None:
            logger.warning("cadence.bootstrap: invalid spec %r for dept %r — skipping", spec, slug)
            continue

        from openexecutive.memory.episodic import _resolve_db_path
        resolved = _resolve_db_path(db_path)

        with _get_conn(resolved) as conn:
            if _has_pending_cadence(slug, conn):
                continue

        try:
            insert_scheduled_action(
                run_at=run_at.isoformat(),
                channel="__internal__",
                channel_ref=slug,
                intent_text=f"Department check-in: {state.config.title}",
                department=slug,
                kind="dept_cadence",
                db_path=db_path,
            )
            inserted += 1
            logger.info("cadence.bootstrap: enqueued %r at %s", slug, run_at.isoformat())
        except Exception:
            logger.exception("cadence.bootstrap: failed to enqueue %r", slug)

    return inserted


def cancel_orphaned_cadences(db_path: Path | None = None) -> int:
    """Cancel pending ``dept_cadence`` actions whose department no longer exists.

    Cleans up rows stranded by departments deleted before ``delete_department``
    learned to cancel their cadence (or removed via any other path). Without
    this, a deleted department keeps surfacing check-in alerts until its next
    occurrence fires. Idempotent; returns the count cancelled.
    """
    from openexecutive.departments import store as dept_store
    from openexecutive.memory.episodic import _get_conn, _resolve_db_path

    resolved = _resolve_db_path(db_path)
    if not resolved.exists():
        return 0

    live_slugs = {s.config.slug for s in dept_store.list_departments(db_path=db_path)}

    cancelled = 0
    with _get_conn(resolved) as conn:
        table_present = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scheduled_actions'"
        ).fetchone()
        if table_present is None:
            return 0
        rows = conn.execute(
            "SELECT id, department FROM scheduled_actions "
            "WHERE kind = 'dept_cadence' AND status = 'pending'"
        ).fetchall()
        for row in rows:
            if row["department"] not in live_slugs:
                conn.execute(
                    "UPDATE scheduled_actions SET status = 'cancelled' WHERE id = ?",
                    (row["id"],),
                )
                cancelled += 1
                logger.info(
                    "cadence.cancel_orphaned: cancelled action %d for missing dept %r",
                    row["id"], row["department"],
                )

    if cancelled:
        logger.info("cadence.cancel_orphaned: cancelled %d orphaned action(s)", cancelled)
    return cancelled


def enqueue_next(
    slug: str,
    *,
    after: datetime | None = None,
    db_path: Path | None = None,
) -> int | None:
    """Insert the next cadence occurrence for *slug*.

    Returns the new ``scheduled_action.id``, or ``None`` if the department
    doesn't exist or has no ``check_in`` cadence configured.
    """
    from openexecutive.departments import store as dept_store
    from openexecutive.memory.episodic import insert_scheduled_action

    state = dept_store.get_department(slug, db_path=db_path)
    if state is None:
        logger.warning("cadence.enqueue_next: unknown department %r", slug)
        return None

    spec = state.config.cadences.get("check_in", "")
    if not spec:
        logger.debug("cadence.enqueue_next: dept %r has no check_in cadence", slug)
        return None

    base = (after or datetime.now(UTC)).astimezone(UTC)
    run_at = _parse_cadence_spec(spec, base)
    if run_at is None:
        logger.warning("cadence.enqueue_next: invalid spec %r for dept %r", spec, slug)
        return None

    try:
        action_id = insert_scheduled_action(
            run_at=run_at.isoformat(),
            channel="__internal__",
            channel_ref=slug,
            intent_text=f"Department check-in: {state.config.title}",
            department=slug,
            kind="dept_cadence",
            db_path=db_path,
        )
        logger.info("cadence.enqueue_next: %r → %s (id=%d)", slug, run_at.isoformat(), action_id)
        return action_id
    except Exception:
        logger.exception("cadence.enqueue_next: insert failed for %r", slug)
        return None
