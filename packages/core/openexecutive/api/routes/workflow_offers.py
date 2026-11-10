"""Offers to do a repeated ask on a schedule (``workflows.repeat_asks``).

- ``GET /workflow-offers``: the caller's own open offers, newest first.
- ``POST /workflow-offers/{id}/accept``: Set it up. Retires the offer; the
  page then opens the build-by-talking flow, where nothing is created until
  the person approves the steps.
- ``POST /workflow-offers/{id}/dismiss``: Not now. The same offer is held
  back for ``SNOOZE_DAYS``.

An offer is only ever the caller's own: a caller with no People entry sees
none and can answer none.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Path, Request

from openexecutive.api import caller as api_caller
from openexecutive.workflows import repeat_asks

router = APIRouter()
# SQLite's integer range: a larger id is a 422, not an overflow.
_OFFER_ID = Path(ge=1, le=2**63 - 1)


def _person(request: Request) -> int | None:
    return api_caller.person_id_for(api_caller.caller(request))


@router.get("/workflow-offers")
def list_offers(request: Request) -> dict[str, Any]:
    person_id = _person(request)
    if person_id is None:
        return {"offers": []}
    return {"offers": repeat_asks.open_offers(person_id)}


def _answer(request: Request, offer_id: int, *, accept: bool) -> dict[str, Any]:
    person_id = _person(request)
    if person_id is None or not repeat_asks.answer_offer(offer_id, person_id, accept=accept):
        raise HTTPException(status_code=404, detail="That offer is gone.")
    return {"ok": True}


@router.post("/workflow-offers/{offer_id}/accept")
def accept_offer(request: Request, offer_id: int = _OFFER_ID) -> dict[str, Any]:
    return _answer(request, offer_id, accept=True)


@router.post("/workflow-offers/{offer_id}/dismiss")
def dismiss_offer(request: Request, offer_id: int = _OFFER_ID) -> dict[str, Any]:
    return _answer(request, offer_id, accept=False)
