"""FastAPI routes for People.

Phase 3 surface: CRUD + archive + approver lookup.
All mutations invalidate the 60s registry cache so the next
Executive turn picks up the change.
"""
from __future__ import annotations

import logging
from datetime import date

from fastapi import APIRouter, Header, HTTPException, Response, status
from pydantic import BaseModel, Field

from openexecutive.api.authorization import PrincipalOnly
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store
from openexecutive.people.models import (
    AuthorityScope,
    AvailabilityWindow,
    Person,
)

logger = logging.getLogger(__name__)

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
def create_person(
    body: PersonCreate,
    _: PrincipalOnly,
    x_caller_email: str | None = Header(default=None, alias="X-Caller-Email"),
) -> Person:
    if body.is_principal:
        if people_store.find_principal_person() is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A principal is already configured.",
            )
        if not body.email or not x_caller_email or body.email.lower() != x_caller_email.lower():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="The principal email must match the verified caller.",
            )
        if AuthorityScope.WILDCARD not in body.authority_scope:
            body.authority_scope.append(AuthorityScope.WILDCARD)
    try:
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
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
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
def patch_person(person_id: int, body: PersonPatch, _: PrincipalOnly) -> Person:
    person = people_store.get_person(person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="Person not found")

    raw = body.model_dump(exclude_unset=True)
    if "email" in raw and person.is_principal:
        current_email = (person.email or "").lower()
        requested_email = (body.email or "").lower()
        if requested_email != current_email:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The principal email is bound during onboarding and cannot be changed.",
            )
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
def archive_person(person_id: int, _: PrincipalOnly) -> Response:
    person = people_store.get_person(person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="Person not found")
    if person.is_principal:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The principal cannot be archived.",
        )
    people_store.archive_person(person_id)
    people_registry.invalidate()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
