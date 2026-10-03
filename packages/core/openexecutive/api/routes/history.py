"""Always in the loop: the caller's own notes and switches (``memory.history``).

Every route is about the CALLER'S OWN notes: each person sees, corrects, pins
and forgets their own, and nobody else's (the principal included). The one
company-wide setting, how long notes last, is the principal's.

The caller is resolved as for Act as me (``api.routes.delegation._caller``'s
rule): a signed-in person on the roster; a request with no caller only under
local login, as the principal. Anyone else gets 403.

Routes:
  GET    /memories/history                   — the caller's notes (``?q=`` to narrow) and switches
  PUT    /memories/history/settings          — the caller's switches; the principal also the company retention
  PATCH  /memories/history/{id}              — pin or unpin, correct or clear a correction
  DELETE /memories/history/{id}              — forget one note
  POST   /memories/history/{id}/forget-conversation — "Don't remember this": forget every note
                                               from that note's conversation and never note it again
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from openexecutive.api import caller as api_caller
from openexecutive.memory import history
from openexecutive.people.models import Person

router = APIRouter()
logger = logging.getLogger(__name__)


class NoteOut(BaseModel):
    id: int
    source: str
    channel: str
    conversation_key: str
    counterpart: str
    subject: str
    kind: str
    summary: str
    quote: str
    due_date: str | None
    trust: str
    occurred_at: str
    created_at: str
    expires_at: str | None
    pinned: bool
    correction: str | None
    corrected_at: str | None


class HistoryOut(BaseModel):
    notes: list[NoteOut]
    # The caller's own switches.
    reply_notes: bool
    retention_days: int | None
    # What applies to the caller's new notes, and the company default.
    effective_retention_days: int | None
    company_retention_days: int | None
    retention_choices: list[int | None]
    # Whether they may turn reply notes on (they can use Act as me) and set
    # the company retention (they are the principal).
    can_note_replies: bool
    can_set_company_retention: bool


class SettingsUpdate(BaseModel):
    """Fields to change; anything left out stays as it is. ``retention_days``
    null means "the company default" for a person, "until forgotten" for the
    company."""

    model_config = ConfigDict(extra="forbid")

    reply_notes: bool | None = None
    retention_days: int | None = None
    company_retention_days: int | None = None


class NoteUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pinned: bool | None = None
    # The caller's own version of the note; "" clears it.
    correction: str | None = None


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
            raise _refuse(403, "sign_in_required", "Sign in to see your notes.")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("history: caller lookup failed")
        raise _refuse(503, "roster_unavailable", "Couldn't read the People list.") from exc
    if person is None or person.id is None:
        raise _refuse(403, "not_on_roster", "Your notes are kept for people on the People list.")
    return person


def _audit(event_type: str, summary: str, details: dict[str, Any]) -> None:
    from openexecutive.audit import log_event

    person_id = details.get("person_id")
    log_event(
        event_type, summary, actor="user", details=details, private=True,
        private_to_person=person_id if isinstance(person_id, int) else None,
    )


def _can_note_replies(person: Person) -> bool:
    from openexecutive.delegation.settings import can_delegate

    try:
        return can_delegate(person)
    except Exception:
        return False


def _note_out(note: history.Note) -> NoteOut:
    return NoteOut(**note.as_dict())


def _state(person: Person, query: str | None = None) -> HistoryOut:
    assert person.id is not None
    own = history.person_settings(person.id)
    return HistoryOut(
        notes=[_note_out(n) for n in history.list_notes(person.id, query=query, limit=history.MAX_LIST)],
        reply_notes=own.reply_notes,
        retention_days=own.retention_days,
        effective_retention_days=history.effective_retention(person.id),
        company_retention_days=history.company_retention(),
        retention_choices=list(history.RETENTION_CHOICES),
        can_note_replies=_can_note_replies(person),
        can_set_company_retention=bool(person.is_principal),
    )


@router.get("/memories/history", response_model=HistoryOut)
async def get_history(request: Request, q: str | None = Query(default=None, max_length=200)) -> HistoryOut:
    """The caller's own notes, newest first, and their switches."""
    return _state(_caller(request), q)


