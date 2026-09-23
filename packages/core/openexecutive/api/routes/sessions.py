from __future__ import annotations

import time
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from openexecutive.api.models import SessionSummary
from openexecutive.api.routes import chat as chat_route
from openexecutive.api.routes.chat import _resolve_caller_person_id
from openexecutive.memory.session_store import (
    delete_session,
    get_session_metadata,
    get_session_owner,
    list_sessions,
    load_messages,
    set_message_feedback,
)
from openexecutive.people.store import is_principal_or_self

router = APIRouter()


@router.get("/sessions", response_model=list[SessionSummary])
def get_sessions(request: Request) -> list[SessionSummary]:
    caller_person_id = _resolve_caller_person_id(request)
    if caller_person_id is None:
        # Either a signed-in user whose email isn't in the roster, or no
        # principal is configured yet (fresh install). Either way they
        # have no chats to see — return empty rather than leaking the
        # legacy NULL-owner rows.
        return []
    return [SessionSummary(**s) for s in list_sessions(caller_person_id)]


@router.get("/sessions/{session_id}", response_model=SessionSummary)
def get_session(session_id: str) -> SessionSummary:
    meta = get_session_metadata(session_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return SessionSummary(**meta)


@router.get("/sessions/{session_id}/messages")
def get_session_messages(session_id: str) -> list[dict]:
    meta = get_session_metadata(session_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return load_messages(session_id)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session_route(session_id: str) -> Response:
    if not delete_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class MessageFeedback(BaseModel):
    """👍/👎 on one assistant reply; ``null`` clears it."""

    feedback: Literal["up", "down"] | None
    note: str | None = Field(default=None, max_length=500)


@router.post(
    "/sessions/{session_id}/messages/{message_id}/feedback",
    status_code=status.HTTP_204_NO_CONTENT,
)
def post_message_feedback(
    session_id: str, message_id: int, body: MessageFeedback, request: Request
) -> Response:
    """Record explicit feedback on an assistant reply.

    Only the session's own caller, or the principal, may rate it: feedback
    feeds per-person learning, so a rating left on someone else's session
    would be read as that person's reaction."""
    exists, owner = get_session_owner(session_id)
    if not exists:
        raise HTTPException(status_code=404, detail="Session not found")
    caller = _resolve_caller_person_id(request)
    if not is_principal_or_self(caller, owner):
        raise HTTPException(status_code=403, detail="Not your session")
    if not set_message_feedback(
        session_id, message_id, body.feedback, body.note, by_person_id=caller
    ):
        raise HTTPException(status_code=404, detail="Message not found")

    from openexecutive.audit import log_event

    log_event(
        "attunement",
        f"feedback={body.feedback or 'cleared'} message_id={message_id}",
        session_id=session_id,
        actor="user",
        details={
            "op": "feedback",
            "message_id": message_id,
            "feedback": body.feedback,
            "person_id": caller,
            "has_note": bool(body.note),
        },
    )
    if body.feedback == "down":
        # A thumbs-down is the clearest style signal there is: re-learn the
        # speaker's working style now rather than after the next N messages
        # (still paced and budgeted inside) — but only when they rated a reply
        # to their own message; anyone else's rating is not evidence about them.
        from openexecutive.attunement.style import rated_reply_speaker, schedule_style_pass

        if caller is not None and rated_reply_speaker(session_id, message_id) == caller:
            schedule_style_pass(caller, force=True, session_id=session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# (session_id, last assistant message id) -> (monotonic ts, suggestion).
# Keyed on the reply being followed up on, so reopening a session or
# remounting the composer does not re-bill the fast model; a new reply is a
# new key. Single-process, like `_suggested_prompts_cache`.
_FOLLOWUP_TTL_S = 3600
_FOLLOWUP_CACHE_MAX = 512
_followup_cache: dict[tuple[str, int], tuple[float, str]] = {}


@router.get("/sessions/{session_id}/followup")
async def get_followup_suggestion(session_id: str, request: Request) -> dict[str, Any]:
    """One suggested next message for the chat composer.

    Grounded in the tail of the conversation. ``suggestion`` is null when the
    conversation does not end on a reply, or when the fast model fails — the
    composer then falls back to its static placeholder. Only the session's
    own caller, or the principal, may ask: the suggestion paraphrases the
    conversation.
    """
    exists, owner = get_session_owner(session_id)
    if not exists:
        raise HTTPException(status_code=404, detail="Session not found")
    if not is_principal_or_self(_resolve_caller_person_id(request), owner):
        raise HTTPException(status_code=403, detail="Not your session")

    messages = load_messages(session_id)
    last = messages[-1] if messages else None
    if last is None or last.get("role") != "assistant" or not last.get("id"):
        return {"suggestion": None}

    key = (session_id, int(last["id"]))
    now = time.monotonic()
    hit = _followup_cache.get(key)
    if hit is not None and (now - hit[0]) < _FOLLOWUP_TTL_S:
        return {"suggestion": hit[1]}

    transcript = chat_route._build_followup_transcript(messages)
    suggestion = await chat_route._generate_followup_via_llm(transcript)
    if suggestion is not None:
        # Failures are not cached: a transient blip should not pin the
        # static placeholder on this reply for an hour.
        if len(_followup_cache) >= _FOLLOWUP_CACHE_MAX:
            _followup_cache.clear()
        _followup_cache[key] = (now, suggestion)
    return {"suggestion": suggestion}
