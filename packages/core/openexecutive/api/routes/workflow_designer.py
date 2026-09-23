"""Conversational workflow design (``/workflows/designer/*``).

The ``/jobs/new`` wizard: the user describes a job, the designer asks
clarifying questions, and the result is a DRAFT ``DynamicWorkflowDef`` the
user reviews. Nothing here writes — the UI saves the reviewed draft through
``POST /workflows/custom``, which validates it again and owns persistence and
cadence scheduling. The step-by-step builder stays reachable as the advanced
editor.

Mounted BEFORE ``workflows.router`` in ``api/main.py`` so no
``/workflows/{name}/...`` pattern can shadow these literal paths (the same
rule ``workflows.py`` follows for ``/workflows/custom`` and ``/workflows/runs``).

Every 4xx/5xx detail below is a fixed string and every log line carries an
exception TYPE, never user or model text.
"""
from __future__ import annotations

import logging
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field

from fastapi import APIRouter, HTTPException

from openexecutive.api.models import (
    WORKFLOW_DESIGNER_MESSAGE_MAX_CHARS,
    WorkflowDesignerDraftResponse,
    WorkflowDesignerMessageRequest,
    WorkflowDesignerSessionRequest,
    WorkflowDesignerStartRequest,
    WorkflowDesignerTranscriptTurn,
    WorkflowDesignerTurnResponse,
)
from openexecutive.onboarding.interview import Turn, transcript_chars
from openexecutive.workflows.designer import (
    MAX_QUESTIONS,
    MAX_TRANSCRIPT_CHARS,
    WorkflowDesignerError,
    WorkflowDesignerTimeout,
    WorkflowDraft,
    advance,
    build_context_block,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@dataclass
class DesignerSession:
    transcript: list[Turn] = field(default_factory=list)
    # Rendered once at start so the replayed first turn is identical on every
    # call of the conversation.
    context_block: str = ""
    questions_asked: int = 0
    # The most recent draft, kept after a follow-up question so the next draft
    # refines it instead of starting over. ``phase`` says whether it is the
    # thing on screen right now.
    draft: WorkflowDraft | None = None
    phase: str = "question"
    last_hint: str = ""
    last_options: list[str] = field(default_factory=list)
    last_touched: float = field(default_factory=time.monotonic)


# In-memory, single-process, not persisted — the same trade-off as the
# onboarding interview sessions: the draft worth keeping is already in the
# client's hands, and GET /workflows/designer/{id} covers a page refresh and
# the hand-off to the advanced editor.
_designer_sessions: OrderedDict[str, DesignerSession] = OrderedDict()
_DESIGNER_TTL_SECONDS = 2 * 3600
_MAX_DESIGNER_SESSIONS = 50


def _sweep_designer_sessions() -> None:
    now = time.monotonic()
    for sid in [
        s
        for s, sess in _designer_sessions.items()
        if now - sess.last_touched > _DESIGNER_TTL_SECONDS
    ]:
        _designer_sessions.pop(sid, None)
    while len(_designer_sessions) > _MAX_DESIGNER_SESSIONS:
        _designer_sessions.popitem(last=False)


def _get_session(session_id: str) -> DesignerSession:
    _sweep_designer_sessions()
    session = _designer_sessions.get(session_id)
    if session is None:
        raise HTTPException(
            status_code=404, detail="Workflow design session not found or expired."
        )
    session.last_touched = time.monotonic()
    _designer_sessions.move_to_end(session_id)
    return session


def _check_message(message: str, session: DesignerSession | None) -> str:
    """Bound and normalize an incoming user message (fixed-string 422s)."""
    if len(message) > WORKFLOW_DESIGNER_MESSAGE_MAX_CHARS:
        raise HTTPException(
            status_code=422,
            detail=(
                "Message is too long "
                f"(limit {WORKFLOW_DESIGNER_MESSAGE_MAX_CHARS:,} characters)."
            ),
        )
    text = message.strip()
    if not text:
        raise HTTPException(status_code=422, detail="Message is empty.")
    if (
        session is not None
        and transcript_chars(session.transcript) + len(text) > MAX_TRANSCRIPT_CHARS
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                "This conversation has gotten long. Draft the workflow now "
                "and edit it in the advanced editor."
            ),
        )
    return text