@router.put("/memories/history/settings", response_model=HistoryOut)
async def update_history_settings(request: Request, body: SettingsUpdate) -> HistoryOut:
    person = _caller(request)
    assert person.id is not None
    fields = body.model_fields_set
    company_change = "company_retention_days" in fields
    own_change = "reply_notes" in fields or "retention_days" in fields
    # Check everything before changing anything, so a refusal never leaves
    # half the request applied.
    if company_change and not person.is_principal:
        raise _refuse(403, "principal_only", "Only the account owner can change how long notes last for everyone.")
    if own_change and body.reply_notes and not _can_note_replies(person):
        raise _refuse(
            403, "not_available_yet",
            "Notes from your replies need Act as me, which isn't available to you here.",
        )
    try:
        company = (
            history.valid_retention(body.company_retention_days) if company_change else history.company_retention()
        )
        if "retention_days" in fields:
            history.valid_person_retention(body.retention_days, company)
    except history.SettingError as exc:
        raise _refuse(422, "invalid_setting", str(exc)) from exc

    if company_change:
        history.set_company_retention(company, by=f"person:{person.id}")
        _audit(
            "history_settings_changed", f"Company note retention changed by person {person.id}",
            {"person_id": person.id, "company_retention_days": company},
        )
    if own_change:
        kwargs: dict[str, Any] = {}
        if "reply_notes" in fields and body.reply_notes is not None:
            kwargs["reply_notes"] = body.reply_notes
        if "retention_days" in fields:
            kwargs["retention_days"] = body.retention_days
        try:
            history.set_person_settings(person.id, by=f"person:{person.id}", **kwargs)
        except history.SettingError as exc:
            raise _refuse(422, "invalid_setting", str(exc)) from exc
        _audit(
            "history_settings_changed", f"Note settings changed by person {person.id}",
            {"person_id": person.id, **kwargs},
        )
    return _state(person)


@router.patch("/memories/history/{note_id}", response_model=NoteOut)
async def update_note(request: Request, note_id: int, body: NoteUpdate) -> NoteOut:
    person = _caller(request)
    assert person.id is not None
    note = history.get_note(person.id, note_id)
    if note is None:
        raise _refuse(404, "not_found", "That note isn't there any more.")
    if body.pinned is not None:
        note = history.pin_note(person.id, note_id, body.pinned) or note
    if body.correction is not None:
        note = history.correct_note(person.id, note_id, body.correction) or note
    _audit(
        "history_note_changed", f"Person {person.id} changed one of their notes",
        {"person_id": person.id, "note_id": note_id, "pinned": body.pinned,
         "corrected": body.correction is not None and bool(body.correction.strip())},
    )
    return _note_out(note)


@router.delete("/memories/history/{note_id}")
async def forget_note(request: Request, note_id: int) -> dict[str, Any]:
    person = _caller(request)
    assert person.id is not None
    if not history.forget_note(person.id, note_id):
        raise _refuse(404, "not_found", "That note isn't there any more.")
    _audit(
        "history_note_forgotten", f"Person {person.id} forgot one of their notes",
        {"person_id": person.id, "note_id": note_id},
    )
    return {"forgotten": 1}


@router.post("/memories/history/{note_id}/forget-conversation")
async def forget_conversation(request: Request, note_id: int) -> dict[str, Any]:
    """"Don't remember this": every note from the same conversation is
    forgotten, and it is never noted again."""
    person = _caller(request)
    assert person.id is not None
    note = history.get_note(person.id, note_id)
    if note is None:
        raise _refuse(404, "not_found", "That note isn't there any more.")
    forgotten = history.forget_conversation(person.id, note.conversation_key)
    _audit(
        "history_conversation_forgotten", f"Person {person.id} asked not to remember a conversation",
        {"person_id": person.id, "forgotten": forgotten},
    )
    return {"forgotten": forgotten}
