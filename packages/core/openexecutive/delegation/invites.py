"""Act as me: meeting invites, answered from a card or on their own.

The inbox watcher (``delegation.inbox``) used to skip every calendar invite.
With Read my calendar on (``delegation.calendar``), an invite from someone the
person knows becomes a card on Home instead: who invited them, the meeting's
title, when it is, and any clash with their calendar. **Accept** and
**Decline** answer it in their own calendar, as them.

**Which invites.** A meeting request (not a cancellation, nor someone's
answer) in the inbox since the watcher was turned on, addressed to the
person, from someone they know: their team, one of their contacts, or
someone they have written to (``inbox.relation_of``), and only when the mail
service confirmed the sender (``inbox.handling_relation``). The event must be
on their own calendar (Google: found by the invite's iCalendar UID in their
primary calendar; Outlook: the invite message's own event), organized by the
sender, not answered yet and still ahead. Anything else is left alone as
before: skipped and recorded with its reason (``invite_stranger``,
``invite_unverified``, ``invite_not_found``, ...). With Read my calendar off
nothing here runs: an invite is skipped as ``automatic``, as it always was.

**Clashes.** The busy times on their calendar that overlap the meeting,
computed in code from the calendar read (``calendar.events_for``): only the
times reach the card, never another event's title, and no model is called
anywhere in this module.

**Answering.** ``answer``, on the card's person's tap (``POST
/decisions/{id}/approve`` with ``edits.answer`` ``accept`` or ``decline``;
the caller checks reply cards use, ``verified.caller_refusal``), claims the
card and calls the mailbox client's ``answer_invite``: on Google their own
attendee entry's answer, with the organizer told (``sendUpdates=all``); on
Outlook Graph's event accept or decline (``sendResponse``). Dismiss
(``reject``) answers nothing. A sign-in that only reads the calendar keeps
reading; its card says to connect again under Read my calendar instead of
offering Accept (``can_answer``).

**On their own.** With Handle it for me on, or Take the lead as you, an
invite is accepted with no tap when plain code finds nothing against it
(``refusal``): the sender is someone they know (and, with Replies in
training, one they ticked "Accept from them on its own next time" for),
signed sign-ins or local login, nothing clashes, nothing sensitive in the
title, their sign-in can answer, and the daily limit and once-a-day-per-thread
rule of Handle it for me (``handle_it.count_refusal``) allow it. It never
declines on its own. Accepted ones are listed with the replies it sent
(``handled``: ``GET /delegation/handled``, Today's ``sent_as_you``), the
person's alone.

**Cards.** A ``delegation_invite`` decision, its person's alone
(``approver_only``) and never an alert, like a reply card. Closed once the
meeting has started or after ``CARD_TTL``. Every row it writes is private.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from email.utils import parseaddr
from typing import Any

logger = logging.getLogger(__name__)

DECISION_CLASS = "delegation_invite"
SOURCE = "invite"
CARD_TTL = timedelta(days=7)
OPEN_CARDS_MAX = 25
TITLE_MAX = 200
_CALENDAR_TIMEOUT_S = 10.0
ANSWERS = ("accept", "decline")

# What happened to an invite (delegation_inbox_messages.outcome).
CARDED = "invite_card"
ACCEPTED = "accepted"
DECLINED = "declined"

# Labels that mean the mail service filed it away.
_FILED = frozenset({"SPAM", "TRASH"})
_NO_REPLY_SENDERS = ("noreply", "no-reply", "do-not-reply", "donotreply", "mailer-daemon", "postmaster")


class AnswerRefused(Exception):
    """Why answering was refused: an HTTP status, a code and what to tell them."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass
class InviteCard:
    """One invite waiting for its person, for ``GET /delegation/invites``."""

    decision_id: int
    status: str
    created_at: str
    from_name: str
    from_email: str
    relation: str
    title: str
    when: str
    start: str
    end: str
    all_day: bool
    clashes: list[str] = field(default_factory=list)
    # False: the calendar couldn't be read for clashes.
    clash_checked: bool = True
    # False: their sign-in reads the calendar but can't answer invites.
    can_answer: bool = True
    # In training: the "Accept from them on its own next time" tick.
    can_allow: bool = False
    # Why it didn't accept on its own (Handle it for me on), in words.
    waited_because: str = ""
    connect_command: str = ""


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _now() -> datetime:
    return datetime.now(UTC)


