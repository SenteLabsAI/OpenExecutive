"""Chat tools that undo or settle something the person said is done or unwanted.

Before these existed a reply could only say "Done" to "remove it from the
brief", "cancel that reminder" or "I sent it": nothing recorded it, so the
brief kept the item and the follow-up still fired.

- ``leave_out_of_brief`` keeps an item out of the principal's brief and the
  /today header (``briefing.brief_requests``) for two weeks. The principal
  only, on a surface that verified it is them, with their own words quoted.
- ``list_followups`` / ``cancel_followup`` / ``move_followup``: the pending
  follow-ups the speaker scheduled, from a conversation of their own (or
  this one, when nobody else reads it). Being in a shared thread, or being
  who a follow-up is for, is not enough. Nothing else can be listed,
  cancelled or moved from here.
- ``mark_draft_sent``: the speaker's latest chat draft that still counts as
  waiting is marked sent, so the brief stops calling it waiting
  (``delegation.drafts``; the Gmail check marks the rest).

Every tool needs a verified speaker (``people_tools``), so an email, an
unattended run or an unrostered sender gets a refusal. The conversation review
(``memory.chat_review``) replays the person's own words through the same
handlers.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

FOLLOWUP_KIND = "ad_hoc"
_LIST_MAX = 20

LEAVE_OUT_OF_BRIEF_TOOL: dict[str, Any] = {
    "name": "leave_out_of_brief",
    "description": (
        "Keep something out of the principal's morning brief and the Today "
        "header for the next two weeks, when the principal asks ('remove it "
        "from the brief', 'stop putting the Halcyon renewal in my brief'). "
        "item: what to leave out, in a few words a reader would recognise, "
        "e.g. 'the Halcyon renewal'. source_quote: the principal's exact "
        "words from this message asking for it. The principal only."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "item": {"type": "string", "description": "What to leave out, a few words."},
            "source_quote": {
                "type": "string",
                "description": "The principal's exact words from this message asking for it.",
            },
        },
        "required": ["item", "source_quote"],
    },
}

LIST_FOLLOWUPS_TOOL: dict[str, Any] = {
    "name": "list_followups",
    "description": (
        "List the pending follow-ups and reminders the person you are "
        "talking to scheduled: id, when, and what each says. Use it before cancel_followup or "
        "move_followup to find the id."
    ),
    "input_schema": {"type": "object", "properties": {}},
}

CANCEL_FOLLOWUP_TOOL: dict[str, Any] = {
    "name": "cancel_followup",
    "description": (
        "Cancel a pending follow-up or reminder when the person says it is no "
        "longer needed. action_id from list_followups. Only follow-ups the "
        "person scheduled."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"action_id": {"type": "integer"}},
        "required": ["action_id"],
    },
}

MOVE_FOLLOWUP_TOOL: dict[str, Any] = {
    "name": "move_followup",
    "description": (
        "Move a pending follow-up or reminder to a new time when the person "
        "asks. action_id from list_followups; run_at is ISO 8601 with a zone, "
        "in the future."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action_id": {"type": "integer"},
            "run_at": {"type": "string", "description": "ISO 8601, e.g. 2026-10-20T09:00:00-04:00"},
        },
        "required": ["action_id", "run_at"],
    },
}

MARK_DRAFT_SENT_TOOL: dict[str, Any] = {
    "name": "mark_draft_sent",
    "description": (
        "When the person says they sent (or deleted) the email draft you "
        "saved in their mailbox from chat, mark their latest waiting chat "
        "draft as done so the brief stops listing it as waiting."
    ),
    "input_schema": {"type": "object", "properties": {}},
}

CORRECTION_TOOLS: list[dict[str, Any]] = [
    CANCEL_FOLLOWUP_TOOL,
    LEAVE_OUT_OF_BRIEF_TOOL,
    LIST_FOLLOWUPS_TOOL,
    MARK_DRAFT_SENT_TOOL,
    MOVE_FOLLOWUP_TOOL,
]
CORRECTION_TOOL_NAMES = frozenset(t["name"] for t in CORRECTION_TOOLS)


def _session() -> Any:
    from openexecutive.orchestrator.schedule_tools import current_session

    return current_session.get()


def _audit(tool: str, ok: bool, summary: str, details: dict[str, Any]) -> None:
    from openexecutive.audit import log_event as audit_log

    session = _session()
    audit_log(
        "tool_invocation",
        summary,
        session_id=getattr(session, "session_id", None),
        actor="executive",
        details={"tool": tool, "ok": ok, **details},
    )


def _refused(detail: str) -> str:
    return json.dumps({"status": "refused", "detail": detail})


def _speaker() -> Any:
    """The verified speaker's People row (principal or teammate), or None."""
    from openexecutive.orchestrator.people_tools import (
        is_principal_on_verified_surface,
        teammate_on_verified_surface,
    )
    from openexecutive.people.store import get_person

    session = _session()
    if session is None or getattr(session, "unattended", False):
        return None
    try:
        if is_principal_on_verified_surface(session):
            caller = getattr(session, "caller_person_id", None)
            if caller is not None:
                return get_person(int(caller))
            from openexecutive.people.store import find_principal_person

            return find_principal_person()
        return teammate_on_verified_surface(session)
    except Exception:
        logger.warning("correction_tools: speaker lookup failed", exc_info=True)
        return None


