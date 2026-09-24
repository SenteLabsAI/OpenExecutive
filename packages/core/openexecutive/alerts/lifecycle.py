"""Alert lifecycle: time-to-live, the "live" read view, and the expiry sweep.

An alert used to leave the ``unread`` queue only when a human acked or
dismissed it, so the briefing's "Needs you" list grew without bound and the
daily briefs re-listed the same rows every day. This module gives every alert
a deterministic lifetime and one read helper every surface shares:

- :func:`ttl_days_for` — per-category TTL (monitoring vs action), from
  settings; ``None`` for sources that own their own lifecycle (artifacts
  persist in the gallery, decision-backed alerts end via approve/reject).
- :func:`is_expired` — pure predicate over an ``Alert`` + ``now``.
- :func:`list_live_alerts` — ``unread`` rows that are neither past their TTL
  nor snoozed. ``/today``, the chat ``<briefing>`` block, the briefs and the
  reflection all read through this, so the page is correct even before the
  sweep has run.
- :func:`expire_stale_alerts` — the sweep the scheduler runs every 15 minutes
  (and once at startup): flips past-TTL ``unread`` rows to ``expired`` and
  audits the batch. ``expired`` is just "not unread": the activity feed and
  the artifacts gallery keep showing the rows, and ``POST /alerts/{id}/reopen``
  brings one back.

Kept free of briefing/route imports at module level (only lazy imports inside
functions) so ``alerts.store`` stays a leaf and this module can be imported by
the scheduler, the routes and the workflows alike.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from openexecutive.alerts.models import Alert

logger = logging.getLogger(__name__)

EXPIRED_STATUS = "expired"
RESOLVED_STATUS = "resolved"

# Rows one sweep examines. Larger than any realistic unread backlog so a
# single pass converges; a bigger one just takes a second tick.
_SWEEP_PAGE = 5000

# Sources whose rows have their own lifecycle and must never be expired,
# closed or routed by the automatic machinery: artifacts persist in the
# gallery until archived; decision-backed alerts end via /decisions.
TTL_EXEMPT_SOURCES: frozenset[str] = frozenset({"artifact", "decision_scheduling"})


def parse_aware(iso: str | None) -> datetime | None:
    """Parse an ISO timestamp to an aware UTC datetime, or ``None``.

    Naive timestamps (a bare ``.isoformat()``) are assumed UTC, mirroring
    ``api/routes/today._parse_aware`` so comparisons against
    ``datetime.now(UTC)`` never raise.
    """
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _ttl_settings() -> tuple[int, int]:
    """Return ``(monitoring_days, action_days)`` from settings.

    Falls back to the documented defaults when settings cannot be built (a
    bare test DB with no env), so a lifecycle read never raises.
    """
    try:
        from openexecutive.config import get_settings

        s = get_settings()
        return int(s.alert_ttl_days_monitoring), int(s.alert_ttl_days_action)
    except Exception:
        return 3, 14


def ttl_days_for(alert: Alert, *, monitoring_days: int, action_days: int) -> int | None:
    """Days an alert may sit ``unread`` before it expires; ``None`` = never.

    ``0`` (or a negative value) for a category disables expiry for it — the
    knob an operator uses to keep an old backlog when upgrading.
    """
    if alert.source in TTL_EXEMPT_SOURCES:
        return None
    from openexecutive.briefing.ranking import categorize

    category = categorize(
        source=alert.source,
        severity=alert.severity,
        routed_to_person_id=alert.routed_to_person_id,
        topic_tags=alert.topic_tags or [],
    )
    days = monitoring_days if category == "monitoring" else action_days
    return days if days > 0 else None


def is_expired(
    alert: Alert,
    now: datetime | None = None,
    *,
    monitoring_days: int | None = None,
    action_days: int | None = None,
) -> bool:
    """True when ``alert`` is older than its category TTL."""
    if monitoring_days is None or action_days is None:
        m, a = _ttl_settings()
        monitoring_days = m if monitoring_days is None else monitoring_days
        action_days = a if action_days is None else action_days
    days = ttl_days_for(alert, monitoring_days=monitoring_days, action_days=action_days)
    if days is None:
        return False
    # A situation that keeps re-firing stays live: the age anchor is the
    # later of created_at and last_seen_at (coalescing bumps the latter).
    anchor = parse_aware(alert.created_at)
    seen = parse_aware(getattr(alert, "last_seen_at", None))
    if seen is not None and (anchor is None or seen > anchor):
        anchor = seen
    if anchor is None:
        return False
    now = now or datetime.now(UTC)
    return anchor + timedelta(days=days) <= now


def _is_snoozed(alert: Alert, now: datetime) -> bool:
    until = parse_aware(getattr(alert, "snoozed_until", None))
    return until is not None and until > now


def is_live(alert: Alert, now: datetime | None = None) -> bool:
    """Would this row show in the live queue right now? (unread, inside its
    TTL, not snoozed.) The single predicate every surface and the sweep share."""
    now = now or datetime.now(UTC)
    return alert.status == "unread" and not is_expired(alert, now) and not _is_snoozed(alert, now)


# How many live alerts every board-shaped surface takes. `/today` renders this
# many cards, and `briefing.context` trusts this many ids for `ack_alert` —
# those two MUST agree, or a card the principal can see and discuss is one the
# Executive is refused permission to clear. Shared here, with `list_live_alerts`,
# rather than duplicated as a literal at each call site.
BOARD_LIMIT = 100


def list_live_alerts(
    limit: int = 100,
    db_path: Path | None = None,
    *,
    now: datetime | None = None,
) -> list[Alert]:
    """``unread`` alerts that are still live: not past TTL, not snoozed.

    Newest first, like ``store.list_alerts``. This is the read every
    user-facing surface shares; the sweep merely persists what this view
    already hides, so the two can never disagree.
    """
    from openexecutive.alerts.store import list_alerts

    now = now or datetime.now(UTC)
    monitoring_days, action_days = _ttl_settings()
    # Over-fetch: the page is newest-first, but snoozed rows and (between
    # sweeps) freshly-expired rows are interleaved with live ones, so a
    # `limit`-sized page could come back short.
    rows = list_alerts(status="unread", limit=max(limit * 3, limit + 50), db_path=db_path)
    live = [
        a for a in rows
        if not is_expired(a, now, monitoring_days=monitoring_days, action_days=action_days)
        and not _is_snoozed(a, now)
    ]
    return live[:limit]


def expire_stale_alerts(
    now: datetime | None = None,
    db_path: Path | None = None,
) -> int:
    """Flip every past-TTL ``unread`` alert to ``expired``. Returns the count.

    Audited as ``alert_sweep`` (actor ``scheduler``) whenever at least one row
    changed, with the ids so the reopen path is one click away. Never raises:
    a sweep failure is logged and reported as ``0`` so the scheduler tick
    continues.
    """
    from openexecutive.alerts.store import bulk_set_status, list_alerts

    now = now or datetime.now(UTC)
    try:
        monitoring_days, action_days = _ttl_settings()
        rows = list_alerts(status="unread", limit=_SWEEP_PAGE, db_path=db_path)
        expired_ids = [
            a.id for a in rows
            if a.id is not None
            and is_expired(a, now, monitoring_days=monitoring_days, action_days=action_days)
        ]
        if not expired_ids:
            return 0
        changed = bulk_set_status(
            EXPIRED_STATUS, alert_ids=expired_ids, only_status="unread", db_path=db_path
        )
        count = len(changed)
    except Exception:
        logger.exception("alerts.lifecycle: expiry sweep failed")
        return 0
    # An alert nobody acted on before its TTL didn't matter: void the review's
    # DMs about it rather than leave them counted as ignored.
    for alert_id in expired_ids:
        resolve_alert_outreach(_AlertRef(alert_id), EXPIRED_STATUS)

    if count:
        try:
            from openexecutive.audit import log_event as audit_log

            audit_log(
                "alert_sweep",
                f"Expired {count} stale alert(s) past TTL",
                actor="scheduler",
                details={
                    "count": count,
                    "alert_ids": expired_ids[:50],
                    "ttl_days_monitoring": monitoring_days,
                    "ttl_days_action": action_days,
                },
            )
        except Exception:
            logger.exception("alerts.lifecycle: audit log failed")
    return count


def mute_pattern_for(alert: Alert) -> str | None:
    """The topic pattern "mute this topic" should add for an alert.

    Prefers the watch slug (``external:<slug>``), then a ``department:``
    tag, then the first topic tag. ``None`` when the alert carries no tags.
    """
    from openexecutive.briefing.ranking import watch_slug_from_tags

    tags = list(alert.topic_tags or [])
    slug = watch_slug_from_tags(tags)
    if slug is not None:
        return f"external:{slug}"
    for tag in tags:
        if tag.startswith("department:"):
            return tag
    return tags[0] if tags else None


class _AlertRef:
    """Just enough of an Alert for :func:`resolve_alert_outreach` to void it."""

    def __init__(self, alert_id: int) -> None:
        self.id: int | None = alert_id
        self.routed_to_person_id: int | None = None


def resolve_alert_outreach(alert: Alert | _AlertRef, status: str) -> None:
    """Resolve the Attunement outcome rows for review DMs about ``alert``.

    Acknowledged or resolved: the DMs to the person it is routed to (and to
    the principal, who is escalated to) landed. Dismissed or stale: the alert
    didn't matter, so those DMs are voided — counted neither way. Never
    attributes an outcome to anyone else the alert was once DM'd to, and a
    dismissal can never lower anyone's rate. Never raises."""
    if alert.id is None:
        return
    try:
        from openexecutive.attunement.outcomes import OUTCOME_ACTED, OUTCOME_VOID, resolve_by_ref
        from openexecutive.people.store import find_principal_person

        ref = f"alert:{alert.id}"
        if status in ("ack", "resolved"):
            owners = {pid for pid in (getattr(alert, "routed_to_person_id", None),) if pid}
            principal = find_principal_person()
            if principal is not None and principal.id is not None:
                owners.add(principal.id)
            resolve_by_ref(ref, OUTCOME_ACTED, person_ids=owners)
        elif status in ("dismissed", "stale", "expired"):
            resolve_by_ref(ref, OUTCOME_VOID)
    except Exception:
        logger.debug("resolve_alert_outreach failed", exc_info=True)


