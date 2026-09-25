"""Delivered-brief state: what the last morning/EoD brief covered.

The principal briefs used to re-list the whole unread queue every day and
call the "activity" block "since last brief" when it was really just the
latest N rows. This module gives each recurring brief kind a memory of what
was last *delivered* so the workflow can:

- bound "what changed" to the window since the previous delivery (`since`),
- split proposals into NEW (created inside that window) vs CARRIED OVER,
- list what the Executive's alert review handled inside the window, and
- skip the model call entirely when the fingerprint of the inputs is
  unchanged, sending a one-line "nothing new" instead.

Storage reuses the ``briefing_narrative`` table (``briefing/narrative_cache``)
under a ``brief:<kind>`` scope: ``input_hash`` holds the fingerprint,
``narrative_text`` the delivered artifact and ``generated_at`` the delivery
time. Scopes are only ever read by exact key, so the namespace cannot
collide with the per-viewer header cache. Only the scheduler records a
delivery (after a successful send), so manual workflow runs never advance
the window.

Each send's outcome, delivered or not, goes under ``brief_delivery:<kind>``
(``record_delivery_outcome``): the reason in ``input_hash`` and the channel
that sent it in ``narrative_text`` — a channel name, never an address. The
Briefing's "not sent" notice and the Setup status page read it back.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast, get_args

from openexecutive.alerts.lifecycle import parse_aware
from openexecutive.briefing import narrative_cache

logger = logging.getLogger(__name__)

SCOPE_PREFIX = "brief:"
DEFAULT_WINDOW = timedelta(hours=24)

SUPPRESSED_TEMPLATE = (
    "Nothing new since yesterday's brief — {n} item{s} still waiting on you."
)

# Audit event types the alert review job emits for autonomous moves. The
# brief's "handled overnight" block is built from these (see handled_since).
REVIEW_EVENT_TYPES: tuple[str, ...] = (
    "alert_review_closed",
    "alert_review_routed",
    "alert_review_nudged",
    "alert_review_escalated",
    "alert_review_drafted",
    "alert_review_merged",
    "alert_review_suggested_workflow",
    "alert_review_changed",
)

# Review events that are bookkeeping on a still-open alert, not a completed
# move. A `changed` verdict rewrites the card's text in place — the alert
# stays in "Needs you" and the card itself shows the note — so listing it
# under "handled" double-reports it and mislabels it as done. It renders in
# the brief's REWRITTEN block instead (see rewritten_since).
_NOT_HANDLED_EVENT_TYPES: frozenset[str] = frozenset({"alert_review_changed"})

# Every audit event type the handled block reads, mapped to the short kind
# the brief and the /today rail render. The research watch policy's
# autonomous moves ride alongside the alert review's.
HANDLED_EVENT_KINDS: dict[str, str] = {
    **{
        t: t.removeprefix("alert_review_")
        for t in REVIEW_EVENT_TYPES
        if t not in _NOT_HANDLED_EVENT_TYPES
    },
    "watchlist_research_added": "watching",
    "watchlist_auto_disabled": "stopped_watching",
}

# The nudge audit summary opens with "[alert N] " so `alerts.review` can count
# delivered nudges per alert with a text query; it is bookkeeping, not prose.
_ALERT_MARKER_RE = re.compile(r"^\[alert \d+\]\s*")


def scope_for(kind: str) -> str:
    return f"{SCOPE_PREFIX}{kind}"


def last_delivered(kind: str) -> narrative_cache.BriefingNarrative | None:
    """The last delivered brief of this kind, or None on a cold store."""
    try:
        return narrative_cache.get(scope_for(kind))
    except Exception:
        logger.exception("brief_state: read failed for %s", kind)
        return None


def since_for(kind: str, now: datetime | None = None) -> datetime:
    """Start of the "what changed" window: the previous delivery, else 24 h ago.

    Bounded to at most 7 days back so a brief that stopped firing for a while
    does not replay a month of history when it resumes.
    """
    now = now or datetime.now(UTC)
    prev = last_delivered(kind)
    delivered_at = parse_aware(prev.generated_at) if prev else None
    if delivered_at is None:
        return now - DEFAULT_WINDOW
    # Never in the future (clock skew / a scheduler `now` earlier than the
    # recorded delivery would otherwise empty the window and suppress).
    return min(now, max(delivered_at, now - timedelta(days=7)))


def record_delivered(kind: str, fingerprint: str, text: str) -> None:
    """Persist the delivered brief so the next run can diff against it."""
    try:
        narrative_cache.put(narrative_cache.BriefingNarrative(
            scope=scope_for(kind),
            input_hash=fingerprint,
            narrative_text=text,
            generated_at=narrative_cache.utc_now_iso(),
        ))
    except Exception:
        logger.exception("brief_state: write failed for %s", kind)


# How a brief's last send went (scheduler.runner.PrincipalDelivery.reason).
DeliveryReason = Literal["delivered", "no_owner", "no_channel", "send_failed"]
_DELIVERY_REASONS: frozenset[str] = frozenset(get_args(DeliveryReason))
DELIVERY_SCOPE_PREFIX = "brief_delivery:"
# The recurring briefs whose sends are recorded, and their names in the app.
BRIEF_NAMES: dict[str, str] = {
    "principal_brief_morning": "morning brief",
    "principal_brief_eod": "end-of-day digest",
}
# Delivery channels (scheduler.runner._delivery_order), as in "sent to you
# by email".
CHANNEL_PHRASES: dict[str, str] = {
    "email": "by email",
    "slack_dm": "on Slack",
    "discord_dm": "on Discord",
    "telegram": "on Telegram",
}
# Why a brief wasn't sent, and what to do about it, in the user's words.
DELIVERY_PROBLEMS: dict[str, tuple[str, str]] = {
    "no_owner": (
        "there's no owner on the People list to send it to",
        "Finish setup so you're on the People list as the owner.",
    ),
    "no_channel": (
        "nothing is set up to send it to you",
        "Connect Gmail, or add your Slack, Telegram or Discord to your People profile.",
    ),
    "send_failed": (
        "every way of sending it failed",
        "The Setup status page shows which connection needs attention.",
    ),
}


@dataclass(frozen=True)
class DeliveryOutcome:
    kind: str
    reason: DeliveryReason
    # The delivery channel that sent it ("email", "slack_dm", ...), if one did.
    channel: str | None
    at: datetime


def record_delivery_outcome(
    kind: str, *, reason: DeliveryReason, channel: str | None
) -> None:
    """Remember how this brief's latest send went. Never raises."""
    try:
        narrative_cache.put(narrative_cache.BriefingNarrative(
            scope=f"{DELIVERY_SCOPE_PREFIX}{kind}",
            input_hash=reason,
            narrative_text=channel or "",
            generated_at=narrative_cache.utc_now_iso(),
        ))
    except Exception:
        logger.exception("brief_state: delivery outcome write failed for %s", kind)


