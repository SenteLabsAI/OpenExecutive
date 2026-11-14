"""The Executive reading its own record of what it did on its own.

``list_my_activity`` returns the same feed the Activity screen shows
(``api.routes.today._build_activity``): follow-ups and check-ins it ran,
workflows it finished, decisions and advice it recorded, alerts it raised and
cards that were settled. Without it, "what did you do on your own last night?"
had no answer but a guess. It reads the team's items plus the asker's own (``viewer``),
never another person's chats, cards or mail, and changes nothing.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

_MAX_ITEMS = 100
_MAX_HOURS = 24 * 30

LIST_MY_ACTIVITY_TOOL: dict[str, Any] = {
    "name": "list_my_activity",
    "description": (
        "Read your own record of what you did on your own, newest first: "
        "follow-ups and check-ins you ran, workflows and research you "
        "finished, decisions and advice you recorded, alerts you raised and "
        "cards that were approved or turned down. Use it whenever someone "
        "asks what you did, did overnight, or did on your own, instead of "
        "guessing or asking the owner. It is the same list the person asking "
        "sees on the Activity screen: the team's work and their own."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "hours": {
                "type": "integer",
                "description": "How far back to look, in hours (default 24, at most 720).",
            },
            "limit": {
                "type": "integer",
                "description": "How many entries at most (default 30, at most 100).",
            },
        },
    },
}


def _bounded(raw: Any, default: int, high: int) -> int:
    if isinstance(raw, bool):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, high))


def _refusal() -> str | None:
    """Why this turn may not read the feed, else None. The feed is the
    team's: a turn answering mail from someone not in People, or one with a
    caller nobody on the roster resolves to (an MCP caller off the roster),
    is refused, as the ``oe://today/activity`` resource refuses them. An
    unattended run (a department's work, a scheduled follow-up) is the
    Executive's own and reads it. Fails closed."""
    try:
        from openexecutive.orchestrator.outside_content import answers_off_team_mail
        from openexecutive.orchestrator.schedule_tools import current_session

        if answers_off_team_mail():
            return "this list is the team's, and this turn answers someone who is not on it"
        session = current_session.get()
        if session is None:
            return "this list is the team's, and nothing says who is asking"
        if not getattr(session, "unattended", False) and getattr(session, "caller_person_id", None) is None:
            return "this list is the team's, and whoever is asking is not on the team"
    except Exception:
        logger.warning("list_my_activity: couldn't tell who is asking — refusing", exc_info=True)
        return "couldn't tell who is asking"
    return None


def _reader() -> int | None:
    """Who this turn answers: their own items join the team's, in a
    conversation only they read. None (the team's alone) on an unattended
    run, or where others read the reply (a channel, a shared chat, a call)."""
    from openexecutive.delegation.settings import private_conversation
    from openexecutive.orchestrator.schedule_tools import current_session

    session = current_session.get()
    if session is None or getattr(session, "unattended", False):
        return None
    try:
        if not private_conversation(session):
            return None
    except Exception:
        logger.warning("list_my_activity: couldn't tell whether the chat is private", exc_info=True)
        return None
    person_id = getattr(session, "caller_person_id", None)
    return person_id if isinstance(person_id, int) and not isinstance(person_id, bool) else None


async def handle_list_my_activity(tool_input: dict[str, Any]) -> str:
    from openexecutive.api.routes.today import _build_activity

    if (reason := _refusal()) is not None:
        return json.dumps({"error": f"list_my_activity: {reason}"})
    hours = _bounded(tool_input.get("hours"), 24, _MAX_HOURS)
    limit = _bounded(tool_input.get("limit"), 30, _MAX_ITEMS)
    since = datetime.now(UTC) - timedelta(hours=hours)
    try:
        items = _build_activity(limit, since=since, viewer=_reader()).items
    except Exception:
        logger.exception("list_my_activity: the activity feed couldn't be read")
        return json.dumps({"error": "list_my_activity: the activity record couldn't be read right now"})
    return json.dumps({
        "hours": hours,
        "count": len(items),
        "items": [
            {
                "at": i.at,
                "what": i.summary,
                **({"detail": i.detail} if i.detail else {}),
                **({"for": i.target} if i.target else {}),
                **({"department": i.department} if i.department else {}),
                "kind": i.kind,
            }
            for i in items
        ],
    })


ACTIVITY_TOOLS: list[dict[str, Any]] = [LIST_MY_ACTIVITY_TOOL]

ACTIVITY_TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[str]]] = {
    "list_my_activity": handle_list_my_activity,
}

__all__ = [
    "ACTIVITY_TOOLS",
    "ACTIVITY_TOOL_HANDLERS",
    "LIST_MY_ACTIVITY_TOOL",
    "handle_list_my_activity",
]
