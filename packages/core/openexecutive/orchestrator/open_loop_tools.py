"""Chat tools over attunement open loops — what each person owes.

``list_open_loops`` answers "what is Sara waiting on / what does Ben owe me?".
What people owe can be sensitive, so the principal sees everyone's loops only
in a private conversation (web chat or a 1:1 DM); anyone else, and the
principal in a shared channel, sees only their own.
``close_open_loop`` closes one the user says is done or no longer needed, so
the nudge engine stops chasing it.

Closing is gated on who is asking. Loop ids appear in the reflection prompt and
in chat replies, and inbound email reaches the Executive, so a quoted "close
loop 12" from an outsider must not work: only a turn whose resolved speaker is
the principal or the loop's owner may close it. The unattended reflection pass
is not given this tool at all.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

LIST_OPEN_LOOPS_TOOL: dict[str, Any] = {
    "name": "list_open_loops",
    "description": (
        "List open loops — concrete things people on the roster committed to "
        "or were asked for in conversation and haven't reported done. Use it "
        "for questions like 'what is Sara waiting on?' or 'what does the team "
        "owe me?'. Overdue loops are already chased automatically."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "person_id": {
                "type": "integer",
                "description": "Only loops this person owns. Omit for everyone.",
            },
        },
    },
}

CLOSE_OPEN_LOOP_TOOL: dict[str, Any] = {
    "name": "close_open_loop",
    "description": (
        "Close an open loop the user says is done, no longer needed, or was "
        "cancelled, so it stops being chased. Take the loop_id from "
        "list_open_loops."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "loop_id": {"type": "integer"},
            "reason": {
                "type": "string",
                "enum": ["done", "not_needed", "cancelled"],
            },
        },
        "required": ["loop_id"],
    },
}

OPEN_LOOP_TOOLS: list[dict[str, Any]] = [LIST_OPEN_LOOPS_TOOL, CLOSE_OPEN_LOOP_TOOL]


def _audit(tool: str, ok: bool, summary: str, details: dict[str, Any]) -> None:
    from openexecutive.audit import log_event as audit_log

    audit_log(
        "tool_invocation",
        summary,
        actor="executive",
        details={"tool": tool, "ok": ok, **details},
    )


def _is_private_surface(session: Any) -> bool:
    """Whether a reply in this session is seen only by the person asking.

    Web chat and 1:1 DMs are private. Slack / Discord channels and threads,
    Telegram groups (negative chat ids), Google Chat spaces and email (which can
    carry cc's) are not — a list rendered there is read by everyone present,
    the same reason Slack keeps the alert board out of shared channels."""
    if getattr(session, "from_web_chat", False):
        return True
    sid = str(getattr(session, "session_id", "") or "")
    if sid.startswith(("slack:dm:", "discord:dm:")):
        return True
    if sid.startswith("telegram:"):
        return not sid.removeprefix("telegram:").startswith("-")
    return False


def _visible_owner(requested: int | None) -> tuple[bool, int | None]:
    """``(allowed, owner filter)`` for listing loops in the current turn.

    The principal may see everyone's loops, but only on a private surface.
    Anyone else — and the principal in a shared channel — sees only their own.
    No resolved speaker sees nothing."""
    from openexecutive.orchestrator.schedule_tools import current_session
    from openexecutive.people.store import get_person

    session = current_session.get()
    caller = getattr(session, "caller_person_id", None) if session is not None else None
    if caller is None:
        return False, None
    person = get_person(caller)
    if person is not None and person.is_principal and _is_private_surface(session):
        return True, requested
    if requested is not None and requested != caller:
        return False, None
    return True, caller


async def handle_list_open_loops(tool_input: dict[str, Any]) -> str:
    from openexecutive.attunement.open_loops import list_open_loops

    raw = tool_input.get("person_id")
    allowed, person_id = _visible_owner(raw if isinstance(raw, int) else None)
    if not allowed:
        return json.dumps({
            "status": "refused",
            "detail": (
                "open loops are visible to their owner, and to the principal in a "
                "private conversation"
            ),
        })
    loops = list_open_loops(person_id=person_id, limit=50)
    return json.dumps({
        "open_loops": [
            {
                "loop_id": loop.id,
                "owner": loop.owner_name,
                "owner_person_id": loop.owner_person_id,
                "description": loop.description,
                "due_at": loop.due_at,
            }
            for loop in loops
        ]
    })


def _may_close(owner_person_id: int) -> tuple[bool, int | None]:
    """Whether the current turn may close a loop owned by ``owner_person_id``,
    plus the caller id when one is known."""
    from openexecutive.orchestrator.schedule_tools import current_session
    from openexecutive.people.store import is_principal_or_self

    session = current_session.get()
    caller = getattr(session, "caller_person_id", None) if session is not None else None
    if caller is not None:
        return is_principal_or_self(caller, owner_person_id), caller
    # No resolved speaker: an unrostered sender on an adapter, a signed-in web
    # user who is not on the roster (a header-less web caller resolves to the
    # principal, so it never lands here), or an unattended session such as a
    # scheduled nudge — whose prompt quotes the loop text people wrote. None of
    # those may close a loop.
    return False, None


async def handle_close_open_loop(tool_input: dict[str, Any]) -> str:
    from openexecutive.attunement.open_loops import close_open_loop, get_open_loop

    try:
        loop_id = int(tool_input["loop_id"])
    except (KeyError, TypeError, ValueError) as exc:
        return json.dumps({"error": f"bad arguments: {exc}"})
    reason = str(tool_input.get("reason") or "done")
    if reason not in ("done", "not_needed", "cancelled"):
        reason = "done"
    loop = get_open_loop(loop_id)
    if loop is None:
        return json.dumps({"status": "not_found", "loop_id": loop_id})
    allowed, caller = _may_close(loop.owner_person_id)
    if not allowed:
        _audit("close_open_loop", False, f"close_open_loop #{loop_id} refused",
               {"loop_id": loop_id, "caller_person_id": caller})
        return json.dumps({
            "status": "refused",
            "detail": "only the principal or the loop's owner can close it",
        })
    closed = close_open_loop(loop_id, reason=reason, closed_by_person_id=caller)
    _audit("close_open_loop", closed, f"close_open_loop #{loop_id} ({reason})",
           {"loop_id": loop_id, "reason": reason, "caller_person_id": caller})
    return json.dumps({"status": "closed" if closed else "not_open", "loop_id": loop_id})


OPEN_LOOP_TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[str]]] = {
    "list_open_loops": handle_list_open_loops,
    "close_open_loop": handle_close_open_loop,
}
