"""FastAPI routes for People.

Phase 3 surface: CRUD + archive + approver lookup.
All mutations invalidate the 60s registry cache so the next
Executive turn picks up the change.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store
from openexecutive.people.models import (
    AuthorityScope,
    AvailabilityWindow,
    Person,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from openexecutive.attunement.style import StyleProfile

router = APIRouter()


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #

class PersonCreate(BaseModel):
    full_name: str = Field(min_length=1, max_length=200)
    role: str = Field(default="", max_length=200)
    is_principal: bool = False
    department_slugs: list[str] = Field(default_factory=list)
    email: str | None = None
    slack_user_id: str | None = None
    telegram_chat_id: str | None = None
    discord_user_id: str | None = None
    preferred_channel: str = "any"
    response_sla_hours: int = Field(default=24, ge=1, le=8760)
    on_leave_until: date | None = None
    reports_to_person_id: int | None = None
    authority_scope: list[AuthorityScope] = Field(default_factory=list)
    availability: list[AvailabilityWindow] = Field(default_factory=list)


class PersonPatch(BaseModel):
    full_name: str | None = Field(default=None, max_length=200)
    role: str | None = Field(default=None, max_length=200)
    email: str | None = None
    slack_user_id: str | None = None
    telegram_chat_id: str | None = None
    discord_user_id: str | None = None
    preferred_channel: str | None = None
    response_sla_hours: int | None = Field(default=None, ge=1, le=8760)
    on_leave_until: date | None = None
    clear_on_leave: bool = False
    reports_to_person_id: int | None = None
    department_slugs: list[str] | None = None
    authority_scope: list[AuthorityScope] | None = None
    availability: list[AvailabilityWindow] | None = None


# --------------------------------------------------------------------------- #
# Read routes
# --------------------------------------------------------------------------- #

@router.get("/people", response_model=list[Person])
def list_people(include_archived: bool = False) -> list[Person]:
    return people_store.list_people(include_archived=include_archived)


@router.get("/people/by-scope/{token}", response_model=list[Person])
def people_by_scope(token: str) -> list[Person]:
    """Return non-archived people who can approve the given scope token."""
    try:
        scope = AuthorityScope(token)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown scope token: {token!r}. Valid tokens: {[s.value for s in AuthorityScope]}",
        ) from exc
    return people_store.find_approvers(scope)


@router.get("/people/{person_id}", response_model=Person)
def get_person(person_id: int) -> Person:
    person = people_store.get_person(person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="Person not found")
    return person


# --------------------------------------------------------------------------- #
# Mutation routes
# --------------------------------------------------------------------------- #

@router.post("/people", response_model=Person, status_code=status.HTTP_201_CREATED)
def create_person(body: PersonCreate) -> Person:
    pid = people_store.upsert_person(
        full_name=body.full_name,
        role=body.role,
        is_principal=body.is_principal,
        department_slugs=body.department_slugs,
        email=body.email,
        slack_user_id=body.slack_user_id,
        telegram_chat_id=body.telegram_chat_id,
        discord_user_id=body.discord_user_id,
        preferred_channel=body.preferred_channel,  # type: ignore[arg-type]
        response_sla_hours=body.response_sla_hours,
        on_leave_until=body.on_leave_until,
        reports_to_person_id=body.reports_to_person_id,
    )
    if body.authority_scope:
        people_store.set_authority_scope(pid, body.authority_scope)
    if body.availability:
        people_store.set_availability(pid, body.availability)
    people_registry.invalidate()
    person = people_store.get_person(pid)
    if person is None:
        raise HTTPException(status_code=500, detail="Person vanished after insert")
    return person


@router.patch("/people/{person_id}", response_model=Person)
def patch_person(person_id: int, body: PersonPatch) -> Person:
    if people_store.get_person(person_id) is None:
        raise HTTPException(status_code=404, detail="Person not found")

    raw = body.model_dump(exclude_unset=True)
    if raw:
        people_store.update_person(
            person_id,
            full_name=body.full_name,
            role=body.role,
            email=body.email,
            slack_user_id=body.slack_user_id,
            telegram_chat_id=body.telegram_chat_id,
            discord_user_id=body.discord_user_id,
            preferred_channel=body.preferred_channel,  # type: ignore[arg-type]
            response_sla_hours=body.response_sla_hours,
            on_leave_until=body.on_leave_until,
            clear_on_leave=body.clear_on_leave,
            reports_to_person_id=body.reports_to_person_id,
            department_slugs=body.department_slugs,
        )
    if "authority_scope" in raw:
        people_store.set_authority_scope(
            person_id, body.authority_scope or []
        )
    if "availability" in raw:
        people_store.set_availability(
            person_id, body.availability or []
        )
    people_registry.invalidate()
    person = people_store.get_person(person_id)
    if person is None:
        raise HTTPException(status_code=500, detail="Person vanished")
    return person


@router.post("/people/{person_id}/archive", status_code=status.HTTP_204_NO_CONTENT)
def archive_person(person_id: int) -> Response:
    if people_store.get_person(person_id) is None:
        raise HTTPException(status_code=404, detail="Person not found")
    people_store.archive_person(person_id)
    people_registry.invalidate()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Open loops (attunement) — what each person owes
# --------------------------------------------------------------------------- #

class OpenLoopOut(BaseModel):
    loop_id: int
    owner_person_id: int
    owner_name: str
    description: str
    due_at: str
    created_at: str


class OpenLoopClose(BaseModel):
    reason: str = Field(default="done", pattern="^(done|not_needed|cancelled)$")


def _require_principal_or(person_id: int, request: Request) -> int:
    """The resolved caller, if it is ``person_id`` or the principal; else 403."""
    from openexecutive.api.routes.chat import _resolve_caller_person_id

    caller = _resolve_caller_person_id(request)
    if caller is None or not people_store.is_principal_or_self(caller, person_id):
        raise HTTPException(status_code=403, detail="Only the principal or the owner")
    return int(caller)


@router.get("/people/{person_id}/open-loops", response_model=list[OpenLoopOut])
def get_person_open_loops(person_id: int, request: Request) -> list[OpenLoopOut]:
    """Open loops this person owns, soonest due first. The principal or that
    person only — what someone owes is not roster-public."""
    from openexecutive.attunement.open_loops import list_open_loops

    if people_store.get_person(person_id) is None:
        raise HTTPException(status_code=404, detail="Person not found")
    _require_principal_or(person_id, request)
    return [
        OpenLoopOut(
            loop_id=loop.id,
            owner_person_id=loop.owner_person_id,
            owner_name=loop.owner_name,
            description=loop.description,
            due_at=loop.due_at,
            created_at=loop.created_at,
        )
        for loop in list_open_loops(person_id=person_id, limit=100)
    ]


@router.post("/open-loops/{loop_id}/close", status_code=status.HTTP_204_NO_CONTENT)
def close_open_loop_route(loop_id: int, body: OpenLoopClose, request: Request) -> Response:
    """Close one open loop. Only the principal or the loop's owner may."""
    from openexecutive.attunement.open_loops import close_open_loop, get_open_loop

    loop = get_open_loop(loop_id)
    if loop is None:
        raise HTTPException(status_code=404, detail="Open loop not found")
    caller = _require_principal_or(loop.owner_person_id, request)
    close_open_loop(loop_id, reason=body.reason, closed_by_person_id=caller)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class OutreachStat(BaseModel):
    source: str
    label: str
    sent: int
    replied: int
    acted: int
    ignored: int
    pending: int


