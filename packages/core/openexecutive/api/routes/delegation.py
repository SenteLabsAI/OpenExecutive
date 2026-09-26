"""Act as me: the caller's own delegation settings and "How I write".

Every route is about the CALLER'S OWN delegation — a person turns it on and
edits their writing profile for themselves; nobody does it for anyone else —
and only for a caller ``delegation.settings.can_delegate`` allows (Phase 1:
the principal). Anyone else gets 403 ``not_available_yet``, so the Settings
card hides itself.

A request with no ``x-caller-email`` is not a sign-in: it would resolve to the
principal (the CLI / curl rule in ``chat._resolve_caller_person_id``), but a
setting that lets the Executive write in someone's name needs the person
themselves. Such a request is refused (403 ``sign_in_required``) unless the
API runs under local login (``make dev``, never on a public deployment). The
header is stamped by the UI proxy from the Google sign-in; whoever holds
``BACKEND_SHARED_SECRET`` is trusted as that proxy, as on every principal-only
route.

Routes:
  GET    /delegation              — on/off and the Gmail connection
  PUT    /delegation              — {enabled}; turning it on needs the caller's
                                    own Gmail connected (409 otherwise)
  GET    /delegation/voice        — "How I write"
  POST   /delegation/voice/learn  — learn it from the caller's sent mail (409
                                    in_progress / locked / too_soon /
                                    not_enough_mail / no_profile / changed)
  POST   /delegation/voice/signature — take the signature from the caller's
                                    Gmail settings again (a locked profile
                                    stays locked; nothing else changes)
  PUT    /delegation/voice        — edit fields, lock / unlock
  DELETE /delegation/voice        — forget it (history kept)

Every change writes a private audit row.
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from openexecutive.delegation.gmail import (
    STATUS_MESSAGES,
    GmailAuthError,
    GmailError,
    gmail_for,
    gmail_status,
)
from openexecutive.delegation.settings import can_delegate, is_enabled, local_login, set_enabled
from openexecutive.delegation.voice import (
    StoredVoice,
    VoiceError,
    get_voice,
    learn_from_sent_mail,
    reset_voice,
    save_voice,
    validate_profile,
)
from openexecutive.people.models import Person

router = APIRouter()
logger = logging.getLogger(__name__)

# Gmail status -> the 409 code a caller gets when it blocks turning it on.
_BLOCKING_CODES: dict[str, str] = {
    "not_configured": "gmail_not_connected",
    "needs_reconnect": "gmail_needs_reconnect",
    "mismatch": "gmail_mismatch",
    "no_email": "no_email",
    "shared_mailbox": "shared_mailbox",
    "error": "gmail_error",
}


class GmailConnection(BaseModel):
    status: str
    message: str
    email: str | None = None
    # The command that connects this caller's own Gmail (upstream has no
    # OAuth callback: the token is minted locally, like the Executive's own).
    connect_command: str


class DelegationOut(BaseModel):
    enabled: bool
    gmail: GmailConnection


class DelegationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class VoiceOut(BaseModel):
    greetings: dict[str, str]
    sign_off: str
    signature: str
    length: str
    formality: str
    habits: list[str]
    avoid: list[str]
    exemplars: list[str]
    locked: bool
    learned_at: str | None
    sample_count: int
    updated_at: str | None


class VoiceUpdate(BaseModel):
    """Fields to change; anything left out stays as it is."""

    model_config = ConfigDict(extra="forbid")

    greetings: dict[str, str] | None = None
    sign_off: str | None = None
    length: str | None = None
    formality: str | None = None
    habits: list[str] | None = None
    avoid: list[str] | None = None
    locked: bool | None = None
    clear_exemplars: bool = False
    clear_signature: bool = False


# What a refused field must be, for the message an edit gets back.
_VOICE_RULES: dict[str, str] = {
    "greetings": "a greeting is one short line, and {first} is its only placeholder",
    "sign_off": "the sign-off is at most two short lines",
    "habits": "a habit or never-rule says how you write, without anyone's name",
    "avoid": "a habit or never-rule says how you write, without anyone's name",
}


def _refuse(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _caller(request: Request) -> Person:
    """The signed-in caller, when they may have Act as me; else 403."""
    from openexecutive.people.store import find_person_by_email, find_principal_person

    email = (request.headers.get("x-caller-email") or "").strip().lower()
    try:
        if email:
            person = find_person_by_email(email)
        elif local_login():
            person = find_principal_person()
        else:
            raise _refuse(403, "sign_in_required", "Sign in to change Act as me.")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("delegation: caller lookup failed")
        raise _refuse(503, "roster_unavailable", "Couldn't read the People list.") from exc
    if person is None or not can_delegate(person):
        raise _refuse(403, "not_available_yet", "Act as me is only for the account owner for now.")
    return person


def _person_id(person: Person) -> int:
    if person.id is None:  # can_delegate already requires one
        raise _refuse(403, "not_available_yet", "Act as me is only for the account owner for now.")
    return person.id


def _connect_command(email: str | None) -> str:
    target = email or "you@example.com"
    return (
        "uv run --with google-auth-oauthlib python scripts/connect-own-gmail.py "
        f"--email {target}"
    )


async def _state(person: Person) -> DelegationOut:
    status = await gmail_status(person.email)
    return DelegationOut(
        enabled=is_enabled(person.id),
        gmail=GmailConnection(
            status=status,
            message=STATUS_MESSAGES[status],
            email=person.email,
            connect_command=_connect_command(person.email),
        ),
    )


def _audit(event_type: str, summary: str, details: dict[str, Any]) -> None:
    from openexecutive.audit import log_event

    log_event(event_type, summary, actor="user", details=details, private=True)


def _voice_out(stored: StoredVoice) -> VoiceOut:
    p = stored.profile
    return VoiceOut(
        greetings=dict(p.greetings),
        sign_off=p.sign_off,
        signature=p.signature,
        length=p.length,
        formality=p.formality,
        habits=list(p.habits),
        avoid=list(p.avoid),
        exemplars=list(p.exemplars),
        locked=stored.locked,
        learned_at=stored.learned_at,
        sample_count=stored.sample_count,
        updated_at=stored.updated_at,
    )


@router.get("/delegation", response_model=DelegationOut)
async def get_delegation(request: Request) -> DelegationOut:
    return await _state(_caller(request))


@router.put("/delegation", response_model=DelegationOut)
async def update_delegation(request: Request, body: DelegationUpdate) -> DelegationOut:
    person = _caller(request)
    person_id = _person_id(person)
    if body.enabled:
        status = await gmail_status(person.email)
        if status != "connected":
            raise _refuse(409, _BLOCKING_CODES.get(status, "gmail_error"), STATUS_MESSAGES[status])
    before = is_enabled(person_id)
    if before != body.enabled:
        set_enabled(person_id, body.enabled, updated_by=f"person:{person_id}")
        if body.enabled:
            _audit(
                "delegation_gmail_verified",
                f"Checked person {person_id}'s own Gmail before turning Act as me on",
                {"person_id": person_id, "status": "connected"},
            )
        _audit(
            "delegation_settings_changed",
            f"Act as me turned {'on' if body.enabled else 'off'} by person {person_id}",
            {"person_id": person_id, "enabled": body.enabled},
        )
    return await _state(person)


@router.get("/delegation/voice", response_model=VoiceOut)
def get_delegation_voice(request: Request) -> VoiceOut:
    person = _caller(request)
    return _voice_out(get_voice(_person_id(person)))


@router.post("/delegation/voice/learn", response_model=VoiceOut)
async def learn_delegation_voice(request: Request) -> VoiceOut:
    person = _caller(request)
    status = await gmail_status(person.email)
    if status != "connected":
        raise _refuse(409, _BLOCKING_CODES.get(status, "gmail_error"), STATUS_MESSAGES[status])
    try:
        stored = await learn_from_sent_mail(person, gmail_for(person.email or ""))
    except VoiceError as exc:
        raise _refuse(409, exc.code, exc.message) from exc
    except GmailAuthError as exc:
        raise _refuse(409, "gmail_needs_reconnect", STATUS_MESSAGES["needs_reconnect"]) from exc
    except GmailError as exc:
        logger.warning("delegation: learning the voice failed", exc_info=True)
        raise _refuse(502, "gmail_error", STATUS_MESSAGES["error"]) from exc
    return _voice_out(stored)


@router.post("/delegation/voice/signature", response_model=VoiceOut)
async def refresh_delegation_signature(request: Request) -> VoiceOut:
    """Read the signature from the caller's Gmail settings again and keep
    everything else, lock included: a relearn would replace their edits."""
    person = _caller(request)
    person_id = _person_id(person)
    status = await gmail_status(person.email)
    if status != "connected":
        raise _refuse(409, _BLOCKING_CODES.get(status, "gmail_error"), STATUS_MESSAGES[status])
    try:
        signature = await gmail_for(person.email or "").send_as_signature()
    except GmailAuthError as exc:
        raise _refuse(409, "gmail_needs_reconnect", STATUS_MESSAGES["needs_reconnect"]) from exc
    except GmailError as exc:
        logger.warning("delegation: reading the Gmail signature failed", exc_info=True)
        raise _refuse(502, "gmail_error", STATUS_MESSAGES["error"]) from exc
    # Read after the Gmail call, so an edit made meanwhile is kept.
    stored = get_voice(person_id)
    profile, _ = validate_profile(
        {**asdict(stored.profile), "signature": signature}, allow_exemplars=True, keep_signature=True
    )
    saved = save_voice(person_id, profile, locked=stored.locked, updated_by=f"person:{person_id}")
    _audit(
        "delegation_voice_changed",
        f"Signature taken from Gmail settings by person {person_id}",
        {"op": "signature", "person_id": person_id, "has_signature": bool(profile.signature)},
    )
    return _voice_out(saved)


@router.put("/delegation/voice", response_model=VoiceOut)
def update_delegation_voice(request: Request, body: VoiceUpdate) -> VoiceOut:
    person = _caller(request)
    person_id = _person_id(person)
    stored = get_voice(person_id)
    current = stored.profile
    sent = body.model_fields_set
    data: dict[str, Any] = {
        "greetings": body.greetings if "greetings" in sent and body.greetings is not None else current.greetings,
        "sign_off": body.sign_off if "sign_off" in sent and body.sign_off is not None else current.sign_off,
        "length": body.length if "length" in sent and body.length is not None else current.length,
        "formality": body.formality if "formality" in sent and body.formality is not None else current.formality,
        "habits": body.habits if "habits" in sent and body.habits is not None else current.habits,
        "avoid": body.avoid if "avoid" in sent and body.avoid is not None else current.avoid,
        "exemplars": [] if body.clear_exemplars else current.exemplars,
        "signature": "" if body.clear_signature else current.signature,
    }
    profile, dropped = validate_profile(data, allow_exemplars=True, keep_signature=True)
    # Anything the person just typed that did not pass is an error to show,
    # not something to drop silently.
    rejected = [
        d for d in dropped
        if d["field"].split(".")[0] in sent
    ]
    if rejected:
        fields = [d["field"].split(".")[0] for d in rejected]
        rules = dict.fromkeys(_VOICE_RULES[f] for f in fields if f in _VOICE_RULES)
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_voice",
                "message": "Some of it can't be saved: "
                + "; ".join(rules or ["write about how you write"])
                + ". No links, handles or amounts.",
                "rejected": rejected,
            },
        )
    for field in ("length", "formality"):
        if field in sent and getattr(body, field) and not getattr(profile, field):
            raise _refuse(422, "invalid_voice", f"{field} must be one of the listed values.")
    locked = body.locked if body.locked is not None else stored.locked
    saved = save_voice(person_id, profile, locked=locked, updated_by=f"person:{person_id}")
    _audit(
        "delegation_voice_changed",
        f"Writing profile edited by person {person_id}",
        {"op": "edit", "person_id": person_id, "fields": sorted(sent), "locked": locked},
    )
    return _voice_out(saved)


@router.delete("/delegation/voice", response_model=VoiceOut)
def reset_delegation_voice(request: Request) -> VoiceOut:
    person = _caller(request)
    person_id = _person_id(person)
    saved = reset_voice(person_id, updated_by=f"person:{person_id}")
    _audit(
        "delegation_voice_changed",
        f"Writing profile reset by person {person_id}",
        {"op": "reset", "person_id": person_id},
    )
    return _voice_out(saved)
