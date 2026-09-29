from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import BaseModel

from openexecutive.memory.episodic import (
    Advice,
    Decision,
    Initiative,
    delete_advice,
    delete_decision,
    delete_initiative,
    get_advice,
    get_decision,
    get_initiative,
    list_advice,
    list_decisions,
    list_initiatives,
    update_advice,
    update_decision,
    update_initiative,
)
from openexecutive.memory.facts import (
    Fact,
    approval_rules,
    approve_fact,
    decline_fact,
    get_fact,
    list_facts,
    retire_fact,
    set_needs_approval,
)
from openexecutive.memory.honcho_client import (
    PERSON_CONCLUSIONS_MAX_PAGE,
    PeopleMemory,
    PersonConclusionsPage,
    people_overview,
    person_conclusions,
)
from openexecutive.people.models import Person

router = APIRouter()
logger = logging.getLogger(__name__)


class DecisionUpdate(BaseModel):
    domain: str | None = None
    summary: str | None = None
    rationale: str | None = None
    outcome: str | None = None
    tags: str | None = None


class InitiativeUpdate(BaseModel):
    title: str | None = None
    status: str | None = None
    summary: str | None = None


class AdviceUpdate(BaseModel):
    domain: str | None = None
    query_summary: str | None = None
    advice_summary: str | None = None


# --- Decisions ---


@router.get("/memories/decisions", response_model=list[Decision])
def get_decisions() -> list[Decision]:
    return list_decisions()


@router.patch("/memories/decisions/{decision_id}", response_model=Decision)
def patch_decision(decision_id: int, body: DecisionUpdate) -> Decision:
    if not update_decision(decision_id, **body.model_dump(exclude_unset=True)):
        raise HTTPException(status_code=404, detail="Decision not found")
    updated = get_decision(decision_id)
    if updated is None:
        raise HTTPException(status_code=404, detail="Decision not found")
    return updated


