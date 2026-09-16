"""GET /today — live dashboard summary for the UI.

Returns a JSON snapshot of:
  • departments — one entry per department with Goal health summary and
    awaiting workflow count
  • people — compact roster with awaiting-action counts and next-SLA time
  • proposals — every unread alert (workflow approvals, triage-classified
    inbound, Executive create_alert calls). The UI splits this into
    "Needs you" vs "Across the team" using caller_person_id plus a
    "principal owns unrouted" rule for alerts with no routed_to_person_id.

The legacy path `/morning-brief` is kept as a deprecated alias for one release
so older UI builds and bookmarked URLs keep working.

GET /today/activity returns the Executive's recent self-initiated activity —
fired scheduled_actions (DMs sent, follow-ups dispatched, cadences run),
plus decisions and advice the system has logged. Powers the "Recent
activity" rail on the briefing-first landing.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from openexecutive.clients.cockpit import ClientCockpitCard, format_practice_for_today

if TYPE_CHECKING:
    from openexecutive.people.models import Person

logger = logging.getLogger(__name__)

# A person whose cached insight note is stale and needs background regen:
# (person, signals-dict, input-hash). Built in `_build_today`, consumed by
# `_regen_stale_insights` after the response is sent.
StaleInsight = tuple["Person", dict[str, Any], str]

# Most attention-worthy (off-track/at-risk) goals shown inline on a Department
# card before the card defers the rest to the department page.
_DEPT_ATTENTION_GOAL_CAP = 3

router = APIRouter()


# --------------------------------------------------------------------------- #
# Response models
# --------------------------------------------------------------------------- #

class GoalBrief(BaseModel):
    """A single attention-worthy goal, surfaced inline on the briefing's
    Department card so the principal sees *which* goal is off and how far —
    `current` vs `target` — without opening the department."""

    key_result: str
    current: str
    target: str
    status: str  # "at_risk" | "off_track" (the only statuses surfaced here)


class DepartmentBriefItem(BaseModel):
    slug: str
    title: str
    authority_level: str
    goal_count: int
    at_risk_count: int
    off_track_count: int
    awaiting_count: int
    # The off-track/at-risk goals themselves (worst first), capped, so the card
    # is insightful at rest. Empty for healthy/inactive departments. Additive —
    # defaults to empty so older clients and test mocks keep working.
    attention_goals: list[GoalBrief] = Field(default_factory=list)


class PersonBriefItem(BaseModel):
    id: int
    full_name: str
    role: str
    is_principal: bool
    preferred_channel: str
    awaiting_count: int
    soonest_sla_at: str | None
    # Enrichment — attention, recency, and routing signals the brief surfaces
    # so the roster is more than a name list. All additive; consumers that
    # only read the original fields (e.g. workflows/morning_brief.py) are
    # unaffected.
    status: str  # on_leave | needs_reply | awaiting | clear
    awaiting_reply_count: int  # open commitments we're waiting on THEM to answer
    oldest_awaiting_reply_at: str | None
    on_leave_until: str | None
    reachable_now: bool
    next_window_at: str | None
    authority_scope: list[str]
    department_slugs: list[str]
    last_contact_at: str | None
    overdue: bool  # any SLA / awaited-reply past its deadline → UI red emphasis
    priority: int  # server-computed sort key; higher = needs attention sooner
    insight: str | None  # utility-fast note, served from cache (None until warm)


class ProposalItem(BaseModel):
    alert_id: int
    headline: str
    # Full intent text behind the proposal. `headline` is a 160-char
    # excerpt used as the card title; `body` is the untruncated text
    # the UI seeds into the chat handoff so the Executive has the
    # full context when the user taps the card.
    body: str
    routed_to_person_id: int | None
    suggested_action: str
    created_at: str
    topic_tags: list[str]
    # Presentation signals (see openexecutive.briefing.ranking). `score` is
    # an attention sort key (higher leads); `category` is "action" (a human
    # should look) or "monitoring" (passive watchlist/external noise the UI
    # can collapse so it stops crowding the "Needs you" queue).
    score: int = 0
    category: str = "action"
    # Why this item is in "Needs you" rather than Monitoring — set only when
    # an external/watchlist signal was pulled into the action lane by its
    # severity (large stock move, etc.). Null for everything else; the UI
    # renders it as a small note so the promotion reads as deliberate.
    surfaced_reason: str | None = None
    # Set when this proposal is backed by a decision_instance (a gated
    # calendar booking awaiting approval). The UI routes Approve/Reject to
    # the /decisions endpoints (which book/cancel server-side) instead of the
    # ack-and-handoff-to-chat flow. Null for ordinary alert-backed proposals.
    decision_instance_id: int | None = None
    # Lifecycle signals (alerts/lifecycle.py, alerts/review.py). Coalescing:
    # how many times this situation re-fired and when it was last seen.
    occurrence_count: int = 1
    last_seen_at: str | None = None
    # The Executive's latest review of this item: verdict ('' | relevant |
    # changed | likely_stale | drafted | routed | merged; closed rows carry
    # resolved | stale but never reach this list), a one-line "what changed
    # since you last looked", the next move the card should lead with, a
    # short "why now", and a deadline when one exists.
    last_reviewed_at: str | None = None
    review_verdict: str = ""
    review_note: str = ""
    recommended_move: str = ""
    why_now: str = ""
    due_at: str | None = None
    # Rows the review folded into this one (merge).
    superseded_count: int = 0
    # Registry workflow the review suggested as the next step ('' = none).
    suggested_workflow: str = ""


def _as_int(raw: Any) -> int | None:
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _alert_status_now(alert_id: int | None, now: datetime) -> str:
    """Current lifecycle state of the alert a handled row touched.

    ``open`` means exactly "in the /today queue right now" (`lifecycle.is_live`:
    unread, inside its TTL, not snoozed) — the only state where a "still open"
    jump link has a card to land on and where a close row can be read as
    undone. ``acked`` is the principal's own approval (not an Undo target),
    ``inactive`` a snoozed / TTL-expired row the sweep has not closed yet.
    """
    if alert_id is None:
        return ""
    try:
        from openexecutive.alerts import lifecycle
        from openexecutive.alerts.store import get_alert

        alert = get_alert(alert_id)
    except Exception:
        logger.debug("today: alert status lookup failed for %s", alert_id, exc_info=True)
        return ""
    if alert is None:
        return ""
    if alert.superseded_by_alert_id is not None:
        return "merged"
    if alert.status in {"resolved", "dismissed", "expired"}:
        return alert.status
    if alert.status == "ack":
        return "acked"
    return "open" if lifecycle.is_live(alert, now) else "inactive"


# Which audit-details keys fill a handled row's `target` and `detail`, per
# kind. `headline` defaults to the alert headline; watch rows use the slug.
_HANDLED_TARGET_KEY: dict[str, str] = {
    "routed": "target_person_name",
    "nudged": "target_person_name",
    "escalated": "new_severity",
    "drafted": "draft_title",
    "merged": "superseded_by_headline",
    "suggested_workflow": "workflow_name",
}
_HANDLED_DETAIL_KEYS: dict[str, tuple[str, ...]] = {
    "closed": ("evidence",),
    "escalated": ("evidence",),
    "watching": ("rationale", "reason"),
    "stopped_watching": ("rationale", "reason"),
}
_WATCH_KINDS = frozenset({"watching", "stopped_watching"})


def _handled_outcome(kind: str, details: dict[str, Any]) -> str:
    """resolved | dismissed for a close, proposed for a gated route, else ''."""
    if kind == "closed":
        return str(details.get("new_status") or "")
    if kind == "routed" and details.get("proposed"):
        return "proposed"
    return ""


def _handled_item(h: dict[str, Any], now: datetime) -> HandledItem:
    """One structured rail row from a `brief_state.handled_since` entry."""
    kind = str(h["kind"])
    raw_details = h.get("details")
    details: dict[str, Any] = raw_details if isinstance(raw_details, dict) else {}
    alert_id = _as_int(h.get("alert_id"))
    headline = details.get("slug") if kind in _WATCH_KINDS else details.get("headline")
    target_key = _HANDLED_TARGET_KEY.get(kind)
    target = details.get(target_key) if target_key else None
    detail = next(
        (str(details[k]) for k in _HANDLED_DETAIL_KEYS.get(kind, ()) if details.get(k)), ""
    )
    return HandledItem(
        kind=kind,
        summary=str(h["summary"]),
        at=str(h["at"]),
        alert_id=alert_id,
        event_type=str(h.get("event_type") or ""),
        headline=str(headline) if headline else None,
        target=str(target) if target else None,
        detail=detail,
        outcome=_handled_outcome(kind, details),
        evidence_ref=str(details.get("evidence_ref") or ""),
        superseded_by_alert_id=_as_int(details.get("superseded_by_alert_id")),
        status=_alert_status_now(alert_id, now),
    )


def _handled_overnight(now: datetime) -> list[HandledItem]:
    """What the alert review completed since the last delivered morning brief."""
    try:
        from openexecutive.briefing import brief_state

        since = brief_state.since_for("principal_brief_morning", now=now)
        return [_handled_item(h, now) for h in brief_state.handled_since(since, limit=20)]
    except Exception:
        logger.debug("today: handled_overnight unavailable", exc_info=True)
        return []


def _watch_trust_by_slug(alerts: list[Any]) -> dict[str, float]:
    """``{watch slug: trust_score}`` for every watch referenced by ``alerts``.

    One lookup per distinct slug so ranking can discount watches the
    principal keeps dismissing. Never raises — a missing monitoring store
    just yields an empty map (no discount).
    """
    from openexecutive.briefing.ranking import watch_slug_from_tags

    slugs = {
        slug for a in alerts
        if (slug := watch_slug_from_tags(list(getattr(a, "topic_tags", []) or []))) is not None
    }
    if not slugs:
        return {}
    try:
        from openexecutive.monitoring import store as monitoring_store

        out: dict[str, float] = {}
        for slug in slugs:
            item = monitoring_store.get_watchlist_item_by_slug(slug)
            if item is not None:
                out[slug] = float(getattr(item, "trust_score", 1.0))
        return out
    except Exception:
        logger.debug("today: watch trust lookup failed", exc_info=True)
        return {}


def _parse_decision_instance_id(topic_tags: list[str]) -> int | None:
    """Extract the decision_instance id from a ``decision_instance:{id}`` tag.

    Returns None when the tag is absent or its suffix isn't an integer.
    """
    from openexecutive.memory.decision_ledger import DECISION_INSTANCE_TAG_PREFIX

    for tag in topic_tags:
        if tag.startswith(DECISION_INSTANCE_TAG_PREFIX):
            suffix = tag[len(DECISION_INSTANCE_TAG_PREFIX):]
            try:
                return int(suffix)
            except ValueError:
                # Malformed suffix — keep scanning in case a later tag is valid.
                continue
    return None


class InFlightItem(BaseModel):
    """An Executive commitment the briefing surfaces as 'about to happen'.

    A user-facing pending scheduled_action — a follow-up it will run or a
    nudge it will send. `target` is the resolved recipient name (or
    department / raw channel ref); `overdue` is True when run_at has already
    passed (the scheduler hasn't fired it yet).
    """

    action_id: int
    intent: str
    run_at: str
    kind: str
    channel: str
    target: str | None
    department: str | None
    overdue: bool


class AwaitingItem(BaseModel):
    """A person we're waiting on — the 'awaiting others' half of in-flight.

    Derived from the same open-commitment set that powers each person's
    `awaiting_reply_count`/`overdue` enrichment, surfaced as its own list so
    the briefing can show "we're chasing X" without scanning the roster.
    """

    person_id: int
    full_name: str
    role: str
    awaiting_count: int
    oldest_at: str | None
    overdue: bool


class HandledItem(BaseModel):
    kind: str
    summary: str
    at: str
    # The alert the move touched, when known — lets the UI offer Undo
    # (POST /alerts/{id}/reopen) right next to the "handled" row.
    alert_id: int | None = None
    # Structured view of the same audit row so the rail can render "who /
    # what / why" instead of one truncated sentence. All additive; `summary`
    # stays the fallback line.
    # Raw audit event type, for a pre-filtered /audit deep link.
    event_type: str = ""
    # The alert's headline when the move was made (a watch's slug for the
    # watching / stopped_watching kinds).
    headline: str | None = None
    # Who or what the move went to: a person's name (routed / nudged), the
    # survivor headline (merged), the new severity (escalated), the draft
    # title (drafted) or the workflow name (suggested_workflow).
    target: str | None = None
    # The "why": the cited evidence (closed / escalated) or the watch
    # policy's reason (watching / stopped_watching).
    detail: str = ""
    # Finer than `kind`: "resolved" | "dismissed" for a close (a dismissal is
    # a judgment call and must read differently), "proposed" for a route
    # that went through the authority gate instead of a DM, else "".
    outcome: str = ""
    # Server-minted evidence ref a close cited (S1 / R2 / A3), else "".
    evidence_ref: str = ""
    # The survivor a merge folded this alert into, for collapsing the pair.
    superseded_by_alert_id: int | None = None
    # The alert's status NOW: "open" (live in the /today queue) | "resolved" |
    # "dismissed" | "expired" | "merged" | "acked" (principal approved it) |
    # "inactive" (snoozed / past TTL, unswept); "" when there is no alert. The
    # rail is rebuilt from the audit log on every fetch, so this is what tells
    # the UI a close was already undone (offer "Reopened", not another Undo)
    # and whether a "still open" link has a card to land on.
    status: str = ""


class TodayResponse(BaseModel):
    departments: list[DepartmentBriefItem]
    people: list[PersonBriefItem]
    proposals: list[ProposalItem]
    # Executive-voice narrative header — "here's what's going on" — served
    # from `briefing.narrative_cache` and regenerated off the hot path. None
    # until the first background generation has run (or on a cold cache).
    narrative: str | None = None
    # In-flight work the Executive will do soon (pending follow-ups / nudges)
    # and people we're awaiting a reply from. Both additive, default empty.
    in_flight: list[InFlightItem] = []
    awaiting: list[AwaitingItem] = []
    # Multi-client practice mode only (2+ client slots): rollup cards for the
    # PARKED clients — overdue follow-ups, awaiting replies, renewals — so the
    # operator sees the whole practice from the active client's brief.
    # Additive, default empty (single-company installs ⇒ no section).
    practice_clients: list[ClientCockpitCard] = []
    # The signed-in caller resolved to a Person id via x-caller-email,
    # so the UI can split `proposals` into "routed to me" vs "across the
    # team" without leaking identity to the client. Null when no caller
    # could be resolved (unrostered signed-in user, or a CLI hit with
    # no header). See chat._resolve_caller_person_id.
    caller_person_id: int | None = None
    # Autonomous alert-review moves since the last delivered morning brief
    # (routed / nudged / escalated / drafted / merged / closed), newest first
    # — the "Executive handled N overnight" pill. Additive, default empty.
    handled_overnight: list[HandledItem] = Field(default_factory=list)


class ActivityItem(BaseModel):
    """One row in the Executive's recent self-initiated activity feed.

    `kind` is a coarse classification — UI uses it to pick an icon / verb.
    `summary` is the human-readable one-liner. `target` names who or what
    the action was directed at (a Person, a channel, a department), or
    None when not applicable. `at` is an ISO timestamp.
    """

    kind: str
    summary: str
    actor: str
    target: str | None
    department: str | None
    at: str


class ActivityResponse(BaseModel):
    items: list[ActivityItem]


class DailyActivityCount(BaseModel):
    """One day in the Pulse heartbeat heatmap. `date` is YYYY-MM-DD (UTC)."""

    date: str
    count: int


class DailyActivityResponse(BaseModel):
    """Dense per-day activity counts for the last N days, oldest → newest.

    Every calendar day in the window is present (count 0 when nothing fired),
    so the UI can render a gap-free contribution grid without client-side fill.
    """

    days: list[DailyActivityCount]


# --------------------------------------------------------------------------- #
# Builder
# --------------------------------------------------------------------------- #

def _person_status(*, on_leave: bool, awaiting_reply: int, awaiting: int) -> str:
    """Single headline status for a person, highest-attention first."""
    if on_leave:
        return "on_leave"
    if awaiting_reply > 0:
        return "needs_reply"
    if awaiting > 0:
        return "awaiting"
    return "clear"


def _parse_aware(iso: str | None) -> datetime | None:
    """Parse an ISO timestamp to an aware UTC datetime, or None.

    Some on-disk timestamps (workflow `awaiting_until`, scheduled_action
    `awaiting_response_since`) are stored as a bare `.isoformat()` and can be
    timezone-naive. Assume UTC for those — mirroring nudge_engine._coerce_aware
    — so comparisons against `datetime.now(UTC)` neither raise TypeError (which
    would 500 the whole /today request) nor silently drop the signal.
    """
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _is_past(iso: str | None, now: datetime) -> bool:
    """True if an ISO timestamp is in the past. Malformed/empty → False."""
    dt = _parse_aware(iso)
    return dt is not None and dt < now


def _reply_overdue(oldest_iso: str | None, sla_hours: int, now: datetime) -> bool:
    """True if an awaited reply has gone unanswered past the person's SLA."""
    dt = _parse_aware(oldest_iso)
    return dt is not None and (now - dt) > timedelta(hours=sla_hours)


def _person_priority(
    *,
    on_leave: bool,
    awaiting: int,
    awaiting_overdue: bool,
    awaiting_reply: int,
    reply_overdue: bool,
    is_principal: bool,
) -> int:
    """Sort key — higher floats to the top of the roster. Overdue work and
    awaited replies dominate; on-leave sinks; principal breaks ties."""
    score = 0
    if reply_overdue:
        score += 60
    elif awaiting_reply > 0:
        score += 40
    if awaiting_overdue:
        score += 30
    elif awaiting > 0:
        score += 20
    if on_leave:
        score -= 40
    if is_principal:
        score += 1
    return score


def _build_today(
    stale_out: list[StaleInsight] | None = None,
) -> TodayResponse:
    """Assemble the live dashboard snapshot.

    Stays synchronous so the deprecated /morning-brief alias and the
    morning_brief workflow (which call this directly) need no changes; the LLM
    insight notes are served from cache here and regenerated off the hot path.
    When `stale_out` is provided, people whose cached note is missing/stale are
    appended for the caller to regenerate in the background.

    The briefing narrative is NOT attached here — it is per-viewer (see
    `_attach_narrative`), so the caller-aware endpoints attach it after
    resolving the viewer from `x-caller-email`.
    """
    from openexecutive.departments.store import list_departments
    from openexecutive.memory.episodic import (
        last_contact_at_by_person,
        list_awaiting_replies_by_person,
    )
    from openexecutive.people import insights, insights_cache
    from openexecutive.people.channel import is_reachable_now, next_window_for
    from openexecutive.people.store import list_people
    from openexecutive.workflows.persistence import list_awaiting_runs

    now = datetime.now(UTC)
    depts = list_departments()
    people = list_people()
    awaiting_runs = list_awaiting_runs()
    awaiting_replies = list_awaiting_replies_by_person()
    last_contact = last_contact_at_by_person()

    pid_to_awaiting: dict[int, int] = {}
    pid_to_soonest: dict[int, str] = {}
    for run in awaiting_runs:
        pid = run.get("awaiting_person_id")
        if pid is not None:
            pid_to_awaiting[pid] = pid_to_awaiting.get(pid, 0) + 1
            until = run.get("awaiting_until") or ""
            if until and (pid not in pid_to_soonest or until < pid_to_soonest[pid]):
                pid_to_soonest[pid] = until

    dept_awaiting: dict[str, int] = {}
    for run in awaiting_runs:
        raw_state = run.get("state_json") or "{}"
        try:
            state = json.loads(raw_state)
        except (json.JSONDecodeError, TypeError):
            state = {}
        slug = state.get("department", "")
        if slug:
            dept_awaiting[slug] = dept_awaiting.get(slug, 0) + 1

    dept_items = []
    for ds in depts:
        cfg = ds.config
        at_risk = sum(1 for g in ds.goals if g.status == "at_risk")
        off_track = sum(1 for g in ds.goals if g.status == "off_track")
        # Surface the actual problem goals inline on the card — off_track
        # (worse) before at_risk, capped so a department with many goals can't
        # blow out the card. Goals already loaded, so no extra query. The id
        # tiebreaker makes intra-tier order deterministic regardless of how
        # ds.goals was loaded (today it's ORDER BY id; this pins it).
        attention = sorted(
            (g for g in ds.goals if g.status in ("off_track", "at_risk")),
            key=lambda g: (0 if g.status == "off_track" else 1, g.id or 0),
        )[:_DEPT_ATTENTION_GOAL_CAP]
        dept_items.append(DepartmentBriefItem(
            slug=cfg.slug,
            title=cfg.title,
            authority_level=cfg.authority_level.value,
            goal_count=len(ds.goals),
            at_risk_count=at_risk,
            off_track_count=off_track,
            awaiting_count=dept_awaiting.get(cfg.slug, 0),
            attention_goals=[
                GoalBrief(
                    key_result=g.key_result,
                    current=g.current,
                    target=g.target,
                    status=g.status,
                )
                for g in attention
            ],
        ))

    person_items = []
    # Per-person reply-overdue, kept separately from PersonBriefItem.overdue
    # (which also folds in workflow-SLA overdue) so the `awaiting` list — which
    # is specifically "we're waiting on their REPLY" — flags only late replies.
    reply_overdue_by_pid: dict[int, bool] = {}
    for person in people:
        pid = person.id or 0
        awaiting = pid_to_awaiting.get(pid, 0)
        soonest_sla_at = pid_to_soonest.get(pid)
        reply_count, oldest_reply = awaiting_replies.get(pid, (0, None))

        on_leave = person.on_leave_until is not None and now.date() <= person.on_leave_until
        reachable = is_reachable_now(person, now=now)
        next_window = None if reachable else next_window_for(person, after=now)
        awaiting_overdue = _is_past(soonest_sla_at, now)
        reply_overdue = _reply_overdue(oldest_reply, person.response_sla_hours, now)
        reply_overdue_by_pid[pid] = reply_overdue
        authority = [s.value for s in person.authority_scope]

        status = _person_status(on_leave=on_leave, awaiting_reply=reply_count, awaiting=awaiting)
        priority = _person_priority(
            on_leave=on_leave,
            awaiting=awaiting,
            awaiting_overdue=awaiting_overdue,
            awaiting_reply=reply_count,
            reply_overdue=reply_overdue,
            is_principal=person.is_principal,
        )

        signals: dict[str, Any] = {
            "role": person.role,
            "is_principal": person.is_principal,
            "status": status,
            "awaiting_count": awaiting,
            "soonest_sla_at": soonest_sla_at,
            "awaiting_reply_count": reply_count,
            "oldest_awaiting_reply_at": oldest_reply,
            "overdue": awaiting_overdue or reply_overdue,
            "on_leave_until": person.on_leave_until.isoformat() if person.on_leave_until else None,
            "reachable_now": reachable,
            "next_window_at": next_window.isoformat() if next_window else None,
            "authority_scope": authority,
            "department_slugs": person.department_slugs,
            "last_contact_at": last_contact.get(pid),
        }

        # Insight: serve from cache; collect stale entries for background regen.
        # Skip unsaved people (no stable id to key the cache on).
        insight: str | None = None
        if pid:
            input_hash = insights.build_insight_input_hash(signals)
            cached = insights_cache.get(pid)
            if cached is not None and cached.input_hash == input_hash:
                insight = cached.insight_text
            elif stale_out is not None:
                stale_out.append((person, signals, input_hash))

        person_items.append(PersonBriefItem(
            id=pid,
            full_name=person.full_name,
            role=person.role,
            is_principal=person.is_principal,
            preferred_channel=person.preferred_channel,
            awaiting_count=awaiting,
            soonest_sla_at=soonest_sla_at,
            status=status,
            awaiting_reply_count=reply_count,
            oldest_awaiting_reply_at=oldest_reply,
            on_leave_until=signals["on_leave_until"],
            reachable_now=reachable,
            next_window_at=signals["next_window_at"],
            authority_scope=authority,
            department_slugs=person.department_slugs,
            last_contact_at=last_contact.get(pid),
            overdue=awaiting_overdue or reply_overdue,
            priority=priority,
            insight=insight,
        ))

    # Attention-worthy people first; stable name order within a priority band.
    person_items.sort(key=lambda p: (-p.priority, p.full_name))

    from openexecutive.alerts.lifecycle import list_live_alerts
    from openexecutive.alerts.store import count_superseded_by
    from openexecutive.briefing.ranking import score_and_categorize

    # Live = unread AND inside its TTL AND not snoozed — the read-side twin of
    # the scheduler's expiry sweep, so the page is right before the sweep runs.
    raw_alerts = list_live_alerts(limit=100, now=now)
    superseded_counts = count_superseded_by()
    trust_by_slug = _watch_trust_by_slug(raw_alerts)
    proposal_items = []
    for alert in raw_alerts:
        # Surface every unread alert as a briefing action item, including
        # alerts with no routed_to_person_id. Previously these were
        # invisible — the Executive's create_alert tool calls (from
        # inbound triage, operational signals, etc.) produced rows that
        # landed in the table but never showed up in the briefing. The
        # UI splits "Needs you" vs "Across the team" using caller +
        # principal-owns-unrouted; everything stays one queue.
        #
        # Score + categorize so the UI can lead with genuinely-actionable
        # items and collapse low-signal monitoring noise.
        item_score, item_category, item_reason = score_and_categorize(
            alert, trust_by_slug=trust_by_slug, now=now
        )
        proposal_items.append(ProposalItem(
            alert_id=alert.id or 0,
            headline=alert.headline,
            body=alert.body or alert.headline,
            routed_to_person_id=alert.routed_to_person_id,
            suggested_action=alert.suggested_action,
            created_at=alert.created_at,
            topic_tags=alert.topic_tags or [],
            score=item_score,
            category=item_category,
            surfaced_reason=item_reason,
            decision_instance_id=_parse_decision_instance_id(alert.topic_tags or []),
            occurrence_count=alert.occurrence_count,
            last_seen_at=alert.last_seen_at,
            last_reviewed_at=alert.last_reviewed_at,
            review_verdict=alert.review_verdict,
            review_note=alert.review_note,
            recommended_move=alert.recommended_move,
            why_now=alert.why_now,
            due_at=alert.due_at,
            superseded_count=superseded_counts.get(alert.id or -1, 0),
            suggested_workflow=alert.suggested_workflow,
        ))

    # Action items first (sharpest by score), monitoring noise after; ties
    # broken by recency (most recent first). Two-pass stable sort: order by
    # created_at DESC first, then by (category, -score) — Python's stable
    # sort preserves the recency order within equal category/score bands.
    # The UI further splits action items into "Needs you" / "Across the
    # team" and collapses the monitoring tail.
    proposal_items.sort(key=lambda p: p.created_at, reverse=True)
    proposal_items.sort(key=lambda p: (0 if p.category == "action" else 1, -p.score))

    response = TodayResponse(
        departments=dept_items,
        people=person_items,
        proposals=proposal_items,
        handled_overnight=_handled_overnight(now),
    )

    # In-flight commitments (pending follow-ups / nudges) + people we're
    # awaiting a reply from. Both reuse data already loaded above, so this
    # adds one cheap query (pending actions) and no extra LLM work. The
    # narrative hash above ignores these keys, so populating them here does
    # not affect narrative freshness.
    from openexecutive.memory.episodic import list_pending_scheduled_actions

    pid_to_name = {p.id: p.full_name for p in people if p.id}
    channel_lookup = _build_channel_lookup()
    in_flight_items: list[InFlightItem] = []
    try:
        for action in list_pending_scheduled_actions():
            target: str | None = None
            if action.assigned_to_person_id is not None:
                target = pid_to_name.get(action.assigned_to_person_id)
            if target is None:
                target = _resolve_channel_target(
                    channel_lookup, action.channel, action.channel_ref or ""
                )
            in_flight_items.append(InFlightItem(
                action_id=action.id or 0,
                intent=action.intent_text,
                run_at=action.run_at,
                kind=action.kind,
                channel=action.channel,
                target=target,
                department=action.department or None,
                overdue=_is_past(action.run_at, now),
            ))
    except Exception:
        logger.exception("today: in-flight scheduled actions read failed")

    awaiting_items = [
        AwaitingItem(
            person_id=p.id,
            full_name=p.full_name,
            role=p.role,
            awaiting_count=p.awaiting_reply_count,
            oldest_at=p.oldest_awaiting_reply_at,
            # Reply-specific overdue (not the person's combined SLA overdue),
            # since this list is about late *replies*.
            overdue=reply_overdue_by_pid.get(p.id, p.overdue),
        )
        for p in person_items
        if p.awaiting_reply_count > 0
    ]
    # Most-overdue first; within a group, those with a known oldest-awaited
    # timestamp (oldest first) ahead of any with none.
    awaiting_items.sort(
        key=lambda a: (not a.overdue, a.oldest_at is None, a.oldest_at or "")
    )

    response.in_flight = in_flight_items
    response.awaiting = awaiting_items

    # Parked clients in multi-client practice mode. The helper returns [] for
    # single-company installs (0–1 slots) and swallows its own errors.
    from openexecutive.config import get_settings as _get_settings

    response.practice_clients = format_practice_for_today(_get_settings())

    return response


def _classify_scheduled_action(kind: str, channel: str) -> str:
    """Map a fired scheduled_action to a UI-friendly activity kind.

    Mirrors the kind/channel taxonomy declared in
    openexecutive.memory.episodic.ScheduledAction. Unknown combinations
    fall back to "action" rather than raising — the activity feed is
    best-effort and should keep rendering even if a new kind is added
    that this code hasn't been taught about.
    """
    if kind == "proactive_nudge":
        return "nudge_sent"
    if kind == "dept_cadence":
        return "cadence_sent"
    if kind == "awaiting_human":
        return "workflow_resumed"
    if channel in ("slack_dm", "discord_dm"):
        return "dm_sent"
    if channel == "email":
        return "email_sent"
    if channel == "telegram":
        return "dm_sent"
    return "action"


def _build_channel_lookup() -> dict[tuple[str, str], str]:
    """Map (channel, channel_ref) → person.full_name for the whole roster.

    Lets the activity rail and the in-flight list say "DM Jordan Avery"
    instead of leaking a raw Discord/Slack/Telegram id. Built once per
    request by the caller.
    """
    from openexecutive.people import registry as people_registry

    lookup: dict[tuple[str, str], str] = {}
    for person in people_registry.list_people():
        if person.discord_user_id:
            lookup[("discord_dm", person.discord_user_id)] = person.full_name
        if person.slack_user_id:
            lookup[("slack_dm", person.slack_user_id)] = person.full_name
        if person.telegram_chat_id:
            lookup[("telegram", person.telegram_chat_id)] = person.full_name
        if person.email:
            lookup[("email", person.email.lower())] = person.full_name
    return lookup


def _resolve_channel_target(
    lookup: dict[tuple[str, str], str], channel: str, channel_ref: str
) -> str | None:
    """Resolve a (channel, channel_ref) to a person name, or the raw ref.

    Email channel_refs sometimes carry a "|thread_id" suffix (written by the
    scheduler runner) — strip it before the email lookup.
    """
    if not channel_ref:
        return None
    key_ref = (
        channel_ref.split("|", 1)[0].strip().lower()
        if channel == "email"
        else channel_ref
    )
    return lookup.get((channel, key_ref), channel_ref)


# Terminal decision-instance status → human verb for the activity rail.
_DECISION_STATUS_LABEL = {
    "approved_unchanged": "Approved",
    "approved_with_edit": "Approved (edited)",
    "rejected": "Rejected",
    "auto_no_response": "Expired (no response)",
    "reversed": "Reversed",
    "executed": "Executed",
    "failed": "Failed",
}


def _payload_headline(payload_json: str | None) -> str | None:
    """Best-effort human label from a decision payload JSON blob.

    Decision payloads aren't a fixed shape, so probe the common human-readable
    keys in priority order; return None if none are present or the blob is not
    parseable, so the caller can fall back to the decision_class.
    """
    if not payload_json:
        return None
    try:
        data = json.loads(payload_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    for key in ("summary", "title", "headline", "name"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _build_activity(limit: int, since: datetime | None = None) -> ActivityResponse:
    """Aggregate recent self-initiated Executive activity across sources.

    ``since`` bounds the feed to items at/after that instant (the briefs pass
    the previous delivery time so "what changed" is a real delta); the pool
    is widened so a busy history cannot starve the window.

    Sources, all pulled from the shared SQLite database:
      • scheduled_actions with status='done' — fired follow-ups, nudges,
        cadences, workflow resumes. Internal channels and the nudge_scan
        heartbeat are filtered out (no user-visible side effect).
      • decisions — items the extractor recorded the user committed to.
      • advice_given — strategic advice the system rendered worth keeping.
      • workflow_runs with status='done' — briefings, department check-ins,
        research runs the Executive completed on its own initiative.
      • initiatives — projects the Executive kicked off / is tracking.
      • decision_instances that reached a terminal state — gated proposals
        approved / rejected / reversed (the autonomy gate outcomes).
      • alerts — operational signals the Executive raised.

    Items are merged, sorted by timestamp DESC, and capped at `limit`.
    The caller is expected to clamp `limit` (the route does this).
    """
    from openexecutive.alerts import store as alerts_store
    from openexecutive.memory import decision_ledger
    from openexecutive.memory.episodic import (
        get_recent_advice,
        get_recent_decisions,
        get_recent_initiatives,
        list_scheduled_actions,
    )
    from openexecutive.workflows import persistence as wf_persistence

    items: list[ActivityItem] = []

    # Pull a generous pool from each source so the post-merge sort
    # produces a representative top-N; the route clamps `limit` itself.
    pool = max(limit * 3, 30) if since is None else max(limit * 5, 50)

    # Resolve channel_ref → person.full_name once per request so the
    # activity rail can say "DM'd Jordan Avery" instead of leaking the
    # raw Discord/Slack/Telegram id.
    channel_lookup = _build_channel_lookup()

    def _resolve_target(channel: str, channel_ref: str) -> str | None:
        return _resolve_channel_target(channel_lookup, channel, channel_ref)

    # Departments in propose_only mode get marked done by the runner
    # WITHOUT dispatching (scheduler/runner.py:135). The action shows up
    # in the activity feed because status=done, but no DM actually went
    # out — labelling those as "DM'd" is misleading. Override the kind
    # so the UI renders them with a different verb ("Proposed to …").
    from openexecutive.departments import store as dept_store
    propose_only_depts = {
        d.config.slug
        for d in dept_store.list_departments()
        if d.config.authority_level == "propose_only"
    }

    for action in list_scheduled_actions(status="done", limit=pool):
        if action.kind == "nudge_scan" or action.channel == "__internal__":
            continue
        # `created_at` is when the action was queued, not when it fired.
        # For ad-hoc follow-ups these are typically minutes apart, but
        # long-running cadences/nudges can have `run_at` days after
        # `created_at`. There is no `done_at` column today; switching
        # to `run_at` would be more truthful for fired actions but is
        # a wider schema change to defer.
        kind = _classify_scheduled_action(action.kind, action.channel)
        if action.department and action.department in propose_only_depts:
            kind = "proposal_routed"
        items.append(ActivityItem(
            kind=kind,
            summary=action.intent_text,
            actor="Executive",
            target=_resolve_target(action.channel, action.channel_ref or ""),
            department=action.department or None,
            at=action.created_at,
        ))

    for decision in get_recent_decisions(limit=pool):
        items.append(ActivityItem(
            kind="decision_logged",
            summary=decision.summary,
            actor="Executive",
            target=None,
            department=decision.department or None,
            at=decision.timestamp,
        ))

    for advice in get_recent_advice(limit=pool):
        items.append(ActivityItem(
            kind="advice_given",
            summary=advice.advice_summary,
            actor="Executive",
            target=None,
            department=advice.department or None,
            at=advice.timestamp,
        ))

    # Completed workflow runs — briefings, department check-ins, research runs
    # the Executive finished on its own. The status filter is pushed into SQL
    # (not applied after the pull) so a backlog of running/awaiting runs can't
    # starve the done ones out of the pool. Bucket at `updated_at` (completion).
    for run in wf_persistence.list_runs(status="done", limit=pool):
        items.append(ActivityItem(
            kind="workflow_done",
            summary=run.get("title") or run.get("workflow_name") or "Workflow",
            actor="Executive",
            target=None,
            department=None,
            at=run["updated_at"],
        ))

    # Initiatives the Executive kicked off / is tracking. `created_at` marks
    # when it started; a non-active status is appended so the row reads true.
    for initiative in get_recent_initiatives(limit=pool):
        summary = initiative.title
        if initiative.status and initiative.status != "active":
            summary = f"{summary} ({initiative.status})"
        items.append(ActivityItem(
            kind="initiative_started",
            summary=summary,
            actor="Executive",
            target=None,
            department=initiative.department or None,
            at=initiative.created_at,
        ))

    # Gated decisions that reached a terminal state (approved / rejected /
    # reversed / …). The pending ones are surfaced as proposals, not activity.
    for instance in decision_ledger.list_recent_resolved(limit=pool):
        label = _DECISION_STATUS_LABEL.get(instance.status, "Resolved")
        detail = (
            _payload_headline(instance.final_payload_json)
            or _payload_headline(instance.proposed_payload_json)
            or instance.decision_class.replace("_", " ")
        )
        items.append(ActivityItem(
            kind="decision_resolved",
            summary=f"{label}: {detail}",
            actor="Executive",
            target=None,
            department=instance.department or None,
            at=instance.resolved_at or instance.created_at,
        ))

    # Operational alerts the Executive raised. All statuses are included on
    # purpose: this is a historical "what happened" feed (like decisions /
    # advice, which have no status), so an alert later read or dismissed still
    # represents a real raise event at its `created_at`. Decision-scheduling
    # alerts are excluded in SQL (not after the pull, so they can't starve real
    # alerts out of the pool) — they are the companion alert for a gated
    # booking, already represented by the `decision_resolved` rows above (and as
    # a live proposal while pending), so surfacing them here would double-count.
    for alert in alerts_store.recent_alerts(
        limit=pool, exclude_source=decision_ledger.DECISION_ALERT_SOURCE,
    ):
        items.append(ActivityItem(
            kind="alert_raised",
            summary=alert.headline,
            actor="Executive",
            target=None,
            department=None,
            at=alert.created_at,
        ))

    if since is not None:
        items = [
            i for i in items
            if (at := _parse_aware(i.at)) is not None and at >= since
        ]
    items.sort(key=lambda i: i.at, reverse=True)
    return ActivityResponse(items=items[:limit])


def _build_daily_activity(days: int) -> DailyActivityResponse:
    """Dense per-day activity counts for the Pulse heartbeat heatmap.

    Delegates the bucketing to `episodic.count_activity_by_day` (sparse: only
    days with activity), then fills every calendar day in the window so the
    grid has no holes. Window = the last `days` days inclusive of today (UTC),
    returned oldest → newest. The caller clamps `days`.
    """
    from openexecutive.memory.episodic import count_activity_by_day

    counts = dict(count_activity_by_day(days))
    start = datetime.now(UTC).date() - timedelta(days=days - 1)
    out: list[DailyActivityCount] = []
    for offset in range(days):
        date = (start + timedelta(days=offset)).isoformat()
        out.append(DailyActivityCount(date=date, count=counts.get(date, 0)))
    return DailyActivityResponse(days=out)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

async def _regen_stale_insights(stale: list[StaleInsight]) -> None:
    """Regenerate and cache insight notes off the request hot path.

    Runs as a FastAPI background task after the response is sent. Each person
    is generated concurrently and guarded so one slow/failed model call can
    neither hang the batch nor surface to the user.
    """
    from openexecutive.people import insights, insights_cache

    async def _one(person: Person, signals: dict[str, Any], input_hash: str) -> None:
        try:
            text = await asyncio.wait_for(
                insights.generate_person_insight(person, signals),
                timeout=25.0,
            )
        except Exception:
            logger.exception("today: insight regen failed for person_id=%s", person.id)
            return
        if text and person.id:
            insights_cache.put(insights_cache.PersonInsight(
                person_id=person.id,
                input_hash=input_hash,
                insight_text=text,
                generated_at=insights_cache.utc_now_iso(),
            ))

    await asyncio.gather(*(_one(p, s, h) for p, s, h in stale))


# --------------------------------------------------------------------------- #
# Per-viewer briefing narrative
# --------------------------------------------------------------------------- #

def _viewer_for(
    response: TodayResponse, caller_person_id: int | None
) -> PersonBriefItem | None:
    """The PersonBriefItem for the resolved caller, or None."""
    if caller_person_id is None:
        return None
    return next((p for p in response.people if p.id == caller_person_id), None)


def _action_proposals(proposals: list[ProposalItem]) -> list[ProposalItem]:
    """Action-category proposals only — drops monitoring/watchlist noise.

    The narrative is a SYNTHESIS (not a re-list of the cards), so it should
    reason over the signal — decisions/approvals — and ignore the passive
    monitoring items the UI collapses into its own section.
    """
    return [p for p in proposals if p.category == "action"]


def _viewer_slice(response: TodayResponse, viewer: PersonBriefItem) -> dict[str, Any]:
    """A `today_data`-shaped dict scoped to one non-principal teammate:
    the action items routed to THEM + their departments' goal health. Mirrors
    the UI's 'routed to me' split; the principal-only 'people waiting on you'
    section is dropped (empty people list)."""
    depts = set(viewer.department_slugs)
    proposals = [
        p for p in _action_proposals(response.proposals)
        if p.routed_to_person_id == viewer.id
    ]
    departments = [d for d in response.departments if d.slug in depts]
    return {
        "departments": [d.model_dump() for d in departments],
        "people": [],
        "proposals": [p.model_dump() for p in proposals],
    }


def _narrative_inputs(
    response: TodayResponse, caller_person_id: int | None
) -> tuple[str, dict[str, Any], dict[str, str] | None, PersonBriefItem | None]:
    """Resolve (cache scope, scoped today_data, viewer descriptor, viewer row).

    The principal and any unresolved caller share the whole-company narrative
    (DEFAULT_SCOPE); each non-principal teammate gets a `person:<id>` scope and
    a role-scoped slice + descriptor.
    """
    from openexecutive.briefing import narrative_cache

    viewer = _viewer_for(response, caller_person_id)
    is_principal = viewer.is_principal if viewer else False
    if viewer is None or is_principal:
        # Whole-company synthesis: feed all ACTION proposals (mine + across the
        # team) so the narrative can connect them, minus monitoring noise.
        # At-risk depts / activity stay company-wide.
        data = response.model_dump()
        data["proposals"] = [
            p.model_dump() for p in _action_proposals(response.proposals)
        ]
        return narrative_cache.DEFAULT_SCOPE, data, None, viewer
    return (
        f"person:{caller_person_id}",
        _viewer_slice(response, viewer),
        {"name": viewer.full_name, "role": viewer.role},
        viewer,
    )


def _attach_narrative(
    response: TodayResponse,
    *,
    caller_person_id: int | None,
    background_tasks: BackgroundTasks | None,
) -> None:
    """Serve the viewer's cached narrative onto `response`, scheduling a
    background regeneration when it's missing/stale. `background_tasks=None`
    (the deprecated alias) serves cache-only without scheduling regen."""
    from openexecutive.briefing import narrative_cache

    scope, today_data, _desc, _viewer = _narrative_inputs(response, caller_person_id)
    try:
        nhash = narrative_cache.build_narrative_input_hash(today_data, scope=scope)
        cached = narrative_cache.get(scope)
        if cached is not None:
            response.narrative = cached.narrative_text
        is_stale = cached is None or cached.input_hash != nhash
        if is_stale and background_tasks is not None:
            background_tasks.add_task(_regen_briefing_narrative, caller_person_id, scope)
    except Exception:
        logger.exception("today: briefing narrative attach failed (scope=%s)", scope)


async def _regen_briefing_narrative(
    caller_person_id: int | None, expected_scope: str
) -> None:
    """Regenerate and cache the viewer's briefing narrative off the hot path.

    Runs as a FastAPI background task after the response is sent. Rebuilds the
    snapshot, derives the viewer's scope / scoped slice / perspective (and, for
    a teammate, filters activity to their departments), synthesizes via the
    shared synthesizer, and caches under the viewer's scope. Guarded so a
    slow/failed model call can never surface to the user.

    `expected_scope` is the scope resolved on the hot path; if the viewer's
    scope changed in between (e.g. the person was deleted, collapsing them to
    the principal scope) we skip the write rather than churn / overwrite a
    different scope's cache entry.
    """
    from openexecutive.briefing import narrative_cache
    from openexecutive.briefing.narrative import synthesize_briefing_narrative

    try:
        snapshot = _build_today()
        scope, today_data, viewer_desc, viewer = _narrative_inputs(
            snapshot, caller_person_id
        )
        if scope != expected_scope:
            logger.info(
                "today: narrative scope changed (%s → %s) between serve and "
                "regen; skipping write", expected_scope, scope,
            )
            return
        activity = [item.model_dump() for item in _build_activity(20).items]
        if viewer_desc is not None and viewer is not None:
            # Teammate view: keep only activity in their departments.
            vdepts = set(viewer.department_slugs)
            activity = [a for a in activity if a.get("department") in vdepts]
        period_label = datetime.now(UTC).strftime("%Y-%m-%d")
        text = await asyncio.wait_for(
            synthesize_briefing_narrative(
                today_data=today_data, activity=activity,
                period_label=period_label, viewer=viewer_desc,
            ),
            timeout=25.0,
        )
    except Exception:
        logger.exception("today: briefing narrative regen failed")
        return

    if not text:
        return
    input_hash = narrative_cache.build_narrative_input_hash(today_data, scope=scope)
    narrative_cache.put(narrative_cache.BriefingNarrative(
        scope=scope,
        input_hash=input_hash,
        narrative_text=text,
        generated_at=narrative_cache.utc_now_iso(),
    ))


@router.get("/today", response_model=TodayResponse, tags=["today"])
async def get_today(request: Request, background_tasks: BackgroundTasks) -> TodayResponse:
    stale: list[StaleInsight] = []
    response = _build_today(stale_out=stale)
    from openexecutive.api.routes.chat import _resolve_caller_person_id
    response.caller_person_id = _resolve_caller_person_id(request)
    if stale:
        background_tasks.add_task(_regen_stale_insights, stale)
    _attach_narrative(
        response,
        caller_person_id=response.caller_person_id,
        background_tasks=background_tasks,
    )
    return response


@router.get("/today/activity", response_model=ActivityResponse, tags=["today"])
def get_today_activity(
    limit: int = Query(20, ge=1, le=100),
) -> ActivityResponse:
    """Recent self-initiated Executive activity for the briefing rail.

    Default limit 20, clamped to [1, 100]. Sources: fired scheduled_actions,
    decisions, advice. See `_build_activity` for the merge rules.
    """
    # FastAPI's Query(ge=, le=) does the clamping/422 for out-of-range.
    # Defensive secondary check in case the signature changes later.
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=400, detail="limit must be in [1, 100]")
    return _build_activity(limit)


@router.get(
    "/today/activity/daily",
    response_model=DailyActivityResponse,
    tags=["today"],
)
def get_today_activity_daily(
    days: int = Query(90, ge=1, le=365),
) -> DailyActivityResponse:
    """Per-day activity counts for the Pulse heartbeat heatmap.

    Default 90 days, clamped to [1, 365]. Returns a dense list (every calendar
    day present, count 0 when nothing fired), oldest → newest. Same source set
    and exclusions as `GET /today/activity` — see `_build_daily_activity`.
    """
    # Query(ge=, le=) clamps to 422; defensive check mirrors get_today_activity.
    if days < 1 or days > 365:
        raise HTTPException(status_code=400, detail="days must be in [1, 365]")
    return _build_daily_activity(days)


@router.get(
    "/morning-brief",
    response_model=TodayResponse,
    tags=["today"],
    deprecated=True,
    summary="Deprecated alias for GET /today",
)
def get_morning_brief(request: Request, response: Response) -> TodayResponse:
    response.headers["Deprecation"] = "true"
    response.headers["Sunset"] = "Sat, 22 Aug 2026 00:00:00 GMT"
    response.headers["Link"] = '</today>; rel="successor-version"'
    payload = _build_today()
    from openexecutive.api.routes.chat import _resolve_caller_person_id
    payload.caller_person_id = _resolve_caller_person_id(request)
    # Serve the viewer's cached narrative (cache-only — this deprecated alias
    # has no BackgroundTasks to schedule a regen).
    _attach_narrative(
        payload, caller_person_id=payload.caller_person_id, background_tasks=None
    )
    return payload
