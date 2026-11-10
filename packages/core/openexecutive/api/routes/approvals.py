"""Delegate → Approvals: what the level did lately, and what could go on its
own (``orchestrator.go_on_its_own``). Reads, plus "Let it go" and "Keep
asking" on an offer; the level and Your rules are set elsewhere
(``/settings/outside-content``, ``/take-the-lead/rules``).

  GET  /approvals/summary            — the reader's last 7 days and offers
  POST /approvals/offers/let-go      — {key}: allow a current offer
  POST /approvals/offers/keep-asking — {key}: drop it until more approvals

Each person sees only their own: As you for whoever may use it, As me (the
Executive's own suggestions, and what it did) for the owner. Letting
something go needs a request the API can tie to its person (signed sign-ins
or local login), the rule Send and the "on its own next time" tick use;
Keep asking holds more back, so it doesn't.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

router = APIRouter()


class WaitedOut(BaseModel):
    at: str
    side: str
    label: str
    why: str
    outcome: str


class WeekOut(BaseModel):
    days: int
    went: int
    waited: int
    approved: int
    declined: int
    because_count: int
    because: str
    items: list[WaitedOut]


class OfferOut(BaseModel):
    key: str
    # "as_you" or "as_me".
    side: str
    label: str
    approvals: int
    last_at: str
    # As you: who replies would go to on their own ('' for As me).
    address: str
    name: str


class SummaryOut(BaseModel):
    week: WeekOut
    offers: list[OfferOut]


class OfferIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str


def _reader(request: Request) -> tuple[Any | None, bool]:
    """The caller's person (or None) and whether they are the owner. A
    request that names no one counts only under local login, as on As you."""
    from openexecutive.api import caller as api_caller
    from openexecutive.api.routes.people import caller_is_principal
    from openexecutive.delegation.settings import local_login
    from openexecutive.people.store import find_principal_person, get_person

    who = api_caller.caller(request)
    if not who.email and not (who.defaults_to_principal and local_login()):
        return None, False
    try:
        if caller_is_principal(request):
            return find_principal_person(), True
        from openexecutive.api.routes.chat import _resolve_caller_person_id

        person_id = _resolve_caller_person_id(request)
        return (get_person(person_id) if person_id is not None else None), False
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Couldn't read the People list.") from exc


def _can_delegate(person: Any) -> bool:
    from openexecutive.delegation.settings import can_delegate

    try:
        return person is not None and person.id is not None and can_delegate(person)
    except Exception:
        return False


def _summary(person: Any | None, owner: bool) -> SummaryOut:
    from openexecutive.orchestrator import go_on_its_own

    person_id = person.id if person is not None else None
    as_you = _can_delegate(person)
    week = go_on_its_own.week(person_id, as_you=as_you, as_me_done=owner)
    offers = go_on_its_own.offers(person_id, as_you=as_you, as_me=owner)
    return SummaryOut(
        week=WeekOut(
            days=go_on_its_own.DAYS, went=week.went, waited=week.waited, approved=week.approved,
            declined=week.declined, because_count=week.because_count, because=week.because,
            items=[WaitedOut(at=i.at, side=i.side, label=i.label, why=i.why, outcome=i.outcome) for i in week.items],
        ),
        offers=[
            OfferOut(
                key=o.key, side=o.side, label=o.label, approvals=o.approvals, last_at=o.last_at,
                address=o.address, name=o.name,
            )
            for o in offers
        ],
    )


def _audit(event_type: str, summary: str, details: dict[str, Any]) -> None:
    from openexecutive.audit import log_event

    try:
        log_event(event_type, summary, actor="user", details=details)
    except Exception:
        logger.warning("approvals: couldn't audit a change", exc_info=True)


@router.get("/approvals/summary", response_model=SummaryOut)
def get_approvals_summary(request: Request) -> SummaryOut:
    person, owner = _reader(request)
    return _summary(person, owner)


def _offer_person(request: Request, key: str, *, narrowing: bool) -> tuple[Any, bool, str]:
    """Who may answer the offer at ``key``: the owner for As me, the person
    it belongs to for As you. Refuses anyone else as if it weren't there."""
    from openexecutive.api.routes import delegation, take_the_lead
    from openexecutive.orchestrator import go_on_its_own

    side = go_on_its_own.side_of(key)
    if side == go_on_its_own.AS_ME:
        principal = take_the_lead._principal(request)
        if not narrowing and not take_the_lead._provably_theirs(request, principal):
            raise HTTPException(
                status_code=409,
                detail="This needs signed sign-ins on this server, so that nobody else can change it for you.",
            )
        return principal, True, side
    if side == go_on_its_own.AS_YOU:
        try:
            person = delegation._signed_caller(request, narrowing=narrowing)
        except HTTPException as exc:
            detail = exc.detail.get("message") if isinstance(exc.detail, dict) else exc.detail
            raise HTTPException(status_code=exc.status_code, detail=detail) from None
        if person.id is None or not key.startswith(f"act_as_me|person:{person.id}|"):
            raise HTTPException(status_code=404, detail="That isn't an offer any more.")
        _, owner = _reader(request)
        return person, owner, side
    raise HTTPException(status_code=404, detail="That isn't an offer any more.")


@router.post("/approvals/offers/let-go", response_model=SummaryOut)
def let_offer_go(request: Request, body: OfferIn) -> SummaryOut:
    """Let it go on its own from now on, where Approvals lets it: the same
    as ticking "on its own next time" on its card."""
    from openexecutive.delegation import training
    from openexecutive.orchestrator import go_on_its_own, take_the_lead

    person, owner, side = _offer_person(request, body.key, narrowing=False)
    try:
        if side == go_on_its_own.AS_ME:
            label = go_on_its_own.let_go_as_me(body.key, created_by=f"person:{person.id}")
        else:
            allowed = training.let_go(person.id, body.key)
            label = allowed.label if allowed is not None else None
    except take_the_lead.RuleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    if label is None:
        raise HTTPException(status_code=404, detail="That isn't an offer any more.")
    _audit("approvals_offer_allowed", f"Let something go on its own: {label}", {"side": side, "person_id": person.id})
    return _summary(person, owner)


@router.post("/approvals/offers/keep-asking", response_model=SummaryOut)
def keep_asking_offer(request: Request, body: OfferIn) -> SummaryOut:
    """Keep asking: drop the offer until it's approved unchanged that many
    times again. Holds more back, so no signed sign-in needed."""
    from openexecutive.delegation import training
    from openexecutive.orchestrator import go_on_its_own

    person, owner, side = _offer_person(request, body.key, narrowing=True)
    if side == go_on_its_own.AS_ME:
        found = go_on_its_own.keep_asking_as_me(body.key)
    else:
        found = training.keep_asking(person.id, body.key)
    if not found:
        raise HTTPException(status_code=404, detail="That isn't an offer any more.")
    _audit("approvals_offer_kept", "Kept asking about something", {"side": side, "person_id": person.id})
    return _summary(person, owner)
