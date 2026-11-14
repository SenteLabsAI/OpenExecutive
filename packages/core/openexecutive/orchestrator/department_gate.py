"""The Approve card a held step waits on (``hold``), and who is speaking
(``speaker``).

``orchestrator.levels`` decides whether a step runs or waits, for the owner
and teammates alike; a step that waits becomes a decision (class
``department_action``) with a card on its approver's Today, and approving it
carries out that exact call on the server (``api.routes.decisions``). The
name is historical: departments no longer hold anything of their own.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openexecutive.departments.models import AuthorityLevel
from openexecutive.orchestrator import card_details, take_the_lead
from openexecutive.people.models import Person

logger = logging.getLogger(__name__)

DECISION_CLASS = "department_action"


@dataclass(frozen=True)
class Verdict:
    """Why a step waits and who approves it."""

    run: bool
    department: str = ""
    level: str = ""
    approver_person_id: int | None = None
    reason: str = ""


def speaker(session: Any) -> Person | None:
    """The team member talking on this turn when it is theirs to gate: a
    turn someone is in (not an unattended run, not one private to the
    principal) whose caller is a team member other than the principal, not
    archived. None otherwise. A roster that can't be read is no answer
    either way, so it holds: the caller is treated as a team member."""
    if session is None or getattr(session, "unattended", False):
        return None
    if getattr(session, "private_to_principal", False):
        return None
    person_id = getattr(session, "caller_person_id", None)
    if person_id is None:
        # Signed in on the web but on nobody's People entry (an archived
        # teammate still allowed in, an allowlisted email): not the
        # principal, so not ungated either. They wait like someone in no
        # department.
        if getattr(session, "from_web_chat", False) is True and getattr(
            session, "web_caller_signed_in", False,
        ) is True:
            return Person(id=None, full_name="", kind="team")
        # The same on a chat channel (Slack, Telegram, Discord, Google
        # Chat): a sender it couldn't match to anyone on People. Slack,
        # Telegram and Discord turn those away before the turn starts;
        # Google Chat matches its sender by email (`caller_person_id` when
        # on People) and lets an unmatched one through to this. Email is left out: whoever writes in is never on People,
        # and its replies go through the mailbox's own gates.
        channel = str(getattr(session, "origin_channel", "") or "")
        if channel and channel != "email":
            return Person(id=None, full_name="", kind="team")
        return None
    from openexecutive.people.store import get_person

    try:
        person = get_person(int(person_id))
    except Exception:
        logger.warning("department_gate: couldn't read the speaker — holding their sends", exc_info=True)
        return Person(id=int(person_id), full_name="", kind="team")
    if person is None or person.is_principal or person.archived or person.kind != "team":
        return None
    return person



def _headline(person: Any, who: str, plain: str, *, held: bool) -> str:
    """A held step's card title: what it does ("Email Dana Price"), with
    who asked when it was a teammate's own ask. A reply to a contact's mail
    is no ask of theirs, so it isn't put in their name; why it waited is on
    the card's next line."""
    if held and getattr(person, "is_principal", False):
        return f"Waiting for your OK: {plain}"
    if getattr(person, "id", None) is not None and getattr(person, "kind", "team") == "team" \
            and not getattr(person, "is_principal", False):
        return f"{who} asked: {plain}"
    return plain[:1].upper() + plain[1:]

