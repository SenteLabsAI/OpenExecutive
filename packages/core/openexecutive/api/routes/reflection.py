"""Reflection: what the Executive got wrong in a person's chats (``memory.chat_review``).

The conversation review grades every turn it reads once a chat goes quiet.
The turns it graded a miss, with what the person wanted instead and the
signals that showed it, are what this serves, with each person's weekly
count of graded turns and misses for the trend.

Who sees what: each person on the roster sees their own misses. The
principal also sees the misses from chats they may open (``?person_id=``
narrows to one person): the same rule as the chat routes
(``chat._session_access``), so a teammate's own chat with the Executive
(the web app, a DM) and a conversation that read its owner's own mail stay
their owner's alone, while a channel or group thread is open to the
principal. The weekly counts carry no words and are shown for everyone.
The caller is resolved as for Always in the loop (``api.routes.history``):
a signed-in person on the roster, or the principal under local login;
anyone else gets 403. Forgetting a miss follows ``is_principal_or_self``.

Routes:
  GET    /memories/reflection              — ``{misses, weeks, sees_everyone}``
  DELETE /memories/reflection/misses/{id}  — forget one miss (204)
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict

from openexecutive.api import caller as api_caller
from openexecutive.memory import chat_review
from openexecutive.people.models import Person

router = APIRouter()
logger = logging.getLogger(__name__)


class MissOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    session_id: str
    message_id: int | None
    person_id: int
    person_name: str
    created_at: str
    message: str
    reply: str
    tools: list[str]
    wanted: str
    kind: str
    signals: list[str]


class WeekOut(BaseModel):
    week: str
    graded: int
    misses: int


class ReflectionOut(BaseModel):
    misses: list[MissOut]
    weeks: list[WeekOut]
    sees_everyone: bool


def _refuse(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _caller(request: Request) -> Person:
    """The caller on the roster; else 403."""
    from openexecutive.delegation.settings import local_login
    from openexecutive.people.store import find_person_by_email, find_principal_person

    who = api_caller.caller(request)
    try:
        if who.email:
            person = find_person_by_email(who.email)
        elif who.defaults_to_principal and local_login():
            person = find_principal_person()
        else:
            raise _refuse(403, "sign_in_required", "Sign in to see what it got wrong in your chats.")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("reflection: caller lookup failed")
        raise _refuse(503, "roster_unavailable", "Couldn't read the People list.") from exc
    if person is None or person.id is None:
        raise _refuse(403, "not_on_roster", "This is kept for people on the People list.")
    return person


def _may_read(request: Request, miss: dict[str, Any], caller_person_id: int) -> bool:
    """A miss is the caller's own, or from a chat the chat routes would open
    for them. Fails closed when the check itself can't be made."""
    from openexecutive.api.routes.chat import _session_access

    if int(miss["person_id"]) == caller_person_id:
        return True
    try:
        return _session_access(request, str(miss["session_id"]), caller_person_id) == "allowed"
    except Exception:
        logger.exception("reflection: couldn't check who may read a chat")
        return False


@router.get("/memories/reflection", response_model=ReflectionOut)
async def get_reflection(
    request: Request, person_id: int | None = Query(default=None)
) -> ReflectionOut:
    """The caller's misses (the principal's: from every chat they may open,
    or one person's with ``person_id``), newest first, and the weekly counts
    behind the trend."""
    person = _caller(request)
    assert person.id is not None
    if person.is_principal:
        scope: int | None = int(person_id) if person_id is not None else None
    else:
        if person_id is not None and int(person_id) != person.id:
            raise _refuse(403, "not_yours", "You can see your own chats only.")
        scope = person.id
    misses: list[dict[str, Any]] = chat_review.list_misses(person_id=scope)
    if person.is_principal:
        misses = [m for m in misses if _may_read(request, m, person.id)]
    weeks = chat_review.weekly_rates(person_id=scope)
    return ReflectionOut(
        misses=[MissOut(**m) for m in misses],
        weeks=[WeekOut(**w) for w in weeks],
        sees_everyone=bool(person.is_principal),
    )


@router.delete("/memories/reflection/misses/{miss_id}", status_code=status.HTTP_204_NO_CONTENT)
async def forget_miss(miss_id: int, request: Request) -> Response:
    """Drop one miss from the record: its own person or the principal."""
    person = _caller(request)
    if not chat_review.forget_miss(miss_id, caller_person_id=person.id):
        raise _refuse(404, "not_found", "No such miss, or it isn't yours to forget.")
    from openexecutive.audit import log_event

    log_event(
        "chat_review",
        f"Forgot miss {miss_id} from the Reflection record",
        actor="user",
        details={"op": "forget_miss", "miss_id": miss_id, "person_id": person.id},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
