"""Decisions API — the approve/reject/edit surface for gated Executive proposals.

This is the approve→execute bridge: proposals written by calendar_tools (and
future classes) sit in the trust ledger as `status='proposed'` until a human
acts here. What approving, rejecting and cancelling DO is per class: each class
resolved here has a ``DecisionClassSpec`` in ``DECISION_CLASSES`` (meeting
booking: create the calendar event on approve, delete it on cancel). An
instance of any other class is refused with 409, never carried out as some
other class.

Routes:
  GET  /decisions            — list instances (filter by class, status)
  GET  /decisions/{id}       — single instance detail
  POST /decisions/{id}/approve — approve (optionally with edited payload)
  POST /decisions/{id}/reject  — reject
  GET  /decisions/classes/meeting_scheduling — the class's autonomy mode
  PUT  /decisions/classes/meeting_scheduling — set it (principal only)
  GET  /audit/reliability    — per-class reliability card (also in audit.py router)
"""
from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from openexecutive.api import caller as api_caller
from openexecutive.memory.decision_ledger import (
    DECISION_ALERT_SOURCE,
    STATUS_APPROVED_UNCHANGED,
    STATUS_APPROVED_WITH_EDIT,
    STATUS_FAILED,
    STATUS_PROPOSED,
    STATUS_REJECTED,
    DecisionInstance,
    ReliabilityCard,
    aggregate_reliability,
    get_class_mode,
    get_decision_instance,
    list_instances,
    mark_resolved,
    mark_reversed,
    set_class_mode,
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

    For a reply card (``delegation_reply``) nothing is edited here: the draft
    is sent as it is in Gmail. ``edits`` carries the person's second yes when
    something changed since the card was made: ``recipients`` (the list they
    were shown) and ``thread_moved_on: true``.
    """
    edits: dict[str, Any] | None = None


class RejectBody(BaseModel):
    reason: str = ""


ClassMode = Literal["propose", "auto_execute"]


class DecisionClassMode(BaseModel):
    """How the Executive handles a decision class: ``propose`` puts each one
    on the briefing for approval, ``auto_execute`` does it straight away."""
    decision_class: str
    mode: ClassMode


class DecisionClassModeUpdate(BaseModel):
    mode: ClassMode


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


def _clear_decision_alert(
    instance_id: int, status: str, source: str | None = DECISION_ALERT_SOURCE
) -> None:
    """Clear the companion briefing alert when a decision is resolved.

    Calendar proposals are surfaced on the briefing as an alert linked by
    ``external_id='decision:{id}'`` (see calendar_tools._propose_via_decision_alert).
    Transition it out of the unread queue when the decision is approved
    (``ack``) or rejected/cancelled (``dismissed``). Best-effort: a missing
    alert (e.g. a decision created before the bridge) is a harmless no-op, and
    a failure here must not 500 a decision that already executed. ``source``
    is the class's ``alert_source``; a class that raises no alert passes None.
    """
    if source is None:
        return
    from openexecutive.alerts.store import set_status_by_external
    from openexecutive.memory.decision_ledger import decision_alert_external_id

    try:
        set_status_by_external(source, decision_alert_external_id(instance_id), status)
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
    from openexecutive.api.routes.people import caller_is_principal

    return caller_is_principal(request)


def _is_private(instance: DecisionInstance) -> bool:
    """Not everyone's to see: a class that is the principal's alone
    (``DecisionClassSpec.principal_only``) or its approver's alone
    (``approver_only``, decided before this is asked), or a payload marked
    ``private`` (a booking with one of the principal's contacts, per
    ``calendar_tools``)."""
    spec = DECISION_CLASSES.get(instance.decision_class)
    if spec is not None and (spec.principal_only or spec.approver_only):
        return True
    payload = _parse_payload(instance)
    return payload.get("private") is True


def _approver_only(instance: DecisionInstance) -> bool:
    spec = DECISION_CLASSES.get(instance.decision_class)
    return spec is not None and spec.approver_only


def _principal_and_approver(instance: DecisionInstance) -> bool:
    spec = DECISION_CLASSES.get(instance.decision_class)
    return spec is not None and spec.principal_and_approver


def _caller_is_approver(instance: DecisionInstance, request: Request) -> bool:
    """Whether the caller is the person ``instance`` went to for approval.
    Fails closed."""
    from openexecutive.api.routes.chat import _resolve_caller_person_id

    try:
        caller = _resolve_caller_person_id(request)
    except Exception:
        logger.exception("decisions: caller lookup failed — treating them as someone else")
        return False
    return caller is not None and caller == instance.approver_person_id


def _visible_instance(instance_id: int, request: Request) -> DecisionInstance:
    """The instance, or 404 — also for a private one when the caller is not
    the principal, and for one only its approver may see when the caller is
    anyone else (the principal included), so its existence (and the contact
    or mail on it) is not revealed."""
    instance = get_decision_instance(instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail="Decision instance not found")
    if _approver_only(instance):
        if not _caller_is_approver(instance, request):
            raise HTTPException(status_code=404, detail="Decision instance not found")
        return instance
    if _principal_and_approver(instance):
        if not (_approver_is_principal(request) or _caller_is_approver(instance, request)):
            raise HTTPException(status_code=404, detail="Decision instance not found")
        return instance
    if _is_private(instance) and not _approver_is_principal(request):
        raise HTTPException(status_code=404, detail="Decision instance not found")
    return instance


_NOT_YOURS = (
    "Only the person this went to for approval, or the owner, can decide it."
)


def _resolver(instance: DecisionInstance, request: Request) -> int | None:
    """The caller's Person id when they may approve, reject or cancel
    ``instance``: the principal, or the person it went to for approval
    (``approver_person_id``) unless it is private to the principal. Anyone
    else gets 403. Fails closed: a roster that can't be read is a 403."""
    from openexecutive.api.routes.chat import _resolve_caller_person_id

    caller = _resolve_caller_person_id(request)
    if _approver_only(instance):
        if caller is not None and caller == instance.approver_person_id:
            return caller
    elif _approver_is_principal(request):
        return caller
    elif _principal_and_approver(instance):
        if caller is not None and caller == instance.approver_person_id:
            return caller
    elif caller is not None and caller == instance.approver_person_id and not _is_private(instance):
        return caller
    logger.info(
        "decisions: caller may not resolve instance %d (class %s)",
        instance.id, instance.decision_class,
    )
    raise HTTPException(status_code=403, detail=_NOT_YOURS)


def _refresh(instance_id: int) -> DecisionInstance:
    updated = get_decision_instance(instance_id)
    if updated is None:
        raise HTTPException(status_code=500, detail="Instance vanished after update")
    return updated


# ---------------------------------------------------------------------------
# Decision classes
# ---------------------------------------------------------------------------

# (instance, body, request, the resolver's Person id) → the updated instance.
_Approve = Callable[
    [DecisionInstance, ApproveBody, Request, int | None], Awaitable[DecisionInstance]
]


@dataclass(frozen=True)
class DecisionClassSpec:
    """How the routes below handle one class of gated decision."""

    name: str
    # Only the principal may see or resolve any instance of it, whatever its
    # payload says.
    principal_only: bool
    # The companion briefing alert's source, cleared when an instance is
    # resolved; None when the class raises none.
    alert_source: str | None
    # Carries out an approval and records it (its own compare-and-set, with
    # the resolver); raises HTTPException to refuse. Called only on a
    # 'proposed' instance, and only for a caller who may resolve it.
    approve: _Approve
    # Best effort, after a reject has been recorded.
    after_reject: Callable[[DecisionInstance], Awaitable[None]] | None = None
    # Undoes what an approval did; None when it can't be undone (409).
    cancel: Callable[[DecisionInstance], Awaitable[None]] | None = None
    # Only the person an instance went to for approval (``approver_person_id``)
    # may see or resolve it — not the principal either, unless it is theirs.
    approver_only: bool = False
    # Only the principal and the person it went to for approval may see or
    # resolve it; nobody else on the team, whatever its payload says.
    principal_and_approver: bool = False


def _spec_for(instance: DecisionInstance) -> DecisionClassSpec:
    spec = DECISION_CLASSES.get(instance.decision_class)
    if spec is None:
        raise HTTPException(
            status_code=409,
            detail=f"Decisions of class {instance.decision_class!r} can't be resolved here.",
        )
    return spec


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/decisions", response_model=list[DecisionInstance])
def get_decisions(
    request: Request,
    decision_class: str = _CALENDAR_CLASS,
    status: str | None = None,
    limit: int = 50,
) -> list[DecisionInstance]:
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be 1–500")
    spec = DECISION_CLASSES.get(decision_class)
    if spec is not None and (spec.approver_only or (spec.principal_and_approver and not _approver_is_principal(request))):
        from openexecutive.api.routes.chat import _resolve_caller_person_id

        caller = _resolve_caller_person_id(request)
        if caller is None:
            return []
        instances = list_instances(decision_class, status=status, limit=500)
        return [i for i in instances if i.approver_person_id == caller][:limit]
    if _approver_is_principal(request):
        return list_instances(decision_class, status=status, limit=limit)
    spec = DECISION_CLASSES.get(decision_class)
    if spec is not None and spec.principal_only:
        return []
    instances = list_instances(decision_class, status=status, limit=limit)
    return [i for i in instances if not _is_private(i)]


@router.get("/decisions/{instance_id}", response_model=DecisionInstance)
def get_decision(instance_id: int, request: Request) -> DecisionInstance:
    return _visible_instance(instance_id, request)


async def _approve_booking(
    instance: DecisionInstance, body: ApproveBody, request: Request, resolver: int | None
) -> DecisionInstance:
    """Meeting booking: create the calendar event, then record the approval."""
    instance_id = instance.id
    original_payload = _parse_payload(instance)
    final_payload = dict(original_payload)

    # Apply edits if provided.
    if body.edits:
        for key in ("title", "start", "end", "description"):
            if key in body.edits:
                final_payload[key] = body.edits[key]
        if "attendee_emails" in body.edits:
            final_payload["attendee_emails"] = body.edits["attendee_emails"]

    # Re-run the backend's conflict check — advisory here, but we log a
    # warning if the slot is busy. (Google: multi-calendar freebusy; Microsoft:
    # the Executive's own calendar view — see integrations.workspace.)
    try:
        from openexecutive.integrations.workspace.registry import get_calendar_provider
        from openexecutive.orchestrator.mcp_gateway import get_active_gateway
        gw = get_active_gateway()
        if gw is not None:
            busy = await get_calendar_provider().has_conflicts(
                gw,
                final_payload["start"],
                final_payload["end"],
                list(final_payload.get("attendee_emails", [])),
            )
            if busy:
                logger.warning(
                    "decisions/approve: calendar reports conflict for instance %d — proceeding anyway (human override)",
                    instance_id,
                )
    except Exception:
        logger.debug("decisions/approve: conflict check skipped", exc_info=True)

    # Create the actual calendar event. Contacts on the invite only when the
    # principal is the one approving.
    result = await _execute_booking(
        instance, final_payload, by_principal=_approver_is_principal(request)
    )
    if "error" in result:
        raise HTTPException(status_code=502, detail=result["error"])

    external_event_id = result.get("event_id")
    # Persist the video-meeting link (Meet or Teams, if one was minted) so the
    # UI and any later reference can surface it.
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
        resolver_person_id=resolver,
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
    return _refresh(instance_id)


async def _cancel_booking(instance: DecisionInstance) -> None:
    """Meeting booking: delete the event an approval created, if any."""
    if not instance.external_event_id:
        return
    from openexecutive.orchestrator.calendar_tools import _do_delete_event
    from openexecutive.orchestrator.mcp_gateway import get_active_gateway

    gw = get_active_gateway()
    if gw is None:
        raise HTTPException(status_code=502, detail="MCP gateway not running")
    result = await _do_delete_event(gw, instance.external_event_id)
    if "error" in result:
        raise HTTPException(status_code=502, detail=result["error"])


_MEETING_BOOKING = DecisionClassSpec(
    name=_CALENDAR_CLASS,
    principal_only=False,
    alert_source=DECISION_ALERT_SOURCE,
    approve=_approve_booking,
    cancel=_cancel_booking,
)


async def _send_reply(
    instance: DecisionInstance, body: ApproveBody, request: Request, resolver: int | None
) -> DecisionInstance:
    """Send the reply's exact Gmail draft, on its person's tap
    (``delegation.reply_send``, which checks everything again first)."""
    from openexecutive.delegation.reply_send import SendRefused, send_approved_reply

    try:
        await send_approved_reply(
            instance, caller=api_caller.caller(request), resolver=resolver, confirm=body.edits,
        )
    except SendRefused as refused:
        raise HTTPException(
            status_code=refused.status,
            detail={"code": refused.code, "message": refused.message, **refused.extra},
        ) from None
    return _refresh(instance.id)


async def _dismiss_reply(instance: DecisionInstance) -> None:
    """Delete the reply's draft from Gmail when nobody edited it."""
    from openexecutive.delegation.replies import dismiss

    await dismiss(instance)


# A reply the inbox watcher drafted as someone (delegation.inbox): theirs
# alone, whatever its payload says (a team member's is hidden from the
# principal too), and never an alert.
_DELEGATED_REPLY = DecisionClassSpec(
    name="delegation_reply",
    principal_only=False,
    approver_only=True,
    alert_source=None,
    approve=_send_reply,
    after_reject=_dismiss_reply,
)

async def _carry_out_lead(
    instance: DecisionInstance, body: ApproveBody, request: Request, resolver: int | None
) -> DecisionInstance:
    """Take the lead: do the exact action that waited, once (claimed first),
    then record it in Recent activity."""
    from openexecutive.memory.decision_ledger import claim_for_execution, finish_execution
    from openexecutive.orchestrator import take_the_lead

    if not claim_for_execution(instance.id, resolver_person_id=resolver):
        raise HTTPException(status_code=409, detail="Someone else resolved this decision first.")
    payload = _parse_payload(instance)
    try:
        result = await take_the_lead.carry_out(payload, by_principal=_approver_is_principal(request))
    except Exception as exc:
        logger.exception("decisions/approve: Take the lead action %d failed", instance.id)
        finish_execution(instance.id, STATUS_FAILED, final_payload={**payload, "error": type(exc).__name__})
        take_the_lead.resolved(instance.id, "failed")
        _clear_decision_alert(instance.id, "dismissed")
        raise HTTPException(status_code=502, detail="It couldn't do that. Nothing was sent.") from None
    failed = take_the_lead.result_failed(result)
    finish_execution(
        instance.id, STATUS_FAILED if failed else STATUS_APPROVED_UNCHANGED,
        final_payload={**payload, "result": result[:2000]},
    )
    take_the_lead.resolved(instance.id, "failed" if failed else "approved")
    _clear_decision_alert(instance.id, "dismissed" if failed else "ack")
    if failed:
        raise HTTPException(status_code=502, detail=f"It couldn't do that: {result[:300]}")
    return _refresh(instance.id)


async def _lead_declined(instance: DecisionInstance) -> None:
    from openexecutive.orchestrator import take_the_lead

    take_the_lead.resolved(instance.id, "declined")


# Take the lead: an action the gate held (orchestrator.take_the_lead), for
# the person whose authority covers it or the principal. What it would do
# quotes the Executive's context, so it is private to them.
_TAKE_THE_LEAD = DecisionClassSpec(
    name="take_the_lead_action",
    principal_only=False,
    principal_and_approver=True,
    alert_source=DECISION_ALERT_SOURCE,
    approve=_carry_out_lead,
    after_reject=_lead_declined,
)

DECISION_CLASSES: dict[str, DecisionClassSpec] = {
    spec.name: spec for spec in (_MEETING_BOOKING, _DELEGATED_REPLY, _TAKE_THE_LEAD)
}


@router.post("/decisions/{instance_id}/approve", response_model=DecisionInstance)
async def approve_decision(
    instance_id: int, body: ApproveBody, request: Request
) -> DecisionInstance:
    instance = _visible_instance(instance_id, request)
    spec = _spec_for(instance)
    resolver = _resolver(instance, request)
    if instance.status != STATUS_PROPOSED:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot approve a decision with status={instance.status!r}",
        )
    return await spec.approve(instance, body, request, resolver)


