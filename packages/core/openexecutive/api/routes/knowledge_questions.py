"""Questions it could not answer (``memory.knowledge_gaps``), for the Knowledge page.

The conversation review notes each question about the company that no
document covered. The principal sees the open ones at the top of Company
documents, with three ways to take one off the list: add a document for it,
tell the answer (kept as a standing fact, as on the Memory page), or dismiss
it. Everyone else gets an empty list: the documents are the principal's to
manage.

Which lines the principal sees: those with at least one ask from a chat they
may open, the Reflection screen's rule (``api.routes.reflection._Readable``),
so a teammate's own chat with the Executive stays theirs.

Routes:
  GET  /knowledge/questions                 — ``{questions, count}``
  POST /knowledge/questions/{id}/answer     — ``{answer}``: keep it, close the line
  POST /knowledge/questions/{id}/document   — a document was added for it
  POST /knowledge/questions/{id}/dismiss    — take it off the list
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from openexecutive.memory import knowledge_gaps

router = APIRouter(prefix="/knowledge/questions")
logger = logging.getLogger(__name__)

ANSWER_MAX = 1000


class QuestionOut(BaseModel):
    id: int
    question: str
    # "You", or the name of the person who asked it last.
    asked_by: str
    # Where it was asked last: "chat", or the chat app's name.
    place: str
    last_asked_at: str
    times: int


class QuestionsOut(BaseModel):
    questions: list[QuestionOut]
    count: int


class AnswerIn(BaseModel):
    answer: str = Field(min_length=1, max_length=ANSWER_MAX)


def _principal(request: Request) -> Any | None:
    """The caller's People row when they are the principal, else None."""
    from openexecutive.api.routes.reflection import _caller

    try:
        person = _caller(request)
    except HTTPException:
        return None
    return person if person.is_principal and person.id is not None else None


def _visible(request: Request, person_id: int) -> list[dict[str, Any]]:
    from openexecutive.api.routes.reflection import _Readable

    readable = _Readable(request, person_id)
    out: list[dict[str, Any]] = []
    for line in knowledge_gaps.list_open():
        if person_id in line["askers"] or any(
            readable({"person_id": -1, "session_id": s}) for s in line["sessions"]
        ):
            out.append(line)
    return out


def _name(person_id: int | None, principal_id: int) -> str:
    if person_id is None:
        return "Someone"
    if person_id == principal_id:
        return "You"
    from openexecutive.api.routes.reflection import _name as person_name

    return person_name(person_id) or "Someone"


@router.get("", response_model=QuestionsOut)
def list_questions(request: Request) -> QuestionsOut:
    principal = _principal(request)
    if principal is None:
        return QuestionsOut(questions=[], count=0)
    lines = _visible(request, int(principal.id))
    questions = [
        QuestionOut(
            id=line["id"], question=line["question"],
            asked_by=_name(line["person_id"], int(principal.id)),
            place=line["place"], last_asked_at=line["last_at"], times=line["times"],
        )
        for line in lines
    ]
    return QuestionsOut(questions=questions, count=len(questions))


def _open_line(request: Request, line_id: int) -> tuple[Any, dict[str, Any]]:
    principal = _principal(request)
    if principal is None:
        raise HTTPException(status_code=403, detail="Only the owner can manage these questions")
    line = next((x for x in _visible(request, int(principal.id)) if x["id"] == line_id), None)
    if line is None:
        raise HTTPException(status_code=404, detail="That question is no longer on the list")
    return principal, line


def _audit(summary: str, details: dict[str, Any]) -> None:
    try:
        from openexecutive.audit import log_event

        log_event("knowledge_question", summary, actor="principal", details=details)
    except Exception:
        logger.warning("knowledge_questions: couldn't write the audit row", exc_info=True)


@router.post("/{line_id}/answer", response_model=QuestionsOut)
def answer_question(line_id: int, body: AnswerIn, request: Request) -> QuestionsOut:
    """Keep the owner's answer as a standing fact on the question, then take
    the line off the list."""
    from openexecutive.memory.facts import record_fact

    _, line = _open_line(request, line_id)
    answer = " ".join(body.answer.split())
    if not answer:
        raise HTTPException(status_code=422, detail="Say what the answer is")
    try:
        fact, _ = record_fact(
            subject=line["question"], statement=answer, source_quote=answer,
            source_channel="web", recorded_by_role="principal",
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    knowledge_gaps.close(line_id, "taught")
    _audit(f"Answered a question it couldn't answer: {line['question'][:80]}",
           {"op": "answer", "line_id": line_id, "fact_id": fact.id})
    return list_questions(request)


@router.post("/{line_id}/document", response_model=QuestionsOut)
def document_added(line_id: int, request: Request) -> QuestionsOut:
    """A document was added for the question: take the line off the list."""
    _, line = _open_line(request, line_id)
    knowledge_gaps.close(line_id, "document")
    _audit(f"Added a document for a question: {line['question'][:80]}",
           {"op": "document", "line_id": line_id})
    return list_questions(request)


@router.post("/{line_id}/dismiss", response_model=QuestionsOut)
def dismiss_question(line_id: int, request: Request) -> QuestionsOut:
    """Take the line off the list; it doesn't come back for whoever asked it."""
    _, line = _open_line(request, line_id)
    knowledge_gaps.close(line_id, "dismissed")
    _audit(f"Dismissed a question: {line['question'][:80]}", {"op": "dismiss", "line_id": line_id})
    return list_questions(request)