@router.delete("/memories/decisions/{decision_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_decision(decision_id: int) -> Response:
    if not delete_decision(decision_id):
        raise HTTPException(status_code=404, detail="Decision not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Initiatives ---


@router.get("/memories/initiatives", response_model=list[Initiative])
def get_initiatives() -> list[Initiative]:
    return list_initiatives()


@router.patch("/memories/initiatives/{initiative_id}", response_model=Initiative)
def patch_initiative(initiative_id: int, body: InitiativeUpdate) -> Initiative:
    if not update_initiative(initiative_id, **body.model_dump(exclude_unset=True)):
        raise HTTPException(status_code=404, detail="Initiative not found")
    updated = get_initiative(initiative_id)
    if updated is None:
        raise HTTPException(status_code=404, detail="Initiative not found")
    return updated


@router.delete("/memories/initiatives/{initiative_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_initiative(initiative_id: int) -> Response:
    if not delete_initiative(initiative_id):
        raise HTTPException(status_code=404, detail="Initiative not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Advice ---


@router.get("/memories/advice", response_model=list[Advice])
def get_advice_list() -> list[Advice]:
    return list_advice()


@router.patch("/memories/advice/{advice_id}", response_model=Advice)
def patch_advice(advice_id: int, body: AdviceUpdate) -> Advice:
    if not update_advice(advice_id, **body.model_dump(exclude_unset=True)):
        raise HTTPException(status_code=404, detail="Advice not found")
    updated = get_advice(advice_id)
    if updated is None:
        raise HTTPException(status_code=404, detail="Advice not found")
    return updated


@router.delete("/memories/advice/{advice_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_advice(advice_id: int) -> Response:
    if not delete_advice(advice_id):
        raise HTTPException(status_code=404, detail="Advice not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Standing facts (corrections that persist everywhere) ---


class FactsPage(BaseModel):
    facts: list[Fact]
    # Whether this caller may retire any fact (the principal only).
    can_retire: bool
    # The facts this caller may retire: every active one for the principal; a
    # teammate's own attributed ones for them.
    retirable_ids: list[int] = []
    # Whether this caller approves or declines teammates' proposed facts and
    # sets who needs approval (the principal only).
    can_review: bool = False


class FactRetire(BaseModel):
    reason: str = ""


class FactReview(BaseModel):
    reason: str = ""


class FactApprovalRule(BaseModel):
    person_id: int
    full_name: str
    needs_approval: bool


class FactApprovalUpdate(BaseModel):
    needs_approval: bool


def _caller_id(request: Request) -> int | None:
    from openexecutive.api.routes.chat import _resolve_caller_person_id

    try:
        return _resolve_caller_person_id(request)
    except Exception:
        return None


def _is_own_teammate_fact(fact: Fact, caller: int | None) -> bool:
    return (
        caller is not None
        and fact.recorded_by_role == "teammate"
        and fact.recorded_by_person_id == caller
    )


def _audit_fact(event: str, summary: str, details: dict[str, object], actor: str) -> None:
    try:
        from openexecutive.audit import log_event as audit_log

        # Private: the row names the fact, and a teammate must not read one the
        # principal retired or declined through /audit when the facts route
        # hides it from them.
        audit_log(event, summary, actor=actor, details=details, private=True)
    except Exception:  # noqa: BLE001 - the change already landed.
        logger.warning("%s audit row failed", event, exc_info=True)


@router.get("/memories/facts", response_model=FactsPage)
def get_facts(
    request: Request,
    include_inactive: bool = Query(True),
    limit: int = Query(200, ge=1, le=500),
) -> FactsPage:
    """The standing facts and corrections the principal and teammates asked
    to keep (``memory.facts``), newest first — with their replaced, retired,
    proposed and declined history unless ``include_inactive=false`` — and the
    company-profile fields changed from chat. Every prompt that produces
    output reads the active ones. The history is the principal's alone:
    anyone else gets the active rows, plus their own proposals waiting for
    the principal.

    The facts themselves are company knowledge every conversation already
    sees, and so is who stated a teammate's (it renders "(per <name>)"); the
    rest of the provenance (the quote, a retire reason, and the session, turn
    and person id it came from) is shown to the principal only."""
    principal = _caller_is_principal(request)
    caller = None if principal else _caller_id(request)
    # A teammate sees only what is in force: a fact the principal retired or
    # replaced (perhaps because it was wrong or too sensitive) no longer
    # renders anywhere, so its text is not theirs to read either.
    if principal:
        rows = list_facts(include_inactive=include_inactive, limit=limit)
        retirable = [f.id for f in rows if f.status == "active" and f.kind != "profile"]
    else:
        rows = [
            f for f in list_facts(include_inactive=True, limit=limit)
            if f.status == "active" or (f.status == "proposed" and _is_own_teammate_fact(f, caller))
        ]
        retirable = [f.id for f in rows if f.status == "active" and _is_own_teammate_fact(f, caller)]
        # Provenance is the principal's: their words, and which of their
        # chats and turns a fact came from (ids other routes may key on).
        rows = [
            f.model_copy(update={
                "source_quote": f.source_quote if _is_own_teammate_fact(f, caller) else "",
                "retired_reason": "",
                "session_id": None, "turn_id": None, "recorded_by_person_id": None,
            })
            for f in rows
        ]
    return FactsPage(facts=rows, can_retire=principal, retirable_ids=retirable, can_review=principal)


@router.post("/memories/facts/{fact_id}/retire", response_model=Fact)
def retire_standing_fact(fact_id: int, request: Request, body: FactRetire | None = None) -> Fact:
    """Stop an active fact rendering into any prompt. The row stays, as
    ``retired``, so the Pulse page still shows what it said. The principal
    can retire any; a teammate only one they recorded themselves."""
    principal = _caller_is_principal(request)
    existing = get_fact(fact_id)
    caller = None if principal else _caller_id(request)
    if not principal and not (existing is not None and _is_own_teammate_fact(existing, caller)):
        raise HTTPException(
            status_code=403,
            detail="Only the principal, or the teammate who recorded it, can retire a standing fact",
        )
    if existing is None or existing.kind == "profile":
        raise HTTPException(status_code=404, detail="Fact not found")
    reason = " ".join(((body.reason if body else "") or "retired from the Pulse page").split())
    retired = retire_fact(fact_id, reason=reason[:280])
    if retired is None:
        raise HTTPException(status_code=409, detail="Fact is no longer active")
    # Not the reason: it is the retiring person's own words.
    _audit_fact(
        "fact_retired", f"Standing fact {fact_id} retired: {existing.subject[:80]}",
        {"fact_id": fact_id, "subject": existing.subject, "statement": existing.statement,
         "by": "principal" if principal else "teammate"},
        actor="principal" if principal else "teammate",
    )
    return retired


def _require_principal(request: Request, what: str) -> None:
    if not _caller_is_principal(request):
        raise HTTPException(status_code=403, detail=f"Only the principal can {what}")


@router.post("/memories/facts/{fact_id}/approve", response_model=Fact)
def approve_standing_fact(fact_id: int, request: Request) -> Fact:
    """Put a teammate's proposed fact in force (it replaces what it names,
    the principal's own fact included). Principal only."""
    _require_principal(request, "approve a teammate's fact")
    approved = approve_fact(fact_id)
    if approved is None:
        raise HTTPException(status_code=409, detail="Fact is not waiting for approval")
    fact, superseded = approved
    _audit_fact(
        "fact_reviewed", f"Standing fact {fact_id} approved: {fact.subject[:80]}",
        {"fact_id": fact_id, "decision": "approved", "subject": fact.subject,
         "statement": fact.statement, "superseded_ids": [f.id for f in superseded]},
        actor="principal",
    )
    return fact


@router.post("/memories/facts/{fact_id}/decline", response_model=Fact)
def decline_standing_fact(fact_id: int, request: Request, body: FactReview | None = None) -> Fact:
    """Drop a teammate's proposed fact; it is never used. Principal only."""
    _require_principal(request, "decline a teammate's fact")
    reason = " ".join(((body.reason if body else "") or "declined from the Pulse page").split())
    declined = decline_fact(fact_id, reason=reason[:280])
    if declined is None:
        raise HTTPException(status_code=409, detail="Fact is not waiting for approval")
    _audit_fact(
        "fact_reviewed", f"Standing fact {fact_id} declined: {declined.subject[:80]}",
        {"fact_id": fact_id, "decision": "declined", "subject": declined.subject,
         "statement": declined.statement},
        actor="principal",
    )
    return declined


def _teammates() -> list[Person]:
    from openexecutive.people.store import list_people

    return [
        p for p in list_people()
        if not p.is_principal and not p.archived and p.kind == "team" and p.id is not None
    ]


@router.get("/memories/facts/approval", response_model=list[FactApprovalRule])
def get_fact_approval_rules(request: Request) -> list[FactApprovalRule]:
    """Every teammate and whether the principal approves their facts before
    they are used ("needs my approval", off by default). Principal only."""
    _require_principal(request, "see who needs approval")
    rules = approval_rules()
    return [
        FactApprovalRule(person_id=pid, full_name=p.full_name, needs_approval=rules.get(pid, False))
        for p in _teammates()
        if (pid := p.id) is not None
    ]


@router.put("/memories/facts/approval/{person_id}", response_model=FactApprovalRule)
def put_fact_approval_rule(person_id: int, body: FactApprovalUpdate, request: Request) -> FactApprovalRule:
    """Turn "needs my approval" on or off for one teammate. Principal only."""
    _require_principal(request, "change who needs approval")
    person = next((p for p in _teammates() if p.id == person_id), None)
    if person is None:
        raise HTTPException(status_code=404, detail="Teammate not found")
    set_needs_approval(person_id, body.needs_approval)
    _audit_fact(
        "fact_approval_changed",
        f"Standing facts from {person.full_name} {'need' if body.needs_approval else 'no longer need'} approval",
        {"person_id": person_id, "needs_approval": body.needs_approval},
        actor="principal",
    )
    return FactApprovalRule(person_id=person_id, full_name=person.full_name,
                            needs_approval=body.needs_approval)


# --- People (peer memory) ---


def _caller_is_principal(request: Request) -> bool:
    from openexecutive.api.routes.people import caller_is_principal

    return caller_is_principal(request)


def _is_principal_id(person_id: int) -> bool:
    """Whether ``person_id`` is a principal row. Fails closed (True): an
    unreadable roster must not open the principal's memory to others."""
    try:
        from openexecutive.people.store import get_person

        person = get_person(person_id)
    except Exception:
        return True
    return bool(person is not None and person.is_principal)


@router.get("/memories/people", response_model=PeopleMemory)
async def list_people_memory(
    request: Request, recent: int = Query(5, ge=1, le=50)
) -> PeopleMemory:
    """What peer memory knows about each rostered person: card, conclusion
    count, last-learned time and the ``recent`` newest conclusions. Read-only
    and LLM-free; ``status`` is ``disabled`` when peer memory is off.

    The principal's own entry is shown to the principal only: it is drawn
    from all their conversations, including about their contacts, which are
    private to them."""
    overview = await people_overview(recent=recent)
    if _caller_is_principal(request):
        return overview
    people = [p for p in overview.people if not p.is_principal]
    return overview.model_copy(update={
        "people": people,
        "conclusion_total": sum(p.conclusion_count for p in people),
    })


@router.get("/memories/people/{person_id}/conclusions", response_model=PersonConclusionsPage)
async def list_person_conclusions(
    person_id: int,
    request: Request,
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=PERSON_CONCLUSIONS_MAX_PAGE),
) -> PersonConclusionsPage:
    """One page of every conclusion peer memory holds about one person,
    newest first. Read-only and LLM-free; 404 when the person is not on the
    roster or peer memory has no peer for them yet — and, for anyone but
    the principal, for the principal (their memory covers their contacts,
    which are private to them)."""
    if _is_principal_id(person_id) and not _caller_is_principal(request):
        raise HTTPException(status_code=404, detail="Person not found in peer memory")
    result = await person_conclusions(person_id, page=page, size=size)
    if result is None:
        raise HTTPException(status_code=404, detail="Person not found in peer memory")
    return result
