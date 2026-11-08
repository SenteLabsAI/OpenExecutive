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
        "the executive should be explicitly notified about. Do NOT use for routine emails. "
        "Never use it for a task someone asks you to add or track (a to-do, something "
        "they or a teammate owe): that is assign_open_loop, which saves the task before "
        "you confirm it. "
        "Say what the card needs from its reader in 'needs', so the card gets the right "
        "controls and their reply comes straight back to you to carry on: 'answer' when you "
        "need information from them (put the exact question in 'question'), 'choice' when "
        "they pick one of 2 to 4 options (give them in 'choices'), 'ok' only when approving "
        "makes you do one exact thing (spell it out in the body), 'fyi' when nothing is "
        "needed. One card asks one thing: put related questions in one 'question'. "
        "When the sender already has an open card, you're told which; call again with "
        "'joins_card' set to say whether this is the same matter or a different decision."
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
            "needs": {
                "type": "string",
                "enum": ["answer", "choice", "ok", "fyi"],
                "description": "What the card needs from its reader.",
            },
            "question": {
                "type": "string",
                "description": (
                    "With 'answer' or 'choice': the question, in plain words, as you'd ask it "
                    "(e.g. 'Which 3 to 5 new features go in the post?')."
                ),
            },
            "choices": {
                "type": "array",
                "items": {"type": "string"},
                "description": "With 'choice': 2 to 4 short options, each a few words.",
            },
            "joins_card": {
                "type": "integer",
                "description": (
                    "Only when told the sender already has an open card: that card's id when "
                    "this is the same matter (a reminder, more detail, a change to what they "
                    "asked), so it joins that card; 0 when it is a different decision, so it "
                    "gets its own card and its own answer."
                ),
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


def _card_ask(tool_input: dict[str, Any]) -> tuple[str, str, list[str]]:
    """(needs, question, choices) the card says it needs. A pick
    with fewer than two usable choices is asked as an answer; an answer or
    pick without a question asks the card's subject."""
    from openexecutive.alerts.models import (
        CARD_NEEDS,
        MAX_CHOICE_CHARS,
        MAX_CHOICES,
        MIN_CHOICES,
        NEEDS_ANSWER,
        NEEDS_CHOICE,
    )

    needs = str(tool_input.get("needs") or "").strip().lower()
    if needs not in CARD_NEEDS:
        return "", "", []
    raw = tool_input.get("choices")
    choices: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        text = " ".join(str(item or "").split())[:MAX_CHOICE_CHARS]
        if text and text.lower() not in {c.lower() for c in choices}:
            choices.append(text)
    choices = choices[:MAX_CHOICES]
    if needs == NEEDS_CHOICE and len(choices) < MIN_CHOICES:
        needs = NEEDS_ANSWER
    if needs != NEEDS_CHOICE:
        choices = []
    question = " ".join(str(tool_input.get("question") or "").split())[:500]
    if needs in (NEEDS_ANSWER, NEEDS_CHOICE) and not question:
        question = " ".join(str(tool_input.get("subject") or "").split())[:500]
    if needs not in (NEEDS_ANSWER, NEEDS_CHOICE):
        question = ""
    return needs, question, choices


# A card in one of these is done with; any other status (unread, read,
# snoozed) is still open, and keeps its message id.
_CLOSED_CARD_STATUSES = frozenset({"ack", "dismissed", "expired", "resolved"})


def _fresh_external_id(source: str, external_id: str, *, folds: bool = False) -> str:
    """The card's message id, unless a card already holds it: then a fresh
    one, so the new ask isn't taken for a replay of that card and dropped.
    A closed card always gives way (the same mail handled again); an open
    one only when the new ask folds into it by sender (``folds``), since
    the pipeline drops a second card for one id before it can fold."""
    if not external_id:
        return external_id
    try:
        from openexecutive.alerts import store

        held = store.get_alert_by_external(source, external_id)
    except Exception:
        logger.warning("create_alert: couldn't check the message id", exc_info=True)
        return external_id
    if held is None or (held.status not in _CLOSED_CARD_STATUSES and not folds):
        return external_id
    return f"{external_id}:{uuid.uuid4().hex[:8]}"


def _one_card_per_sender(event: Any, session: Any) -> None:
    """A turn answering an email: while a card from that sender is still
    open, what they write next joins it ("Then: ...") instead of making a
    second card, so one approval answers them once. Keyed by the address
    the mail really came from (``Session.email_from``), never what the mail
    or the model says, and the same for every mail service. A card a team
    member raised keeps its own key (the pipeline prefixes it). A card routed
    to one person keys apart from the shared one, and a private card is kept
    apart by the pipeline (``private:`` prefix)."""
    if getattr(session, "origin_channel", "") != "email" or event.raised_by:
        return
    sender = str(getattr(session, "email_from", "") or "").strip().lower()
    if not sender:
        return
    routed = str(getattr(event, "user", "") or "")
    routed_part = f":{routed}" if routed.startswith("person:") else ""
    event.dedup_hint = f"email-from:{sender}{routed_part}"
    event.follow_up = True


def _sender_card_choice(event: Any, joins: Any) -> str | None:
    """One card per matter, not per sender: a sender's second, unrelated
    ask never joins the card of their first, where one answer would settle
    both. While the sender has open cards, the turn says which one this is
    (``joins_card``): that card's id joins it, 0 starts a card of its own
    (keyed ``email-from:<sender>#<n>``, so what they write about it next
    can join it). Returns what to tell the turn when it hasn't said yet,
    else None with ``event`` keyed to its card. Never raises."""
    if not event.follow_up or not event.dedup_hint:
        return None
    from openexecutive.alerts import store

    base = event.dedup_hint
    stored = f"private:{base}" if event.private else base
    try:
        cards = store.open_alerts_keyed(event.source, stored)
    except Exception:
        logger.warning("create_alert: couldn't read the sender's open cards", exc_info=True)
        return None
    if not cards:
        return None
    pick: int | None = None
    if joins is not None and not isinstance(joins, bool):
        with contextlib.suppress(TypeError, ValueError):
            pick = int(joins)
    if pick is None:
        listed = "; ".join(
            f'card {c.id}: "{c.headline}"' + (f' (asks: "{c.question}")' if c.question else "")
            for c in cards
        )
        return json.dumps({
            "status": "sender_has_open_card",
            "open_cards": [{"id": c.id, "headline": c.headline} for c in cards],
            "message": (
                f"Nothing was raised yet. This sender already has open cards: {listed}. "
                "Call create_alert again with the same fields plus 'joins_card': that card's id "
                "if this is the same matter, or 0 if it is a different decision, so each "
                "decision gets its own card and answer."
            ),
        })
    match = next((c for c in cards if c.id == pick), None) if pick else None
    if match is not None:
        key = str(match.dedup_key or "")
        event.dedup_hint = key.removeprefix("private:") if event.private else key
    else:
        event.dedup_hint = f"{base}#{uuid.uuid4().hex[:8]}"
    return None


# What create_alert answers when the question is for the person in the chat.
ASK_IN_REPLY = (
    "Nothing was raised. You are talking with the person this card would ask, so "
    "ask them in your reply instead and carry on from their answer. Don't say you "
    "raised a card."
)

# What create_alert answers once the alert is handed to review.
SENT_FOR_REVIEW = (
    "Sent for review. It becomes a card only if review keeps it, so don't tell "
    "them a card is waiting; say what you need in your reply if it matters now."
)


def _asks_the_speaker(session: Any, tool_input: dict[str, Any]) -> bool:
    """Whether this card would only ask the principal a question while they
    are in the chat: they speak live (not on mail, not a workflow or other
    unattended run, not a shared chat) and the card goes to no one else (no
    department, no other person). Such a question belongs in the reply, not
    on a card they find later. Never raises; False when unsure."""
    try:
        from openexecutive.alerts.models import NEEDS_ANSWER, NEEDS_CHOICE
        from openexecutive.orchestrator.content_trust import principal_speaking

        needs = str(tool_input.get("needs") or "").strip().lower()
        if needs not in (NEEDS_ANSWER, NEEDS_CHOICE):
            return False
        if _workflow_owner.get() is not None or not principal_speaking(session):
            return False
        if str(getattr(session, "origin_channel", "") or "") == "email":
            return False
        if getattr(session, "shared_chat", False) is True:
            return False
        if str(tool_input.get("department") or "").strip():
            return False
        routed = tool_input.get("assigned_to_person_id")
        return routed is None or routed == getattr(session, "caller_person_id", None)
    except Exception:
        return False


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
    # A turn answering mail from someone not in People: the alert is for the
    # principal alone, routed to no one else, and names the mail's real
    # sender rather than whatever the mail asked it to say
    # (orchestrator.outside_content.SENDER).
    from openexecutive.orchestrator.outside_content import answers_off_team_mail

    if answers_off_team_mail():
        private = True
        sender = getattr(current_session.get(), "email_from", "") or ""
        tool_input = {
            **{k: v for k, v in tool_input.items() if k not in ("department", "assigned_to_person_id")},
            "source": "email",
            "from_address": sender,
        }
    # A script or saved workflow calls this without the tool schema's checks,
    # so a missing field gets an answer the caller can act on, not a KeyError.
    missing = [k for k in ("subject", "body") if not str(tool_input.get(k) or "").strip()]
    if missing:
        logger.warning("create_alert: missing %s", ", ".join(missing))
        return json.dumps({"error": (
            f"create_alert needs {' and '.join(missing)}: pass a short 'subject' "
            "headline and a 'body' saying what needs attention"
        )})
    source, external_id, from_address = (
        str(tool_input.get("source") or "unknown"),
        str(tool_input.get("external_id") or ""),
        str(tool_input.get("from_address") or ""),
    )
    if _asks_the_speaker(current_session.get(), tool_input):
        logger.info("create_alert: question for the person in the chat; asked in the reply")
        return json.dumps({"status": "ask_in_reply", "message": ASK_IN_REPLY})
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
        event.needs, event.question, event.choices = _card_ask(tool_input)
        _one_card_per_sender(event, current_session.get())
        ask_first = _sender_card_choice(event, tool_input.get("joins_card"))
        if ask_first is not None:
            return ask_first
        event.external_id = _fresh_external_id(event.source, event.external_id, folds=event.follow_up)
        schedule_evaluation(event)
        logger.info("create_alert: scheduled subject=%r", tool_input["subject"])
        audit_log(
            "alert",
            f"Alert: {str(tool_input['subject'])[:200]}",
            actor="executive",
            details={
                "source": source,
                "external_id": event.external_id,
                "from": from_address,
                "raised_by": raised_by,
                "body_preview": str(tool_input.get("body", ""))[:300],
            },
            private=event.private,
        )
        return json.dumps({
            "status": "alert_scheduled", "subject": tool_input["subject"], "message": SENT_FOR_REVIEW,
        })
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