@router.get("/people/{person_id}/outreach", response_model=list[OutreachStat])
def get_person_outreach(person_id: int, request: Request) -> list[OutreachStat]:
    """How this person responded to proactive outreach over the last 30 days,
    per kind of outreach. The principal or that person only."""
    from openexecutive.attunement import outcomes

    # Authorize first, so a non-principal can't probe which ids exist.
    _require_principal_or(person_id, request)
    if people_store.get_person(person_id) is None:
        raise HTTPException(status_code=404, detail="Person not found")
    rows: list[OutreachStat] = []
    for (_, source), s in sorted(outcomes.acceptance(person_id=person_id).items()):
        rows.append(OutreachStat(
            source=source,
            label=outcomes.SOURCE_LABELS.get(source, source),
            sent=s.sent,
            replied=s.counts.get(outcomes.OUTCOME_REPLIED, 0),
            acted=s.counts.get(outcomes.OUTCOME_ACTED, 0),
            ignored=s.counts.get(outcomes.OUTCOME_IGNORED, 0),
            pending=s.pending,
        ))
    return rows


# ---------------------------------------------------------------------------
# Working style (attunement) — how this person likes replies
# ---------------------------------------------------------------------------


class WorkingStyleRule(BaseModel):
    text: str
    basis: str


class WorkingStyleOut(BaseModel):
    rules: list[WorkingStyleRule]
    locked: bool
    updated_at: str | None = None
    updated_by: str | None = None


