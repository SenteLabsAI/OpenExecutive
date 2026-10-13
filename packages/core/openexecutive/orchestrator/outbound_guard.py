"""Anti-spam guard for proactive outbound DMs.

Every proactive DM the Executive sends — nudges, principal briefs, executive
reflection, watchlist-research findings, department cadences, and ad-hoc
"DM the CFO about X" actions — funnels through the three send-tool handlers in
``orchestrator.schedule_tools`` (``handle_send_telegram_message`` /
``handle_send_slack_dm`` / ``handle_send_discord_dm``). Those handlers used to do
only a roster gate, so nothing stopped the same person from getting many DMs in
quick succession, near-identical messages repeatedly, or a 3am ping.

``check_outbound_allowed`` is the single chokepoint guard those handlers call
just before the network send. It enforces three controls and, on a violation,
returns a human-readable *reason* string (it never raises) so the caller can
suppress-and-log the send and the Executive can read the reason and adapt
(hold the message, or ``schedule_followup`` for the next open window):

1. Content dedup — refuse a near-identical message already sent to this
   recipient within ``outbound_dedup_window_minutes``.
2. Per-recipient rate cap — refuse once ``outbound_max_per_recipient_per_window``
   sends have gone to this recipient within ``outbound_rate_window_minutes``.
3. Quiet hours / availability — when ``outbound_respect_quiet_hours`` is set,
   refuse while the recipient is on leave or outside their availability windows.

Separately, ``check_hold`` applies the recipient's own notification settings
(``people.notifications``: quiet hours, "In my briefs") to the Executive's
own outreach only — a DM sent while a proactive source is tagged
(``attunement.outcomes.tag_proactive``). It does not refuse: the send handler
stores the message (``people.held_messages``) and it goes when their quiet
hours end or with their next brief.

The delivery history is read from the ``status='done'`` ``scheduled_actions``
rows that ``_record_send_to_activity`` already writes for every send, so there
is no new table. The guard is *fail-open*: any internal lookup failure returns
``None`` (allow), because an anti-spam helper must never turn a working send
into a dropped message or a crash.

Scope note: the send handlers are the same tools the Executive uses for both
autonomous proactive DMs and sends a human explicitly asked for, so the guard
applies to both — by design. Suppression is non-destructive: it returns a
reason the Executive reads, so a genuinely-wanted resend can be reworded, or
held / ``schedule_followup``-ed for the recipient's next window. Operators who
want to send identical/off-hours messages can widen the knobs or set
``OUTBOUND_RESPECT_QUIET_HOURS=false``.
"""
from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openexecutive.people.models import Person
    from openexecutive.people.notifications import Hold

logger = logging.getLogger(__name__)

