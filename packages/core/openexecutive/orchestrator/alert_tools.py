from __future__ import annotations

import contextlib
import json
import logging
from typing import Any

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


async def handle_create_alert(tool_input: dict[str, Any]) -> str:
    from openexecutive.alerts.models import AlertEvent
    from openexecutive.alerts.pipeline import schedule_evaluation
    from openexecutive.audit import log_event as audit_log

    try:
        event = AlertEvent(
            source=tool_input.get("source", "unknown"),
            external_id=tool_input.get("external_id", ""),
            subject=tool_input["subject"],
            body=tool_input["body"],
            **{"from": tool_input.get("from_address", "")},
        )
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
        schedule_evaluation(event)
        logger.info("create_alert: scheduled subject=%r", tool_input["subject"])
        audit_log(
            "alert",
            f"Alert: {str(tool_input['subject'])[:200]}",
            actor="executive",
            details={
                "source": tool_input.get("source", "unknown"),
                "external_id": tool_input.get("external_id", ""),
                "from": tool_input.get("from_address", ""),
                "body_preview": str(tool_input.get("body", ""))[:300],
            },
        )
        return json.dumps({"status": "alert_scheduled", "subject": tool_input["subject"]})
    except Exception as exc:
        logger.exception("create_alert: failed")
        audit_log(
            "alert",
            f"Alert FAILED: {str(tool_input.get('subject', ''))[:160]} — {exc}",
            actor="executive",
            details={
                "source": tool_input.get("source", "unknown"),
                "external_id": tool_input.get("external_id", ""),
                "error": str(exc)[:300],
                "ok": False,
            },
        )
        return json.dumps({"error": str(exc)})
