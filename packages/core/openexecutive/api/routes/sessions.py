from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from openexecutive.api import chat_live
from openexecutive.api.models import SessionSummary
from openexecutive.api.routes import chat as chat_route
from openexecutive.api.routes.chat import (
    _SSE_KEEPALIVE,
    _acquire_session_turn,
    _is_channel_namespaced,
    _release_session_turn,
    _resolve_caller_person_id,
    _session_access,
    _shared_member,
    chat_people,
    forget_session,
)
from openexecutive.memory import chat_export
from openexecutive.memory.session_store import (
    delete_session,
    get_session_metadata,
    get_session_owner,
    last_turn_sender,
    leave_share,
    list_sessions,
    list_shared_with_person,
    load_messages,
    rename_session,
    rewind_last_turn,
    search_sessions,
    session_mail_private,
    set_message_feedback,
    set_pinned,
    set_shares,
    shared_with,
)

router = APIRouter()


_MAX_QUERY_CHARS = 200


@router.get("/sessions", response_model=list[SessionSummary], response_model_exclude_none=True)
def get_sessions(request: Request, q: str = "") -> list[SessionSummary]:
    """The caller's chats, newest first. With ``q``, only those whose title
    or messages contain it, each with a snippet of the newest match."""
    caller_person_id = _resolve_caller_person_id(request)
    if caller_person_id is None:
        # Either a signed-in user whose email isn't in the roster, or no
        # principal is configured yet (fresh install). Either way they
        # have no chats to see — return empty rather than leaking the
        # legacy NULL-owner rows.
        return []
    query = q.strip()[:_MAX_QUERY_CHARS]
    if query:
        return [SessionSummary(**s) for s in search_sessions(caller_person_id, query)]
    return [SessionSummary(**s) for s in list_sessions(caller_person_id)]


def _require_session_access(request: Request, session_id: str) -> int | None:
    """404 unless the caller may use this session; returns the caller's id.

    Session ids are guessable (`slack:dm:<user id>`, `telegram:<chat id>`), so
    every per-session route checks ownership rather than trusting the id. An
    unknown session and someone else's answer the same, so the routes can't be
    used to probe which chats exist (as `/chat/stop` does for turn ids)."""
    caller = _resolve_caller_person_id(request)
    if _session_access(request, session_id, caller) != "allowed":
        raise HTTPException(status_code=404, detail="Session not found")
    return caller


def _require_chat_access(request: Request, session_id: str) -> tuple[int | None, bool]:
    """404 unless the caller may read and write in this chat; returns their
    id and whether they are in it because its owner shared it with them
    (``chat._shared_member``). Anyone who may use it
    (``_require_session_access``) is no member."""
    caller = _resolve_caller_person_id(request)
    if _session_access(request, session_id, caller) == "allowed":
        return caller, False
    if _shared_member(session_id, caller):
        return caller, True
    raise HTTPException(status_code=404, detail="Session not found")


def _require_owner(request: Request, session_id: str) -> int:
    """404 unless the caller owns this chat (the principal owns an ownerless
    legacy one); returns their id. Pinning, naming, sharing and redoing a
    turn are the owner's, not the principal's on someone else's chat."""
    caller = _require_session_access(request, session_id)
    exists, owner = get_session_owner(session_id)
    if caller is None or not exists:
        raise HTTPException(status_code=404, detail="Session not found")
    if owner == caller:
        return caller
    if owner is None and session_id not in chat_route._session_starters:
        # An ownerless legacy chat is the principal's, but not one someone
        # the roster can't resolve is talking in right now.
        from openexecutive.people.store import get_person

        person = get_person(caller)
        if person is not None and person.is_principal and not person.archived:
            return caller
    raise HTTPException(status_code=404, detail="Session not found")


def _owner_rows(session_id: str, owner: int | None) -> contextlib.AbstractContextManager[None]:
    """Audit rows about a chat stay as private as the chat: a teammate's own,
    or one that read its owner's mail, is its owner's alone, as every row its
    turns wrote is (``chat._session_access``)."""
    from openexecutive.audit.context import rows_for_person

    if chat_route._teammates_own(owner) or session_mail_private(session_id):
        return rows_for_person(owner)
    return contextlib.nullcontext()


def _person_name(person_id: int | None) -> str | None:
    if person_id is None:
        return None
    from openexecutive.people.store import get_person

    person = get_person(int(person_id))
    return person.full_name if person is not None else None


