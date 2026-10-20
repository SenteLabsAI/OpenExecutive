"""What it does for you, beside each Delegate tab (``delegation.may_do``): read-only.

  GET /what-it-may-do — for the signed-in caller: what goes out as them from
                        their own mailbox (when they may have Act as me),
                        what the Executive does on its own (the principal
                        only, as ``GET /take-the-lead``), and each
                        department's level (everyone on the team, as the
                        Departments page)

Who: a team member on People, read through ``api/caller.py`` only. A request
with no caller resolves to the principal only under local login, as on the
Act as me routes; a contact or anyone not on People gets 403. Nothing here
changes a setting or reads anyone's mail.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from openexecutive.api import caller as api_caller

logger = logging.getLogger(__name__)

router = APIRouter()


class RowOut(BaseModel):
    id: str
    what: str
    # goes | waits | always_waits | drafts | off
    outcome: str
    then: str
    why: str
    # act_as_me | handle_it | take_the_lead | pause | department:<slug> | ""
    setting: str
    items: list[str]


class SectionOut(BaseModel):
    # as_you | executive | departments
    key: str
    title: str
    available: bool
    unavailable: str
    notes: list[str]
    rows: list[RowOut]
    footer: str
    # How many rows go out (or act) without asking.
    goes: int


class MayDoOut(BaseModel):
    paused: bool
    sections: list[SectionOut]


def _viewer(request: Request) -> Any:
    """The person asking, through the app's one caller resolver
    (``chat._resolve_caller_person_id``), so this page knows people the same
    way every other page does; a deployment that maps a signed-in account to
    its person there is followed here too. A request that names no one only
    counts as the principal under local login."""
    from openexecutive.api.routes.chat import _resolve_caller_person_id
    from openexecutive.people.store import find_principal_person, get_person
    from openexecutive.utils.deployment import is_local_login

    who = api_caller.caller(request)
    if not who.email and not (who.defaults_to_principal and is_local_login()):
        raise HTTPException(status_code=403, detail="Sign in to see what it may do.")
    try:
        person_id = _resolve_caller_person_id(request)
        if person_id is None:
            # The resolver reads a roster it can't open as "no one"; read it
            # once more so an outage answers 503, not "not for you".
            find_principal_person()
        person = get_person(person_id) if person_id is not None else None
    except Exception as exc:
        logger.exception("may_do: caller lookup failed")
        raise HTTPException(status_code=503, detail="Couldn't read the People list.") from exc
    if person is None or person.id is None or person.archived or person.kind != "team":
        raise HTTPException(status_code=403, detail="Only people on the team can see this.")
    return person


@router.get("/what-it-may-do", response_model=MayDoOut)
def get_what_it_may_do(request: Request) -> MayDoOut:
    from openexecutive.delegation import may_do
    from openexecutive.delegation.settings import can_delegate

    person = _viewer(request)
    try:
        found = may_do.summary(
            person.id, can_have_it=can_delegate(person), is_principal=bool(person.is_principal),
        )
    except Exception as exc:
        logger.exception("may_do: couldn't put the page together")
        raise HTTPException(status_code=503, detail="Couldn't read the settings.") from exc
    return MayDoOut(
        paused=found.paused,
        sections=[
            SectionOut(
                key=s.key, title=s.title, available=s.available, unavailable=s.unavailable, notes=list(s.notes),
                footer=s.footer, goes=s.goes,
                rows=[
                    RowOut(
                        id=r.id, what=r.what, outcome=r.outcome, then=r.then, why=r.why, setting=r.setting,
                        items=list(r.items),
                    )
                    for r in s.rows
                ],
            )
            for s in found.sections
        ],
    )
