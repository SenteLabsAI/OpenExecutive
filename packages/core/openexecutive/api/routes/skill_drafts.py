"""Review queue for playbook changes the Executive proposed from chat.

See knowledge/skill_drafts.py: chat's create/update/delete_skill only save a
draft; a person approves or discards it here (the Playbooks tab).
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response

from openexecutive.api.models import (
    SkillDraftApproval,
    SkillDraftListResponse,
    SkillDraftOut,
)
from openexecutive.api.routes.skills import _detail, _get_store
from openexecutive.knowledge.skill_drafts import (
    SkillDraft,
    SkillDraftNotFoundError,
    approve_draft,
    discard_draft,
    get_draft,
    list_drafts,
)
from openexecutive.knowledge.skills import SkillParseError
from openexecutive.knowledge.skills_repo import (
    SkillConflictError,
    SkillNotFoundError,
    get_skill,
)

router = APIRouter(prefix="/skill-drafts")


def _out(draft: SkillDraft) -> SkillDraftOut:
    current = None
    if draft.action != "create":
        try:
            current = _detail(get_skill(draft.name, include_hidden=True))
        except (SkillNotFoundError, SkillParseError):
            current = None
    return SkillDraftOut(**draft.model_dump(), current=current)


@router.get("", response_model=SkillDraftListResponse)
async def list_skill_drafts() -> SkillDraftListResponse:
    return SkillDraftListResponse(drafts=[_out(d) for d in list_drafts()])


@router.get("/{name}", response_model=SkillDraftOut)
async def get_skill_draft(name: str) -> SkillDraftOut:
    try:
        return _out(get_draft(name))
    except SkillDraftNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except SkillParseError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/{name}/approve", response_model=SkillDraftApproval)
async def approve_skill_draft(name: str, request: Request) -> SkillDraftApproval:
    """Apply the draft. On a conflict or a vanished playbook the draft is kept."""
    try:
        result = approve_draft(name, store=_get_store(request))
    except SkillDraftNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except SkillNotFoundError as e:
        raise HTTPException(status_code=409, detail=f"{e} — discard or edit the draft") from e
    except SkillConflictError as e:
        raise HTTPException(status_code=409, detail=f"{e} — discard or edit the draft") from e
    except SkillParseError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    skill = result.get("skill")
    return SkillDraftApproval(
        action=result["action"],
        name=name,
        outcome=result.get("outcome"),
        skill=_detail(skill) if skill is not None else None,
    )


@router.delete("/{name}", status_code=204)
async def discard_skill_draft(name: str) -> Response:
    try:
        discard_draft(name)
    except SkillDraftNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except SkillParseError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return Response(status_code=204)