def hold(
    person: Person,
    tool: str,
    tool_input: dict[str, Any],
    verdict: Verdict,
    *,
    mcp: bool,
    session_id: str | None = None,
    db_path: Path | None = None,
    held: bool = False,
    more: list[dict[str, Any]] | None = None,
) -> int:
    """Make a held action a decision for its approver, with a card on Today.
    The same action still waiting is that card, not a new one. ``held``: the
    owner's level put it on a card (``orchestrator.levels``), so Approve may
    carry out any step the Executive runs, not only a send. ``more``: further
    calls of the same tool held with it; the one card lists them all and its
    Approve carries out each in order (``take_the_lead.carry_out``)."""
    from openexecutive.alerts.models import PRIVATE_ALERT_TAG, AlertSeverity
    from openexecutive.alerts.store import insert_alert
    from openexecutive.audit import log_event
    from openexecutive.memory.decision_ledger import (
        DECISION_ALERT_SOURCE,
        create_decision_instance,
        decision_alert_external_id,
        decision_instance_tag,
        get_live_by_idem,
    )

    base = "department_action:" + hashlib.sha256(
        # Someone on no People entry is told apart by their chat.
        json.dumps(
            [person.id if person.id is not None else f"session:{session_id}", tool, tool_input, mcp]
            + (["held"] if held else [])
            + ([more] if more else []),
            sort_keys=True, default=str,
        ).encode()
    ).hexdigest()[:32]
    idem: str | None = None
    for attempt in range(_HOLD_KEY_TRIES):
        key = f"{base}:{attempt}"
        waiting = get_live_by_idem(key, db_path=db_path)
        if waiting is not None:
            return waiting.id
        if not take_the_lead._idem_used(key, db_path=db_path):
            idem = key
            break
    steps = [tool_input, *(more or [])]
    private = take_the_lead._is_principal(verdict.approver_person_id)
    # What it would say quotes the conversation: a card on the team's Today
    # shows only the plain line; the approver opens the decision for the rest.
    each = [take_the_lead.summarize(tool, step, mcp=mcp) for step in steps]
    each_shown = each if private else [
        take_the_lead.summarize(tool, step, mcp=mcp, quote=False) for step in steps
    ]
    summary, shown = each[0], each_shown[0]
    # The title says what it does in a few words ("Email Dana Price"); the
    # message itself shows in full under it.
    plain = take_the_lead.title(tool, tool_input, mcp=mcp, private=private)
    listed = ""
    if more:
        summary = f"{each[0]} and {len(more)} more like it"
        shown = f"{each_shown[0]} and {len(more)} more like it"
        plain = f"{plain} and {len(more)} more like it"
        listed = "\n".join(f"- {line}" for line in each_shown)
    who = person.full_name or (
        "Someone on the team" if person.id is not None else "Someone who isn't on People"
    )
    escalated = verdict.level == AuthorityLevel.ESCALATE.value
    payload = {
        "tool": tool, "input": tool_input, "mcp": mcp, "summary": summary,
        "requested_by_person_id": person.id, "requested_by": who,
        "department": verdict.department, "level": verdict.level, "reason": verdict.reason,
        # A card for several calls can't be edited field by field.
        "fields": [] if more else take_the_lead.editable_fields(tool, tool_input, mcp=mcp),
        # Everything the call says, in full: the card shows this before it
        # can be approved, since the summary keeps only the start.
        "details": card_details.plain_steps(tool, steps),
        **({"held": True} if held else {}),
        **({"steps": steps} if more else {}),
    }
    decision_id = create_decision_instance(
        decision_class=DECISION_CLASS,
        department=verdict.department or "executive",
        originating_session_id=session_id,
        proposed_payload=payload,
        idempotency_key=idem,
        gate_mode="escalate" if escalated else "propose",
        approver_person_id=verdict.approver_person_id,
        confidence=None,
        db_path=db_path,
    )
    external_id = decision_alert_external_id(decision_id)
    try:
        insert_alert(
            source=DECISION_ALERT_SOURCE,
            external_id=external_id,
            severity=AlertSeverity.HIGH.value if escalated else AlertSeverity.MEDIUM.value,
            headline=_headline(person, who, plain, held=held)[:160],
            body=f"{listed or shown}\n\nIt waited because {verdict.reason}.",
            suggested_action=shown,
            topic_tags=[
                decision_instance_tag(decision_id),
                f"decision_class:{DECISION_CLASS}",
                *([f"department:{verdict.department}"] if verdict.department else []),
                *([PRIVATE_ALERT_TAG] if private else []),
            ],
            dedup_key=external_id,
            routed_to_person_id=verdict.approver_person_id,
        )
    except Exception:
        logger.exception("department_gate: couldn't put decision %d on Today", decision_id)
    from openexecutive.orchestrator.session import note_step_held

    note_step_held()
    log_event(
        "department_action_held",
        f"Held for approval: {take_the_lead.summarize(tool, tool_input, mcp=mcp, quote=False)}",
        actor=f"person:{person.id}" if person.id is not None else "user",
        details={
            "decision_id": decision_id, "tool": tool, "mcp": mcp,
            "requested_by_person_id": person.id, "department": verdict.department,
            "level": verdict.level, "approver_person_id": verdict.approver_person_id,
            "reason": verdict.reason,
        },
        department=verdict.department or None,
        # The principal's, or the approver's alone: it names who asked and
        # what for.
        private=private,
        private_to_person=None if private else verdict.approver_person_id,
    )
    return decision_id


_HOLD_KEY_TRIES = 50