def _turn_response(session_id: str, session: DesignerSession) -> WorkflowDesignerTurnResponse:
    transcript = [
        WorkflowDesignerTranscriptTurn(role=t.role, text=t.text) for t in session.transcript
    ]
    if session.phase == "draft" and session.draft is not None:
        d = session.draft
        return WorkflowDesignerTurnResponse(
            session_id=session_id,
            phase="draft",
            questions_asked=session.questions_asked,
            max_questions=MAX_QUESTIONS,
            draft=WorkflowDesignerDraftResponse(
                definition=d.definition.model_dump(mode="json"),
                summary=d.summary,
                assumptions=list(d.assumptions),
            ),
            transcript=transcript,
        )
    question = next(
        (t.text for t in reversed(session.transcript) if t.role == "assistant"), None
    )
    return WorkflowDesignerTurnResponse(
        session_id=session_id,
        phase="question",
        questions_asked=session.questions_asked,
        max_questions=MAX_QUESTIONS,
        question=question,
        hint=session.last_hint or None,
        options=list(session.last_options),
        transcript=transcript,
    )


async def _advance(
    session_id: str, session: DesignerSession, *, force_draft: bool = False
) -> WorkflowDesignerTurnResponse:
    """Run one designer turn and fold the result into the session."""
    try:
        result = await advance(
            session.transcript,
            context_block=session.context_block,
            previous_draft=session.draft.definition if session.draft else None,
            force_draft=force_draft,
            questions_asked=session.questions_asked,
        )
    except WorkflowDesignerTimeout as exc:
        logger.warning("workflow designer: timed out (%s)", type(exc).__name__)
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except WorkflowDesignerError as exc:
        # Messages are fixed, input-free strings by contract.
        logger.error("workflow designer: failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if isinstance(result, WorkflowDraft):
        session.draft = result
        session.phase = "draft"
        session.last_hint = ""
        session.last_options = []
        # Record the draft as an assistant turn so the transcript never ends
        # on a user turn (two in a row would be coalesced, but the model would
        # lose the fact that it already drafted).
        session.transcript.append(
            Turn(
                role="assistant",
                text=result.summary.strip()
                or f"(drafted the workflow “{result.definition.title}”)",
            )
        )
    else:
        session.phase = "question"
        session.transcript.append(Turn(role="assistant", text=result.question))
        session.questions_asked += 1
        session.last_hint = result.hint
        session.last_options = list(result.options)
    return _turn_response(session_id, session)


@router.post("/workflows/designer/start", response_model=WorkflowDesignerTurnResponse)
async def start_designer(body: WorkflowDesignerStartRequest) -> WorkflowDesignerTurnResponse:
    """Open a design session from the user's description of the workflow."""
    text = _check_message(body.message, None)

    session_id = uuid.uuid4().hex
    session = DesignerSession(
        transcript=[Turn(role="user", text=text)],
        context_block=build_context_block(),
    )
    _designer_sessions[session_id] = session
    # Sweep AFTER inserting so the cap holds on what is stored.
    _sweep_designer_sessions()
    try:
        return await _advance(session_id, session)
    except HTTPException:
        # The error body carries no session_id, so the client cannot resume
        # this one — drop it rather than let failures evict live sessions.
        _designer_sessions.pop(session_id, None)
        raise


@router.post("/workflows/designer/message", response_model=WorkflowDesignerTurnResponse)
async def designer_message(body: WorkflowDesignerMessageRequest) -> WorkflowDesignerTurnResponse:
    session = _get_session(body.session_id)
    text = _check_message(body.message, session)
    session.transcript.append(Turn(role="user", text=text))
    try:
        return await _advance(body.session_id, session)
    except HTTPException:
        # Roll the unanswered turn back so a retry does not send it twice and
        # the transcript the client re-renders matches what the model saw.
        session.transcript.pop()
        raise


@router.post("/workflows/designer/draft", response_model=WorkflowDesignerTurnResponse)
async def designer_draft(body: WorkflowDesignerSessionRequest) -> WorkflowDesignerTurnResponse:
    """Draft now, however much is still unclear."""
    session = _get_session(body.session_id)
    return await _advance(body.session_id, session, force_draft=True)


@router.get("/workflows/designer/{session_id}", response_model=WorkflowDesignerTurnResponse)
async def get_designer(session_id: str) -> WorkflowDesignerTurnResponse:
    """Resume after a refresh, or hand the draft to the advanced editor."""
    return _turn_response(session_id, _get_session(session_id))