_NOT_VERIFIED = (
    "this request did not come from a conversation that confirms who is asking "
    "(the web app signed in, or their own Slack, Discord or Telegram)"
)


# --------------------------------------------------------------------------- #
# leave_out_of_brief
# --------------------------------------------------------------------------- #


async def handle_leave_out_of_brief(tool_input: dict[str, Any]) -> str:
    from openexecutive.briefing import brief_requests
    from openexecutive.orchestrator.fact_tools import _quote_error

    tool = "leave_out_of_brief"
    person = _speaker()
    if person is None or not getattr(person, "is_principal", False):
        return _refused("only the principal can change their brief, and " + _NOT_VERIFIED)
    item = brief_requests.clean_text(str(tool_input.get("item") or ""))
    quote = str(tool_input.get("source_quote") or "").strip()
    if not item:
        return json.dumps({"error": "item is required: what to leave out, in a few words"})
    error = _quote_error(quote, _session())
    if error:
        return json.dumps({"error": error})
    row = brief_requests.add(
        brief_requests.KIND_LEAVE_OUT, item, source_quote=quote, person_id=person.id,
        session_id=getattr(_session(), "session_id", None),
    )
    if row is None:
        return json.dumps({"status": "noop", "noop": True, "detail": "already left out, or the list is full"})
    _audit(tool, True, f"leave_out_of_brief #{row.id}", {"request_id": row.id})
    return json.dumps({
        "status": "left_out", "item": row.text, "until": row.expires_at[:10],
    })


# --------------------------------------------------------------------------- #
# follow-ups
# --------------------------------------------------------------------------- #


def _scheduled_by(action: Any, person: Any, session: Any) -> bool:
    """Whether ``person`` scheduled ``action``: from a conversation that is
    theirs (``sessions.caller_person_id``), or from this one when nobody else
    can read it. A shared Slack or Discord thread is one session for everyone
    in it, so being in it is not enough; nor is being who a follow-up is for."""
    from openexecutive.delegation.settings import private_conversation
    from openexecutive.memory.session_store import get_session_owner

    origin = action.originating_session_id
    if not origin:
        return False
    if origin == getattr(session, "session_id", None) and private_conversation(session):
        return True
    try:
        from openexecutive.memory.episodic import _resolve_db_path

        exists, owner = get_session_owner(str(origin), db_path=_resolve_db_path(None))
    except Exception:
        logger.warning("correction_tools: session owner lookup failed", exc_info=True)
        return False
    if not exists:
        return False
    # A conversation with no recorded owner is the principal's (the web chat
    # before sign-in named people, the CLI).
    return owner == person.id or (owner is None and bool(getattr(person, "is_principal", False)))


def _mine(action: Any, person: Any, session: Any) -> bool:
    """Whether ``action`` is a pending follow-up ``person`` scheduled."""
    if action is None or action.status != "pending" or action.kind != FOLLOWUP_KIND:
        return False
    if action.channel == "__internal__":
        return False
    return _scheduled_by(action, person, session)


