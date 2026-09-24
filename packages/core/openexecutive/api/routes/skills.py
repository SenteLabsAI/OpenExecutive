from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from openexecutive.api.models import (
    SkillCreate,
    SkillDeleteResponse,
    SkillDetail,
    SkillListResponse,
    SkillMeta,
    SkillSearchHit,
    SkillSearchResponse,
)
from openexecutive.knowledge.skills import SkillParseError
from openexecutive.knowledge.skills_index import search_skills as _search_skills
from openexecutive.knowledge.skills_repo import (
    SkillConflictError,
    SkillNotFoundError,
    create_skill,
    delete_skill,
    get_skill,
    list_skills,
    restore_skill,
    skill_to_dict,
    update_skill,
)
from openexecutive.knowledge.store import ChromaDBStore

router = APIRouter(prefix="/skills")


def _get_store(request: Request) -> ChromaDBStore:
    if hasattr(request.app.state, "store"):
        return request.app.state.store  # type: ignore[no-any-return]
    from openexecutive.config import get_settings

    return ChromaDBStore(persist_directory=get_settings().vector_store_path)


@router.get("", response_model=SkillListResponse)
async def list_all_skills(include_hidden: bool = False) -> SkillListResponse:
    skills = list_skills(include_hidden=include_hidden)
    return SkillListResponse(
        skills=[SkillMeta(**skill_to_dict(s, include_body=False)) for s in skills]
    )


@router.get("/search", response_model=SkillSearchResponse)
async def search_skills_endpoint(
    request: Request,
    q: str = Query(..., min_length=1),
    n: int = Query(5, ge=1, le=20),
) -> SkillSearchResponse:
    hits = _search_skills(query=q, store=_get_store(request), n_results=n)
    return SkillSearchResponse(results=[SkillSearchHit(**h) for h in hits])


@router.get("/{name}", response_model=SkillDetail)
async def get_skill_endpoint(name: str) -> SkillDetail:
    # Hidden built-ins stay readable here so the UI can show one before
    # restoring it; the Executive's `load_skill` does not see them.
    try:
        skill = get_skill(name, include_hidden=True)
    except SkillNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except SkillParseError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return SkillDetail(**skill_to_dict(skill, include_body=True))


@router.post("", response_model=SkillDetail, status_code=201)
async def create_skill_endpoint(body: SkillCreate, request: Request) -> SkillDetail:
    try:
        skill = create_skill(
            name=body.name,
            description=body.description,
            when_to_use=body.when_to_use,
            category=body.category,
            body=body.body,
            store=_get_store(request),
        )
    except SkillConflictError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except SkillParseError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return SkillDetail(**skill_to_dict(skill, include_body=True))


@router.put("/{name}", response_model=SkillDetail)
async def update_skill_endpoint(
    name: str, body: SkillCreate, request: Request
) -> SkillDetail:
    if body.name != name:
        raise HTTPException(
            status_code=400, detail="Body 'name' must match the URL path 'name'."
        )
    try:
        skill = update_skill(
            name=name,
            description=body.description,
            when_to_use=body.when_to_use,
            category=body.category,
            body=body.body,
            store=_get_store(request),
        )
    except SkillNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except SkillParseError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return SkillDetail(**skill_to_dict(skill, include_body=True))


@router.delete("/{name}", response_model=SkillDeleteResponse)
async def delete_skill_endpoint(name: str, request: Request) -> SkillDeleteResponse:
    """Delete a company skill, revert a customization, or hide a built-in."""
    try:
        outcome = delete_skill(name, store=_get_store(request))
    except SkillNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except SkillParseError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return SkillDeleteResponse(name=name, outcome=outcome)


@router.post("/{name}/restore", response_model=SkillDetail)
async def restore_skill_endpoint(name: str, request: Request) -> SkillDetail:
    """Un-hide a built-in skill."""
    try:
        skill = restore_skill(name, store=_get_store(request))
    except SkillNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except SkillParseError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return SkillDetail(**skill_to_dict(skill, include_body=True))
