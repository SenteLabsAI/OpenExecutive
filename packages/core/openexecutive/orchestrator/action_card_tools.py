"""``propose_actions``: leave the person you are speaking with a card of
actions to approve with one tap (``delegation.action_cards``).

It rides with Act as me's tools (``delegation_tools.DELEGATION_TOOLS``), so it
is offered and re-checked the same way (``_writer``: the pin and the verified
surface). It opens no mailbox and does nothing itself, so it stays on after a
turn read mail, when ``message_person``, the calendar tools and roster writes
are withheld: that is what it is for. Nothing on the card happens until the
person approves it, signed in on the web: in the chat it was left in (its chip
carries the card's id) or on Today.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

PROPOSE_ACTIONS = "propose_actions"

PROPOSE_ACTIONS_TOOL: dict[str, Any] = {
    "name": PROPOSE_ACTIONS,
    "description": (
        "Leave the person you are speaking with a card of actions to approve: they "
        "see each action exactly as it will happen and tap Approve (in this "
        "chat, or on Today in the web app) to do it. Nothing happens until they do. Use it "
        "when an email of theirs calls for a meeting, a message to someone, or a new "
        "contact, and you can't do it yourself on this turn. Up to 5 actions a card. "
        "Kinds: `invite` (title, start, end as local date-times, attendee_person_ids "
        "from the roster, optional description), `message` (person_id from the "
        "roster, text exactly as it will be sent), `add_contact` (full_name, email, "
        "optional role; the owner's cards only). Tell them the card is waiting; "
        "never say it is done."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "why": {
                "type": "string",
                "description": "One line on what this is for, e.g. 'From Dana's email about the pilot'.",
            },
            "actions": {
                "type": "array",
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": ["invite", "message", "add_contact"]},
                        "title": {"type": "string"},
                        "start": {"type": "string", "description": "Local date and time, e.g. 2026-10-10T14:00."},
                        "end": {"type": "string", "description": "Local date and time, e.g. 2026-10-10T14:30."},
                        "attendee_person_ids": {"type": "array", "items": {"type": "integer"}},
                        "description": {"type": "string"},
                        "person_id": {"type": "integer"},
                        "text": {"type": "string"},
                        "full_name": {"type": "string"},
                        "email": {"type": "string"},
                        "role": {"type": "string"},
                    },
                    "required": ["kind"],
                },
            },
        },
        "required": ["why", "actions"],
    },
}


def _error(message: str) -> str:
    return json.dumps({"error": message})


async def handle_propose_actions(tool_input: dict[str, Any]) -> str:
    from openexecutive.delegation import action_cards
    from openexecutive.orchestrator.delegation_tools import _writer
    from openexecutive.orchestrator.schedule_tools import current_session

    writer = _writer(PROPOSE_ACTIONS)
    if isinstance(writer, str):
        return writer
    pinned = writer.pinned
    if getattr(pinned, "action_cards", 0) >= action_cards.CARDS_PER_TURN:
        return _error(f"That's {action_cards.CARDS_PER_TURN} cards this turn. Put the rest on one of them.")
    checked = action_cards.check(tool_input.get("actions"), writer.person, now=datetime.now(UTC))
    if isinstance(checked, str):
        return _error(checked)
    try:
        if len(action_cards.open_cards(writer.person.id)) >= action_cards.OPEN_CARDS_MAX:
            return _error(f"They already have {action_cards.OPEN_CARDS_MAX} cards waiting. Ask them to clear some first.")
        pinned.action_cards = getattr(pinned, "action_cards", 0) + 1
        session = current_session.get()
        decision_id = action_cards.propose(
            writer.person, checked, str(tool_input.get("why") or ""),
            session_id=getattr(session, "session_id", None),
        )
    except Exception:
        logger.warning("propose_actions: couldn't store the card", exc_info=True)
        return _error("Couldn't leave the card just now. Try again in a moment.")
    action_cards.audit("delegation_actions_proposed", f"Left person {writer.person.id} an action card", {
        "person_id": writer.person.id, "decision_id": decision_id,
        "kinds": [a["kind"] for a in checked],
    })
    return json.dumps({
        "status": "waiting_for_approval",
        "decision_id": decision_id,
        "actions": [a["summary"] for a in checked],
        "note": "Nothing has happened yet. It is in this chat and on Today in the web app for them to approve.",
    })


ACTION_CARD_TOOLS: list[dict[str, Any]] = [PROPOSE_ACTIONS_TOOL]
ACTION_CARD_TOOL_HANDLERS: dict[str, Any] = {PROPOSE_ACTIONS: handle_propose_actions}
