"""What it worked out: the caller's own figures (``memory.worked_out``).

Each person sees and removes their own figures, and nobody else's (the
principal included). The caller is resolved as for the History notes
(``api.routes.history._caller``): a signed-in person on the roster, or the
principal under local login. Anyone else gets 403.

Routes:
  GET    /memories/worked-out        — the caller's figures, newest first
  PATCH  /memories/worked-out/{id}   — change one to their own wording
  DELETE /memories/worked-out/{id}   — remove one of them
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from openexecutive.api.routes.history import _audit, _caller, _refuse
from openexecutive.memory import worked_out

router = APIRouter()


class FigureOut(BaseModel):
    id: int
    subject: str
    figure: str
    source: str
    as_of: str | None
    private: bool
    created_at: str


class FigureEdit(BaseModel):
    subject: str | None = None
    figure: str | None = None
    source: str | None = None


class WorkedOutOut(BaseModel):
    figures: list[FigureOut]


@router.get("/memories/worked-out", response_model=WorkedOutOut)
async def list_worked_out(request: Request) -> WorkedOutOut:
    person = _caller(request)
    assert person.id is not None
    rows = worked_out.list_figures(person.id)
    return WorkedOutOut(figures=[
        FigureOut(**{k: r[k] for k in FigureOut.model_fields}) for r in rows
    ])


@router.delete("/memories/worked-out/{figure_id}")
async def forget_worked_out(request: Request, figure_id: int) -> dict[str, Any]:
    person = _caller(request)
    assert person.id is not None
    if not worked_out.forget_figure(figure_id, person_id=person.id):
        raise _refuse(404, "not_found", "That figure isn't there any more.")
    _audit(
        "worked_out_forgotten", f"Person {person.id} removed one of their figures",
        {"person_id": person.id, "figure_id": figure_id},
    )
    return {"forgotten": 1}


@router.patch("/memories/worked-out/{figure_id}", response_model=FigureOut)
async def edit_worked_out(request: Request, figure_id: int, body: FigureEdit) -> FigureOut:
    person = _caller(request)
    assert person.id is not None
    try:
        row = worked_out.edit_figure(figure_id, person_id=person.id, subject=body.subject,
                                     figure=body.figure, source=body.source)
    except ValueError as exc:
        raise _refuse(422, "not_allowed", str(exc)) from None
    if row is None:
        raise _refuse(404, "not_found", "That figure isn't there any more.")
    _audit(
        "worked_out_edited", f"Person {person.id} edited one of their figures",
        {"person_id": person.id, "figure_id": figure_id},
    )
    return FigureOut(**{k: row[k] for k in FigureOut.model_fields})
