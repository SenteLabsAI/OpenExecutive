"""Act as me: the caller's own delegation settings and "How I write".

Every route is about the CALLER'S OWN delegation — a person turns it on and
edits their writing profile for themselves; nobody does it for anyone else —
and only for a caller ``delegation.settings.can_delegate`` allows (the
principal, and team members once the owner lets them). Anyone else gets 403
``not_available_yet``, so the Settings card hides itself.

A request with no ``x-caller-email`` is not a sign-in: it would resolve to the
principal (the CLI / curl rule in ``chat._resolve_caller_person_id``), but a
setting that lets the Executive write in someone's name needs the person
themselves. Such a request is refused (403 ``sign_in_required``) unless the
API runs under local login (``make dev``, never on a public deployment). The
header is stamped by the UI proxy from the Google sign-in; whoever holds
``BACKEND_SHARED_SECRET`` is trusted as that proxy, as on every principal-only
route.

Routes:
  GET    /delegation              — on/off, the Gmail connection and the inbox
                                    watcher's switch and health
  PUT    /delegation              — {enabled}; turning it on needs the caller's
                                    own Gmail connected (409 otherwise); turning
                                    it off turns the inbox watcher off too
  PUT    /delegation/inbox        — {enabled}: "Draft replies to my inbox";
                                    turning it on needs Act as me on and Gmail
                                    connected (409), and starts from now
  POST   /delegation/inbox/check  — check the inbox now (202; 409 when the
                                    switch is off or a check is running)
  GET    /delegation/replies      — the reply cards waiting for the caller
                                    (from the database; no Gmail call)
  PUT    /delegation/handle-it    — {enabled?, levels?}: Handle it for me,
                                    the inbox watcher sending some replies on
                                    its own (delegation.handle_it). Needs a
                                    caller the API knows is that person
                                    (signed sign-ins, or local login: 403 /
                                    409 ``caller_signing_required``), and
                                    turning it on needs Draft replies to my
                                    inbox on (409). Turning the inbox watcher
                                    off turns it off too
  GET    /delegation/handled      — the caller's replies sent on its own in
                                    the last 7 days, with the questions each
                                    left for them
  GET    /delegation/voice        — "How I write"
  POST   /delegation/voice/learn  — learn it from the caller's sent mail (409
                                    in_progress / locked / too_soon /
                                    not_enough_mail / no_profile / changed)
  POST   /delegation/voice/signature — take the signature from the caller's
                                    Gmail settings again (a locked profile
                                    stays locked; nothing else changes)
  PUT    /delegation/voice        — edit fields, lock / unlock
  DELETE /delegation/voice        — forget it (history kept)
  PUT    /delegation/team         — {enabled}: the owner's "Let team members
                                    use Act as me" (principal only; 409
                                    ``not_available`` unless the install
                                    allows it, ``DELEGATION_TEAM_MEMBERS``)

``GET /delegation`` gives the principal ``team`` while the install allows it:
the switch and, for each team member who has it on, counts only — drafts
saved and sent in the last 30 days, never what they said or to whom. A team
member's own mail, cards and rows are theirs alone.

Every change writes a private audit row.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from openexecutive.api import caller as api_caller
from openexecutive.delegation.gmail import (
    BLOCKING_CODES,
    STATUS_MESSAGES,
    GmailAuthError,
    GmailError,
    credential_provider,
    gmail_for,
    gmail_status,
)
from openexecutive.delegation.settings import (
    can_delegate,
    enabled_person_ids,
    is_enabled,
    local_login,
    set_enabled,
    set_team_members,
    team_members_available,
    team_members_switch,
)
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
_BLOCKING_CODES = BLOCKING_CODES


class GmailConnection(BaseModel):
    status: str
    message: str
    email: str | None = None
    # The commands that connect this caller's own Gmail or Outlook (there is
    # no OAuth callback: the token is minted locally, like the Executive's own).
    connect_command: str
    outlook_connect_command: str = ""
    # Which mailbox the saved sign-in opens: "google" or "microsoft".
    provider: str = "google"


class InboxOut(BaseModel):
    enabled: bool
    status: str
    message: str
    watch_since: str | None = None
    last_poll_at: str | None = None
    checking: bool = False


class TeamMemberUse(BaseModel):
    """One team member's use of Act as me, as the owner sees it: counts only."""

    person_id: int
    name: str
    enabled: bool
    inbox: bool
    drafts_30d: int
    sent_30d: int


class TeamOut(BaseModel):
    enabled: bool
    members: list[TeamMemberUse]