async def handle_list_followups(tool_input: dict[str, Any]) -> str:
    from openexecutive.memory.episodic import list_scheduled_actions

    person = _speaker()
    if person is None:
        return _refused(_NOT_VERIFIED)
    session = _session()
    rows = [
        a for a in list_scheduled_actions(status="pending", limit=500, exclude_internal=True)
        if _mine(a, person, session)
    ][:_LIST_MAX]
    return json.dumps({
        "followups": [
            {"action_id": a.id, "run_at": a.run_at, "says": " ".join(a.intent_text.split())[:200]}
            for a in rows
        ],
    })


async def handle_cancel_followup(tool_input: dict[str, Any]) -> str:
    from openexecutive.memory.episodic import cancel_scheduled_action, get_scheduled_action

    tool = "cancel_followup"
    person = _speaker()
    if person is None:
        return _refused(_NOT_VERIFIED)
    try:
        action_id = int(tool_input["action_id"])
    except (KeyError, TypeError, ValueError):
        return json.dumps({"error": "action_id is required (from list_followups)"})
    action = get_scheduled_action(action_id)
    if action is None:
        return json.dumps({"status": "not_found", "action_id": action_id})
    if not _mine(action, person, _session()):
        _audit(tool, False, f"cancel_followup #{action_id} refused", {"action_id": action_id})
        return _refused("only a pending follow-up you scheduled")
    result = cancel_scheduled_action(action_id)
    if result != "cancelled":
        return json.dumps({"status": "not_found" if result == "not_found" else "not_open", "action_id": action_id})
    _audit(tool, True, f"cancel_followup #{action_id}", {"action_id": action_id})
    return json.dumps({"status": "cancelled", "action_id": action_id})


async def handle_move_followup(tool_input: dict[str, Any]) -> str:
    from openexecutive.config import get_settings
    from openexecutive.memory.episodic import _get_conn, _resolve_db_path, get_scheduled_action

    tool = "move_followup"
    person = _speaker()
    if person is None:
        return _refused(_NOT_VERIFIED)
    try:
        action_id = int(tool_input["action_id"])
        parsed = datetime.fromisoformat(str(tool_input["run_at"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return json.dumps({"error": "action_id (from list_followups) and run_at (ISO 8601) are required"})
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    run_at = parsed.astimezone(UTC)
    now = datetime.now(UTC)
    if run_at <= now:
        return json.dumps({"error": "run_at must be in the future"})
    if run_at > now + timedelta(days=get_settings().max_scheduled_horizon_days):
        return json.dumps({"error": "run_at is too far out"})
    action = get_scheduled_action(action_id)
    if action is None:
        return json.dumps({"status": "not_found", "action_id": action_id})
    if not _mine(action, person, _session()):
        _audit(tool, False, f"move_followup #{action_id} refused", {"action_id": action_id})
        return _refused("only a pending follow-up you scheduled")
    with _get_conn(_resolve_db_path(None)) as conn:
        cur = conn.execute(
            "UPDATE scheduled_actions SET run_at = ? WHERE id = ? AND status = 'pending'",
            (run_at.isoformat(), action_id),
        )
    if cur.rowcount == 0:
        return json.dumps({"status": "not_open", "action_id": action_id})
    _audit(tool, True, f"move_followup #{action_id}", {"action_id": action_id, "run_at": run_at.isoformat()})
    return json.dumps({"status": "moved", "action_id": action_id, "run_at": run_at.isoformat()})


# --------------------------------------------------------------------------- #
# mark_draft_sent
# --------------------------------------------------------------------------- #


async def handle_mark_draft_sent(tool_input: dict[str, Any]) -> str:
    from openexecutive.delegation import drafts

    tool = "mark_draft_sent"
    person = _speaker()
    if person is None or person.id is None:
        return _refused(_NOT_VERIFIED)
    now = datetime.now(UTC)
    row_id = drafts.latest_waiting(int(person.id), now - drafts.SETTLE_WINDOW)
    if row_id is None:
        return json.dumps({"status": "noop", "noop": True, "detail": "no chat draft is waiting"})
    drafts.mark_gone(row_id, now)
    _audit(tool, True, "mark_draft_sent", {"person_id": person.id})
    return json.dumps({"status": "marked_sent"})


CORRECTION_TOOL_HANDLERS: dict[str, Any] = {
    "cancel_followup": handle_cancel_followup,
    "leave_out_of_brief": handle_leave_out_of_brief,
    "list_followups": handle_list_followups,
    "mark_draft_sent": handle_mark_draft_sent,
    "move_followup": handle_move_followup,
}
