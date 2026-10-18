"""``propose_actions``: leave the person you are speaking with a card of
actions to approve with one tap (``delegation.action_cards``).

It rides with Act as me's tools (``delegation_tools.DELEGATION_TOOLS``), so it
is offered and re-checked the same way (``_writer``: the pin and the verified
surface). It opens no mailbox and does nothing itself, so it stays on after a
turn read mail, when ``message_person``, the calendar tools and roster writes
are withheld: that is what it is for. Nothing on the card happens until the
person approves it, signed in on the web: in the chat it was left in (its chip
carries the card's id) or on Today. The one exception is a card of actions
they allowed with Approve + allow, with Suggested actions in training
(``action_cards.run_on_its_own``). That card is usually proposed on a turn
that read untrusted mail, so a message it sends has model-written text
nobody reads first: the allowance covers who and what kind, not the words,
and the sensitive, link, amount and length checks and the daily cap are
what bound it.

A turn another AI app asked to act (``Session.mcp_client_may_act``) is
offered this tool alone of Act as me's, with no mailbox: what reaches another
person (a message, an invite) is proposed here instead of sent. Its speaker
is the caller the API verified (``Session.caller_person_id``), with Act as me
on. Such a card follows their Handle it for me: it is carried out at once
when that is on (and Suggested actions not in training) and plain code finds
nothing against it (``action_cards.on_its_own_refusal`` with
``follow_handle_it``), and the turn read no outside text
(``outside_content.read_outside``) or mail; otherwise it waits for their
tap, as any card does.
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


def _ai_app_speaker(session: Any) -> tuple[Any, Any] | str:
    """``(person, pinned)`` for a turn another AI app asked to act, or the
    refusal: the caller the API verified, on the roster, with Act as me on
    (a card is approved, and carried out, under Act as me)."""
    from openexecutive.delegation.settings import can_delegate, is_enabled, turn_delegation
    from openexecutive.orchestrator.people_tools import _is_verified_speaker_surface
    from openexecutive.people.store import get_person

    pinned = turn_delegation(session)
    caller = getattr(session, "caller_person_id", None)
    try:
        person = get_person(int(caller)) if isinstance(caller, int) and not isinstance(caller, bool) else None
    except Exception:
        logger.warning("propose_actions: couldn't read the speaker", exc_info=True)
        person = None
    if pinned is None or person is None or person.id is None or not _is_verified_speaker_surface(session):
        return _error(
            "I can't tell who this is for, so I can't leave a card. Tell the person "
            "to do it in Open Executive itself. Do not retry."
        )
    if not can_delegate(person) or not is_enabled(person.id):
        return _error(
            "Cards need Act as me on for them, and it is off. Tell the person to do "
            "it in Open Executive itself. Do not retry."
        )
    return person, pinned


async def handle_propose_actions(tool_input: dict[str, Any]) -> str:
    from openexecutive.audit import rows_via
    from openexecutive.delegation import action_cards
    from openexecutive.orchestrator import outside_content
    from openexecutive.orchestrator.delegation_tools import _writer
    from openexecutive.orchestrator.schedule_tools import current_session, mcp_client_acting

    session = current_session.get()
    # Another AI app asked, and may act: the card follows their Handle it for me.
    ai_app = mcp_client_acting(session)
    if ai_app:
        found = _ai_app_speaker(session)
        if isinstance(found, str):
            return found
        person, pinned = found
    else:
        writer = _writer(PROPOSE_ACTIONS)
        if isinstance(writer, str):
            return writer
        person, pinned = writer.person, writer.pinned
    if getattr(pinned, "action_cards", 0) >= action_cards.CARDS_PER_TURN:
        return _error(f"That's {action_cards.CARDS_PER_TURN} cards this turn. Put the rest on one of them.")
    checked = action_cards.check(tool_input.get("actions"), person, now=datetime.now(UTC))
    if isinstance(checked, str):
        return _error(checked)
    via = (rows_via() or "an AI app") if ai_app else None
    try:
        if len(action_cards.open_cards(person.id)) >= action_cards.OPEN_CARDS_MAX:
            return _error(f"They already have {action_cards.OPEN_CARDS_MAX} cards waiting. Ask them to clear some first.")
        pinned.action_cards = getattr(pinned, "action_cards", 0) + 1
        # In training, a card of actions they all allowed is carried out now;
        # from an AI app, one their Handle it for me covers.
        on_its_own = action_cards.on_its_own_refusal(
            person, checked, now=datetime.now(UTC), follow_handle_it=ai_app,
        ) is None
        # From an AI app, a turn that read outside text (a document, a web
        # page, mail) never sends on its own: its words may be that text's.
        if ai_app and on_its_own and (
            outside_content.read_outside() or getattr(pinned, "touched_mail", False) is True
        ):
            on_its_own = False
        decision_id = action_cards.propose(
            person, checked, str(tool_input.get("why") or ""),
            session_id=getattr(session, "session_id", None), on_its_own=on_its_own,
            follow_handle_it=ai_app, via=via,
        )
    except Exception:
        logger.warning("propose_actions: couldn't store the card", exc_info=True)
        return _error("Couldn't leave the card just now. Try again in a moment.")
    action_cards.audit("delegation_actions_proposed", f"Left person {person.id} an action card", {
        "person_id": person.id, "decision_id": decision_id,
        "kinds": [a["kind"] for a in checked],
        **({"via": via} if via else {}),
    })
    if on_its_own:
        try:
            results = await action_cards.run_on_its_own(decision_id, person)
        except Exception:
            logger.warning("propose_actions: carrying out an allowed card failed", exc_info=True)
            results = None
        if results is not None:
            return json.dumps({
                "status": "done_on_its_own",
                "decision_id": decision_id,
                "actions": [
                    {"action": checked[r["index"]]["summary"], "status": r["status"], "detail": r.get("detail", "")}
                    for r in results
                ],
                "note": (
                    "Their settings (Handle it for me) let these go without a tap, so they were carried out now. Tell them what happened."
                    if ai_app else
                    "They allowed these in training, so they were carried out now. Tell them what happened."
                ),
            })
    return json.dumps({
        "status": "waiting_for_approval",
        "decision_id": decision_id,
        "actions": [a["summary"] for a in checked],
        "note": "Nothing has happened yet. It is in this chat and on Today in the web app for them to approve.",
    })


ACTION_CARD_TOOLS: list[dict[str, Any]] = [PROPOSE_ACTIONS_TOOL]
ACTION_CARD_TOOL_HANDLERS: dict[str, Any] = {PROPOSE_ACTIONS: handle_propose_actions}
