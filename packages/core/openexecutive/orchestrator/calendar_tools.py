"""Anthropic tool definitions + handlers for calendar booking (first-climb autonomy).

Exposes two tools to the Executive:
- `create_calendar_event` — propose (or auto-execute when promoted) a meeting
- `cancel_calendar_event` — cancel a previously booked event

The handler enforces code-enforced caps regardless of trust-ledger mode, routes
through gate_action for the MEETING_SCHEDULING scope, writes a decision_instances
row so the trust ledger fills, and delegates the actual calendar operation to the
Google Workspace MCP via MCPGateway.call_tool("google_workspace__manage_event").

The approve→execute bridge lives in api/routes/decisions.py.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from openexecutive.departments.authority import GateDecision

logger = logging.getLogger(__name__)

# An impromptu meeting starts a beat from now so the invite lands before it
# begins; the post-meeting recap ask fires a few minutes after it ends.
_INSTANT_START_LEAD = timedelta(minutes=1)
_POST_MEETING_FOLLOWUP_DELAY = timedelta(minutes=5)
# A single calendar event can't sensibly run longer than a day.
_MAX_INSTANT_DURATION_MINUTES = 24 * 60

# ---------------------------------------------------------------------------
# Tool definitions (Anthropic tool-use schema)
# ---------------------------------------------------------------------------

CREATE_CALENDAR_EVENT_TOOL: dict[str, Any] = {
    "name": "create_calendar_event",
    "description": (
        "Schedule a FUTURE calendar meeting with one or more people from the "
        "roster (use create_instant_meeting for a call happening right now). "
        "In propose-only mode (default) this creates a Proposal that the "
        "approver must action before the event is actually created. A Google "
        "Meet video link is attached automatically unless you set "
        "add_google_meet=false for an in-person meeting. "
        "Use `attendee_person_ids` — NOT raw email addresses — so the system can "
        "verify attendees are on the People roster. "
        "You may use this proactively (e.g. when a goal has stalled and a sync "
        "would unblock it); proactive bookings still go through approval. "
        "Put any agenda or talking points in `description` so attendees see them. "
        "You MUST supply `confidence` (0.0–1.0) reflecting how certain you are "
        "this meeting, at this time, with these people, is the right action."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Meeting title (max 200 chars)",
            },
            "start": {
                "type": "string",
                "description": "Start time in ISO 8601 format with timezone (e.g. 2025-06-15T14:00:00+00:00)",
            },
            "end": {
                "type": "string",
                "description": "End time in ISO 8601 format with timezone. Must be after start.",
            },
            "attendee_person_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Person IDs (from lookup_person) of all attendees. Max 7 plus the organizer.",
            },
            "description": {
                "type": "string",
                "description": "Optional meeting agenda or notes (max 2000 chars)",
            },
            "confidence": {
                "type": "number",
                "description": (
                    "Your confidence (0.0–1.0) that this booking is correct and timely. "
                    "Required for trust-ledger calibration."
                ),
                "minimum": 0.0,
                "maximum": 1.0,
            },
            "include_principal": {
                "type": "boolean",
                "description": (
                    "Set true ONLY when you explicitly intend to book the principal "
                    "(the account owner). Never assume — always ask first."
                ),
            },
            "add_google_meet": {
                "type": "boolean",
                "description": (
                    "Whether to attach a Google Meet video link (default true). "
                    "Set false only for an explicitly in-person meeting."
                ),
            },
        },
        "required": ["title", "start", "end", "attendee_person_ids", "confidence"],
    },
}

CREATE_INSTANT_MEETING_TOOL: dict[str, Any] = {
    "name": "create_instant_meeting",
    "description": (
        "Start an impromptu meeting RIGHT NOW with one or more people from the "
        "roster and get back a Google Meet link to share immediately. Use this "
        "(not create_calendar_event) when the user wants to meet now / asap, or "
        "when you proactively decide a live call is needed this moment. The event "
        "starts in ~1 minute and a Google Meet link is always attached. This "
        "books immediately (no approval step) and emails a calendar invite to "
        "every attendee, so use it deliberately. Use `attendee_person_ids` — NOT "
        "raw emails. Supply `confidence` (0.0–1.0) for trust-ledger calibration."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "attendee_person_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Person IDs (from lookup_person) of all attendees. Max 7 plus the organizer.",
            },
            "title": {
                "type": "string",
                "description": "Meeting title (max 200 chars). Defaults to 'Quick sync' if omitted.",
            },
            "duration_minutes": {
                "type": "integer",
                "description": "Meeting length in minutes (default 30, max 1440).",
            },
            "description": {
                "type": "string",
                "description": "Optional agenda or context (max 2000 chars).",
            },
            "confidence": {
                "type": "number",
                "description": "Your confidence (0.0–1.0) that starting this meeting now is correct.",
                "minimum": 0.0,
                "maximum": 1.0,
            },
            "include_principal": {
                "type": "boolean",
                "description": (
                    "Set true ONLY when you explicitly intend to book the principal "
                    "(the account owner). Never assume — always ask first."
                ),
            },
        },
        "required": ["attendee_person_ids", "confidence"],
    },
}

CANCEL_CALENDAR_EVENT_TOOL: dict[str, Any] = {
    "name": "cancel_calendar_event",
    "description": (
        "Cancel a previously booked calendar event by its decision_instance_id. "
        "All attendees will be notified. This action is irreversible."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "decision_instance_id": {
                "type": "integer",
                "description": "The decision_instance_id returned when the event was approved.",
            },
        },
        "required": ["decision_instance_id"],
    },
}

CALENDAR_TOOLS: list[dict[str, Any]] = [
    CREATE_CALENDAR_EVENT_TOOL,
    CREATE_INSTANT_MEETING_TOOL,
    CANCEL_CALENDAR_EVENT_TOOL,
]

# Maps the calendar tool names to the integration flag checked by the tool filter.
CALENDAR_TOOL_INTEGRATION = "calendar"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_iso(raw: str) -> datetime | None:
    """Parse an ISO 8601 string; return None on failure."""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except (ValueError, AttributeError):
        return None


def _check_business_hours(dt: datetime, start_hhmm: str, end_hhmm: str) -> bool:
    """Return True if dt falls within business hours (single-tz, v1 limitation)."""
    try:
        sh, sm = (int(x) for x in start_hhmm.split(":"))
        eh, em = (int(x) for x in end_hhmm.split(":"))
    except (ValueError, AttributeError):
        return True  # misconfigured cap → don't block
    if dt.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    t_minutes = dt.hour * 60 + dt.minute
    return (sh * 60 + sm) <= t_minutes < (eh * 60 + em)


def _resolve_attendees(
    attendee_person_ids: list[Any],
    include_principal: bool,
    max_attendees: int,
) -> tuple[list[str], list[int]] | dict[str, str]:
    """Validate attendee person IDs against the roster and resolve their emails.

    Returns ``(emails, int_ids)`` in input order on success, or an
    ``{"error": ...}`` dict on the first validation failure. Enforces: non-empty,
    max-attendees, roster membership, non-archived, present email, and
    principal-protection (the principal is only allowed when ``include_principal``
    is set). Shared by both the scheduled and instant booking handlers.

    A contact is a valid attendee only on a turn the principal started on a
    verified surface (``people_tools.contacts_reachable_now``) — the same rule
    the gateway's calendar gate applies when the event is created. On any
    other turn a contact's id reads as "not found", like an unknown one.
    """
    from openexecutive.orchestrator.people_tools import (
        PRIVATE_TURN_REFUSAL,
        contacts_reachable_now,
        turn_is_private_to_principal,
    )
    from openexecutive.people.store import list_people

    if not attendee_person_ids:
        return {"error": "attendee_person_ids must not be empty"}
    if len(attendee_person_ids) > max_attendees:
        return {"error": f"too many attendees (max {max_attendees})"}

    all_people = {
        p.id: p
        for p in list_people(include_contacts=contacts_reachable_now())
        if p.id is not None
    }
    attendee_emails: list[str] = []
    int_ids: list[int] = []
    for pid in attendee_person_ids:
        try:
            person_id = int(pid)
        except (TypeError, ValueError):
            return {"error": f"attendee_person_id {pid!r} is not an integer"}
        person = all_people.get(person_id)
        if person is None:
            return {"error": f"person_id {person_id} not found on roster"}
        if not person.email:
            return {"error": f"person {person.full_name!r} (id={person_id}) has no email"}
        if getattr(person, "archived", False):
            return {"error": f"person {person.full_name!r} (id={person_id}) is archived"}
        if turn_is_private_to_principal() and not person.is_principal:
            return {"error": PRIVATE_TURN_REFUSAL}
        if getattr(person, "is_principal", False) and not include_principal:
            return {"error": (
                f"{person.full_name!r} is the principal. Set include_principal=true "
                "to explicitly confirm you intend to book them."
            )}
        attendee_emails.append(person.email)
        int_ids.append(person_id)
    return attendee_emails, int_ids


def _has_contact(person_ids: list[int]) -> bool:
    """Whether any of these (already validated) attendees is one of the
    principal's contacts. Such a booking is private to the principal: its
    proposal is theirs to approve and hidden from everyone else."""
    from openexecutive.people.store import get_person

    for pid in person_ids:
        person = get_person(pid)
        if person is not None and person.kind != "team":
            return True
    return False


def _principal_id() -> int | None:
    from openexecutive.people.store import find_principal_person

    principal = find_principal_person()
    return principal.id if principal is not None else None


def _daily_cap_reached(now: datetime, max_per_day: int) -> bool:
    """True if today's non-failed meeting_scheduling bookings hit the daily cap."""
    from openexecutive.memory.decision_ledger import STATUS_FAILED, list_instances

    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    today_instances = [
        i for i in list_instances("meeting_scheduling")
        if i.created_at >= today_start
        and i.status not in (STATUS_FAILED, "rejected", "auto_no_response")
    ]
    return len(today_instances) >= max_per_day


