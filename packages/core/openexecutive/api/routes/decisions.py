"""Decisions API — the approve/reject/edit surface for gated Executive proposals.

This is the approve→execute bridge: proposals written by calendar_tools (and
future classes) sit in the trust ledger as `status='proposed'` until a human
acts here.  On approve, the actual calendar event is created via the MCP.

Routes:
  GET  /decisions            — list instances (filter by class, status)
  GET  /decisions/{id}       — single instance detail
  POST /decisions/{id}/approve — approve (optionally with edited payload)
  POST /decisions/{id}/reject  — reject
  GET  /audit/reliability    — per-class reliability card (also in audit.py router)
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from openexecutive.memory.decision_ledger import (
    STATUS_APPROVED_UNCHANGED,
    STATUS_APPROVED_WITH_EDIT,
    STATUS_PROPOSED,
    STATUS_REJECTED,
    DecisionInstance,
    ReliabilityCard,
    aggregate_reliability,
    get_decision_instance,
    list_instances,
    mark_resolved,
    mark_reversed,
)

router = APIRouter()
logger = logging.getLogger(__name__)

_CALENDAR_CLASS = "meeting_scheduling"


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class ApproveBody(BaseModel):
    """Approve a proposal, optionally with an edited meeting payload.

    Any field present in `edits` overwrites the corresponding field in the
    original proposed_payload.  Missing fields retain their proposed values.
    Supported edit keys: title, start, end, attendee_emails, description.
    """
    edits: dict[str, Any] | None = None


class RejectBody(BaseModel):
    reason: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_payload(instance: DecisionInstance) -> dict[str, Any]:
    try:
        return json.loads(instance.proposed_payload_json)
    except (json.JSONDecodeError, TypeError):
        return {}


def _payload_diff(original: dict[str, Any], final: dict[str, Any]) -> bool:
    """Return True if the payloads differ in any semantically meaningful field."""
    for key in ("title", "start", "end", "description"):
        if original.get(key) != final.get(key):
            return True
    orig_attendees = sorted(original.get("attendee_emails", []))
    final_attendees = sorted(final.get("attendee_emails", []))
    return orig_attendees != final_attendees


def _clear_decision_alert(instance_id: int, status: str) -> None:
    """Clear the companion briefing alert when a decision is resolved.

    Calendar proposals are surfaced on the briefing as an alert linked by
    ``external_id='decision:{id}'`` (see calendar_tools._propose_via_decision_alert).
    Transition it out of the unread queue when the decision is approved
    (``ack``) or rejected/cancelled (``dismissed``). Best-effort: a missing
    alert (e.g. a decision created before the bridge) is a harmless no-op, and
    a failure here must not 500 a decision that already executed.
    """
    from openexecutive.alerts.store import set_status_by_external
    from openexecutive.memory.decision_ledger import (
        DECISION_ALERT_SOURCE,
        decision_alert_external_id,
    )

    try:
        set_status_by_external(
            DECISION_ALERT_SOURCE, decision_alert_external_id(instance_id), status
        )
    except Exception:
        logger.exception(
            "decisions: failed to clear companion alert for instance %d", instance_id
        )


async def _execute_booking(
    instance: DecisionInstance,
    final_payload: dict[str, Any],
    *,
    by_principal: bool = False,
) -> dict[str, Any]:
    """Call the MCP to create the calendar event. Returns the gateway response.

    ``by_principal``: the approver is the principal, acting in the web app. An
    approval runs outside any chat turn, so without this the gateway would
    refuse a contact on the invite even though the principal asked for it.
    """
    from openexecutive.orchestrator.calendar_tools import _do_create_event
    from openexecutive.orchestrator.mcp_gateway import get_active_gateway
    from openexecutive.orchestrator.people_tools import grant_contact_egress

    gateway = get_active_gateway()
    if gateway is None:
        return {"error": "MCP gateway not running — cannot create calendar event"}
    if by_principal:
        with grant_contact_egress():
            return await _do_create_event(gateway, final_payload)
    return await _do_create_event(gateway, final_payload)


def _approver_is_principal(request: Request) -> bool:
    """Whether the caller resolves to the principal (a request with no
    ``x-caller-email`` does — the CLI, direct curl, local login). Fails
    closed."""
    from openexecutive.api.routes.chat import _resolve_caller_person_id
    from openexecutive.people.store import is_principal_or_self

    try:
        return is_principal_or_self(_resolve_caller_person_id(request), None)
    except Exception:
        logger.exception("decisions/approve: principal check failed — contacts stay off")
        return False


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/decisions", response_model=list[DecisionInstance])
def get_decisions(
    decision_class: str = _CALENDAR_CLASS,
    status: str | None = None,
    limit: int = 50,
) -> list[DecisionInstance]:
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be 1–500")
    return list_instances(decision_class, status=status, limit=limit)


@router.get("/decisions/{instance_id}", response_model=DecisionInstance)
def get_decision(instance_id: int) -> DecisionInstance:
    instance = get_decision_instance(instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail="Decision instance not found")
    return instance


@router.post("/decisions/{instance_id}/approve", response_model=DecisionInstance)
async def approve_decision(
    instance_id: int, body: ApproveBody, request: Request
) -> DecisionInstance:
    instance = get_decision_instance(instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail="Decision instance not found")
    if instance.status != STATUS_PROPOSED:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot approve a decision with status={instance.status!r}",
        )

    original_payload = _parse_payload(instance)
    final_payload = dict(original_payload)

    # Apply edits if provided.
    if body.edits:
        for key in ("title", "start", "end", "description"):
            if key in body.edits:
                final_payload[key] = body.edits[key]
        if "attendee_emails" in body.edits:
            final_payload["attendee_emails"] = body.edits["attendee_emails"]

    # Re-run the freebusy conflict check if the MCP supports it — advisory here,
    # but we log a warning if the slot is busy.
    try:
        from openexecutive.orchestrator.mcp_gateway import get_active_gateway
        gw = get_active_gateway()
        if gw is not None:
            fb_result = await gw.call_tool({
                "name": "google_workspace__query_freebusy",
                "arguments": {
                    "time_min": final_payload["start"],
                    "time_max": final_payload["end"],
                    "calendar_ids": final_payload.get("attendee_emails", []),
                },
            })
            fb = json.loads(fb_result) if isinstance(fb_result, str) else fb_result
            if isinstance(fb, dict) and fb.get("has_conflicts"):
                logger.warning(
                    "decisions/approve: freebusy reports conflict for instance %d — proceeding anyway (human override)",
                    instance_id,
                )
    except Exception:
        logger.debug("decisions/approve: freebusy check skipped", exc_info=True)

    # Create the actual calendar event. Contacts on the invite only when the
    # principal is the one approving.
    result = await _execute_booking(
        instance, final_payload, by_principal=_approver_is_principal(request)
    )
    if "error" in result:
        raise HTTPException(status_code=502, detail=result["error"])

    external_event_id = result.get("event_id")
    # Persist the Google Meet link (if one was minted) so the UI and any later
    # reference can surface it.
    if result.get("meet_link"):
        final_payload["meet_link"] = result["meet_link"]
    edited = _payload_diff(original_payload, final_payload)
    outcome = STATUS_APPROVED_WITH_EDIT if edited else STATUS_APPROVED_UNCHANGED

    # Compare-and-set: if a concurrent approve already won, our event is a
    # duplicate and must be deleted before we return 409.
    recorded = mark_resolved(
        instance_id,
        outcome,
        final_payload=final_payload,
        external_event_id=external_event_id,
    )
    if not recorded:
        # A concurrent approve beat us.  Delete the event we just created to
        # avoid a ghost booking with no ledger row referencing it.
        if external_event_id:
            try:
                from openexecutive.orchestrator.calendar_tools import _do_delete_event
                gw2 = get_active_gateway()
                if gw2 is not None:
                    await _do_delete_event(gw2, external_event_id)
            except Exception:
                logger.exception(
                    "decisions/approve: failed to clean up leaked event %s for instance %d",
                    external_event_id, instance_id,
                )
        raise HTTPException(
            status_code=409,
            detail="A concurrent approval already processed this proposal.",
        )

    # Won the compare-and-set: clear the companion briefing alert.
    _clear_decision_alert(instance_id, "ack")

    updated = get_decision_instance(instance_id)
    if updated is None:
        raise HTTPException(status_code=500, detail="Instance vanished after update")
    return updated


@router.post("/decisions/{instance_id}/reject", response_model=DecisionInstance)
def reject_decision(instance_id: int, body: RejectBody) -> DecisionInstance:
    instance = get_decision_instance(instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail="Decision instance not found")
    if instance.status != STATUS_PROPOSED:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot reject a decision with status={instance.status!r}",
        )
    mark_resolved(instance_id, STATUS_REJECTED)
    _clear_decision_alert(instance_id, "dismissed")
    updated = get_decision_instance(instance_id)
    if updated is None:
        raise HTTPException(status_code=500, detail="Instance vanished after update")
    return updated


@router.post("/decisions/{instance_id}/cancel", response_model=DecisionInstance)
async def cancel_decision(instance_id: int) -> DecisionInstance:
    """Cancel an approved/executed event (reverse it)."""
    instance = get_decision_instance(instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail="Decision instance not found")
    if instance.status not in (
        STATUS_APPROVED_UNCHANGED, STATUS_APPROVED_WITH_EDIT, STATUS_PROPOSED,
    ):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot cancel a decision with status={instance.status!r}",
        )

    if instance.external_event_id:
        from openexecutive.orchestrator.calendar_tools import _do_delete_event
        from openexecutive.orchestrator.mcp_gateway import get_active_gateway
        gw = get_active_gateway()
        if gw is None:
            raise HTTPException(status_code=502, detail="MCP gateway not running")
        result = await _do_delete_event(gw, instance.external_event_id)
        if "error" in result:
            raise HTTPException(status_code=502, detail=result["error"])

    mark_reversed(instance_id, reason="cancelled_via_ui")
    _clear_decision_alert(instance_id, "dismissed")
    updated = get_decision_instance(instance_id)
    if updated is None:
        raise HTTPException(status_code=500, detail="Instance vanished after update")
    return updated


@router.get("/audit/reliability", response_model=ReliabilityCard)
def get_reliability(
    decision_class: str = _CALENDAR_CLASS,
    days: int = 30,
) -> ReliabilityCard:
    if days < 1 or days > 365:
        raise HTTPException(status_code=400, detail="days must be 1–365")
    return aggregate_reliability(decision_class, window_days=days)
