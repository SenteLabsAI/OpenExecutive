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

Lessons (``memory.lessons``), what the review learned from the misses, are
listed the same way and forgotten the same way; a forgotten lesson is
retired, not erased, so the screen can show it stopped.

Routes:
  GET    /memories/reflection               — ``{misses, lessons, weeks, replay, sees_everyone}``
  DELETE /memories/reflection/misses/{id}   — forget one miss (204)
  DELETE /memories/reflection/lessons/{id}  — forget one lesson (204)
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict

from openexecutive.api import caller as api_caller
from openexecutive.memory import chat_review, lessons, replay
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
    # How its replays went (memory.replay): "open" until one passes,
    # "closed" (it would go right today), "evidence" (can't be judged today).
    replay_state: str = "open"
    replayed_at: str | None = None
    closed_at: str | None = None
    replay_notes: str = ""


class ReplayOut(BaseModel):
    """"Of the N things that went wrong in the last 30 days, M would go
    right today" (memory.replay), over the misses this caller sees."""

    misses: int
    would_pass: int
    evidence: int
    last_run_at: str | None


class LessonOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    person_id: int
    person_name: str
    text: str
    quote: str
    session_id: str
    created_at: str
    retired_at: str | None
    retired_reason: str | None
    shown: int
    fine_after: int
    miss_after: int


class WeekOut(BaseModel):
    week: str
    graded: int
    misses: int


class ReflectionOut(BaseModel):
    misses: list[MissOut]
    lessons: list[LessonOut]
    weeks: list[WeekOut]
    replay: ReplayOut
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
            raise _refuse(403, "sign_in_required", "Sign in to see what it learned from your chats.")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("reflection: caller lookup failed")
        raise _refuse(503, "roster_unavailable", "Couldn't read the People list.") from exc
    if person is None or person.id is None:
        raise _refuse(403, "not_on_roster", "This is kept for people on the People list.")
    return person


class _Readable:
    """Which misses the caller may see: their own, or from a chat the chat
    routes would open for them (`chat._session_access`, checked once per
    chat). Fails closed when the check itself can't be made."""

    def __init__(self, request: Request, caller_person_id: int) -> None:
        self._request = request
        self._caller = caller_person_id
        self._by_session: dict[str, bool] = {}

    def __call__(self, miss: dict[str, Any]) -> bool:
        if int(miss["person_id"]) == self._caller:
            return True
        session_id = str(miss["session_id"])
        if session_id not in self._by_session:
            from openexecutive.api.routes.chat import _session_access

            try:
                allowed = _session_access(self._request, session_id, self._caller) == "allowed"
            except Exception:
                logger.exception("reflection: couldn't check who may read a chat")
                allowed = False
            self._by_session[session_id] = allowed
        return self._by_session[session_id]


_PAGE = 200
_CHUNK = 200


def _visible(
    read: Callable[..., list[dict[str, Any]]], person_id: int | None, readable: _Readable
) -> list[dict[str, Any]]:
    """The principal's page of misses or lessons: read in chunks, newest
    first, until the page is full or the record runs out, so hidden rows
    never crowd it out."""
    out: list[dict[str, Any]] = []
    offset = 0
    while len(out) < _PAGE:
        chunk = read(person_id=person_id, limit=_CHUNK, offset=offset)
        out.extend(m for m in chunk if readable(m))
        if len(chunk) < _CHUNK:
            break
        offset += _CHUNK
    return out[:_PAGE]


@router.get("/memories/reflection", response_model=ReflectionOut)
def get_reflection(
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
    if person.is_principal:
        readable = _Readable(request, person.id)
        misses = _visible(chat_review.list_misses, scope, readable)
        learned = _visible(lessons.list_lessons, scope, readable)
    else:
        misses = chat_review.list_misses(person_id=scope, limit=_PAGE)
        learned = lessons.list_lessons(person_id=scope, limit=_PAGE)
    weeks = chat_review.weekly_rates(person_id=scope)
    return ReflectionOut(
        misses=[MissOut(**m) for m in misses],
        lessons=[LessonOut(**lesson, person_name=_name(lesson["person_id"])) for lesson in learned],
        replay=ReplayOut(**replay.summary_for(misses)),
        weeks=[WeekOut(**w) for w in weeks],
        sees_everyone=bool(person.is_principal),
    )


def _name(person_id: int) -> str:
    from openexecutive.people.store import get_person

    try:
        person = get_person(int(person_id))
    except Exception:
        return ""
    return person.full_name if person is not None else ""


@router.delete("/memories/reflection/lessons/{lesson_id}", status_code=status.HTTP_204_NO_CONTENT)
def forget_lesson(lesson_id: int, request: Request) -> Response:
    """Retire one lesson: its own person or the principal, as for a miss."""
    person = _caller(request)
    assert person.id is not None
    lesson = lessons.get_lesson(lesson_id)
    if lesson is None or not _Readable(request, person.id)(lesson):
        raise _refuse(404, "not_found", "No such lesson, or it isn't yours to forget.")
    if not lessons.forget_lesson(lesson_id, caller_person_id=person.id):
        raise _refuse(404, "not_found", "No such lesson, or it isn't yours to forget.")
    from openexecutive.audit import log_event

    log_event(
        "chat_review",
        f"Forgot lesson {lesson_id} from the Reflection record",
        actor="user",
        details={"op": "forget_lesson", "lesson_id": lesson_id, "person_id": person.id},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/memories/reflection/misses/{miss_id}", status_code=status.HTTP_204_NO_CONTENT)
def forget_miss(miss_id: int, request: Request) -> Response:
    """Drop one miss from the record: its own person or the principal."""
    person = _caller(request)
    assert person.id is not None
    miss = chat_review.get_miss(miss_id)
    # The same visibility rule as the list: a miss the caller can't see
    # doesn't exist for them, so the answer gives nothing away.
    if miss is None or not _Readable(request, person.id)(miss):
        raise _refuse(404, "not_found", "No such miss, or it isn't yours to forget.")
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