class HandleItOut(BaseModel):
    """Handle it for me: the switch and each kind's level (off, ask, handle)."""

    enabled: bool
    levels: dict[str, str]
    # Whether this server can tie the switch to the person (signed sign-ins
    # or local login); without it nothing is sent on its own.
    available: bool
    sent_today: int = 0


class HandleItUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    levels: dict[str, str] | None = None


class HandledReplyOut(BaseModel):
    decision_id: int
    sent_at: str
    to_name: str
    to_email: str
    subject: str
    body: str
    open_questions: list[str]
    gmail_link: str


class HandledOut(BaseModel):
    replies: list[HandledReplyOut]


class DelegationOut(BaseModel):
    enabled: bool
    gmail: GmailConnection
    inbox: InboxOut
    # Absent on a backend that predates Handle it for me.
    handle_it: HandleItOut | None = None
    # The principal's "Let team members use Act as me", while the install
    # allows it; None for anyone else.
    team: TeamOut | None = None


class TeamUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class InboxUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class ReplyCardOut(BaseModel):
    decision_id: int
    status: str
    created_at: str
    thread_id: str
    from_name: str
    from_email: str
    relation: str
    sender_verified: bool
    subject: str
    received_at: str
    they_wrote: str
    draft_to: list[str]
    draft_subject: str
    draft_body: str
    open_questions: list[str]
    flags: list[str]
    gmail_link: str
    # Why Handle it for me left it for you ("" when it didn't decide).
    waited_because: str = ""


class RepliesOut(BaseModel):
    cards: list[ReplyCardOut]


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

    who = api_caller.caller(request)
    try:
        if who.email:
            person = find_person_by_email(who.email)
        elif who.defaults_to_principal and local_login():
            person = find_principal_person()
        else:
            raise _refuse(403, "sign_in_required", "Sign in to change Act as me.")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("delegation: caller lookup failed")
        raise _refuse(503, "roster_unavailable", "Couldn't read the People list.") from exc
    if person is None or not can_delegate(person):
        raise _refuse(403, "not_available_yet", _NOT_AVAILABLE)
    return person


_NOT_AVAILABLE = "Act as me isn't available to you here. The account owner can let team members use it."


def _person_id(person: Person) -> int:
    if person.id is None:  # can_delegate already requires one
        raise _refuse(403, "not_available_yet", _NOT_AVAILABLE)
    return person.id


def _connect_command(email: str | None) -> str:
    target = email or "you@example.com"
    return (
        "uv run --with google-auth-oauthlib python scripts/connect-own-gmail.py "
        f"--email {target}"
    )


def _outlook_connect_command(email: str | None) -> str:
    target = email or "you@example.com"
    return f"uv run python scripts/connect-own-outlook.py --email {target}"


def _inbox_out(person_id: int) -> InboxOut:
    from openexecutive.delegation import inbox

    watch = inbox.get_watch(person_id)
    checking = inbox.scanning(person_id)
    status = "checking" if checking else watch.status
    return InboxOut(
        enabled=watch.enabled,
        status=status,
        message=inbox.STATUS_MESSAGES.get(status, inbox.STATUS_MESSAGES["ok"]),
        watch_since=watch.watch_since,
        last_poll_at=watch.last_poll_at,
        checking=checking,
    )


def _team_out(person: Person) -> TeamOut | None:
    """The owner's switch and each team member's counts, for the principal
    while the install allows it; None otherwise."""
    from datetime import UTC, datetime, timedelta

    from openexecutive.delegation import drafts, inbox
    from openexecutive.people.store import get_person

    if not person.is_principal or not team_members_available():
        return None
    since = datetime.now(UTC) - timedelta(days=30)
    members: list[TeamMemberUse] = []
    for person_id in enabled_person_ids():
        if person_id == person.id:
            continue
        try:
            member = get_person(person_id)
            if member is None or member.archived or member.kind != "team":
                continue
            drafted, sent = drafts.usage_since(person_id, since)
            watching = inbox.get_watch(person_id).enabled
        except Exception:
            logger.warning("delegation: couldn't read a team member's use", exc_info=True)
            continue
        members.append(TeamMemberUse(
            person_id=person_id, name=member.full_name or member.email or f"Person {person_id}",
            enabled=True, inbox=watching, drafts_30d=drafted, sent_30d=sent,
        ))
    return TeamOut(enabled=team_members_switch(), members=members)