def record_ack_feedback(alert: Alert, status: str) -> None:
    """Teach the source of an alert from the principal's verdict.

    ``dismissed`` on a watch-sourced alert lowers that watch's trust score
    (and bumps its dismiss_count); ``ack`` recovers it a little. Other
    sources have no feedback sink yet. Never raises.
    """
    from openexecutive.briefing.ranking import watch_slug_from_tags

    resolve_alert_outreach(alert, status)

    slug = watch_slug_from_tags(list(alert.topic_tags or []))
    if slug is None:
        return
    try:
        from openexecutive.monitoring import store as monitoring_store

        if status == "dismissed":
            monitoring_store.record_dismissal(slug)
        elif status == "ack":
            monitoring_store.record_confirmation(slug)
    except Exception:
        logger.debug("alerts.lifecycle: ack feedback failed for %s", slug, exc_info=True)


_MUTE_MIN_CHARS = 4


def mute_pattern_allowed(alert: Alert, pattern: str) -> bool:
    """A mute added from a card must be specific: one of that alert's own
    tags, or an ``external:`` / ``department:`` scoped pattern of at least
    ``_MUTE_MIN_CHARS`` characters. Mutes are substring matches over every
    future alert's tags, so ``"e"`` would silence the company."""
    p = pattern.strip().lower()
    if len(p) < _MUTE_MIN_CHARS:
        return False
    tags = {t.lower() for t in (alert.topic_tags or [])}
    if p in tags:
        return True
    return p.startswith(("external:", "department:")) and len(p.split(":", 1)[1]) >= 2


def apply_mute_request(alert: Alert, mute_topic: str | bool | None) -> str | None:
    """Handle the optional ``mute_topic`` on a dismiss: an explicit pattern,
    or ``True`` to derive one from the alert's tags. Returns the pattern
    added, or ``None`` (also when the pattern is too broad to allow)."""
    if not mute_topic:
        return None
    pattern = mute_topic if isinstance(mute_topic, str) else mute_pattern_for(alert)
    if not pattern or not mute_pattern_allowed(alert, pattern):
        return None
    from openexecutive.alerts.store import add_mute

    try:
        add_mute(pattern)
    except Exception:
        logger.exception("alerts.lifecycle: add_mute failed for %r", pattern)
        return None
    return pattern


__all__ = [
    "EXPIRED_STATUS",
    "apply_mute_request",
    "mute_pattern_allowed",
    "mute_pattern_for",
    "record_ack_feedback",
    "RESOLVED_STATUS",
    "TTL_EXEMPT_SOURCES",
    "expire_stale_alerts",
    "is_expired",
    "is_live",
    "list_live_alerts",
    "parse_aware",
    "ttl_days_for",
]