def _extract_meet_link(event: dict[str, Any]) -> str | None:
    """Pull the Google Meet video URL out of a created-event response.

    The workspace-mcp ``manage_event`` returns the Calendar event object; the
    Meet link lives under ``conferenceData.entryPoints[].uri`` for the ``video``
    entry point. Some servers also surface ``hangoutLink`` directly. Tolerant of
    both snake_case and camelCase key spellings.
    """
    for key in ("meet_link", "hangoutLink", "hangout_link"):
        val = event.get(key)
        if isinstance(val, str) and val:
            return val
    conf = event.get("conferenceData") or event.get("conference_data") or {}
    if isinstance(conf, dict):
        entry_points = conf.get("entryPoints") or conf.get("entry_points") or []
        for ep in entry_points:
            if isinstance(ep, dict) and ep.get("entryPointType") == "video":
                uri = ep.get("uri")
                if isinstance(uri, str) and uri:
                    return uri
    return None


def _pick_recap_target(
    attendee_person_ids: list[Any], settings: Any
) -> tuple[str, str, str] | None:
    """Choose the first non-principal attendee reachable on a DM channel.

    Returns ``(scheduled_channel, channel_ref, display_name)`` or ``None`` when
    no attendee has a configured, well-formed DM channel.
    """
    from openexecutive.orchestrator.schedule_tools import (
        configured_integrations,
        resolve_person_scheduled_dm,
    )
    from openexecutive.people.store import get_person

    configured = configured_integrations(settings)
    for pid in attendee_person_ids:
        try:
            person = get_person(int(pid))
        except (TypeError, ValueError):
            continue
        if person is None or getattr(person, "archived", False):
            continue
        # The recap ask is a chase that fires later, unattended: never a
        # contact (they cannot answer the bot anyway).
        if getattr(person, "is_principal", False) or getattr(person, "kind", "team") != "team":
            continue
        resolved = resolve_person_scheduled_dm(person, configured)
        if resolved is not None:
            channel, channel_ref = resolved
            return channel, channel_ref, person.full_name
    return None


