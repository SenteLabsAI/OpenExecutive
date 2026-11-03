"""What it remembered lately: the caller's own memory changes (``memory.changes``).

Read by the chat's "Memory updated" chip (one conversation, since the turn
began) and the Memory page's "New since your last visit" (and the dot on
Memory in the sidebar). Each person sees only their own: the facts they
recorded (the principal: every fact recorded as the principal), the figures
it worked out for them, the lessons from their chats and their own notes.
The caller is resolved as for the History notes (``api.routes.history._caller``):
a signed-in person on the roster, or the principal under local login.
Anyone else gets 403. Undo uses each kind's own route.

Routes:
  GET /memories/changes?since=<ISO time>[&session_id=<id>]
"""
from __future__ import annotations

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from openexecutive.api.routes.history import _caller, _refuse
from openexecutive.memory import changes

router = APIRouter()


class ChangeOut(BaseModel):
    # fact | decision | worked_out | lesson | note
    kind: str
    id: int
    title: str
    # Where a worked-out figure came from; empty for the other kinds.
    source: str
    # active, or proposed (a fact waiting for the principal)
    status: str
    created_at: str
    can_undo: bool


class ChangesOut(BaseModel):
    changes: list[ChangeOut]


@router.get("/memories/changes", response_model=ChangesOut)
async def list_changes(
    request: Request,
    since: str = Query(..., max_length=64),
    session_id: str | None = Query(None, max_length=128),
) -> ChangesOut:
    from openexecutive.api.routes.people import caller_is_principal

    person = _caller(request)
    assert person.id is not None
    after = changes.normalize_since(since)
    if after is None:
        raise _refuse(422, "bad_since", "since must be a time, like 2026-11-03T21:00:00Z.")
    rows = changes.changes_since(
        person.id, principal=caller_is_principal(request), since=after, session_id=session_id or None,
    )
    return ChangesOut(changes=[ChangeOut(**r) for r in rows])