class WorkingStyleIn(BaseModel):
    """``rules`` omitted keeps the current rules (and their provenance) and
    only sets the lock."""

    rules: list[str] | None = Field(default=None, max_length=4)
    locked: bool = False


def _style_out(profile: StyleProfile) -> WorkingStyleOut:
    return WorkingStyleOut(
        rules=[WorkingStyleRule(text=r.text, basis=r.basis) for r in profile.rules],
        locked=profile.locked,
        updated_at=profile.updated_at,
        updated_by=profile.updated_by,
    )


def _style_person(person_id: int, request: Request) -> int:
    """Authorize (principal or that person), then 404 an unknown or archived
    id — archiving drops the profile, and nothing may re-create it."""
    caller = _require_principal_or(person_id, request)
    person = people_store.get_person(person_id)
    if person is None or person.archived:
        raise HTTPException(status_code=404, detail="Person not found")
    return caller


@router.get("/people/{person_id}/attunement", response_model=WorkingStyleOut)
def get_person_working_style(person_id: int, request: Request) -> WorkingStyleOut:
    """The short working-style rules pinned into this person's turns. The
    principal or that person only."""
    from openexecutive.attunement.style import get_profile

    _style_person(person_id, request)
    return _style_out(get_profile(person_id))


@router.put("/people/{person_id}/attunement", response_model=WorkingStyleOut)
def put_person_working_style(
    person_id: int, body: WorkingStyleIn, request: Request
) -> WorkingStyleOut:
    """Replace the rules and set the lock. Rules pass the same style-only
    checks as learned ones (they reach a tool-capable turn). Rules typed here
    are kept by the learning pass, which only fills the remaining slots; a
    locked profile is never rewritten by it at all."""
    from openexecutive.attunement.style import (
        BASIS_EDITED,
        StyleRule,
        get_profile,
        save_profile,
        validate_edited_rule,
    )

    caller = _style_person(person_id, request)
    if body.rules is None:
        # Only the lock changes; rules stored under an older check that no
        # longer pass it are dropped rather than carried forward.
        current = [r for r in get_profile(person_id).rules if validate_edited_rule(r.text)[1] is None]
        return _style_out(save_profile(person_id, current, locked=body.locked,
                                       updated_by=f"person:{caller}"))
    rules: list[StyleRule] = []
    for raw in body.rules:
        text, rejection = validate_edited_rule(raw)
        if rejection:
            raise HTTPException(
                status_code=422,
                detail=f"Rule not accepted ({rejection}): keep each rule to one short "
                "sentence about how replies are written — no actions, people, links "
                "or amounts.",
            )
        if text.lower() not in {r.text.lower() for r in rules}:
            rules.append(StyleRule(text=text, basis=BASIS_EDITED))
    return _style_out(save_profile(person_id, rules, locked=body.locked,
                                   updated_by=f"person:{caller}"))


@router.delete("/people/{person_id}/attunement", status_code=status.HTTP_204_NO_CONTENT)
def delete_person_working_style(person_id: int, request: Request) -> Response:
    """Forget the rules and unlock, so they are re-learned from scratch."""
    from openexecutive.attunement.style import reset_profile

    caller = _style_person(person_id, request)
    reset_profile(person_id, updated_by=f"person:{caller}")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
