"""Delegate → What it may do (``delegation.may_do``): read-only.

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
    from openexecutive.people.store import find_person_by_email, find_principal_person
    from openexecutive.utils.deployment import is_local_login

    who = api_caller.caller(request)
    try:
        if who.email:
            person = find_person_by_email(who.email)
        elif who.defaults_to_principal and is_local_login():
            person = find_principal_person()
        else:
            raise HTTPException(status_code=403, detail="Sign in to see what it may do.")
    except HTTPException:
        raise
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