async def _state(person: Person) -> DelegationOut:
    status = await gmail_status(person.email)
    return DelegationOut(
        enabled=is_enabled(person.id),
        gmail=GmailConnection(
            status=status,
            message=STATUS_MESSAGES[status],
            email=person.email,
            connect_command=_connect_command(person.email),
            outlook_connect_command=_outlook_connect_command(person.email),
            provider=credential_provider(person.email or ""),
        ),
        inbox=_inbox_out(_person_id(person)),
        handle_it=_handle_it_out(_person_id(person)),
        team=_team_out(person),
    )


def _handle_it_out(person_id: int) -> HandleItOut:
    from datetime import UTC, datetime

    from openexecutive.delegation import handle_it

    stored = handle_it.get(person_id)
    try:
        today = handle_it.sent_today(person_id, datetime.now(UTC))
    except Exception:
        today = 0
    return HandleItOut(
        enabled=stored.enabled, levels=dict(stored.levels), available=handle_it.signing_ok(), sent_today=today,
    )


def _audit(event_type: str, summary: str, details: dict[str, Any]) -> None:
    from openexecutive.audit import log_event

    person_id = details.get("person_id")
    log_event(
        event_type, summary, actor="user", details=details, private=True,
        private_to_person=person_id if isinstance(person_id, int) else None,
    )


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
    if not body.enabled:
        # The inbox watcher needs Act as me; off here, it starts from scratch
        # (watch_since) when turned on again, never catching up.
        _set_inbox(person_id, False)
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


@router.put("/delegation/team", response_model=DelegationOut)
async def update_delegation_team(request: Request, body: TeamUpdate) -> DelegationOut:
    """The owner's "Let team members use Act as me". Off takes it away from
    every team member from their next message (``can_delegate``), and their
    inbox watchers stop at their next check; their own switches, drafts and
    cards are kept for if it is turned on again."""
    person = _caller(request)
    person_id = _person_id(person)
    if not person.is_principal:
        raise _refuse(403, "principal_only", "Only the account owner can change this.")
    if not team_members_available():
        raise _refuse(409, "not_available", "Act as me for team members isn't available on this install.")
    if team_members_switch() != body.enabled:
        set_team_members(body.enabled, updated_by=f"person:{person_id}")
        _audit(
            "delegation_settings_changed",
            f"Act as me for team members turned {'on' if body.enabled else 'off'} by person {person_id}",
            {"person_id": person_id, "team_members": body.enabled},
        )
    return await _state(person)


def _set_inbox(person_id: int, enabled: bool) -> None:
    from openexecutive.delegation import inbox

    if not enabled:
        # Handle it for me sends what the watcher drafts: it goes with it.
        _set_handle_it(person_id, enabled=False, levels=None)
    before = inbox.get_watch(person_id).enabled
    if before == enabled:
        return
    inbox.set_watch(person_id, enabled, updated_by=f"person:{person_id}")
    _audit(
        "delegation_inbox_changed",
        f"Draft replies to my inbox turned {'on' if enabled else 'off'} by person {person_id}",
        {"person_id": person_id, "enabled": enabled},
    )


# Checks started from the card, kept so they aren't collected mid-run.
_CHECKS: set[asyncio.Task[Any]] = set()


@router.put("/delegation/inbox", response_model=DelegationOut)
async def update_delegation_inbox(request: Request, body: InboxUpdate) -> DelegationOut:
    person = _caller(request)
    person_id = _person_id(person)
    if body.enabled:
        if not is_enabled(person_id):
            raise _refuse(409, "act_as_me_off", "Turn Act as me on first.")
        status = await gmail_status(person.email)
        if status != "connected":
            raise _refuse(409, _BLOCKING_CODES.get(status, "gmail_error"), STATUS_MESSAGES[status])
    _set_inbox(person_id, body.enabled)
    return await _state(person)


@router.post("/delegation/inbox/check", status_code=202, response_model=InboxOut)
async def check_delegation_inbox(request: Request) -> InboxOut:
    """Check the caller's inbox now, in the background; ``GET /delegation``
    shows when it is done."""
    from openexecutive.audit.context import unscoped_audit_rows
    from openexecutive.delegation import inbox

    person = _caller(request)
    person_id = _person_id(person)
    if not inbox.get_watch(person_id).enabled:
        raise _refuse(409, "inbox_off", "Turn on Draft replies to my inbox first.")
    if inbox.scanning(person_id):
        raise _refuse(409, "in_progress", "It's checking your inbox already.")
    with unscoped_audit_rows():
        task = asyncio.create_task(inbox.scan_person(person))
    _CHECKS.add(task)
    task.add_done_callback(_CHECKS.discard)
    # The scan marks itself as running on its first step; say so either way.
    out = _inbox_out(person_id)
    return out.model_copy(update={"checking": True, "status": "checking",
                                  "message": inbox.STATUS_MESSAGES["checking"]})


