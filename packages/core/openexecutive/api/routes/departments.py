"""FastAPI routes for Departments + their Goals.

Phase 1 surface — no behaviour change to the chat path; this exists so the
UI (Phase 5) can list, read, edit a department charter / authority / cadences,
and CRUD its Goals. The cached registry is invalidated after every mutation
so the principal sees their own edit immediately.

Phase E renamed the OKR endpoints to /goals. The legacy /okrs paths remain
as deprecated aliases for one release (RFC 8594 Deprecation/Sunset/Link
headers, same shape as Phase A's /morning-brief).
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from openexecutive.departments import registry, store
from openexecutive.departments.cadence import (
    CADENCE_FORMATS_HINT,
    is_valid_cadence_spec,
    next_check_in_at,
    reschedule_check_in,
)
from openexecutive.departments.models import (
    AuthorityLevel,
    DepartmentCharter,
    DepartmentState,
    Goal,
    GoalStatus,
    PeriodType,
)
from openexecutive.departments.store import _UNSET

logger = logging.getLogger(__name__)

router = APIRouter()


# --------------------------------------------------------------------------- #
# Deprecation headers (mirrors api/routes/today.py legacy alias style)
# --------------------------------------------------------------------------- #

# 90 days from the Phase E ship date.
_GOAL_ALIAS_SUNSET = "Fri, 22 Aug 2026 00:00:00 GMT"


def _set_deprecated_headers(response: Response, successor: str) -> None:
    response.headers["Deprecation"] = "true"
    response.headers["Sunset"] = _GOAL_ALIAS_SUNSET
    response.headers["Link"] = f'<{successor}>; rel="successor-version"'


# --------------------------------------------------------------------------- #
# Request bodies (only fields a client may set; server-managed timestamps and
# slugs are NOT exposed here, so Pydantic's `exclude_unset` semantics map
# naturally onto partial updates).
# --------------------------------------------------------------------------- #

class DepartmentCreate(BaseModel):
    title: str = Field(min_length=1, max_length=128)
    mission: str = Field(default="", max_length=1024)


class DepartmentPatch(BaseModel):
    title: str | None = None
    charter: DepartmentCharter | None = None
    authority_level: AuthorityLevel | None = None
    head_person_id: int | None = None
    head_persona_slug: str | None = None
    cadences: dict[str, str] | None = None
    headcount: int | None = None
    budget_usd: float | None = None
    # Department-scoped broadcast channels. Each is independently
    # nullable — the request distinguishes "field omitted" (leave as-is)
    # from "field explicitly null" (clear back to NULL) via _UNSET in
    # the route handler below.
    slack_channel_id: str | None = None
    discord_channel_id: str | None = None
    telegram_chat_id: str | None = None
    # Named external entities the department wants watched (strong grounding
    # for the research watch policy). Sending `[]` clears the list.
    watched_entities: list[str] | None = Field(default=None, max_length=50)

    @field_validator("watched_entities")
    @classmethod
    def _clean_watched_entities(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in value:
            name = " ".join(raw.split())
            if not name:
                continue
            if len(name) > 128:
                raise ValueError("watched entity names must be 128 characters or fewer")
            key = name.casefold()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(name)
        return cleaned


class DepartmentDetail(DepartmentState):
    """One department as its page reads it: the state plus when its next
    check-in is queued (ISO-8601 UTC, ``None`` when none is: the check-in is
    off, the workspace is solo, or the scheduler has not queued it yet)."""

    next_check_in_at: str | None = None


def _detail(state: DepartmentState) -> DepartmentDetail:
    return DepartmentDetail(
        **state.model_dump(),
        next_check_in_at=next_check_in_at(state.config.slug),
    )


class GoalCreate(BaseModel):
    """Only `key_result` (the goal itself) is required. A missing or blank
    `period_value` is filled with the current period for `period_type`, and
    `target` may be left empty. Text is stripped first, so whitespace alone
    counts as blank."""

    model_config = ConfigDict(str_strip_whitespace=True)

    period_type: PeriodType = "quarter"
    period_value: str | None = Field(default=None, max_length=64)
    key_result: str = Field(min_length=1, max_length=512)
    target: str = Field(default="", max_length=512)
    current: str = Field(default="", max_length=512)
    status: GoalStatus = "on_track"


class GoalCreateInArea(GoalCreate):
    """A goal for an area named in words: an existing one by slug or title,
    else a new area of that name, else (blank) ``DEFAULT_GOAL_AREA``."""

    area: str = Field(default="", max_length=80)


# Where a goal goes when no area is named.
DEFAULT_GOAL_AREA = "General"


class GoalPatch(BaseModel):
    # Stripped first, so a whitespace-only goal or period is rejected like "".
    model_config = ConfigDict(str_strip_whitespace=True)

    period_type: PeriodType | None = None
    period_value: str | None = Field(default=None, min_length=1, max_length=64)
    key_result: str | None = Field(default=None, min_length=1, max_length=512)
    target: str | None = Field(default=None, max_length=512)
    current: str | None = Field(default=None, max_length=512)
    status: GoalStatus | None = None


def _translate_legacy_body(body: dict) -> dict:
    """Map the legacy `quarter` key to `period_value` so a client that hasn't
    updated keeps working through the /okrs alias's Sunset date.

    Acts on a shallow copy so the input dict is not mutated. If both keys are
    present, the new `period_value` wins (the legacy alias is best-effort
    compatibility, not authoritative).
    """
    translated = dict(body)
    if "quarter" in translated:
        translated.setdefault("period_value", translated.pop("quarter"))
        translated.setdefault("period_type", "quarter")
    return translated


# --------------------------------------------------------------------------- #
# Read routes
# --------------------------------------------------------------------------- #

@router.get("/departments", response_model=list[DepartmentState])
def list_departments() -> list[DepartmentState]:
    """List all departments with their Goals."""
    # Read straight through the store (not the cache) so the API view is always
    # fresh; the registry cache is for the per-turn prompt-block path only.
    return store.list_departments()


@router.get("/departments/{slug}", response_model=DepartmentDetail)
def get_department(slug: str) -> DepartmentDetail:
    state = store.get_department(slug)
    if state is None:
        raise HTTPException(status_code=404, detail="Unknown department")
    return _detail(state)


# --------------------------------------------------------------------------- #
# Department mutation
# --------------------------------------------------------------------------- #

@router.post("/departments", response_model=DepartmentState, status_code=status.HTTP_201_CREATED)
def create_department(body: DepartmentCreate) -> DepartmentState:
    """Create a new custom department. Slug is auto-derived from title."""
    try:
        state = store.create_department(body.title, mission=body.mission)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    registry.invalidate()
    return state


@router.delete("/departments/{slug}", status_code=status.HTTP_204_NO_CONTENT)
def delete_department(slug: str) -> Response:
    """Delete a department and all its Goals."""
    if store.get_department(slug) is None:
        raise HTTPException(status_code=404, detail="Unknown department")
    store.delete_department(slug)
    registry.invalidate()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch("/departments/{slug}", response_model=DepartmentDetail)
def patch_department(slug: str, patch: DepartmentPatch) -> DepartmentDetail:
    before = store.get_department(slug)
    if before is None:
        raise HTTPException(status_code=404, detail="Unknown department")

    raw = patch.model_dump(exclude_unset=True)
    if not raw:
        return _detail(before)

    # An unparseable spec would be stored and then silently skipped by the
    # scheduler, so reject it here. Only an exactly empty spec means "no
    # cadence" (the scheduler's own check is `if not spec`).
    for name, spec in (patch.cadences or {}).items():
        if spec and not is_valid_cadence_spec(spec):
            raise HTTPException(
                status_code=422,
                detail=f"Invalid {name} cadence {spec!r}: use {CADENCE_FORMATS_HINT}.",
            )

    # A department head is chased by check-ins and routed approvals, so a new
    # head must be a team member. One of the principal's contacts gets exactly
    # the answer an id that does not exist gets — no name, nothing that tells
    # the caller (any signed-in user may PATCH a department) a contact is
    # there. An unchanged head is not re-checked, so saving the other settings
    # never fails over a head set before this rule.
    if (
        "head_person_id" in raw
        and patch.head_person_id is not None
        and patch.head_person_id != _must_get(slug).config.head_person_id
    ):
        from openexecutive.people.store import get_person

        head = get_person(patch.head_person_id)
        if head is None or head.kind != "team":
            raise HTTPException(
                status_code=422,
                detail=f"person_id {patch.head_person_id} is not on the team.",
            )

    store.update_department(
        slug,
        title=patch.title,
        charter=patch.charter,
        authority_level=patch.authority_level,
        # Use _UNSET when the field was not present in the request so "not
        # provided" and "explicitly null (clear)" are distinguishable.
        head_person_id=patch.head_person_id if "head_person_id" in raw else _UNSET,
        head_persona_slug=patch.head_persona_slug,
        cadences=patch.cadences,
        headcount=patch.headcount,
        budget_usd=patch.budget_usd,
        slack_channel_id=patch.slack_channel_id if "slack_channel_id" in raw else _UNSET,
        discord_channel_id=patch.discord_channel_id if "discord_channel_id" in raw else _UNSET,
        telegram_chat_id=patch.telegram_chat_id if "telegram_chat_id" in raw else _UNSET,
        watched_entities=patch.watched_entities,
    )
    registry.invalidate()
    # A changed check-in cadence takes effect now: the pending check-in on the
    # old schedule is cancelled and the next one on the new schedule queued
    # (none when it was switched off with an empty spec).
    if (
        patch.cadences is not None
        and patch.cadences.get("check_in", "") != before.config.cadences.get("check_in", "")
    ):
        try:
            reschedule_check_in(slug)
        except Exception:  # noqa: BLE001 - the edit is saved; a failed re-time keeps the old row.
            logger.exception("departments.patch: could not re-time %r's check-in", slug)
    return _detail(_must_get(slug))


# --------------------------------------------------------------------------- #
# Goal routes (primary)
# --------------------------------------------------------------------------- #

def _current_period_value(period_type: str) -> str:
    """The current period's label in the user's zone — what the goal editor
    and `create_goal` fill in when no period is given."""
    from openexecutive.orchestrator.department_tools import (
        _today_local,
        default_period_value,
    )

    try:
        today = _today_local()
    except Exception:  # noqa: BLE001 - a zone read must not fail the create.
        today = datetime.now(UTC).date()
    return default_period_value(period_type, today)


def _create_goal(slug: str, body: GoalCreate) -> Goal:
    if store.get_department(slug) is None:
        raise HTTPException(status_code=404, detail="Unknown department")
    period_value = (body.period_value or "").strip() or _current_period_value(body.period_type)
    goal_id = store.insert_goal(
        slug,
        period_type=body.period_type,
        period_value=period_value,
        key_result=body.key_result,
        target=body.target,
        current=body.current,
        status=body.status,
    )
    goal = store.get_goal(goal_id)
    if goal is None:
        # Inserted but missing on read-back — only plausible cause is a
        # concurrent delete; surface as 500 so the UI can refresh.
        raise HTTPException(status_code=500, detail="Goal vanished after insert")
    registry.invalidate()
    return goal


def _patch_goal(slug: str, goal_id: int, body: GoalPatch) -> Goal:
    existing = store.get_goal(goal_id)
    if existing is None or existing.department_slug != slug:
        raise HTTPException(status_code=404, detail="Unknown Goal for department")
    store.update_goal(
        goal_id,
        period_type=body.period_type,
        period_value=body.period_value,
        key_result=body.key_result,
        target=body.target,
        current=body.current,
        status=body.status,
    )
    updated = store.get_goal(goal_id)
    if updated is None:
        raise HTTPException(status_code=404, detail="Goal vanished mid-update")
    registry.invalidate()
    return updated


def _delete_goal_impl(slug: str, goal_id: int) -> None:
    existing = store.get_goal(goal_id)
    if existing is None or existing.department_slug != slug:
        raise HTTPException(status_code=404, detail="Unknown Goal for department")
    store.delete_goal(goal_id)
    registry.invalidate()


@router.post(
    "/departments/{slug}/goals",
    response_model=Goal,
    status_code=status.HTTP_201_CREATED,
)
def create_goal(slug: str, body: GoalCreate) -> Goal:
    return _create_goal(slug, body)


@router.post("/goals", response_model=Goal, status_code=status.HTTP_201_CREATED)
def create_goal_in_area(body: GoalCreateInArea) -> Goal:
    """Add a goal by area name, creating the area when there is none by that
    name. The Goals page uses it when the workspace has no areas yet (setup
    creates none for a solo workspace or a team that names none). Like
    ``POST /departments``, a new area gets no specialist."""
    title = body.area or DEFAULT_GOAL_AREA
    store.initialize_db()
    known = [d.config for d in store.list_departments()]
    match = store.match_department(title, known)
    if match is None:
        try:
            match = store.create_department(title).config
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        registry.invalidate()
    return _create_goal(match.slug, GoalCreate.model_validate(body.model_dump(exclude={"area"})))


@router.patch("/departments/{slug}/goals/{goal_id}", response_model=Goal)
def patch_goal(slug: str, goal_id: int, body: GoalPatch) -> Goal:
    return _patch_goal(slug, goal_id, body)


@router.delete(
    "/departments/{slug}/goals/{goal_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_goal(slug: str, goal_id: int) -> Response:
    _delete_goal_impl(slug, goal_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Legacy /okrs aliases — deprecated, removed at Sunset
# --------------------------------------------------------------------------- #

@router.post(
    "/departments/{slug}/okrs",
    response_model=Goal,
    status_code=status.HTTP_201_CREATED,
    deprecated=True,
    summary="Deprecated alias for POST /departments/{slug}/goals",
)
def create_okr_alias(slug: str, body: dict, response: Response) -> Goal:
    _set_deprecated_headers(response, f"/departments/{slug}/goals")
    try:
        parsed = GoalCreate(**_translate_legacy_body(body))
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _create_goal(slug, parsed)


@router.patch(
    "/departments/{slug}/okrs/{okr_id}",
    response_model=Goal,
    deprecated=True,
    summary="Deprecated alias for PATCH /departments/{slug}/goals/{goal_id}",
)
def patch_okr_alias(slug: str, okr_id: int, body: dict, response: Response) -> Goal:
    _set_deprecated_headers(response, f"/departments/{slug}/goals/{okr_id}")
    try:
        parsed = GoalPatch(**_translate_legacy_body(body))
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _patch_goal(slug, okr_id, parsed)


@router.delete(
    "/departments/{slug}/okrs/{okr_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    deprecated=True,
    summary="Deprecated alias for DELETE /departments/{slug}/goals/{goal_id}",
)
def delete_okr_alias(slug: str, okr_id: int, response: Response) -> Response:
    _delete_goal_impl(slug, okr_id)
    # Even with 204 the response body is empty; headers still ride along.
    response.status_code = status.HTTP_204_NO_CONTENT
    _set_deprecated_headers(response, f"/departments/{slug}/goals/{okr_id}")
    return response


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _must_get(slug: str) -> DepartmentState:
    state = store.get_department(slug)
    if state is None:
        # Should be impossible — caller verified existence — but raising 500
        # is more honest than returning a synthetic empty state.
        raise HTTPException(status_code=500, detail="Department vanished")
    return state