@router.post("/decisions/{instance_id}/reject", response_model=DecisionInstance)
async def reject_decision(instance_id: int, body: RejectBody, request: Request) -> DecisionInstance:
    instance = _visible_instance(instance_id, request)
    spec = _spec_for(instance)
    resolver = _resolver(instance, request)
    if instance.status != STATUS_PROPOSED:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot reject a decision with status={instance.status!r}",
        )
    if not mark_resolved(instance_id, STATUS_REJECTED, resolver_person_id=resolver):
        raise HTTPException(status_code=409, detail="Someone else resolved this decision first.")
    _clear_decision_alert(instance_id, "dismissed", spec.alert_source)
    updated = _refresh(instance_id)
    if spec.after_reject is not None:
        try:
            await spec.after_reject(updated)
        except Exception:
            logger.exception("decisions/reject: after-reject step failed for instance %d", instance_id)
    return updated


@router.post("/decisions/{instance_id}/cancel", response_model=DecisionInstance)
async def cancel_decision(instance_id: int, request: Request) -> DecisionInstance:
    """Cancel an approved/executed event (reverse it)."""
    instance = _visible_instance(instance_id, request)
    spec = _spec_for(instance)
    _resolver(instance, request)
    if spec.cancel is None:
        raise HTTPException(status_code=409, detail="This decision can't be undone.")
    if instance.status not in (
        STATUS_APPROVED_UNCHANGED, STATUS_APPROVED_WITH_EDIT, STATUS_PROPOSED,
    ):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot cancel a decision with status={instance.status!r}",
        )
    await spec.cancel(instance)
    mark_reversed(instance_id, reason="cancelled_via_ui", decision_class=spec.name)
    _clear_decision_alert(instance_id, "dismissed", spec.alert_source)
    return _refresh(instance_id)