@router.get("/delegation/replies", response_model=RepliesOut)
def get_delegation_replies(request: Request) -> RepliesOut:
    """The reply cards waiting for the caller. Their own only: everyone else
    gets the 403 every route here gives."""
    from openexecutive.delegation.replies import cards

    person = _caller(request)
    _person_id(person)
    try:
        found = cards(person)
    except Exception as exc:
        logger.exception("delegation: reading the reply cards failed")
        raise _refuse(503, "unavailable", "Couldn't read the replies waiting for you.") from exc
    return RepliesOut(cards=[
        ReplyCardOut(**{k: v for k, v in asdict(card).items() if k in ReplyCardOut.model_fields})
        for card in found
    ])


def _set_handle_it(person_id: int, *, enabled: bool | None, levels: dict[str, str] | None) -> None:
    from openexecutive.delegation import handle_it

    before = handle_it.get(person_id)
    if (enabled is None or enabled == before.enabled) and not levels:
        return
    after = handle_it.set_(person_id, enabled=enabled, levels=levels, updated_by=f"person:{person_id}")
    if after.enabled == before.enabled and after.levels == before.levels:
        return
    _audit(
        "delegation_handle_it_changed",
        f"Handle it for me {'on' if after.enabled else 'off'} for person {person_id}",
        {"person_id": person_id, "enabled": after.enabled, "levels": after.levels},
    )


@router.put("/delegation/handle-it", response_model=DelegationOut)
async def update_delegation_handle_it(request: Request, body: HandleItUpdate) -> DelegationOut:
    from openexecutive.delegation import handle_it, inbox
    from openexecutive.delegation.gmail import normalize_email
    from openexecutive.delegation.verified import NOT_YOURS, caller_refusal

    person = _caller(request)
    person_id = _person_id(person)
    refused = caller_refusal(api_caller.caller(request), normalize_email(person.email or ""))
    if refused == NOT_YOURS:
        raise _refuse(403, "not_yours", "Only you can change Handle it for me.")
    # Turning it off only narrows what the watcher does, so it stays
    # possible after the server loses signed sign-ins.
    turning_off = body.enabled is False and not body.levels
    if refused is not None and not turning_off:
        raise _refuse(
            409, "caller_signing_required",
            "Handle it for me needs signed sign-ins on this server, so that nobody else can turn it on for you.",
        )
    levels = body.levels or {}
    for kind, level in levels.items():
        if kind not in handle_it.KINDS or level not in handle_it.LEVELS:
            raise _refuse(422, "bad_level", f"Unknown kind or level: {kind}={level}.")
    if body.enabled:
        if not is_enabled(person_id):
            raise _refuse(409, "act_as_me_off", "Turn Act as me on first.")
        if not inbox.get_watch(person_id).enabled:
            raise _refuse(409, "inbox_off", "Turn on Draft replies to my inbox first.")
    _set_handle_it(person_id, enabled=body.enabled, levels=levels or None)
    return await _state(person)


@router.get("/delegation/handled", response_model=HandledOut)
def get_delegation_handled(request: Request) -> HandledOut:
    """What Handle it for me sent for the caller in the last 7 days. Their
    own only: everyone else gets the 403 every route here gives."""
    from openexecutive.delegation import handle_it
    from openexecutive.delegation.gmail import mailbox_link

    person = _caller(request)
    person_id = _person_id(person)
    email = (person.email or "").strip().lower()
    try:
        found = handle_it.handled(person_id)
    except Exception as exc:
        logger.exception("delegation: reading what was sent on its own failed")
        raise _refuse(503, "unavailable", "Couldn't read what it handled for you.") from exc
    return HandledOut(replies=[
        HandledReplyOut(
            decision_id=h.decision_id, sent_at=h.sent_at, to_name=h.to_name, to_email=h.to_email,
            subject=h.subject, body=h.body, open_questions=h.open_questions,
            gmail_link=mailbox_link(email, thread_id=h.thread_id) if email and h.thread_id else "",
        )
        for h in found
    ])


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
    everything else, lock included: a relearn would replace their edits.
    Outlook has no signature an app can read, so it is refused there rather
    than clearing the one they have."""
    person = _caller(request)
    person_id = _person_id(person)
    if credential_provider(person.email or "") == "microsoft":
        raise _refuse(
            409, "signature_unavailable",
            "Outlook doesn't let apps read your signature, so drafts end with your sign-off.",
        )
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