# Mirrors the intent_text truncation in schedule_tools._record_send_to_activity
# (text[:160]); dedup only ever has the first 160 chars of prior sends to compare
# against, so normalizing the candidate to the same prefix keeps it apples-to-apples.
_DEDUP_PREFIX_CHARS = 160
_WS_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Clip to the dedup prefix length, then collapse whitespace + casefold.

    Clip BEFORE normalizing (not after) so this is symmetric with how prior
    sends were stored: ``_record_send_to_activity`` persists the raw
    ``text[:160]``, and at read time we normalize that already-clipped value.
    Normalizing the *full* candidate and clipping afterwards would let a
    different amount of post-strip content survive on each side, so a true
    duplicate whose 160-char prefix contains leading/trailing whitespace would
    fail to match. Clip-then-normalize keeps both sides == normalize(raw[:160]).
    """
    return _WS_RE.sub(" ", text[:_DEDUP_PREFIX_CHARS]).strip().casefold()


def _parse_iso(value: str) -> datetime | None:
    """Parse a stored ISO timestamp into a UTC-aware datetime, or None."""
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _availability_reason(channel: str, channel_ref: str, now: datetime) -> str | None:
    """Reason to hold the send for quiet-hours/availability, or None to allow.

    Unresolvable recipient => None (don't block a send we can't attribute).
    """
    try:
        from openexecutive.people.channel import is_within_availability, next_window_for
        from openexecutive.people.store import find_person_by_channel_ref

        person: Person | None = find_person_by_channel_ref(channel, channel_ref)
        if person is None:
            return None
        if is_within_availability(person, now=now):
            return None
        # On-leave must be checked BEFORE next_window_for: that scan only looks at
        # availability windows and ignores on_leave_until, so a windowed person
        # who is also on leave would otherwise get a misleading "next window
        # tomorrow" (which would itself be suppressed) instead of a leave-aware
        # message the Executive can schedule past. Mirrors channel.is_within_availability.
        if (
            person.on_leave_until is not None
            and now.astimezone(UTC).date() <= person.on_leave_until
        ):
            return (
                f"recipient is on leave until {person.on_leave_until.isoformat()}; "
                "holding message"
            )
        nxt = next_window_for(person, after=now)
        if nxt is not None:
            return (
                "recipient is outside their availability window (quiet hours); "
                f"next open window is {nxt.isoformat()} — hold or schedule for then"
            )
        return "recipient is outside their availability window; holding message"
    except Exception:
        logger.exception("outbound_guard: availability check failed; failing open")
        return None


def check_outbound_allowed(
    channel: str,
    channel_ref: str,
    text: str,
    *,
    now: datetime | None = None,
) -> str | None:
    """Return a refusal reason if this proactive DM should be suppressed, else None.

    `channel` is one of the scheduled-action channels (``telegram``,
    ``slack_dm``, ``discord_dm``, ``email``); `channel_ref` is the *final*
    recipient ref (post roster-resolution); `text` is the message body.

    Fail-open: any unexpected error (including a misconfigured settings object)
    returns ``None`` so the guard can never turn a working send into a crash.
    """
    try:
        return _evaluate(channel, channel_ref, text, now or datetime.now(UTC))
    except Exception:
        logger.exception("outbound_guard: unexpected failure; failing open")
        return None


def _evaluate(channel: str, channel_ref: str, text: str, now: datetime) -> str | None:
    from openexecutive.config import get_settings

    settings = get_settings()

    # One scan over recent sends covers both dedup and rate windows.
    window_min = max(
        settings.outbound_dedup_window_minutes, settings.outbound_rate_window_minutes
    )
    since_iso = (now - timedelta(minutes=window_min)).isoformat()
    try:
        from openexecutive.memory.episodic import recent_sends_for_channel_ref

        recent = recent_sends_for_channel_ref(channel, channel_ref, since_iso)
    except Exception:
        logger.exception("outbound_guard: recent-sends lookup failed; failing open")
        recent = []

    # 1. Content dedup (cheapest signal first).
    norm = _normalize(text)
    dedup_since = now - timedelta(minutes=settings.outbound_dedup_window_minutes)
    for created_at, intent_text in recent:
        ts = _parse_iso(created_at)
        if ts is not None and ts >= dedup_since and _normalize(intent_text) == norm:
            return (
                "a near-identical message was already sent to this recipient at "
                f"{created_at}; skipping the duplicate"
            )

    # 2. Per-recipient rate cap.
    rate_since = now - timedelta(minutes=settings.outbound_rate_window_minutes)
    count = sum(
        1
        for created_at, _ in recent
        if (ts := _parse_iso(created_at)) is not None and ts >= rate_since
    )
    if count >= settings.outbound_max_per_recipient_per_window:
        return (
            f"per-recipient rate cap reached: {count} message(s) already sent to "
            f"this recipient in the last {settings.outbound_rate_window_minutes} "
            f"minutes (max {settings.outbound_max_per_recipient_per_window}); "
            "hold and consolidate"
        )

    # 3. Quiet hours / availability.
    if settings.outbound_respect_quiet_hours:
        reason = _availability_reason(channel, channel_ref, now)
        if reason is not None:
            return reason

    return None


def check_hold(
    channel: str,
    channel_ref: str,
    *,
    now: datetime | None = None,
) -> tuple[int, str, Hold] | None:
    """``(person_id, source, hold)`` when this DM is the Executive's own
    outreach and the recipient's notification settings hold it back, else
    None (send it now).

    Only a tagged proactive send is ever held (``attunement.outcomes``): a
    message someone asked for in chat, a brief, a reminder they set and a
    release of held messages carry no tag. Fail-open like the guard: any
    lookup error sends.
    """
    try:
        from openexecutive.attunement.outcomes import current_tag

        tag = current_tag()
        if tag is None:
            return None
        from openexecutive.people.notifications import (
            hold_for,
            is_approval_send,
            is_urgent_send,
        )

        if is_approval_send():
            return None  # a question something waits on: never held
        from openexecutive.people.store import find_person_by_channel_ref

        person = find_person_by_channel_ref(channel, channel_ref)
        if person is None or person.id is None or person.archived:
            return None
        source = tag[0]
        hold = hold_for(
            person.id, source=source, now=now or datetime.now(UTC), urgent=is_urgent_send()
        )
        return None if hold is None else (person.id, source, hold)
    except Exception:
        logger.exception("outbound_guard: hold check failed; sending")
        return None
