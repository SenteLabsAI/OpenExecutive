"""Take the lead (orchestrator.take_the_lead): the switches and rules on
Settings → Your Executive. What it did shows in Recent activity
(``today._build_activity``), with each person's own As you rows.

  GET    /take-the-lead              — the As the Executive switch, its Ask
                                       first switches and the company's rules
                                       (the principal's alone)
  PUT    /take-the-lead              — {enabled?, training?, ask_first?}
  POST   /take-the-lead/rules        — {kind, value}: a company rule
  DELETE /take-the-lead/rules/{id}
  POST   /take-the-lead/learned      — {key}: allow a suggested action
  DELETE /take-the-lead/learned/{id} — ask first again for an allowed one

What it's learned (``learned``: actions allowed in training, from a card or
a suggestion, with any edit kept as an example) and what it suggests
allowing (``suggested``) come with the switch.

Changing anything that lets more happen on its own needs a request the API
can tie to the principal (signed sign-ins or local login, as Send does);
turning something off, or holding more back, does not.
"""
from __future__ import annotations

import json
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
    hint: str
    on: bool


class RuleOut(BaseModel):
    id: int
    kind: str
    value: str


class LearnedOut(BaseModel):
    id: int
    label: str
    # The edit it keeps as an example of how you want it done, by field.
    example: dict[str, str]
    uses: int
    created_at: str


class SuggestedOut(BaseModel):
    key: str
    label: str
    approvals: int


class LeadOut(BaseModel):
    enabled: bool
    # In training: everything waits for a yes unless allowed (``learned``).
    training: bool
    ask_first: list[AskFirstOut]
    rules: list[RuleOut]
    learned: list[LearnedOut]
    suggested: list[SuggestedOut]
    # Whether this server can tie a change to the owner (signed sign-ins or
    # local login); without it nothing here can be turned on.
    available: bool
    # The Executive is paused: nothing here runs until it resumes.
    paused: bool


class LeadUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    training: bool | None = None
    ask_first: dict[str, bool] | None = None


class LearnIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str


class RuleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    value: str


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
        learned = take_the_lead.list_allowed()
        suggested = take_the_lead.suggestions()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Couldn't read the rules.") from exc
    return LeadOut(
        enabled=lead.enabled,
        training=lead.training,
        ask_first=[
            AskFirstOut(
                kind=k,
                label=take_the_lead.KIND_LABELS[k],
                hint=take_the_lead.KIND_HINTS[k],
                on=lead.asks_first(k),
            )
            for k in take_the_lead.KINDS
        ],
        rules=[RuleOut(id=r.id, kind=r.kind, value=r.value) for r in rules],
        learned=[
            LearnedOut(id=a.id, label=a.label, example=_example(a.example), uses=a.uses, created_at=a.created_at)
            for a in learned
        ],
        suggested=[SuggestedOut(key=s.key, label=s.label, approvals=s.approvals) for s in suggested],
        available=_signing_available(),
        paused=_paused(),
    )


def _example(text: str) -> dict[str, str]:
    try:
        parsed = json.loads(text) if text else {}
    except ValueError:
        return {}
    return {str(k): str(v) for k, v in parsed.items()} if isinstance(parsed, dict) else {}


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
    turning_on = bool(body.enabled) and not current.enabled
    # Leaving training while on lets everything it was asking about go.
    leaving_training = (
        body.training is False and current.training and (body.enabled if body.enabled is not None else current.enabled)
    )
    loosening = turning_on or bool(leaving_training) or any(
        value is False and current.asks_first(kind) for kind, value in (body.ask_first or {}).items()
    )
    if loosening and not _provably_theirs(request, principal):
        raise HTTPException(
            status_code=409,
            detail="This needs signed sign-ins on this server, so that nobody else can turn it on for you.",
        )
    after = take_the_lead.set_(
        take_the_lead.SCOPE_EXECUTIVE, enabled=body.enabled, training=body.training, ask_first=body.ask_first,
        updated_by=f"person:{principal.id}",
    )
    if after != current:
        _audit(
            f"Take the lead as the Executive {'in training' if after.enabled and after.training else 'on' if after.enabled else 'off'}",
            {"scope": "executive", "enabled": after.enabled, "training": after.training, "ask_first": after.ask_first},
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


@router.post("/take-the-lead/learned", response_model=LeadOut)
def allow_suggested(request: Request, body: LearnIn) -> LeadOut:
    """Allow one of the actions it suggests: only a current suggestion, so
    what it's called comes from the cards you approved, never the request."""
    from openexecutive.orchestrator import take_the_lead

    principal = _principal(request)
    if not _provably_theirs(request, principal):
        raise HTTPException(
            status_code=409,
            detail="This needs signed sign-ins on this server, so that nobody else can change it for you.",
        )
    match = next((s for s in take_the_lead.suggestions() if s.key == body.key), None)
    if match is None:
        raise HTTPException(status_code=404, detail="That isn't a suggestion any more.")
    try:
        take_the_lead.allow(match.key, match.label, created_by=f"person:{principal.id}")
    except take_the_lead.RuleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    _audit(f"Allowed a suggestion: {match.label}", {"scope": "executive", "allowed": match.label})
    return _out()


@router.delete("/take-the-lead/learned/{allowed_id}", response_model=LeadOut)
def remove_learned(request: Request, allowed_id: int) -> LeadOut:
    """Ask first again for something it was allowed (holding more back, so
    no signed sign-in needed)."""
    from openexecutive.orchestrator import take_the_lead

    _principal(request)
    if not take_the_lead.disallow(allowed_id):
        raise HTTPException(status_code=404, detail="That isn't there.")
    _audit("Removed something it learned", {"scope": "executive", "allowed_id": allowed_id})
    return _out()