def _moment(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _tz() -> tzinfo:
    try:
        from openexecutive.memory.workspace_settings import get_user_timezone

        return get_user_timezone()
    except Exception:
        return UTC


def when_text(start: str, end: str, all_day: bool, tz: tzinfo | None = None) -> str:
    """When the meeting is, as the person reads it: "Wed 21 Oct, 14:30–15:00",
    or "Wed 21 Oct, all day"."""
    zone = tz or _tz()
    if all_day:
        try:
            day = datetime.fromisoformat(start[:10])
        except ValueError:
            return start[:10]
        return f"{day:%a} {day.day} {day:%b}, all day"
    s, e = _moment(start), _moment(end)
    if s is None:
        return start[:25]
    s = s.astimezone(zone)
    text = f"{s:%a} {s.day} {s:%b}, {s:%H:%M}"
    if e is not None:
        e = e.astimezone(zone)
        text += f"–{e:%H:%M}" if e.date() == s.date() else f" – {e:%a} {e.day} {e:%b}, {e:%H:%M}"
    return text


def short_when(start: str, all_day: bool, tz: tzinfo | None = None) -> str:
    """The start alone: "Wed 21 Oct, 14:30"."""
    return when_text(start, start, all_day, tz).split("–")[0]


def _first_name(name: str, email: str) -> str:
    return (name or "").split()[0] if (name or "").split() else email


def _payload(card: Any) -> dict[str, Any]:
    try:
        payload = json.loads(card.proposed_payload_json)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _audit(event_type: str, summary: str, details: dict[str, Any]) -> None:
    from openexecutive.delegation.inbox import _audit as inbox_audit

    inbox_audit(event_type, summary, details)


def watching(person_id: int | None) -> bool:
    """Whether invites get cards for ``person_id``: Read my calendar on."""
    from openexecutive.delegation import calendar

    return calendar.enabled(person_id)


def skip_reason(message: Any, *, own: set[str], exec_address: str, watch_since: datetime, now: datetime) -> str | None:
    """Why an invite gets no card, or None: the watcher's own rules
    (``inbox.skip_reason``), less the ones about writing a reply. A calendar
    service sends invites with its own Sender header, so that alone isn't
    "automatic"."""
    from openexecutive.delegation.inbox import STALE, _parse

    received = _parse(message.received_at)
    if received is None or received < watch_since:
        return "before_watch"
    if now - received > STALE:
        return "stale"
    sender = message.from_addr
    if not sender:
        return "no_sender"
    if sender in own:
        return "from_you"
    if exec_address and sender == exec_address:
        return "from_executive"
    if message.ghostwritten:
        return "ours"
    if message.delivery_report or any(mark in sender.partition("@")[0] for mark in _NO_REPLY_SENDERS):
        return "automatic"
    if message.mailing_list:
        return "mailing_list"
    if set(message.labels) & _FILED:
        return "category"
    if not {*message.to, *message.cc} & own:
        return "not_addressed"
    return None


# --------------------------------------------------------------------------- #
# Cards
# --------------------------------------------------------------------------- #


def open_cards(person_id: int) -> list[Any]:
    """``person_id``'s open invite cards (waiting, or being answered), newest first."""
    from openexecutive.memory.decision_ledger import OPEN_STATUSES, list_instances

    found = [
        c for status in OPEN_STATUSES
        for c in list_instances(DECISION_CLASS, status=status, approver_person_id=person_id, limit=1000)
    ]
    return sorted(found, key=lambda c: c.created_at, reverse=True)


def _live(payload: dict[str, Any], created_at: str, now: datetime) -> bool:
    created = _moment(created_at)
    start = _moment(str(payload.get("start") or ""))
    if created is not None and now - created > CARD_TTL:
        return False
    return start is None or start > now


def create_card(key: str, payload: dict[str, Any], *, person_id: int, on_its_own: bool) -> int:
    """The card for one invite, made once per message (``key``). A card
    still open under that key is that card; one already settled (answered,
    dismissed, closed) doesn't stop a new one."""
    return create_once(
        DECISION_CLASS, key, payload, approver_person_id=person_id,
        gate_mode="auto_execute" if on_its_own else "propose",
    )


def create_once(
    decision_class: str, key: str, payload: dict[str, Any], *, approver_person_id: int, gate_mode: str,
    confidence: float | None = None,
) -> int:
    """Create a card under idempotency ``key``: the open one already under it,
    if any, else a new one, also when a settled card still holds the key (it
    is then made under the key with a fresh suffix). One scan per person at a
    time, so two new cards never race for one message."""
    from openexecutive.memory.decision_ledger import create_decision_instance, get_live_by_idem

    def make(idem: str) -> int:
        return create_decision_instance(
            decision_class=decision_class, department="", originating_session_id=None,
            proposed_payload=payload, idempotency_key=idem, gate_mode=gate_mode,
            approver_person_id=approver_person_id, confidence=confidence,
        )

    try:
        return make(key)
    except sqlite3.IntegrityError:
        existing = get_live_by_idem(key)
        if existing is not None:
            return existing.id
    return make(f"{key}:{uuid.uuid4().hex[:12]}")


def cards(person: Any, *, now: datetime | None = None) -> list[InviteCard]:
    """``person``'s invite cards still waiting, for ``GET /delegation/invites``."""
    from openexecutive.delegation import handle_it, training

    at = now or _now()
    trained = training.get(person.id).replies
    out: list[InviteCard] = []
    for card in open_cards(person.id):
        payload = _payload(card)
        if payload.get("person_id") != person.id or not _live(payload, card.created_at, at):
            continue
        sender = str(payload.get("from_email") or "")
        can_answer = payload.get("can_answer") is not False
        out.append(InviteCard(
            decision_id=card.id,
            status=card.status,
            created_at=card.created_at,
            from_name=str(payload.get("from_name") or ""),
            from_email=sender,
            relation=str(payload.get("relation") or ""),
            title=str(payload.get("title") or ""),
            when=when_text(str(payload.get("start") or ""), str(payload.get("end") or ""),
                           payload.get("all_day") is True),
            start=str(payload.get("start") or ""),
            end=str(payload.get("end") or ""),
            all_day=payload.get("all_day") is True,
            clashes=[str(c) for c in payload.get("clashes") or [] if isinstance(c, str)],
            clash_checked=payload.get("clash_checked") is not False,
            can_answer=can_answer,
            can_allow=bool(
                trained and can_answer and sender and training.allowed_inviter(person.id, sender) is None
            ),
            waited_because=handle_it.REASONS.get(str(payload.get("handle_it_reason") or ""), ""),
            connect_command=str(payload.get("connect_command") or ""),
        ))
    return out


def reconcile(person_id: int, *, now: datetime) -> int:
    """Close ``person_id``'s waiting invite cards whose meeting has started,
    or that are older than ``CARD_TTL``. Returns how many closed."""
    from openexecutive.delegation.inbox import EXPIRED, _set_outcome
    from openexecutive.memory.decision_ledger import STATUS_PROPOSED, close_externally

    closed = 0
    for card in open_cards(person_id):
        payload = _payload(card)
        if card.status != STATUS_PROPOSED or _live(payload, card.created_at, now):
            continue
        if close_externally(card.id, reason="expired"):
            _set_outcome(person_id, str(payload.get("message_id") or ""), EXPIRED, reason="expired")
            closed += 1
    return closed


def carded_threads(person_id: int) -> set[str]:
    """The threads an open invite card is for: the watcher leaves them until
    the card is settled."""
    return {str(_payload(c).get("thread_id") or "") for c in open_cards(person_id)}


# --------------------------------------------------------------------------- #
# Clashes and the rules for accepting on its own
# --------------------------------------------------------------------------- #


async def clashes(mailbox: Any, event: Any, *, tz: tzinfo | None = None) -> list[str] | None:
    """The busy times on the person's calendar that overlap ``event``, as
    "14:00–14:30" in their time zone; None when the calendar couldn't be
    read. Only start and end times are used: no other event's title or
    words leave this function."""
    from openexecutive.delegation import calendar

    if getattr(event, "all_day", False):
        return []
    start, end = _moment(event.start), _moment(event.end)
    if start is None or end is None or end <= start:
        return []
    zone = tz or _tz()
    try:
        found = await asyncio.wait_for(calendar.events_for(mailbox, start, end), timeout=_CALENDAR_TIMEOUT_S)
    except Exception as exc:
        logger.info("delegation.invites: couldn't read the calendar for clashes (%s)", type(exc).__name__)
        return None
    out: list[str] = []
    for other in found:
        other_id = str(getattr(other, "id", "") or "")
        if other_id and (other_id == event.id or other_id.startswith(f"{event.id}_")):
            continue  # the meeting itself (or one of its own occurrences)
        if getattr(other, "all_day", False) or getattr(other, "response", "") == "declined":
            continue
        s, e = _moment(getattr(other, "start", "")), _moment(getattr(other, "end", ""))
        if s is None or e is None or not (s < end and e > start):
            continue
        if s == start and e == end and getattr(other, "title", "") == getattr(event, "title", ""):
            continue  # an occurrence of this same series, under its own id
        out.append(f"{s.astimezone(zone):%H:%M}–{e.astimezone(zone):%H:%M}")
    return list(dict.fromkeys(out))


def refusal(
    person_id: int, *, sender: str, relation: str, title: str, thread_id: str, clash: list[str] | None,
    can_answer: bool, now: datetime,
) -> str | None:
    """Why this invite may not be accepted on its own (a
    ``handle_it.REASONS`` code), or None when it may. Plain code only;
    never raises (an error refuses)."""
    try:
        return _refusal(
            person_id, sender=sender, relation=relation, title=title, thread_id=thread_id, clash=clash,
            can_answer=can_answer, now=now,
        )
    except Exception:
        logger.warning("delegation.invites: the rules failed — asking instead", exc_info=True)
        return "uncountable"


def _refusal(
    person_id: int, *, sender: str, relation: str, title: str, thread_id: str, clash: list[str] | None,
    can_answer: bool, now: datetime,
) -> str | None:
    from openexecutive.delegation import handle_it, training

    settings = handle_it.get(person_id)
    if not settings.enabled:
        return "level"
    trained = handle_it.in_training(person_id, training.REPLIES)
    lead = not trained and handle_it.leading(person_id)
    if relation not in handle_it.KNOWN_RELATIONS:
        return "level"
    if trained:
        if training.allowed_inviter(person_id, sender) is None:
            return "invite_training"
    elif not lead and settings.level(handle_it.kind_for(relation)) != handle_it.LEVEL_HANDLE:
        return "level"
    if not handle_it.signing_ok():
        return "signing_off"
    if clash is None:
        return "calendar_unread"
    if clash:
        return "clash"
    if not can_answer:
        return "calendar_write"
    if handle_it.sensitive(title):
        return "sensitive"
    if lead and handle_it._lead_rule(person_id, [title], [sender], None):
        return "lead_rule"
    return handle_it.count_refusal(person_id, thread_id, now)


# --------------------------------------------------------------------------- #
# The watcher's step for one invite
# --------------------------------------------------------------------------- #


async def consider(
    person: Any, client: Any, message: Any, thread: Any, *, now: datetime, result: Any,
) -> None:
    """Handle one invite the watcher found (``inbox._consider``, after the
    skip rules): a card, an accept on its own, or skipped with a reason.
    Once claimed, it is finished or left for a later scan."""
    from openexecutive.delegation.gmail import (
        CalendarNotGranted,
        GmailAuthError,
        GmailError,
        GmailNotFound,
        GmailRateLimited,
    )
    from openexecutive.delegation.inbox import (
        PROCESSING,
        SKIPPED,
        _claim,
        _retry_later,
        _set_outcome,
        handling_relation,
        relation_of,
    )

    known_as = await relation_of(message.from_addr, client, contacts=bool(person.is_principal))
    relation = handling_relation(known_as, message)
    if relation == "stranger":
        reason = "invite_stranger" if known_as == "stranger" else "invite_unverified"
        if _claim(person.id, message, relation=relation, outcome=SKIPPED, reason=reason, now=now):
            result.skipped += 1
        return
    if len(open_cards(person.id)) >= OPEN_CARDS_MAX:
        result.deferred += 1  # not recorded: looked at again once there is room
        return
    if not _claim(person.id, message, relation=relation, outcome=PROCESSING, now=now):
        return

    def skip(reason: str) -> None:
        _set_outcome(person.id, message.id, SKIPPED, reason=reason)
        result.skipped += 1

    try:
        try:
            event = await client.find_invite(message)
        except GmailNotFound:
            event = None
        if event is None:
            skip("invite_not_found")
            return
        organizer = parseaddr(event.organizer)[1].strip().lower() if event.organizer else ""
        if organizer and organizer != message.from_addr:
            skip("invite_not_organizer")
            return
        if event.response in ("accepted", "declined", "organizer"):
            skip("invite_answered")
            return
        start = _moment(event.start)
        if start is None or start <= now:
            skip("invite_past")
            return
        tz = _tz()
        clash = await clashes(client, event, tz=tz)
        try:
            granted = await client.calendar_write_granted()
        except (GmailAuthError, GmailRateLimited):
            raise
        except GmailError:
            granted = None
    except CalendarNotGranted:
        skip("invite_calendar_not_granted")
        return
    except (GmailAuthError, GmailRateLimited):
        _retry_later(person.id, message.id, "invite_read_failed", count=False)
        raise  # the mailbox itself: the scan backs off
    except GmailError:
        # A calendar trouble only: tried again on a later scan, never a back-off for the mail.
        from openexecutive.delegation.inbox import _count_retry

        _count_retry(result, _retry_later(person.id, message.id, "invite_read_failed"))
        return
    can_answer = granted is not False
    held = refusal(
        person.id, sender=message.from_addr, relation=relation, title=event.title, thread_id=thread.id,
        clash=clash, can_answer=can_answer, now=now,
    )
    handling_on = _handle_it_on(person.id)
    payload = _card_payload(
        person, message, thread, event, relation=known_as, clash=clash, can_answer=can_answer,
        handle_it_reason=held if handling_on and held != "level" else None,
    )
    decision_id = create_card(
        f"{DECISION_CLASS}:{person.id}:{message.id}", payload, person_id=person.id, on_its_own=held is None,
    )
    _set_outcome(person.id, message.id, CARDED, decision_id=decision_id)
    result.invites += 1
    _audit("delegation_invite_carded", f"Left a meeting invite for person {person.id}", {
        "person_id": person.id, "decision_id": decision_id, "thread_id": thread.id, "relation": relation,
        "clash": bool(clash), "can_answer": can_answer,
        **({"handle_it": held or "accept"} if handling_on else {}),
    })
    if held is None and await accept_on_its_own(person, client, decision_id, now=now):
        result.handled += 1


def _handle_it_on(person_id: int) -> bool:
    from openexecutive.delegation import handle_it

    return handle_it.get(person_id).enabled


def _card_payload(
    person: Any, message: Any, thread: Any, event: Any, *, relation: str, clash: list[str] | None,
    can_answer: bool, handle_it_reason: str | None,
) -> dict[str, Any]:
    from openexecutive.delegation.ghostwriter import one_line

    payload: dict[str, Any] = {
        # A card is its person's alone (approver_only hides the class); this
        # keeps every reader that checks the payload in step.
        "private": True,
        "person_id": person.id,
        "message_id": message.id,
        "thread_id": thread.id,
        "event_id": event.id,
        "from_name": one_line(message.from_name, 120),
        "from_email": message.from_addr,
        "relation": relation,
        "sender_verified": bool(message.sender_authenticated),
        "title": one_line(event.title, TITLE_MAX),
        "start": event.start,
        "end": event.end,
        "all_day": bool(event.all_day),
        "clashes": clash or [],
        "clash_checked": clash is not None,
        "can_answer": can_answer,
    }
    if not can_answer:
        payload["connect_command"] = _connect_command(person)
    if handle_it_reason:
        payload["handle_it_reason"] = handle_it_reason
    return payload


def _connect_command(person: Any) -> str:
    """The command that connects their mailbox again with calendar access."""
    from openexecutive.delegation.gmail import credential_provider

    email = (person.email or "").strip().lower()
    if email and credential_provider(email) == "microsoft":
        return f"uv run python scripts/connect-own-outlook.py --email {email} --calendar"
    return f"uv run --with google-auth-oauthlib python scripts/connect-own-gmail.py --email {email} --calendar"


# --------------------------------------------------------------------------- #
# Answering
# --------------------------------------------------------------------------- #


async def accept_on_its_own(person: Any, client: Any, decision_id: int, *, now: datetime) -> bool:
    """Accept the invite on card ``decision_id`` under Handle it for me,
    checking the rules again first. Anything that stops it hands the card
    back to the person, saying why. True when it accepted."""
    from openexecutive.delegation import handle_it, training
    from openexecutive.delegation.gmail import CalendarWriteNotGranted, GmailError
    from openexecutive.delegation.inbox import _set_outcome
    from openexecutive.memory.decision_ledger import (
        STATUS_EXECUTED,
        claim_for_execution,
        finish_execution,
        get_decision_instance,
        hand_back,
        release_claim,
    )

    card = get_decision_instance(decision_id)
    if card is None or card.gate_mode != "auto_execute" or card.approver_person_id != person.id:
        return False
    payload = _payload(card)
    sender = str(payload.get("from_email") or "")
    held = refusal(
        person.id, sender=sender, relation=str(payload.get("relation") or ""),
        title=str(payload.get("title") or ""), thread_id=str(payload.get("thread_id") or ""),
        clash=[str(c) for c in payload.get("clashes") or []] if payload.get("clash_checked") is not False else None,
        can_answer=payload.get("can_answer") is not False, now=now,
    )
    if held is not None:
        hand_back(decision_id, {**payload, "handle_it_reason": held})
        return False
    if not claim_for_execution(decision_id, resolver_person_id=None):
        return False
    try:
        await client.answer_invite(str(payload.get("event_id") or ""), accept=True)
    except Exception as exc:
        reason = "calendar_write" if isinstance(exc, CalendarWriteNotGranted) else None
        if not isinstance(exc, GmailError):
            logger.warning("delegation.invites: accepting on its own failed (%s)", type(exc).__name__)
        # Answering is the same however often it's done, so the card simply
        # waits for the person's tap.
        release_claim(decision_id)
        back = {**payload, **({"handle_it_reason": reason} if reason else {})}
        if reason:
            back["can_answer"] = False
            back["connect_command"] = _connect_command(person)
        hand_back(decision_id, back)
        return False
    finish_execution(decision_id, STATUS_EXECUTED, final_payload={"answer": "accepted", "on_its_own": True})
    try:
        handle_it.record_handled(person.id, str(payload.get("thread_id") or ""), decision_id, None, now=now)
    except Exception:
        logger.exception("delegation.invites: recording an invite accepted on its own failed")
    allowance = training.allowed_inviter(person.id, sender)
    if allowance is not None:
        training.used(allowance)
    _set_outcome(person.id, str(payload.get("message_id") or ""), ACCEPTED, reason="handled")
    _audit("delegation_invite_handled", f"Accepted a meeting invite as person {person.id}, on its own", {
        "person_id": person.id, "decision_id": decision_id, "thread_id": payload.get("thread_id"),
    })
    return True


async def answer(
    instance: Any, *, caller: Any, resolver: int | None, answer: Any, allow: bool = False,
    mailbox: Any = None, now: datetime | None = None,
) -> str:
    """Answer ``instance``'s invite for its person, on their tap: ``accept``
    or ``decline``. ``allow`` (accept, Replies in training) also lets that
    sender's invites be accepted on their own from now on. Returns
    ``accepted`` or ``declined``; raises ``AnswerRefused``."""
    from openexecutive.delegation import calendar, training
    from openexecutive.delegation.gmail import (
        BLOCKING_CODES,
        STATUS_MESSAGES,
        CalendarNotGranted,
        CalendarWriteNotGranted,
        GmailError,
        GmailNotFound,
        gmail_for,
        gmail_status,
        normalize_email,
    )
    from openexecutive.delegation.inbox import CLOSED, _set_outcome
    from openexecutive.delegation.settings import can_delegate, is_enabled
    from openexecutive.delegation.verified import NOT_YOURS, caller_refusal
    from openexecutive.memory.decision_ledger import (
        STATUS_APPROVED_UNCHANGED,
        STATUS_FAILED,
        claim_for_execution,
        finish_execution,
        release_claim,
        update_open_payload,
    )
    from openexecutive.orchestrator import take_the_lead
    from openexecutive.people.store import get_person

    at = now or _now()
    payload = _payload(instance)
    person_id = payload.get("person_id")
    person = get_person(person_id) if isinstance(person_id, int) else None
    if person is None or person.id is None or person.archived or person.id != instance.approver_person_id:
        raise AnswerRefused(404, "gone", "This invite is no longer here.")
    email = normalize_email(person.email or "")
    refused = caller_refusal(caller, email)
    if refused == NOT_YOURS:
        raise AnswerRefused(403, "not_yours", "Only the person this invite is for can answer it.")
    if refused is not None:
        raise AnswerRefused(
            409, "caller_signing_required",
            "Answering needs signed sign-ins on this server, so that nobody else can answer for you.",
        )
    if answer not in ANSWERS:
        raise AnswerRefused(422, "bad_answer", "Accept or decline.")
    if not can_delegate(person) or not is_enabled(person.id):
        raise AnswerRefused(409, "act_as_me_off", "Act as me is off, so this invite can't be answered from here.")
    if not calendar.enabled(person.id):
        raise AnswerRefused(409, "calendar_off", "Read my calendar is off, so this invite can't be answered from here.")
    if not _live(payload, instance.created_at, at):
        raise AnswerRefused(409, "expired", "This meeting has started or the card is too old. Answer it in your calendar.")
    sender = str(payload.get("from_email") or "")
    accept = answer == "accept"
    if allow:
        if not accept or not training.get(person.id).replies:
            raise AnswerRefused(
                422, "cant_allow", "Accepting on its own next time is for invites you accept while Replies is in training.",
            )
        if not training.room_for(person.id, training.inviter_key(person.id, sender)):
            raise AnswerRefused(409, "learned_full", "It has learned as much as it can hold. Remove something first.")
    client = mailbox if mailbox is not None else gmail_for(email)
    status = await gmail_status(email, gmail=client)
    if status != "connected":
        raise AnswerRefused(409, BLOCKING_CODES.get(status, "gmail_error"), STATUS_MESSAGES.get(status, ""))
    if not claim_for_execution(instance.id, resolver_person_id=resolver):
        raise AnswerRefused(409, "already_handled", "This invite was already answered.")
    try:
        await client.answer_invite(str(payload.get("event_id") or ""), accept=accept)
    except (CalendarWriteNotGranted, CalendarNotGranted) as exc:
        release_claim(instance.id)
        update_open_payload(instance.id, {**payload, "can_answer": False, "connect_command": _connect_command(person)})
        write = isinstance(exc, CalendarWriteNotGranted)
        raise AnswerRefused(
            409, "calendar_write_needed" if write else "calendar_not_granted",
            "Your calendar sign-in can read but not answer invites. Connect it again under Read my calendar, then try again."
            if write else "Your saved sign-in doesn't include your calendar any more. Connect it again under Read my calendar.",
        ) from None
    except GmailNotFound:
        finish_execution(instance.id, STATUS_FAILED, final_payload={"answer": answer, "error": "not_found"})
        _set_outcome(person.id, str(payload.get("message_id") or ""), CLOSED, reason="invite_gone")
        raise AnswerRefused(409, "gone", "This meeting isn't on your calendar any more.") from None
    except GmailError:
        # Answering again is harmless, so the card waits for another try.
        release_claim(instance.id)
        raise AnswerRefused(502, "calendar_error", "Couldn't reach your calendar. Try again in a moment.") from None
    except Exception:
        logger.exception("delegation.invites: answering failed")
        release_claim(instance.id)
        raise AnswerRefused(502, "calendar_error", "Couldn't reach your calendar. Try again in a moment.") from None
    done = ACCEPTED if accept else DECLINED
    finish_execution(instance.id, STATUS_APPROVED_UNCHANGED, final_payload={"answer": done})
    _set_outcome(person.id, str(payload.get("message_id") or ""), done, reason="tapped")
    _audit("delegation_invite_answered", f"Person {person.id} {done} a meeting invite", {
        "person_id": person.id, "decision_id": instance.id, "answer": done, "allowed": allow,
    })
    if allow:
        try:
            found = training.allow_inviter(person.id, sender, str(payload.get("from_name") or ""), decision_id=instance.id)
        except take_the_lead.RuleError:
            logger.warning("delegation.invites: accepted, but the list filled up before allowing")
        else:
            _audit("delegation_reply_allowed", f"Person {person.id} allowed invites in training", {
                "person_id": person.id, "decision_id": instance.id, "allowed_id": found.id, "invites": True,
            })
    return done


async def dismissed(instance: Any) -> None:
    """After a card was dismissed: nothing answered, the audit row and the
    watcher's record."""
    from openexecutive.delegation.inbox import DISMISSED, _set_outcome

    payload = _payload(instance)
    person_id = payload.get("person_id")
    if isinstance(person_id, int):
        _set_outcome(person_id, str(payload.get("message_id") or ""), DISMISSED, reason="dismissed")
    _audit("delegation_invite_dismissed", f"Person {person_id} dismissed a meeting invite card", {
        "person_id": person_id, "decision_id": instance.id,
    })


# --------------------------------------------------------------------------- #
# Done for you
# --------------------------------------------------------------------------- #


def handled(person_id: int, *, since: str, limit: int = 50) -> list[Any]:
    """``person_id``'s invites accepted on their own since ``since``, as
    ``handle_it.HandledReply`` rows (``source`` ``invite``), theirs alone."""
    from openexecutive.delegation.handle_it import HandledReply
    from openexecutive.memory.decision_ledger import STATUS_EXECUTED, list_instances

    out: list[Any] = []
    tz = _tz()
    for card in list_instances(
        DECISION_CLASS, status=STATUS_EXECUTED, approver_person_id=person_id, resolved_since=since, limit=limit,
    ):
        payload = _payload(card)
        if payload.get("person_id") != person_id:
            continue
        name = str(payload.get("from_name") or "")
        sender = str(payload.get("from_email") or "")
        title = str(payload.get("title") or "")
        when = short_when(str(payload.get("start") or ""), payload.get("all_day") is True, tz)
        out.append(HandledReply(
            decision_id=card.id,
            sent_at=card.resolved_at or card.created_at,
            to_name=name,
            to_email=sender,
            subject=title,
            body="",
            open_questions=[],
            thread_id=str(payload.get("thread_id") or ""),
            source=SOURCE,
            summary=f"Accepted {_first_name(name, sender)}'s {title or 'meeting'}, {when}",
        ))
    return out


__all__ = [
    "ANSWERS", "DECISION_CLASS", "AnswerRefused", "InviteCard", "answer", "cards", "clashes", "consider",
    "create_once", "dismissed", "handled", "reconcile", "refusal", "skip_reason", "watching", "when_text",
]
