"""Department cadence scheduler helpers.

A *cadence* is a recurring proactive action tied to a department — e.g.
``"daily@09:00"`` fires a check-in workflow every day at 09:00 UTC.

Department cadences are always read in UTC. ``_parse_cadence_spec`` also
takes a zone, which the principal's own rhythm (the briefs and the
reflection in ``scheduler.runner``) uses to fire at a local wall-clock time.

In solo mode (``memory.workspace_settings``) there are no department
check-ins: ``bootstrap_cadences`` does nothing.

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
from datetime import UTC, date, datetime, timedelta, tzinfo
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


def _at_local(day: date, hour: int, minute: int, tz: tzinfo) -> datetime:
    """The UTC instant of wall-clock ``hour:minute`` on ``day`` in ``tz``.

    DST-safe by construction: the wall-clock time is built in the zone and
    only then converted, so it stays at the same local time across a
    transition. A time the spring-forward gap skips (02:30 in New York in
    March) resolves to the instant just after the jump (03:30 local); a time
    the fall-back repeats (01:30 in November) resolves to its first
    occurrence — ``fold=0`` semantics either way. Raises ``ValueError`` for an
    out-of-range hour or minute.
    """
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz).astimezone(UTC)


def _parse_cadence_spec(
    spec: str, after: datetime, tz: tzinfo = UTC
) -> datetime | None:
    """Return the next occurrence of ``spec`` that is strictly *after* ``after``.

    The spec's times of day are wall-clock times in ``tz`` (UTC by default,
    which is what department and workflow cadences use). The result is
    always UTC-aware. A naive ``after`` is treated as UTC. Returns ``None``
    for unrecognised specs.
    """
    if after.tzinfo is None:
        after = after.replace(tzinfo=UTC)
    after_utc = after.astimezone(UTC)
    after_local = after.astimezone(tz)
    today = after_local.date()

    daily_m = _DAILY_RE.match(spec)
    if daily_m:
        hour, minute = int(daily_m.group(1)), int(daily_m.group(2))
        candidate = _at_local(today, hour, minute, tz)
        if candidate <= after_utc:
            candidate = _at_local(today + timedelta(days=1), hour, minute, tz)
        return candidate

    weekly_m = _WEEKLY_RE.match(spec)
    if weekly_m:
        dow_str = weekly_m.group(1).lower()
        hour, minute = int(weekly_m.group(2)), int(weekly_m.group(3))
        target_dow = _DOW_MAP.get(dow_str)
        if target_dow is None:
            logger.warning("cadence: unknown DOW token %r in spec %r", dow_str, spec)
            return None
        day = today + timedelta(days=(target_dow - today.weekday()) % 7)
        candidate = _at_local(day, hour, minute, tz)
        if candidate <= after_utc:
            candidate = _at_local(day + timedelta(weeks=1), hour, minute, tz)
        return candidate

    quarterly_m = _QUARTERLY_RE.match(spec)
    if quarterly_m:
        day_of_month = int(quarterly_m.group(1))
        hour, minute = int(quarterly_m.group(2)), int(quarterly_m.group(3))
        # Walk forward through up to 5 quarter-start months until we find a
        # valid date that is strictly after `after`.
        year = today.year
        start_q = (today.month - 1) // 3  # 0-based index of current quarter
        for offset in range(5):
            q_idx = (start_q + offset) % 4
            q_year = year + (start_q + offset) // 4
            q_month = _QUARTER_START_MONTHS[q_idx]
            try:
                candidate = _at_local(
                    date(q_year, q_month, day_of_month), hour, minute, tz
                )
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

    A no-op in solo mode. The check lives here rather than at a call site
    because the lifespan, the fixture reset and client-slot activation all
    call this, and none of them should start department check-ins for
    someone using Open Executive just for themselves.
    """
    from openexecutive.departments import store as dept_store
    from openexecutive.memory.episodic import _get_conn, insert_scheduled_action
    from openexecutive.memory.workspace_settings import get_workspace

    if get_workspace(db_path).mode == "solo":
        logger.info("cadence.bootstrap: solo workspace — no department check-ins")
        return 0

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


def cancel_pending_cadences(db_path: Path | None = None) -> int:
    """Cancel every pending ``dept_cadence`` action (switching to solo mode).

    A running row is left alone: it finishes, and ``enqueue_next`` does not
    chain another in solo. Idempotent; safe when no DB or table exists.
    Returns the count cancelled.
    """
    from openexecutive.memory.episodic import _get_conn, _resolve_db_path

    resolved = _resolve_db_path(db_path)
    if not resolved.exists():
        return 0
    with _get_conn(resolved) as conn:
        table_present = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scheduled_actions'"
        ).fetchone()
        if table_present is None:
            return 0
        cancelled = conn.execute(
            "UPDATE scheduled_actions SET status = 'cancelled' "
            "WHERE kind = 'dept_cadence' AND status = 'pending'"
        ).rowcount
    if cancelled:
        logger.info("cadence.cancel_pending: cancelled %d department check-in(s)", cancelled)
    return cancelled


def enqueue_next(
    slug: str,
    *,
    after: datetime | None = None,
    db_path: Path | None = None,
) -> int | None:
    """Insert the next cadence occurrence for *slug*.

    Returns the new ``scheduled_action.id``, or ``None`` if the department
    doesn't exist, has no ``check_in`` cadence configured, or the workspace
    is solo — a check-in that was already running when the install switched
    to solo finishes but does not schedule another.
    """
    from openexecutive.departments import store as dept_store
    from openexecutive.memory.episodic import insert_scheduled_action
    from openexecutive.memory.workspace_settings import get_workspace

    if get_workspace(db_path).mode == "solo":
        logger.info("cadence.enqueue_next: solo workspace — not chaining %r", slug)
        return None

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