async def _schedule_post_meeting_followup(
    payload: dict[str, Any],
    event_id: str | None,
    meet_link: str | None,
) -> None:
    """Queue a post-meeting recap ask, DM'd to a human attendee ~5m after the end.

    Best-effort: callers wrap this so a scheduling failure never breaks a booking.
    The Executive's reply ingestion (outbound→inbound DM linkage) handles the
    attendee's response in a later turn — nothing else to wire here.
    """
    from openexecutive.config import get_settings

    settings = get_settings()
    if not getattr(settings, "calendar_post_meeting_followup_enabled", True):
        return
    end = _parse_iso(str(payload.get("end", "")))
    attendee_person_ids = payload.get("attendee_person_ids") or []
    if end is None or not attendee_person_ids:
        return
    target = _pick_recap_target(attendee_person_ids, settings)
    if target is None:
        logger.info(
            "calendar_tools: no reachable attendee for post-meeting recap — "
            "skipping follow-up for event %s", event_id,
        )
        return
    channel, channel_ref, target_name = target
    scope_key = f"meeting_followup:{event_id}" if event_id else None
    from openexecutive.memory.episodic import (
        insert_scheduled_action,
        recent_nudge_for_scope,
    )

    # Dedup: a concurrent re-execution of the same event (e.g. a lost
    # approve-CAS race) must not schedule the recap twice.
    if scope_key and recent_nudge_for_scope(scope_key, since=datetime.now(UTC)):
        logger.info(
            "calendar_tools: post-meeting follow-up already scheduled for %s — "
            "skipping", scope_key,
        )
        return

    run_at = (end + _POST_MEETING_FOLLOWUP_DELAY).isoformat()
    title = payload.get("title") or "the meeting"
    intent = (
        f"The meeting {title!r} has just ended. Send {target_name} a brief, "
        f"friendly direct message asking for a quick recap: the key decisions "
        f"made and any action items (who owns what, and by when). Keep it to one "
        f"short message."
    )

    insert_scheduled_action(
        run_at=run_at,
        channel=channel,
        channel_ref=channel_ref,
        intent_text=intent,
        kind="ad_hoc",
        scope_key=scope_key,
    )
    logger.info(
        "calendar_tools: scheduled post-meeting recap follow-up via %s at %s "
        "(event %s)", channel, run_at, event_id,
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _propose_via_decision_alert(
    instance_id: int,
    proposed_payload: dict[str, Any],
    approver_person_id: int | None,
    severity: str,
    *,
    private: bool = False,
) -> None:
    """Surface a proposed meeting as a routed briefing alert.

    Bridges the decision ledger into the briefing: the alert is linked to
    the decision_instance by ``external_id='decision:{id}'`` so approve/reject
    from the briefing can clear it, and tagged so today.py can expose the
    ``decision_instance_id`` on the card. Routed to the approver so it lands in
    their "Needs you" queue. Best-effort — a failed insert must NOT break the
    proposal (the ledger row is the source of truth); mirrors the exception
    handling in authority.propose_via_alert.
    """
    from openexecutive.alerts.models import PRIVATE_ALERT_TAG
    from openexecutive.alerts.store import insert_alert
    from openexecutive.memory.decision_ledger import (
        DECISION_ALERT_SOURCE,
        decision_alert_external_id,
        decision_instance_tag,
    )

    title = str(proposed_payload.get("title") or "Untitled meeting")
    start = str(proposed_payload.get("start") or "")
    end = str(proposed_payload.get("end") or "")
    attendees = proposed_payload.get("attendee_emails") or []
    description = str(proposed_payload.get("description") or "")

    body_lines = [f"Meeting: {title}"]
    if start or end:
        body_lines.append(f"When: {start} → {end}")
    if isinstance(attendees, list) and attendees:
        body_lines.append(f"Attendees: {', '.join(str(a) for a in attendees)}")
    if description:
        body_lines.append(f"\n{description}")
    body = "\n".join(body_lines)

    external_id = decision_alert_external_id(instance_id)
    try:
        insert_alert(
            source=DECISION_ALERT_SOURCE,
            external_id=external_id,
            severity=severity,
            headline=f"Approve meeting: {title}"[:160],
            body=body,
            suggested_action=f'Book "{title}" for {start}.',
            topic_tags=[
                decision_instance_tag(instance_id),
                "decision_class:meeting_scheduling",
                # A booking with one of the principal's contacts: the card
                # (which lists their address) is the principal's alone.
                *([PRIVATE_ALERT_TAG] if private else []),
            ],
            dedup_key=external_id,
            routed_to_person_id=approver_person_id,
        )
    except Exception:
        logger.exception(
            "calendar_tools: failed to surface decision %d as briefing alert",
            instance_id,
        )


def _solo_meeting_gate(class_mode: str, session: Any) -> GateDecision:
    """The meeting gate in solo mode. No department is consulted — the
    install's departments are only the principal's areas, not an
    organisation with an operations team — so the ``meeting_scheduling``
    class mode (the principal's own setting, PUT
    /decisions/classes/meeting_scheduling) decides, and ``auto_execute``
    books right away only when the principal started this turn on a surface
    that verified it is them (the roster-write rule,
    ``people_tools.is_principal_on_verified_surface``). Anything else — an
    inbound email, a contact's message, an unattended run — is proposed to
    the principal on the briefing, so text someone else wrote cannot put a
    meeting on the principal's calendar."""
    from openexecutive.departments.authority import GateDecision
    from openexecutive.orchestrator.people_tools import is_principal_on_verified_surface

    if class_mode == "auto_execute" and is_principal_on_verified_surface(session):
        return GateDecision(
            allowed=True, action="execute", reason="solo: meeting_scheduling is auto_execute"
        )
    principal_id: int | None = None
    try:
        from openexecutive.people.store import find_principal_person

        principal = find_principal_person()
        principal_id = principal.id if principal is not None else None
    except Exception:
        logger.warning("calendar_tools: principal lookup failed", exc_info=True)
    return GateDecision(
        allowed=False,
        action="propose",
        assignee_person_id=principal_id,
        reason=(
            "solo: meeting_scheduling proposes to the principal"
            if class_mode != "auto_execute"
            else "solo: auto_execute needs the principal on a verified surface — proposing"
        ),
    )


async def handle_create_calendar_event(tool_input: dict[str, Any]) -> str:
    """Propose (or auto-execute when promoted) a calendar meeting."""
    from openexecutive.config import get_settings
    from openexecutive.departments.authority import gate_action
    from openexecutive.memory.decision_ledger import (
        STATUS_FAILED,
        _idem_key,
        create_decision_instance,
        get_class_mode,
        get_live_by_idem,
        mark_executed,
        mark_resolved,
    )
    from openexecutive.memory.workspace_settings import effective_workspace_mode
    from openexecutive.orchestrator.mcp_gateway import get_active_gateway
    from openexecutive.orchestrator.schedule_tools import current_session
    from openexecutive.people.models import AuthorityScope

    settings = get_settings()

    # 1. Gate: calendar must be enabled and MCP must be active.
    if not settings.calendar_booking_enabled:
        return json.dumps({"error": "calendar_booking_enabled is not set to true"})
    gateway = get_active_gateway()
    if gateway is None:
        return json.dumps({"error": "MCP gateway is not running — calendar unavailable"})

    # 2. Parse required fields.
    try:
        title = str(tool_input["title"]).strip()
        start_raw = str(tool_input["start"])
        end_raw = str(tool_input["end"])
        attendee_person_ids = list(tool_input["attendee_person_ids"])
        confidence_raw = tool_input["confidence"]
    except (KeyError, TypeError) as exc:
        return json.dumps({"error": f"missing required field: {exc}"})

    description = str(tool_input.get("description", "")).strip()
    include_principal = bool(tool_input.get("include_principal", False))

    if not title:
        return json.dumps({"error": "title must not be empty"})
    if len(title) > 200:
        return json.dumps({"error": "title must be 200 characters or fewer"})

    try:
        confidence = float(confidence_raw)
        if not (0.0 <= confidence <= 1.0):
            raise ValueError
    except (TypeError, ValueError):
        return json.dumps({"error": "confidence must be a number between 0.0 and 1.0"})

    # 3. Parse and validate datetimes.
    now = datetime.now(UTC)
    start = _parse_iso(start_raw)
    end = _parse_iso(end_raw)
    if start is None:
        return json.dumps({"error": f"start is not valid ISO 8601: {start_raw!r}"})
    if end is None:
        return json.dumps({"error": f"end is not valid ISO 8601: {end_raw!r}"})
    if start <= now:
        return json.dumps({"error": "start must be in the future"})
    if end <= start:
        return json.dumps({"error": "end must be after start"})

    # 4. Cap: horizon.
    horizon = now + timedelta(days=settings.calendar_horizon_days)
    if start > horizon:
        return json.dumps({
            "error": f"start is more than {settings.calendar_horizon_days} days out",
        })

    # 5. Cap: business hours / weekday (single-tz v1 — TODO: per-person timezone).
    if not _check_business_hours(start, settings.calendar_business_hours_start,
                                 settings.calendar_business_hours_end):
        return json.dumps({
            "error": (
                f"start is outside business hours "
                f"({settings.calendar_business_hours_start}–"
                f"{settings.calendar_business_hours_end}) or falls on a weekend"
            ),
        })

    # 6-7. Caps: attendee roster gate + principal protection.
    resolved = _resolve_attendees(
        attendee_person_ids, include_principal, settings.calendar_max_attendees
    )
    if isinstance(resolved, dict):
        return json.dumps(resolved)
    attendee_emails, attendee_int_ids = resolved

    # 8. Cap: max events per day.
    if _daily_cap_reached(now, settings.calendar_max_events_per_day):
        return json.dumps({
            "error": f"daily calendar booking cap reached (max {settings.calendar_max_events_per_day})",
        })

    # 9. Idempotency: if an identical live proposal exists, return it. No new
    # briefing alert here — the original propose call already created one
    # (and insert_alert's UNIQUE(source, external_id) would dedup it anyway).
    idem = _idem_key(attendee_emails, start.isoformat(), end.isoformat(), title)
    existing = get_live_by_idem(idem)
    if existing is not None:
        return json.dumps({
            "status": "already_proposed",
            "decision_instance_id": existing.id,
            "message": "An identical proposal is already pending approval.",
        })

    # 10. Authority gate.
    session = current_session.get()
    session_id = getattr(session, "session_id", None) if session is not None else None
    class_mode = get_class_mode("meeting_scheduling")

    if effective_workspace_mode(session) == "solo":
        # Solo: the principal is the only approver and there is no operations
        # department to consult — the class mode decides.
        gate_decision = _solo_meeting_gate(class_mode, session)
    else:
        gate_decision = gate_action(
            "operations",
            "meeting_scheduling",
            required_scope=AuthorityScope.MEETING_SCHEDULING,
            now=now,
        )
    # With a contact on the invite the booking is private to the principal:
    # they approve it (only they may reach a contact at approval time) and
    # nobody else sees the proposal.
    private = _has_contact(attendee_int_ids)
    approver_person_id = _principal_id() if private else gate_decision.assignee_person_id

    # A Google Meet link is requested by default; the model can opt out for an
    # in-person meeting via add_google_meet=false.
    add_google_meet = bool(tool_input.get(
        "add_google_meet", getattr(settings, "calendar_meet_links_enabled", True)
    ))

    # Build the payload that will be re-used at execute time.
    proposed_payload = {
        "title": title,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "attendee_emails": attendee_emails,
        "attendee_person_ids": attendee_int_ids,
        "description": description,
        "add_google_meet": add_google_meet,
        **({"private": True} if private else {}),
    }

    # 11. Record in the ledger.
    try:
        instance_id = create_decision_instance(
            decision_class="meeting_scheduling",
            department="operations",
            originating_session_id=session_id,
            proposed_payload=proposed_payload,
            idempotency_key=idem,
            gate_mode=gate_decision.action,
            approver_person_id=approver_person_id,
            confidence=confidence,
        )
    except Exception:
        logger.exception("calendar_tools: failed to create decision_instance")
        return json.dumps({"error": "internal error recording proposal"})

    # 12. Execute or propose.
    if gate_decision.action == "execute" and class_mode == "auto_execute":
        # Auto-execute path (Build 3 — only reachable after promotion). The
        # booking happens now, so there is nothing to approve — deliberately
        # NO briefing alert here.
        result = await _do_create_event(gateway, proposed_payload)
        if "error" in result:
            # Mark the ledger row failed so it doesn't pollute the
            # approver queue as a phantom pending proposal.
            mark_resolved(instance_id, STATUS_FAILED)
            logger.error(
                "calendar_tools: auto-execute failed for instance %d: %s",
                instance_id, result.get("error"),
            )
            return json.dumps(result)
        final_payload = {**proposed_payload, "meet_link": result.get("meet_link")}
        mark_executed(
            instance_id,
            external_event_id=result.get("event_id"),
            final_payload=final_payload,
        )
        return json.dumps({
            "status": "created",
            "decision_instance_id": instance_id,
            "event_id": result.get("event_id"),
            "meet_link": result.get("meet_link"),
        })

    # Default: propose — don't call the MCP yet. Surface the proposal as a
    # routed briefing alert so the approver sees it (and can approve/reject it)
    # directly in the briefing.
    _propose_via_decision_alert(
        instance_id,
        proposed_payload,
        approver_person_id,
        severity="high" if gate_decision.action == "escalate" else "medium",
        private=private,
    )
    return json.dumps({
        "status": "proposed",
        "decision_instance_id": instance_id,
        "message": (
            "The booking has been surfaced on the briefing for approval. "
            "The approver will be notified."
        ),
    })


async def handle_create_instant_meeting(tool_input: dict[str, Any]) -> str:
    """Book an impromptu meeting starting now and return the Google Meet link.

    Unlike create_calendar_event this auto-executes immediately (no approval
    step — the user asked for a call *now*) and skips the horizon and
    business-hours/weekday caps. It still enforces the roster gate,
    max-attendees, principal-protection, and the daily booking cap, and records
    an executed row in the decision ledger for audit.
    """
    from openexecutive.config import get_settings
    from openexecutive.memory.decision_ledger import (
        STATUS_FAILED,
        _idem_key,
        create_decision_instance,
        mark_executed,
        mark_resolved,
    )
    from openexecutive.orchestrator.mcp_gateway import get_active_gateway
    from openexecutive.orchestrator.schedule_tools import current_session

    settings = get_settings()

    if not settings.calendar_booking_enabled:
        return json.dumps({"error": "calendar_booking_enabled is not set to true"})
    gateway = get_active_gateway()
    if gateway is None:
        return json.dumps({"error": "MCP gateway is not running — calendar unavailable"})

    try:
        attendee_person_ids = list(tool_input["attendee_person_ids"])
        confidence_raw = tool_input["confidence"]
    except (KeyError, TypeError) as exc:
        return json.dumps({"error": f"missing required field: {exc}"})

    title = (str(tool_input.get("title") or "").strip() or "Quick sync")
    if len(title) > 200:
        return json.dumps({"error": "title must be 200 characters or fewer"})
    description = str(tool_input.get("description", "")).strip()
    include_principal = bool(tool_input.get("include_principal", False))

    try:
        confidence = float(confidence_raw)
        if not (0.0 <= confidence <= 1.0):
            raise ValueError
    except (TypeError, ValueError):
        return json.dumps({"error": "confidence must be a number between 0.0 and 1.0"})

    default_minutes = getattr(settings, "calendar_instant_meeting_minutes", 30)
    try:
        duration_minutes = int(tool_input.get("duration_minutes", default_minutes))
    except (TypeError, ValueError):
        return json.dumps({"error": "duration_minutes must be an integer"})
    if not (1 <= duration_minutes <= _MAX_INSTANT_DURATION_MINUTES):
        return json.dumps({
            "error": f"duration_minutes must be between 1 and {_MAX_INSTANT_DURATION_MINUTES}",
        })

    now = datetime.now(UTC)
    # Start a beat out so the invite lands before the event begins. No horizon
    # or business-hours caps — an impromptu call is intentionally "now".
    start = now + _INSTANT_START_LEAD
    end = start + timedelta(minutes=duration_minutes)

    resolved = _resolve_attendees(
        attendee_person_ids, include_principal, settings.calendar_max_attendees
    )
    if isinstance(resolved, dict):
        return json.dumps(resolved)
    attendee_emails, attendee_int_ids = resolved

    if _daily_cap_reached(now, settings.calendar_max_events_per_day):
        return json.dumps({
            "error": f"daily calendar booking cap reached (max {settings.calendar_max_events_per_day})",
        })

    payload = {
        "title": title,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "attendee_emails": attendee_emails,
        "attendee_person_ids": attendee_int_ids,
        "description": description,
        "add_google_meet": True,  # instant meetings always get a Meet link
        **({"private": True} if _has_contact(attendee_int_ids) else {}),
    }

    session = current_session.get()
    session_id = getattr(session, "session_id", None) if session is not None else None
    idem = _idem_key(attendee_emails, start.isoformat(), end.isoformat(), title)

    try:
        instance_id = create_decision_instance(
            decision_class="meeting_scheduling",
            department="operations",
            originating_session_id=session_id,
            proposed_payload=payload,
            idempotency_key=idem,
            gate_mode="execute",
            approver_person_id=None,
            confidence=confidence,
        )
    except Exception:
        logger.exception("calendar_tools: failed to create decision_instance (instant)")
        return json.dumps({"error": "internal error recording booking"})

    result = await _do_create_event(gateway, payload)
    if "error" in result:
        mark_resolved(instance_id, STATUS_FAILED)
        logger.error(
            "calendar_tools: instant meeting create failed for instance %d: %s",
            instance_id, result.get("error"),
        )
        return json.dumps(result)

    final_payload = {**payload, "meet_link": result.get("meet_link")}
    mark_executed(
        instance_id,
        external_event_id=result.get("event_id"),
        final_payload=final_payload,
    )
    return json.dumps({
        "status": "created",
        "decision_instance_id": instance_id,
        "event_id": result.get("event_id"),
        "meet_link": result.get("meet_link"),
        "start": start.isoformat(),
        "end": end.isoformat(),
    })


async def _do_create_event(
    gateway: Any,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Call google_workspace__manage_event action=create via the MCP gateway.

    Returns a dict with "event_id" (and "meet_link" when a Google Meet link was
    minted) on success, or "error" on failure. On success it also schedules the
    best-effort post-meeting recap follow-up. The gateway backstop
    (_check_calendar_attendees) will run again on this call — belt-and-suspenders.
    """
    from openexecutive.config import get_settings

    settings = get_settings()
    arguments: dict[str, Any] = {
        "action": "create",
        "summary": payload["title"],
        "start_time": payload["start"],
        "end_time": payload["end"],
        "attendees": payload["attendee_emails"],
        "send_updates": "all",
    }
    # Request a Google Meet link unless the payload explicitly opted out.
    if bool(payload.get("add_google_meet", getattr(settings, "calendar_meet_links_enabled", True))):
        arguments["add_google_meet"] = True
    if payload.get("description"):
        arguments["description"] = payload["description"]

    try:
        raw = await gateway.call_tool({
            "name": "google_workspace__manage_event",
            "arguments": arguments,
        })
        result = json.loads(raw) if isinstance(raw, str) else raw
        # If the MCP returned an error dict, surface it directly.
        if isinstance(result, dict) and "error" in result:
            return {"error": result["error"]}
        # The MCP returns the event object; extract the id field.
        event_id = (
            result.get("id")
            or result.get("event_id")
            or result.get("eventId")
        )
        meet_link = _extract_meet_link(result) if isinstance(result, dict) else None
        out: dict[str, Any] = {"event_id": event_id, "raw": result}
        if meet_link:
            out["meet_link"] = meet_link
        # Best-effort: a follow-up failure must never break a successful booking.
        try:
            await _schedule_post_meeting_followup(payload, event_id, meet_link)
        except Exception:
            logger.exception("calendar_tools: post-meeting follow-up scheduling failed")
        return out
    except Exception as exc:
        logger.exception("calendar_tools: manage_event create failed")
        return {"error": str(exc)}


async def _do_delete_event(
    gateway: Any,
    external_event_id: str,
) -> dict[str, Any]:
    """Call google_workspace__manage_event action=delete via the MCP gateway."""
    try:
        raw = await gateway.call_tool({
            "name": "google_workspace__manage_event",
            "arguments": {
                "action": "delete",
                "event_id": external_event_id,
                "send_updates": "all",
            },
        })
        result = json.loads(raw) if isinstance(raw, str) else raw
        return result if isinstance(result, dict) else {"ok": True}
    except Exception as exc:
        logger.exception("calendar_tools: manage_event delete failed")
        return {"error": str(exc)}


async def handle_cancel_calendar_event(tool_input: dict[str, Any]) -> str:
    """Cancel a booked event by its decision_instance_id."""
    from openexecutive.memory.decision_ledger import (
        get_decision_instance,
        mark_reversed,
    )
    from openexecutive.orchestrator.mcp_gateway import get_active_gateway

    try:
        instance_id = int(tool_input["decision_instance_id"])
    except (KeyError, TypeError, ValueError):
        return json.dumps({"error": "decision_instance_id must be an integer"})

    instance = get_decision_instance(instance_id)
    if instance is None:
        return json.dumps({"error": f"decision_instance {instance_id} not found"})

    if instance.status not in (
        "approved_unchanged", "approved_with_edit", "executed", "proposed"
    ):
        return json.dumps({
            "error": f"cannot cancel a decision_instance with status={instance.status!r}",
        })

    # If there's an actual calendar event, delete it via the MCP.
    if instance.external_event_id:
        gateway = get_active_gateway()
        if gateway is None:
            return json.dumps({"error": "MCP gateway not running — cannot delete calendar event"})
        result = await _do_delete_event(gateway, instance.external_event_id)
        if "error" in result:
            return json.dumps(result)

    mark_reversed(instance_id, reason="cancelled_by_executive")
    return json.dumps({"status": "cancelled", "decision_instance_id": instance_id})


CALENDAR_TOOL_HANDLERS: dict[str, Any] = {
    "create_calendar_event": handle_create_calendar_event,
    "create_instant_meeting": handle_create_instant_meeting,
    "cancel_calendar_event": handle_cancel_calendar_event,
}