def last_delivery_outcome() -> DeliveryOutcome | None:
    """The latest send of either recurring brief, or None when neither has
    been sent yet (or the store can't be read). Never raises."""
    latest: DeliveryOutcome | None = None
    for kind in BRIEF_NAMES:
        try:
            row = narrative_cache.get(f"{DELIVERY_SCOPE_PREFIX}{kind}")
        except Exception:
            logger.exception("brief_state: delivery outcome read failed for %s", kind)
            continue
        at = parse_aware(row.generated_at) if row is not None else None
        if row is None or at is None or row.input_hash not in _DELIVERY_REASONS:
            continue
        outcome = DeliveryOutcome(
            kind=kind,
            reason=cast(DeliveryReason, row.input_hash),  # checked above
            channel=row.narrative_text or None,
            at=at,
        )
        if latest is None or outcome.at > latest.at:
            latest = outcome
    return latest


def still_unsent(outcome: DeliveryOutcome | None, *, can_deliver: bool) -> bool:
    """Whether ``outcome`` is a failure the user still needs to hear about: a
    send that failed, or a brief that had nowhere to go while that is still
    true (``can_deliver``: a channel exists now, so the next one will go)."""
    if outcome is None or outcome.reason == "delivered":
        return False
    return outcome.reason == "send_failed" or not can_deliver


