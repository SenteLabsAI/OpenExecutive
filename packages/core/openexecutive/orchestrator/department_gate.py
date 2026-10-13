"""Department authority on a teammate's chat turn.

A department's authority level (``departments.authority``) used to govern
only unattended work: the scheduler's department-scoped rows, check-ins, the
alert review. A team member asking the Executive in chat to send or change
something went straight through, so a ``propose_only`` or ``escalate``
department held nothing its own people asked for.

On a turn a team member (not the principal) is in, every tool that sends or
edits goes through ``gate_action`` for their department first:

- ``auto_execute``: it runs.
- ``propose_only``: it runs when the speaker could approve it themselves
  (they hold the approval range it needs); otherwise it is held.
- ``escalate``: it is held, for someone other than the speaker.

A held action becomes a decision (class ``department_action``) with a card
on its approver's Today; approving it carries out that exact call on the
server (``api.routes.decisions``). The model is told it is waiting and not
to retry or work around it. The gate fails closed: an error holds the action.

With several departments the strictest level applies; with none, the action
is held as ``propose_only`` would hold it, and so is a web caller signed in
but on no People entry. "Could approve it themselves" means holding a range
for every kind of approval the action touches, not just the first. The principal's own turns,
unattended runs (Take the lead's gate and the scheduler's) and Act as me are
not this module's.
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openexecutive.departments.models import AuthorityLevel
from openexecutive.orchestrator import take_the_lead
from openexecutive.people.models import AuthorityScope, Person

logger = logging.getLogger(__name__)

DECISION_CLASS = "department_action"

# The acting tools a teammate's turn may call that send or change something.
# Take the lead's acting set, less two that are not sends or edits: putting
# someone on the chase list (assign_open_loop refuses any session but the
# asker's, so a held one could never be carried out) and starting a workflow
# (run_workflow: its own action steps are approved step by step).
GATED_TOOLS: frozenset[str] = take_the_lead.ACTING_TOOLS - {"assign_open_loop", "run_workflow"}

# Not sends themselves but they lead to some later (the chase messages
# people; a workflow's steps run with its approved tools), and neither can be
# carried out from a card: under escalate they are refused instead.
_REFUSED_ON_ESCALATE: frozenset[str] = take_the_lead.ACTING_TOOLS - GATED_TOOLS

# Strictest first.
_LEVEL_RANK: dict[AuthorityLevel, int] = {
    AuthorityLevel.ESCALATE: 2,
    AuthorityLevel.PROPOSE_ONLY: 1,
    AuthorityLevel.AUTO_EXECUTE: 0,
}

_Handler = Callable[[dict[str, Any]], Awaitable[Any]]


@dataclass(frozen=True)
class Verdict:
    """What the gate decided for one action."""

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


def _department(person: Person) -> tuple[str, AuthorityLevel]:
    """The speaker's department whose level is strictest, and that level. An
    unknown department reads as ``propose_only``; no department at all as
    ``("", propose_only)``."""
    from openexecutive.departments import registry as dept_registry

    best: tuple[str, AuthorityLevel] | None = None
    for slug in person.department_slugs:
        state = dept_registry.get_state(slug)
        level = state.config.authority_level if state is not None else AuthorityLevel.PROPOSE_ONLY
        if best is None or _LEVEL_RANK[level] > _LEVEL_RANK[best[1]]:
            best = (slug, level)
    return best if best is not None else ("", AuthorityLevel.PROPOSE_ONLY)


def approval_ranges(tool: str, tool_input: dict[str, Any]) -> list[list[AuthorityScope]]:
    """The People approval ranges this action needs, one group per kind it
    touches (money, a contract, a people decision, the board), each closest
    fit first, as Take the lead reads them. Every group must be held: a $12
    lunch in a termination letter still needs Hiring. Empty when it needs
    no particular range."""
    text = "\n".join(take_the_lead._strings(tool_input))
    kinds = []
    if take_the_lead._MONEY_RE.search(text) or take_the_lead.amounts(text):
        kinds.append(take_the_lead.MONEY)
    if take_the_lead._CONTRACTS_RE.search(text):
        kinds.append(take_the_lead.CONTRACTS)
    if take_the_lead._PEOPLE_RE.search(text):
        kinds.append(take_the_lead.PEOPLE_DECISIONS)
    groups = [take_the_lead.approval_ranges(kind, text) for kind in kinds]
    board = take_the_lead.approval_ranges("", text)
    if board:
        groups.append(board)
    return [group for group in groups if group]


def _holds(person: Person, groups: list[list[AuthorityScope]]) -> bool:
    """Whether ``person`` could approve it: they hold a range from every
    group it needs, or approve everything."""
    scopes = set(person.authority_scope)
    if AuthorityScope.WILDCARD in scopes:
        return True
    return bool(groups) and all(any(r in scopes for r in group) for group in groups)


def _other_approver(scope: AuthorityScope | None, speaker_id: int | None) -> int | None:
    """Someone other than the speaker who can approve ``scope``: a teammate
    holding it, else the principal."""
    from openexecutive.people.registry import get_principal
    from openexecutive.people.store import find_approvers

    for person in find_approvers(scope or AuthorityScope.WILDCARD):
        if person.id is not None and person.id != speaker_id:
            return person.id
    principal = get_principal()
    return principal.id if principal is not None else None


def decide(
    person: Person, tool: str, tool_input: dict[str, Any], *, mcp: bool, now: datetime | None = None,
) -> Verdict:
    """Whether ``person``'s department lets this action run now, and if not
    who approves it."""
    from openexecutive.departments.authority import _route_proposal, gate_action

    now = now or datetime.now(UTC)
    if not mcp and take_the_lead._only_to_principal(tool, tool_input):
        # The principal is who it would wait for: there's nothing to approve.
        return Verdict(run=True, reason="it only messages the owner")
    slug, level = _department(person)
    ranges = approval_ranges(tool, tool_input)
    # One range routes to who holds it; several to someone who approves
    # everything, since no one range covers them all.
    scope = ranges[0][0] if len(ranges) == 1 else None
    if slug:
        gate = gate_action(slug, tool, required_scope=scope, now=now)
    else:
        gate = _route_proposal(
            department_slug="", action="propose", required_scope=scope, now=now,
            reason="they aren't in a department, so it waits like propose_only",
        )
    if gate.action == "execute":
        return Verdict(run=True, department=slug, level=level.value, reason=gate.reason)
    if gate.action == "propose" and _holds(person, ranges):
        return Verdict(
            run=True, department=slug, level=level.value,
            reason="they can approve this themselves",
        )
    approver = gate.assignee_person_id
    if approver is None or approver == person.id:
        approver = _other_approver(scope, person.id)
    if not take_the_lead._team_only(tool, tool_input):
        # Someone outside the team is the principal's to approve: only their
        # approval opens contact egress, and contacts are theirs alone.
        approver = take_the_lead._principal_id()
    return Verdict(
        run=False, department=slug, level=level.value, approver_person_id=approver,
        reason=_why(slug, level),
    )


def _why(slug: str, level: AuthorityLevel) -> str:
    if not slug:
        return "they aren't in a department, so it waits for approval"
    from openexecutive.departments import registry as dept_registry

    state = dept_registry.get_state(slug)
    name = (state.config.title if state is not None else "") or slug
    if level == AuthorityLevel.ESCALATE:
        return f"{name} escalates everything it does to a person"
    return f"{name} proposes before it acts"


def hold(
    person: Person,
    tool: str,
    tool_input: dict[str, Any],
    verdict: Verdict,
    *,
    mcp: bool,
    session_id: str | None = None,
    db_path: Path | None = None,
) -> int:
    """Make a held action a decision for its approver, with a card on Today.
    The same action still waiting is that card, not a new one."""
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
            [person.id if person.id is not None else f"session:{session_id}", tool, tool_input, mcp],
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
    summary = take_the_lead.summarize(tool, tool_input, mcp=mcp)
    private = take_the_lead._is_principal(verdict.approver_person_id)
    # What it would say quotes the conversation: a card on the team's Today
    # shows only the plain line; the approver opens the decision for the rest.
    shown = summary if private else take_the_lead.summarize(tool, tool_input, mcp=mcp, quote=False)
    who = person.full_name or (
        "Someone on the team" if person.id is not None else "Someone signed in who isn't on People"
    )
    escalated = verdict.level == AuthorityLevel.ESCALATE.value
    payload = {
        "tool": tool, "input": tool_input, "mcp": mcp, "summary": summary,
        "requested_by_person_id": person.id, "requested_by": who,
        "department": verdict.department, "level": verdict.level, "reason": verdict.reason,
        "fields": take_the_lead.editable_fields(tool, tool_input, mcp=mcp),
        # Everything the call says, in full: the card shows this before it
        # can be approved, since the summary keeps only the start.
        "details": _details(tool_input),
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
            headline=f"{who} asked the Executive to: {shown}"[:160],
            body=f"{shown}\n\nIt waited because {verdict.reason}.",
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


def _details(tool_input: dict[str, Any]) -> list[dict[str, str]]:
    """Each field of the call and its whole value, as text."""
    out = []
    for key, value in tool_input.items():
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        out.append({"field": str(key), "value": text})
    return out


def _waiting(decision_id: int, verdict: Verdict) -> str:
    from openexecutive.people.store import get_person

    name = ""
    if verdict.approver_person_id is not None:
        try:
            approver = get_person(verdict.approver_person_id)
            name = approver.full_name if approver is not None else ""
        except Exception:
            name = ""
    whom = f"{name}'s approval" if name else "approval"
    return json.dumps({
        "status": "waiting_for_approval",
        "decision_instance_id": decision_id,
        "message": (
            f"Not done yet: it waits for {whom} on Today because {verdict.reason}. "
            "Tell them it's waiting. Do not retry or work around it."
        ),
    })


async def _gated(
    person: Person, name: str, inner: _Handler, tool_input: dict[str, Any], *, mcp: bool,
    session_id: str | None,
) -> Any:
    try:
        verdict = decide(person, name, tool_input, mcp=mcp)
    except Exception:
        logger.warning("department_gate: the gate failed — holding the action", exc_info=True)
        verdict = Verdict(
            run=False, approver_person_id=take_the_lead._principal_id(),
            reason="its department's authority couldn't be read",
        )
    if verdict.run:
        return await inner(tool_input)
    try:
        decision_id = hold(person, name, tool_input, verdict, mcp=mcp, session_id=session_id)
    except Exception:
        logger.exception("department_gate: couldn't hold %s", name)
        return json.dumps({"error": f"{name} was not done: it needs someone's approval first. Do not retry."})
    return _waiting(decision_id, verdict)


def gated_handlers(handlers: dict[str, Any], person: Person, *, session_id: str | None = None) -> dict[str, Any]:
    """``handlers`` with every send or edit behind ``person``'s department."""
    out = dict(handlers)
    for name, inner in handlers.items():
        if name in _REFUSED_ON_ESCALATE:
            async def _refused(tool_input: dict[str, Any], _name: str = name, _inner: Any = inner) -> Any:
                try:
                    escalated = _department(person)[1] == AuthorityLevel.ESCALATE
                except Exception:
                    escalated = True
                if escalated:
                    return json.dumps({"error": (
                        f"{_name} was not done: this person's department escalates everything it "
                        "does to a person, and this can't wait on a card. Say so. Do not retry."
                    )})
                return await _inner(tool_input)

            out[name] = _refused
        elif name in GATED_TOOLS:
            async def _wrapped(tool_input: dict[str, Any], _name: str = name, _inner: Any = inner) -> Any:
                return await _gated(person, _name, _inner, tool_input, mcp=False, session_id=session_id)

            out[name] = _wrapped
    return out


def gated_call_tool(call_tool: _Handler, person: Person, *, session_id: str | None = None) -> _Handler:
    """The MCP gateway's ``call_tool`` with every connected tool that may
    change something (``take_the_lead.mcp_tool_acts``, which fails closed)
    behind ``person``'s department."""

    async def _call(tool_input: dict[str, Any]) -> Any:
        name = str(tool_input.get("name") or "")
        if not take_the_lead.mcp_tool_acts(name):
            return await call_tool(tool_input)
        arguments = tool_input.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            # Held as-is it would be approved as a different call.
            return json.dumps({"error": f"{name} was not done: its arguments must be an object."})

        async def _inner(_: dict[str, Any]) -> Any:
            return await call_tool(tool_input)

        return await _gated(person, name, _inner, arguments, mcp=True, session_id=session_id)

    return _call
