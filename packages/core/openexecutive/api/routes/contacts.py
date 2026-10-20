"""Routes for contacts the Executive keeps up to date (``people.contact_keeper``).

- ``GET /contacts/changes`` — the switch, how the last look went, and the
  changes of the last 30 days (``person_id`` for one person's every change).
- ``PUT /contacts/keeper`` — the switch: ``on``, ``ask`` or ``off``.
- ``POST /contacts/changes/{id}/undo`` — take back a change.
- ``POST /contacts/changes/{id}/accept`` / ``/dismiss`` — Add / Not now on a
  suggestion.
- ``POST /contacts/changes/seen`` — Got it: clears the card.
- ``GET /contacts/{person_id}/note`` — what the Executive remembers about one
  person, contact or teammate (``people.contact_notes``): 404 when it has
  nothing yet. The principal's alone, whoever it is about.

Contacts are the principal's alone, so every route answers only a caller
that resolves to the principal (``people.caller_is_principal``): 404 for
anyone else, so nobody learns there is anything here.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from openexecutive.api.routes.people import caller_is_principal
from openexecutive.people import contact_changes as changes
from openexecutive.people import contact_notes

router = APIRouter()


class ContactChangesOut(BaseModel):
    mode: changes.Mode
    # How the last look went: "ok", "act_as_me_off", "not_connected", ...;
    # "" before the first one.
    status: str
    last_run_at: str | None
    changes: list[changes.ContactChange]


class KeeperModeIn(BaseModel):
    mode: changes.Mode


def _require_principal(request: Request) -> None:
    if not caller_is_principal(request):
        raise HTTPException(status_code=404, detail="Not found")


@router.get("/contacts/changes", response_model=ContactChangesOut)
def get_contact_changes(request: Request, person_id: int | None = None) -> ContactChangesOut:
    _require_principal(request)
    last, status = changes.last_run()
    return ContactChangesOut(
        mode=changes.get_mode(),
        status=status,
        last_run_at=last.isoformat() if last is not None else None,
        changes=changes.list_changes(person_id=person_id),
    )


@router.put("/contacts/keeper", response_model=ContactChangesOut)
def put_contact_keeper(body: KeeperModeIn, request: Request) -> ContactChangesOut:
    _require_principal(request)
    changes.set_mode(body.mode)
    return get_contact_changes(request)


@router.post("/contacts/changes/seen", response_model=ContactChangesOut)
def post_contact_changes_seen(request: Request) -> ContactChangesOut:
    _require_principal(request)
    changes.mark_seen()
    return get_contact_changes(request)


def _decide(change_id: int, request: Request, verb: str) -> changes.ContactChange:
    _require_principal(request)
    act = {"undo": changes.undo, "accept": changes.accept, "dismiss": changes.dismiss}[verb]
    try:
        return act(change_id)
    except changes.UndoRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/contacts/changes/{change_id}/undo", response_model=changes.ContactChange)
def post_contact_change_undo(change_id: int, request: Request) -> changes.ContactChange:
    return _decide(change_id, request, "undo")


@router.post("/contacts/changes/{change_id}/accept", response_model=changes.ContactChange)
def post_contact_change_accept(change_id: int, request: Request) -> changes.ContactChange:
    return _decide(change_id, request, "accept")


@router.post("/contacts/changes/{change_id}/dismiss", response_model=changes.ContactChange)
def post_contact_change_dismiss(change_id: int, request: Request) -> changes.ContactChange:
    return _decide(change_id, request, "dismiss")


@router.get("/contacts/{person_id}/note", response_model=contact_notes.ContactNote)
def get_contact_note(person_id: int, request: Request) -> contact_notes.ContactNote:
    _require_principal(request)
    from openexecutive.people import store as people_store

    person = people_store.get_person(person_id)
    note = contact_notes.get(person_id) if person is not None and not person.is_principal else None
    if note is None or person is None or person.archived:
        raise HTTPException(status_code=404, detail="Not found")
    return note
