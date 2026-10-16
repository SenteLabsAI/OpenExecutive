from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import uuid
from collections.abc import Iterator
from typing import Any

from openexecutive.people.models import Person

logger = logging.getLogger(__name__)

CREATE_ALERT_TOOL: dict[str, Any] = {
    "name": "create_alert",
    "description": (
        "Create an operational alert for something in an inbound message that requires "
        "attention or follow-up. Use for urgent issues, important decisions, or anything "
        "the executive should be explicitly notified about. Do NOT use for routine emails."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "subject": {
                "type": "string",
                "description": "Brief alert headline",
            },
            "body": {
                "type": "string",
                "description": "Full description of what needs attention",
            },
            "source": {
                "type": "string",
                "description": "Origin of the alert, e.g. 'email'",
            },
            "external_id": {
                "type": "string",
                "description": "ID of the originating message",
            },
            "from_address": {
                "type": "string",
                "description": "Sender of the originating message",
            },
            "department": {
                "type": "string",
                "description": (
                    "Optional department slug (e.g. 'finance'). When set, the alert "
                    "is tagged with 'department:<slug>' for routing and filtering."
                ),
            },
            "assigned_to_person_id": {
                "type": "integer",
                "description": "Optional person id to route this alert to.",
            },
        },
        "required": ["subject", "body"],
    },
}


def _routable_person(person_id: int) -> bool:
    """False only for one of the principal's contacts: they cannot sign in to
    see a routed alert, and routing is what alert review chases. Any other id
    routes as before (the pipeline resolves it)."""
    try:
        from openexecutive.people.store import get_person

        person = get_person(person_id)
    except Exception:
        return True
    return person is None or person.kind == "team"


# Who created the saved workflow whose step is raising an alert now
# (``workflows.action_step``). A workflow runs with no chat turn — from the
# Jobs page or on its cadence — so its creator stands in for the speaker.
_workflow_owner: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "create_alert_workflow_owner", default=None
)


@contextlib.contextmanager
def raised_for_workflow_owner(person_id: int | None) -> Iterator[None]:
    """Run the block as a step of a workflow ``person_id`` created."""
    token = _workflow_owner.set(person_id)
    try:
        yield
    finally:
        _workflow_owner.reset(token)


def team_member_speaker(session: Any) -> Person | None:
    """The team member (not the owner) whose turn this is, or None. Their
    alerts are labelled as theirs and never broadcast company-wide, and they
    may dismiss only the alerts assigned to them (``schedule_tools``)."""
    from openexecutive.orchestrator import department_gate

    return department_gate.speaker(session)


def _team_member_raising(session: Any) -> Person | None:
    """The team member an alert raised now comes from: the speaker, else the
    creator of the workflow raising it, unless that is the principal. A
    creator the roster can't place still counts as a team member."""
    speaker = team_member_speaker(session)
    if speaker is not None:
        return speaker
    owner_id = _workflow_owner.get()
    if owner_id is None:
        return None
    try:
        from openexecutive.people.store import get_person

        owner = get_person(owner_id)
    except Exception:
        logger.warning("create_alert: couldn't read the workflow's creator", exc_info=True)
        owner = None
    if owner is None:
        return Person(id=owner_id, full_name="", kind="team")
    return None if owner.is_principal else owner


def _origin_for(speaker: Person | None, tool_input: dict[str, Any]) -> tuple[str, str, str, str]:
    """(source, external_id, from, raised_by) for the alert. A team member's
    own words decide none of the first three: an alert they raise names them
    and claims no source or message id a real alert could arrive with."""
    if speaker is None:
        return (
            str(tool_input.get("source") or "unknown"),
            str(tool_input.get("external_id") or ""),
            str(tool_input.get("from_address") or ""),
            "",
        )
    from openexecutive.alerts.models import TEAM_RAISED_SOURCE

    name = (speaker.full_name or "").strip() or "a team member"
    return TEAM_RAISED_SOURCE, uuid.uuid4().hex, speaker.email or name, name


async def handle_create_alert(tool_input: dict[str, Any]) -> str:
    from openexecutive.alerts.models import AlertEvent
    from openexecutive.alerts.pipeline import schedule_evaluation
    from openexecutive.audit import log_event as audit_log
    from openexecutive.orchestrator.schedule_tools import current_session

    # A turn about the principal's private mail raises a private alert (the
    # pipeline routes it to the principal whatever was asked below), and its
    # audit rows — which name the sender and quote the body — are the
    # principal's alone to read.
    private = getattr(current_session.get(), "private_to_principal", False) is True
    source, external_id, from_address = (
        str(tool_input.get("source") or "unknown"),
        str(tool_input.get("external_id") or ""),
        str(tool_input.get("from_address") or ""),
    )
    try:
        source, external_id, from_address, raised_by = _origin_for(
            _team_member_raising(current_session.get()), tool_input,
        )
        event = AlertEvent.model_validate({
            "source": source,
            "external_id": external_id,
            "subject": tool_input["subject"],
            "body": tool_input["body"],
            "from": from_address,
            "raised_by": raised_by,
            "user": raised_by or None,
        })
        # Carry department and person routing hints via the channel field
        # (AlertEvent.channel is a free-text tag used by the pipeline).
        dept = str(tool_input.get("department") or "").strip()
        person_id_raw = tool_input.get("assigned_to_person_id")
        if dept:
            event.channel = f"department:{dept}"
        if person_id_raw is not None:
            with contextlib.suppress(TypeError, ValueError):
                routed_id = int(person_id_raw)
                if _routable_person(routed_id):
                    event.routed_to_person_id = routed_id
                    event.user = f"person:{routed_id}"
        if private:
            event.private = True
        schedule_evaluation(event)
        logger.info("create_alert: scheduled subject=%r", tool_input["subject"])
        audit_log(
            "alert",
            f"Alert: {str(tool_input['subject'])[:200]}",
            actor="executive",
            details={
                "source": source,
                "external_id": external_id,
                "from": from_address,
                "raised_by": raised_by,
                "body_preview": str(tool_input.get("body", ""))[:300],
            },
            private=event.private,
        )
        return json.dumps({"status": "alert_scheduled", "subject": tool_input["subject"]})
    except Exception as exc:
        logger.exception("create_alert: failed")
        audit_log(
            "alert",
            f"Alert FAILED: {str(tool_input.get('subject', ''))[:160]} — {exc}",
            actor="executive",
            details={
                "source": source,
                "external_id": external_id,
                "error": str(exc)[:300],
                "ok": False,
            },
            private=private,
        )
        return json.dumps({"error": str(exc)})
