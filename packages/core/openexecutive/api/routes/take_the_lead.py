"""Take the lead (orchestrator.take_the_lead): the switches and rules on
Settings → On its own, and What I did on Today.

  GET    /take-the-lead              — the As the Executive switch, its Ask
                                       first switches and the company's rules
                                       (the principal's alone)
  PUT    /take-the-lead              — {enabled?, ask_first?}
  POST   /take-the-lead/rules        — {kind, value}: a company rule
  DELETE /take-the-lead/rules/{id}
  GET    /take-the-lead/done         — What I did: everything done on its own
                                       for the caller in the last week, As the
                                       Executive (the principal only) and As
                                       you (their own replies and follow-ups)

Changing anything that lets more happen on its own needs a request the API
can tie to the principal (signed sign-ins or local login, as Send does);
turning something off, or holding more back, does not.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from openexecutive.api import caller as api_caller

logger = logging.getLogger(__name__)

router = APIRouter()


class AskFirstOut(BaseModel):
    kind: str
    label: str
    on: bool


class RuleOut(BaseModel):
    id: int
    kind: str
    value: str


class LeadOut(BaseModel):
    enabled: bool
    ask_first: list[AskFirstOut]
    rules: list[RuleOut]
    # Whether this server can tie a change to the owner (signed sign-ins or
    # local login); without it nothing here can be turned on.
    available: bool
    # The Executive is paused: nothing here runs until it resumes.
    paused: bool


class LeadUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    ask_first: dict[str, bool] | None = None


class RuleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    value: str


class DoneOut(BaseModel):
    at: str
    # "executive" or "you"
    actor: str
    title: str
    detail: str = ""
    why: str = ""
    # done, waiting, approved, declined, failed
    status: str
    link: str = ""


class DoneListOut(BaseModel):
    items: list[DoneOut]


def _principal(request: Request) -> Any:
    """The principal, when they are the caller; else 403."""
    from openexecutive.api.routes.people import caller_is_principal
    from openexecutive.people.store import find_principal_person

    if not caller_is_principal(request):
        raise HTTPException(status_code=403, detail="Only the account owner can change this.")
    try:
        principal = find_principal_person()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Couldn't read the People list.") from exc
    if principal is None:
        raise HTTPException(status_code=403, detail="Only the account owner can change this.")
    return principal


def _provably_theirs(request: Request, person: Any) -> bool:
    from openexecutive.delegation.gmail import normalize_email
    from openexecutive.delegation.verified import caller_refusal

    try:
        return caller_refusal(api_caller.caller(request), normalize_email(person.email or "")) is None
    except Exception:
        return False


def _signing_available() -> bool:
    from openexecutive.delegation.handle_it import signing_ok

    return signing_ok()


def _paused() -> bool:
    from openexecutive.scheduler.pause import is_paused

    return is_paused()


def _out() -> LeadOut:
    from openexecutive.orchestrator import take_the_lead

    lead = take_the_lead.get(take_the_lead.SCOPE_EXECUTIVE)
    try:
        rules = take_the_lead.list_rules([take_the_lead.SCOPE_COMPANY])
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Couldn't read the rules.") from exc
    return LeadOut(
        enabled=lead.enabled,
        ask_first=[
            AskFirstOut(kind=k, label=take_the_lead.KIND_LABELS[k], on=lead.asks_first(k))
            for k in take_the_lead.KINDS
        ],
        rules=[RuleOut(id=r.id, kind=r.kind, value=r.value) for r in rules],
        available=_signing_available(),
        paused=_paused(),
    )


def _audit(summary: str, details: dict[str, Any]) -> None:
    from openexecutive.audit import log_event

    try:
        log_event("take_the_lead_changed", summary, actor="user", details=details)
    except Exception:
        logger.warning("take_the_lead: couldn't audit a change", exc_info=True)


@router.get("/take-the-lead", response_model=LeadOut)
def get_take_the_lead(request: Request) -> LeadOut:
    _principal(request)
    return _out()


@router.put("/take-the-lead", response_model=LeadOut)
def update_take_the_lead(request: Request, body: LeadUpdate) -> LeadOut:
    from openexecutive.orchestrator import take_the_lead

    principal = _principal(request)
    current = take_the_lead.get(take_the_lead.SCOPE_EXECUTIVE)
    unknown = [k for k in (body.ask_first or {}) if k not in take_the_lead.KINDS]
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown kind: {unknown[0]}.")
    loosening = bool(body.enabled) and not current.enabled or any(
        value is False and current.asks_first(kind) for kind, value in (body.ask_first or {}).items()
    )
    if loosening and not _provably_theirs(request, principal):
        raise HTTPException(
            status_code=409,
            detail="This needs signed sign-ins on this server, so that nobody else can turn it on for you.",
        )
    after = take_the_lead.set_(
        take_the_lead.SCOPE_EXECUTIVE, enabled=body.enabled, ask_first=body.ask_first,
        updated_by=f"person:{principal.id}",
    )
    if after != current:
        _audit(
            f"Take the lead as the Executive {'on' if after.enabled else 'off'}",
            {"scope": "executive", "enabled": after.enabled, "ask_first": after.ask_first},
        )
    return _out()


@router.post("/take-the-lead/rules", response_model=LeadOut)
def add_company_rule(request: Request, body: RuleIn) -> LeadOut:
    from openexecutive.orchestrator import take_the_lead

    principal = _principal(request)
    try:
        rule = take_the_lead.add_rule(
            take_the_lead.SCOPE_COMPANY, body.kind, body.value, created_by=f"person:{principal.id}",
        )
    except take_the_lead.RuleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    _audit("Added a Take the lead rule", {"scope": "company", "kind": rule.kind, "value": rule.value})
    return _out()


@router.delete("/take-the-lead/rules/{rule_id}", response_model=LeadOut)
def delete_company_rule(request: Request, rule_id: int) -> LeadOut:
    from openexecutive.orchestrator import take_the_lead

    principal = _principal(request)
    if not _provably_theirs(request, principal):
        raise HTTPException(
            status_code=409,
            detail="This needs signed sign-ins on this server, so that nobody else can change it for you.",
        )
    if not take_the_lead.delete_rule(rule_id, take_the_lead.SCOPE_COMPANY):
        raise HTTPException(status_code=404, detail="That rule isn't there.")
    _audit("Removed a Take the lead rule", {"scope": "company", "rule_id": rule_id})
    return _out()


@router.get("/take-the-lead/done", response_model=DoneListOut)
def get_done(request: Request) -> DoneListOut:
    """What it did on its own for the caller this week, newest first."""
    from openexecutive.api.routes.chat import _resolve_caller_person_id
    from openexecutive.api.routes.people import caller_is_principal
    from openexecutive.orchestrator import take_the_lead

    try:
        person_id = _resolve_caller_person_id(request)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Couldn't read the People list.") from exc
    items: list[DoneOut] = []
    if caller_is_principal(request):
        for d in take_the_lead.done([take_the_lead.SCOPE_EXECUTIVE]):
            items.append(DoneOut(at=d.at, actor="executive", title=d.summary, why=d.why, status=d.status))
    if person_id is not None:
        items.extend(_handled_as_you(person_id))
    items.sort(key=lambda i: i.at, reverse=True)
    return DoneListOut(items=items[:100])


def _handled_as_you(person_id: int) -> list[DoneOut]:
    """Replies and follow-ups Handle it for me sent as the person."""
    from openexecutive.delegation import handle_it
    from openexecutive.delegation.gmail import mailbox_link
    from openexecutive.people.store import get_person

    try:
        person = get_person(person_id)
        found = handle_it.handled(person_id)
    except Exception:
        logger.warning("take_the_lead: couldn't read what was sent as them", exc_info=True)
        return []
    email = ((person.email if person else "") or "").strip().lower()
    out = []
    for h in found:
        follow_up = h.source == "follow_up"
        out.append(DoneOut(
            at=h.sent_at,
            actor="you",
            title=f"{'Followed up with' if follow_up else 'Replied to'} {h.to_name or h.to_email}",
            detail=" ".join((h.body or "").split())[:160],
            why=(
                "Nobody had answered your question in a few days." if follow_up
                else f"They wrote to you about {h.subject}." if h.subject else "They wrote to you."
            ),
            status="done",
            link=mailbox_link(email, thread_id=h.thread_id) if email and h.thread_id else "",
        ))
    return out