def _class_mode_response(decision_class: str) -> DecisionClassMode:
    # An unknown stored value reads as the fail-safe default, like the gate.
    mode = get_class_mode(decision_class)
    return DecisionClassMode(
        decision_class=decision_class,
        mode="auto_execute" if mode == "auto_execute" else "propose",
    )


def _has_active_principal() -> bool:
    """Whether a non-archived principal is on the roster. Fails closed."""
    from openexecutive.people.store import find_principal_person

    try:
        return find_principal_person() is not None
    except Exception:
        logger.exception("decisions: principal lookup failed — treating as none")
        return False


@router.get(
    f"/decisions/classes/{_CALENDAR_CLASS}", response_model=DecisionClassMode
)
def get_meeting_class_mode() -> DecisionClassMode:
    """Whether the Executive proposes meetings for approval or books them."""
    return _class_mode_response(_CALENDAR_CLASS)


@router.put(
    f"/decisions/classes/{_CALENDAR_CLASS}", response_model=DecisionClassMode
)
def set_meeting_class_mode(
    request: Request, body: DecisionClassModeUpdate
) -> DecisionClassMode:
    """Set the meeting class mode. Principal only — the same rule as the
    workspace settings (``chat._caller_is_principal_or_unclaimed``): letting
    the Executive book meetings on its own is the principal's call. While no
    principal exists (when that rule lets anyone in) only ``propose`` can be
    set — ``auto_execute`` is a 409. Every change is audited."""
    from openexecutive.api.routes.chat import _caller_is_principal_or_unclaimed

    if not _caller_is_principal_or_unclaimed(request):
        raise HTTPException(
            status_code=403,
            detail="Only the principal can change how meetings are scheduled",
        )
    if body.mode == "auto_execute" and not _has_active_principal():
        # Before anyone is the principal the PUT is open to every caller (so
        # first-run setup is never locked out), and auto-booking would then
        # be switched on by nobody in particular. Propose stays allowed.
        raise HTTPException(
            status_code=409,
            detail=(
                "Meetings can only be booked automatically once someone is set "
                "as the principal (the owner) on the People page. Until then "
                "they are proposed for approval."
            ),
        )
    before = _class_mode_response(_CALENDAR_CLASS).mode
    if body.mode != before:
        set_class_mode(_CALENDAR_CLASS, body.mode)
        from openexecutive.audit import log_event as audit_log

        audit_log(
            "decision_class_mode_changed",
            f"Meeting scheduling mode changed: {before} → {body.mode}",
            actor=api_caller.actor(request),
            details={
                "decision_class": _CALENDAR_CLASS,
                "mode": {"from": before, "to": body.mode},
            },
        )
    return _class_mode_response(_CALENDAR_CLASS)


@router.get("/audit/reliability", response_model=ReliabilityCard)
def get_reliability(
    request: Request,
    decision_class: str = _CALENDAR_CLASS,
    days: int = 30,
) -> ReliabilityCard:
    """How a class's decisions went. The principal's alone: it counts every
    instance, private ones included."""
    if not _approver_is_principal(request):
        raise HTTPException(status_code=403, detail="Only the owner can see this.")
    if days < 1 or days > 365:
        raise HTTPException(status_code=400, detail="days must be 1–365")
    return aggregate_reliability(decision_class, window_days=days)
