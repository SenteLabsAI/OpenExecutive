from __future__ import annotations

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
from openexecutive.memory.facts import Fact, get_fact, list_facts, retire_fact
from openexecutive.memory.honcho_client import (
    PERSON_CONCLUSIONS_MAX_PAGE,
    PeopleMemory,
    PersonConclusionsPage,
    people_overview,
    person_conclusions,
)

router = APIRouter()


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
    # Whether this caller may retire a fact (the principal only).
    can_retire: bool


class FactRetire(BaseModel):
    reason: str = ""


@router.get("/memories/facts", response_model=FactsPage)
def get_facts(
    request: Request,
    include_inactive: bool = Query(True),
    limit: int = Query(200, ge=1, le=500),
) -> FactsPage:
    """The standing facts and corrections the principal asked to keep
    (``memory.facts``), newest first — with their replaced and retired
    history unless ``include_inactive=false`` — and the company-profile
    fields changed from chat. Every prompt that produces output reads the
    active ones. The history is the principal's alone: anyone else gets the
    active rows only.

    The facts themselves are company knowledge every conversation already
    sees; the provenance quote and a retire reason are the principal's own
    words and are shown to the principal only."""
    principal = _caller_is_principal(request)
    # A teammate sees only what is in force: a fact the principal retired or
    # replaced (perhaps because it was wrong or too sensitive) no longer
    # renders anywhere, so its text is not theirs to read either.
    rows = list_facts(include_inactive=include_inactive and principal, limit=limit)
    if not principal:
        rows = [f.model_copy(update={"source_quote": "", "retired_reason": ""}) for f in rows]
    return FactsPage(facts=rows, can_retire=principal)


@router.post("/memories/facts/{fact_id}/retire", response_model=Fact)
def retire_standing_fact(fact_id: int, request: Request, body: FactRetire | None = None) -> Fact:
    """Stop an active fact rendering into any prompt. The row stays, as
    ``retired``, so the Pulse page still shows what it said. Principal only:
    a standing fact carries their authority in every later prompt."""
    if not _caller_is_principal(request):
        raise HTTPException(status_code=403, detail="Only the principal can retire a standing fact")
    existing = get_fact(fact_id)
    if existing is None or existing.kind == "profile":
        raise HTTPException(status_code=404, detail="Fact not found")
    reason = " ".join(((body.reason if body else "") or "retired from the Pulse page").split())
    retired = retire_fact(fact_id, reason=reason[:280])
    if retired is None:
        raise HTTPException(status_code=409, detail="Fact is no longer active")
    try:
        from openexecutive.audit import log_event as audit_log

        audit_log(
            "fact_retired",
            f"Standing fact {fact_id} retired: {existing.subject[:80]}",
            actor="principal",
            # Not the reason: it is the principal's own words, and the audit
            # log is readable by every signed-in teammate.
            details={"fact_id": fact_id, "subject": existing.subject,
                     "statement": existing.statement},
        )
    except Exception:  # noqa: BLE001 - the retire already landed.
        pass
    return retired


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
