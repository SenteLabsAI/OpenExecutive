"""Questions it could not answer (``memory.knowledge_gaps``), for the Knowledge page.

The conversation review notes each question about the company that no
document covered. The principal sees the open ones at the top of Company
documents, with three ways to take one off the list: add a document for it,
tell the answer (kept as a standing fact, as on the Memory page), or dismiss
it. Everyone else gets an empty list: the documents are the principal's to
manage.

Which lines the principal sees, and what of them: each asking is kept on its
own, and a line shows only through the asks from chats they may open, the
Reflection screen's rule (``api.routes.reflection._Readable``): its wording
(the first such ask), who asked last, where, when and how many times. So a
teammate's own chat with the Executive stays theirs even when its question
matches one the principal can see.

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

# A standing fact's statement holds this much (memory.facts.STATEMENT_MAX):
# a longer answer is refused rather than cut.
ANSWER_MAX = 500


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


def _name(person_id: int, principal_id: int) -> str:
    if person_id == principal_id:
        return "You"
    from openexecutive.api.routes.reflection import _name as person_name

    return person_name(person_id) or "Someone"


def _shown(request: Request, principal_id: int) -> list[tuple[list[int], QuestionOut]]:
    """The rows the principal sees, each with the lines it stands for.

    A line is one person's asks of one question (``memory.knowledge_gaps``).
    Of each, only the asks in chats the principal may open count (their own,
    or one ``_Readable`` lets them read); a line with none isn't shown. Lines
    whose readable wording asks the same question then show as one row:
    the first wording, who asked last, where, when, and how many times.
    Nothing a hidden ask said or when it came decides a row."""
    from openexecutive.api.routes.reflection import _Readable

    readable = _Readable(request, principal_id)
    groups: list[tuple[list[int], list[dict[str, Any]]]] = []
    for line in knowledge_gaps.list_open():
        asks = [
            a for a in line["asks"]
            if a["person_id"] == principal_id
            or readable({"person_id": -1, "session_id": a["session_id"]})
        ]
        if not asks:
            continue
        group = next(
            (g for g in groups if knowledge_gaps.same_question(g[1][0]["question"], asks[0]["question"])),
            None,
        )
        if group is None:
            groups.append(([int(line["id"])], list(asks)))
        else:
            group[0].append(int(line["id"]))
            group[1].extend(asks)
    out: list[tuple[list[int], QuestionOut]] = []
    for ids, asks in groups:
        asks.sort(key=lambda a: a["asked_at"])
        first, last = asks[0], asks[-1]
        out.append((ids, QuestionOut(
            id=min(ids), question=first["question"],
            asked_by=_name(last["person_id"], principal_id), place=last["place"],
            last_asked_at=last["asked_at"], times=len(asks),
        )))
    out.sort(key=lambda pair: pair[1].last_asked_at, reverse=True)
    return out


@router.get("", response_model=QuestionsOut)
def list_questions(request: Request) -> QuestionsOut:
    principal = _principal(request)
    if principal is None:
        return QuestionsOut(questions=[], count=0)
    questions = [shown for _, shown in _shown(request, int(principal.id))]
    return QuestionsOut(questions=questions, count=len(questions))


def _open_line(request: Request, row_id: int) -> tuple[list[int], QuestionOut]:
    """The row holding this line (any of its ids), with its lines; 403 for anyone else,
    404 when it isn't on their list."""
    principal = _principal(request)
    if principal is None:
        raise HTTPException(status_code=403, detail="Only the owner can manage these questions")
    found = next((pair for pair in _shown(request, int(principal.id)) if row_id in pair[0]), None)
    if found is None:
        raise HTTPException(status_code=404, detail="That question is no longer on the list")
    return found


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

    ids, line = _open_line(request, line_id)
    answer = " ".join(body.answer.split())
    if not answer:
        raise HTTPException(status_code=422, detail="Say what the answer is")
    try:
        fact, _ = record_fact(
            subject=line.question, statement=answer, source_quote=answer,
            source_channel="web", recorded_by_role="principal",
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    for one in ids:
        knowledge_gaps.close(one, "taught")
    _audit(f"Answered a question it couldn't answer: {line.question[:80]}",
           {"op": "answer", "line_id": line_id, "fact_id": fact.id})
    return list_questions(request)


@router.post("/{line_id}/document", response_model=QuestionsOut)
def document_added(line_id: int, request: Request) -> QuestionsOut:
    """A document was added for the question: take the line off the list."""
    ids, line = _open_line(request, line_id)
    for one in ids:
        knowledge_gaps.close(one, "document")
    _audit(f"Added a document for a question: {line.question[:80]}",
           {"op": "document", "line_id": line_id})
    return list_questions(request)


@router.post("/{line_id}/dismiss", response_model=QuestionsOut)
def dismiss_question(line_id: int, request: Request) -> QuestionsOut:
    """Take the line off the list; it doesn't come back for whoever asked it."""
    ids, line = _open_line(request, line_id)
    for one in ids:
        knowledge_gaps.close(one, "dismissed")
    _audit(f"Dismissed a question: {line.question[:80]}", {"op": "dismiss", "line_id": line_id})
    return list_questions(request)