def split_proposals(
    proposals: list[dict[str, Any]], since: datetime | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(new, carried)``: proposals created at/after ``since`` vs earlier.

    With ``since`` None everything is "new" (the legacy single-list view).
    """
    if since is None:
        return list(proposals), []
    new: list[dict[str, Any]] = []
    carried: list[dict[str, Any]] = []
    for p in proposals:
        created = parse_aware(p.get("created_at"))
        (new if created is None or created >= since else carried).append(p)
    return new, carried


def rewritten_since(
    proposals: list[dict[str, Any]], since: datetime | None
) -> list[dict[str, Any]]:
    """Open proposals the review rewrote (verdict ``changed``) at/after ``since``.

    These are the alerts that dropped out of the handled block: still open,
    text or severity refreshed by the Executive. The brief reports them under
    "what changed", never as done. Empty when ``since`` is None.
    """
    if since is None:
        return []
    out: list[dict[str, Any]] = []
    for p in proposals:
        if p.get("review_verdict") != "changed":
            continue
        # Fail open like split_proposals: a rewrite with no readable stamp is
        # reported rather than dropped from every block.
        reviewed = parse_aware(p.get("last_reviewed_at"))
        if reviewed is None or reviewed >= since:
            out.append(p)
    return out


def rewritten_lines(
    proposals: list[dict[str, Any]], since: datetime | None, limit: int = 10
) -> list[str]:
    """Bullet lines for the briefs' REWRITTEN block (one per rewritten open
    proposal): ``- <headline> — <review note>``. Shared by the morning brief
    and the end-of-day digest so the two never drift."""
    return [
        f"- {str(p.get('headline', ''))[:160]} — {str(p.get('review_note', ''))[:120]}"
        for p in rewritten_since(proposals, since)[:limit]
    ]


def handled_since(since: datetime, limit: int = 20) -> list[dict[str, Any]]:
    """Completed autonomous moves recorded in the audit log since ``since``.

    Each item: ``{"kind": event_type sans prefix, "event_type": str,
    "summary": str, "at": iso, "alert_id": int | None, "details": dict}``,
    newest first. ``summary`` has the nudge bookkeeping marker stripped;
    ``details`` is the audit row's structured payload (headline, target
    person, evidence ref, new status …) for callers that render more than
    one line. Empty when the audit store is unavailable.
    """
    try:
        from openexecutive.audit.logger import get_audit_logger

        logger_ = get_audit_logger()
        out: list[dict[str, Any]] = []
        for event_type, kind in HANDLED_EVENT_KINDS.items():
            for ev in logger_.query(event_type=event_type, since=since.isoformat(), limit=limit):
                details = ev.details if isinstance(ev.details, dict) else {}
                out.append({
                    "kind": kind,
                    "event_type": event_type,
                    "summary": _ALERT_MARKER_RE.sub("", ev.summary or ""),
                    "at": ev.ts,
                    "alert_id": details.get("alert_id"),
                    "details": details,
                })
        out.sort(key=lambda e: e["at"], reverse=True)
        return out[:limit]
    except Exception:
        logger.debug("brief_state: handled_since unavailable", exc_info=True)
        return []


def build_brief_fingerprint(
    *,
    today_data: dict[str, Any],
    activity: list[dict[str, Any]],
    handled: list[dict[str, Any]],
    since: datetime | None,
    pending_watch_suggestions: int = 0,
) -> str:
    """Stable hash of everything the brief would say. Deliberately free of
    dates and timestamps so an unchanged day yields the same fingerprint
    tomorrow (activity is keyed by kind + summary, never by its stamp)."""
    new, carried = split_proposals(today_data.get("proposals", []), since)
    payload = {
        "new": sorted(int(p.get("alert_id") or 0) for p in new),
        "carried": sorted(int(p.get("alert_id") or 0) for p in carried),
        "likely_stale": sum(1 for p in carried if p.get("review_verdict") == "likely_stale"),
        "activity": sorted(
            (str(a.get("kind", "")), str(a.get("summary", ""))[:80]) for a in activity
        ),
        "handled": sorted((h["kind"], h["summary"][:80]) for h in handled),
        # A rewrite alone must still un-suppress the brief now that it no
        # longer rides in `handled` (keyed on the note, never the stamp). Same
        # list the REWRITTEN block renders: carried items only — a new item
        # already moves the fingerprint by id.
        "rewritten": sorted(
            (int(p.get("alert_id") or 0), str(p.get("review_note", ""))[:80])
            for p in rewritten_since(carried, since)
        ),
        "depts": sorted(
            (d.get("slug", ""), d.get("at_risk_count", 0), d.get("off_track_count", 0))
            for d in today_data.get("departments", [])
            if d.get("at_risk_count", 0) or d.get("off_track_count", 0)
        ),
        "awaiting": sorted(
            int(p.get("id", 0)) for p in today_data.get("people", []) if p.get("awaiting_count", 0)
        ),
        "watch_suggestions": int(pending_watch_suggestions),
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def suppress_unchanged_enabled() -> bool:
    """`PRINCIPAL_BRIEF_SUPPRESS_UNCHANGED`, defaulting to on when settings
    cannot be built (bare test DB with no env)."""
    try:
        from openexecutive.config import get_settings

        return bool(get_settings().principal_brief_suppress_unchanged)
    except Exception:
        return True


def suppressed_line(n_waiting: int) -> str:
    return SUPPRESSED_TEMPLATE.format(n=n_waiting, s="" if n_waiting == 1 else "s")


def pending_watch_suggestions() -> int:
    """Research watch suggestions awaiting the principal on /watchlist.
    Zero when the monitoring store is unavailable."""
    try:
        from openexecutive.monitoring import store as monitoring_store

        return len(monitoring_store.list_pending_suggestions())
    except Exception:
        logger.debug("brief_state: pending_watch_suggestions unavailable", exc_info=True)
        return 0


__all__ = [
    "HANDLED_EVENT_KINDS",
    "REVIEW_EVENT_TYPES",
    "SUPPRESSED_TEMPLATE",
    "build_brief_fingerprint",
    "handled_since",
    "last_delivered",
    "pending_watch_suggestions",
    "record_delivered",
    "rewritten_lines",
    "rewritten_since",
    "scope_for",
    "since_for",
    "split_proposals",
    "suppress_unchanged_enabled",
    "suppressed_line",
]