@router.get(
    "/sessions/shared-with-me",
    response_model=list[SessionSummary],
    response_model_exclude_none=True,
)
def get_shared_sessions(request: Request) -> list[SessionSummary]:
    """Chats other people shared with the caller, newest first, each with who
    shared it (``shared_by``) and when (``shared_at``)."""
    caller = _resolve_caller_person_id(request)
    if caller is None:
        return []
    out: list[SessionSummary] = []
    for row in list_shared_with_person(caller):
        out.append(SessionSummary(
            session_id=row["session_id"],
            title=row["title"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            message_count=row["message_count"],
            shared_by=_person_name(row["owner_person_id"]) or "Someone on your team",
            shared_at=row["shared_at"],
            shared_with_you=True,
        ))
    return out


@router.get("/sessions/{session_id}", response_model=SessionSummary, response_model_exclude_none=True)
def get_session(session_id: str, request: Request) -> SessionSummary:
    _, member = _require_chat_access(request, session_id)
    meta = get_session_metadata(session_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Session not found")
    summary = SessionSummary(**meta)
    if member:
        # Only the owner pins, so a member never sees the owner's pin.
        _, owner = get_session_owner(session_id)
        summary.pinned_at = None
        summary.shared_with_you = True
        summary.shared_by = _person_name(owner) or "Someone on your team"
    return summary


@router.get("/sessions/{session_id}/messages")
def get_session_messages(session_id: str, request: Request) -> list[dict]:
    """The chat's messages. In a shared chat each person's message also says
    who wrote it (``sender_person_id``, ``sender_name``)."""
    _, member = _require_chat_access(request, session_id)
    meta = get_session_metadata(session_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Session not found")
    people = chat_people(session_id)
    names = {pid: name for pid, name, _ in people}
    owner_id = next((pid for pid, _, is_owner in people if is_owner), None)
    messages = load_messages(session_id, with_senders=bool(names))
    for msg in messages:
        sender = msg.get("sender_person_id")
        if sender is None and "sender_person_id" in msg and owner_id is not None:
            # Written before anyone recorded who sent it: the owner's.
            sender = msg["sender_person_id"] = owner_id
        if sender in names:
            msg["sender_name"] = names[sender]
        if member:
            # The owner's ratings are theirs.
            msg.pop("feedback", None)
    return messages


class ChatPerson(BaseModel):
    person_id: int
    full_name: str
    owner: bool


class ChatPeople(BaseModel):
    """Who is in a chat. ``people`` is empty for a chat nobody else is in."""

    you: int | None
    people: list[ChatPerson]


@router.get("/sessions/{session_id}/people", response_model=ChatPeople)
def get_session_people(session_id: str, request: Request) -> ChatPeople:
    """Everyone in the chat, its owner first, for anyone in it."""
    caller, _ = _require_chat_access(request, session_id)
    return ChatPeople(
        you=caller,
        people=[ChatPerson(person_id=pid, full_name=name, owner=owner) for pid, name, owner in chat_people(session_id)],
    )


@router.post("/sessions/{session_id}/typing", status_code=status.HTTP_204_NO_CONTENT)
def post_session_typing(session_id: str, request: Request) -> Response:
    """Tell the others in a shared chat that the caller is writing. Sent at
    most every couple of seconds while they type; a chat of one has nobody to
    tell."""
    caller, _ = _require_chat_access(request, session_id)
    if caller is not None and chat_live.watchers(session_id) > 1:
        name = _person_name(caller)
        if name:
            chat_live.typing(session_id, caller, name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# How often an open chat's stream says it is still there, and checks the
# caller may still see it. Under the proxies' idle timeouts.
_LIVE_KEEPALIVE_S = 20.0


def _still_in(request: Request, session_id: str, caller: int | None, member: bool) -> bool:
    if member:
        return _shared_member(session_id, caller)
    return _session_access(request, session_id, caller) == "allowed"


@router.get("/sessions/{session_id}/live")
async def live_session(session_id: str, request: Request) -> StreamingResponse:
    """The chat as it happens, for a page that has it open: someone's
    message and the reply as it is written, who is typing, and who joined
    or left (``chat_live``). It says ``ready`` first, with the turn running
    now if any; the page then reads the saved chat. 429 when too many pages
    have it open."""
    caller, member = await asyncio.to_thread(_require_chat_access, request, session_id)
    if not chat_live.admits(session_id, caller):
        raise HTTPException(status_code=429, detail="This chat is open in too many places.")

    async def stream() -> AsyncIterator[str]:
        # Registered here, not before the response: a page gone before the
        # body starts never runs this, and so leaves nothing registered.
        watcher = chat_live.subscribe(session_id, caller)
        if watcher is None:
            yield f"data: {json.dumps({'type': 'resync', 'session_id': session_id})}\n\n"
            return
        # Access is checked again this often, idle or not, so someone
        # archived mid-turn stops receiving it.
        next_check = time.monotonic() + _LIVE_KEEPALIVE_S
        try:
            ready = {"type": "ready", "session_id": session_id, "turn": chat_live.snapshot(session_id)}
            yield f"data: {json.dumps(ready)}\n\n"
            while True:
                try:
                    event: dict[str, Any] | None = await asyncio.wait_for(
                        watcher.queue.get(), timeout=_LIVE_KEEPALIVE_S
                    )
                except TimeoutError:
                    event = None
                if event is None or time.monotonic() >= next_check:
                    if await request.is_disconnected():
                        return
                    if not await asyncio.to_thread(_still_in, request, session_id, caller, member):
                        yield f"data: {json.dumps({'type': 'removed', 'session_id': session_id})}\n\n"
                        return
                    next_check = time.monotonic() + _LIVE_KEEPALIVE_S
                if event is None:
                    yield _SSE_KEEPALIVE
                    continue
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") in ("removed", "deleted", "resync"):
                    return
        finally:
            chat_live.unsubscribe(watcher)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class SessionUpdate(BaseModel):
    """Rename and / or pin a chat. A field left out is left as it is."""

    title: str | None = Field(default=None, min_length=1, max_length=120)
    pinned: bool | None = None


@router.patch("/sessions/{session_id}", response_model=SessionSummary, response_model_exclude_none=True)
def update_session(session_id: str, body: SessionUpdate, request: Request) -> SessionSummary:
    """The owner names their chat (the automatic title never replaces it
    after) or pins it to the top of their list."""
    _require_owner(request, session_id)
    if body.title is not None:
        title = " ".join(body.title.split())
        if not title:
            raise HTTPException(status_code=422, detail="A title can't be blank")
        rename_session(session_id, title)
    if body.pinned is not None:
        set_pinned(session_id, body.pinned)
    meta = get_session_metadata(session_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return SessionSummary(**meta)


class ShareEntry(BaseModel):
    person_id: int
    full_name: str
    shared_at: str


class SharesUpdate(BaseModel):
    """Everyone the chat should be shared with; anyone not listed is unshared."""

    person_ids: list[int] = Field(default_factory=list, max_length=100)


def _share_list(session_id: str) -> list[ShareEntry]:
    out: list[ShareEntry] = []
    for row in shared_with(session_id):
        name = _person_name(row["person_id"])
        if name is not None:
            out.append(ShareEntry(person_id=row["person_id"], full_name=name, shared_at=row["shared_at"]))
    return out


@router.get("/sessions/{session_id}/shares", response_model=list[ShareEntry])
def get_session_shares(session_id: str, request: Request) -> list[ShareEntry]:
    """Who the owner shared this chat with."""
    _require_owner(request, session_id)
    return _share_list(session_id)


@router.put("/sessions/{session_id}/shares", response_model=list[ShareEntry])
def put_session_shares(session_id: str, body: SharesUpdate, request: Request) -> list[ShareEntry]:
    """Share the chat with exactly these team members, who then read it and
    write in it with its owner. Only a chat started here: a chat app's
    conversation lives in that app.

    Team members only (not contacts, not archived, not the owner). A chat
    that read its owner's own mail (Act as me) is never shared: 409."""
    owner = _require_owner(request, session_id)
    person_ids = sorted(set(body.person_ids))
    if person_ids and _is_channel_namespaced(session_id):
        raise HTTPException(status_code=409, detail="Only a chat started here can be shared.")
    if person_ids and session_mail_private(session_id):
        raise HTTPException(
            status_code=409,
            detail="This chat read your own mail, so it stays yours alone.",
        )
    from openexecutive.people.store import get_person

    for pid in person_ids:
        person = get_person(pid)
        if person is None or person.archived or person.kind != "team" or pid == owner:
            raise HTTPException(status_code=422, detail=f"Can't share with person {pid}")
    added, removed = set_shares(session_id, person_ids, owner)
    if added or removed:
        from openexecutive.audit import log_event

        with _owner_rows(session_id, owner):
            log_event(
                "session_shared",
                f"Chat shared with {len(person_ids)} people (+{len(added)} -{len(removed)})",
                session_id=session_id,
                actor="user",
                details={"by_person_id": owner, "added": added, "removed": removed},
            )
        if removed:
            chat_live.end_for(session_id, set(removed), "removed")
        chat_live.publish(session_id, {"type": "people"})
    return _share_list(session_id)


@router.delete("/sessions/{session_id}/shares/me", status_code=status.HTTP_204_NO_CONTENT)
def leave_session(session_id: str, request: Request) -> Response:
    """Leave a chat someone shared with the caller. It goes from their list;
    its owner can add them again."""
    caller, member = _require_chat_access(request, session_id)
    if not member or caller is None or not leave_share(session_id, caller):
        raise HTTPException(status_code=404, detail="Session not found")
    from openexecutive.audit import log_event

    _, owner = get_session_owner(session_id)
    with _owner_rows(session_id, owner):
        log_event(
            "session_shared",
            "Left a shared chat",
            session_id=session_id,
            actor="user",
            details={"by_person_id": caller, "added": [], "removed": [caller]},
        )
    chat_live.end_for(session_id, {caller}, "removed")
    chat_live.publish(session_id, {"type": "people"})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/sessions/{session_id}/export")
def export_session(session_id: str, request: Request) -> Response:
    """The chat as a Markdown file, for anyone in it."""
    _require_chat_access(request, session_id)
    meta = get_session_metadata(session_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Session not found")
    _, owner = get_session_owner(session_id)
    text = chat_export.to_markdown(meta, owner_person_id=owner)
    return Response(
        content=text,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{chat_export.file_name(meta["title"])}"'
        },
    )


class RewindRequest(BaseModel):
    """The id of the reply being replaced, which must still be the chat's last."""

    last_message_id: int


class RewindResponse(BaseModel):
    """The words of the turn just removed, to send again (Try again) or as
    the starting point of an edit."""

    message: str


@router.post("/sessions/{session_id}/rewind", response_model=RewindResponse)
async def rewind_session(session_id: str, body: RewindRequest, request: Request) -> RewindResponse:
    """Take back the chat's last turn so the owner can ask it again, as it
    was (Try again) or edited. The page then sends the message as a normal
    turn, through the same gates as any other.

    Web chats only: a Slack, Telegram or Discord conversation also lives in
    that app, where the reply can't be taken back. 409 while a turn is
    answering in this chat, or when the page's last reply is no longer the
    chat's last (another tab moved on)."""
    owner = _require_owner(request, session_id)
    if _is_channel_namespaced(session_id):
        raise HTTPException(status_code=409, detail="Only a chat started here can be redone.")
    # A message saved without a sender (before sharing) is the owner's.
    if chat_people(session_id) and last_turn_sender(session_id) not in (None, owner):
        # In a shared chat the last message may be someone else's words.
        raise HTTPException(status_code=409, detail="The last message is someone else's, so it can't be redone.")
    lock = await _acquire_session_turn(session_id, 0.5)
    if lock is None:
        raise HTTPException(status_code=409, detail="The Executive is still answering in this chat.")
    try:
        asked = rewind_last_turn(session_id, body.last_message_id)
    finally:
        _release_session_turn(session_id, lock)
    if asked is None:
        raise HTTPException(status_code=409, detail="This chat has moved on. Reload it and try again.")
    from openexecutive.audit import log_event

    with _owner_rows(session_id, owner):
        log_event(
            "session_rewound",
            f"Last turn taken back (reply {body.last_message_id})",
            session_id=session_id,
            actor="user",
            details={"last_message_id": body.last_message_id, "messages": len(asked)},
        )
    return RewindResponse(message="\n\n".join(asked))


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session_route(session_id: str, request: Request) -> Response:
    _require_session_access(request, session_id)
    deleted = delete_session(session_id)
    # A chat whose row never persisted still counts: dropping its live state
    # is the delete the caller asked for.
    if not forget_session(session_id) and not deleted:
        raise HTTPException(status_code=404, detail="Session not found")
    chat_live.end_for(session_id, None, "deleted")
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
    caller = _require_session_access(request, session_id)
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
    composer then falls back to its static placeholder. Only someone in the
    chat may ask: the suggestion paraphrases the conversation.
    """
    _require_chat_access(request, session_id)

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
