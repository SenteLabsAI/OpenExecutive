"""Approval cards: actions the Executive suggests on a turn about someone's
mail, done only when that person taps Approve (``orchestrator.action_card_tools``).

A turn that read someone's mail may not message anyone, invite anyone or
change the roster (``delegation.lockdown``): text in the mail could otherwise
steer it. What the mail asks for is still often right, so the Executive
leaves it on a card instead: each action spelled out exactly as it will
happen, for the person to approve with one tap.

**What a card holds.** Up to ``MAX_ACTIONS`` actions of three kinds, each
checked when it is proposed and again when it is carried out:

- ``invite``: a meeting with people on the roster (``create_calendar_event``).
  Approving books it when the person is also the one the meeting gate would
  ask (``calendar_tools.approved_by``); otherwise it goes to that approver.
- ``message``: a direct message to someone on the roster (``message_person``).
- ``add_contact``: a new contact for the principal; their cards only, since
  contacts are the principal's.

**Whose it is.** A card is a ``delegation_actions`` decision for the person
who was speaking, theirs alone (``approver_only``: not even the principal sees
a team member's) and never an alert, like a reply card. It is approved on the
web, by that person signed in (``verified.caller_refusal``), and carried out
exactly as stored, with no model call: claimed first, so it runs once.

**Limits.** ``CARDS_PER_TURN`` a turn and ``OPEN_CARDS_MAX`` waiting a
person; a card is dropped after ``CARD_TTL``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

DECISION_CLASS = "delegation_actions"
KINDS: tuple[str, ...] = ("invite", "message", "add_contact")
MAX_ACTIONS = 5
CARDS_PER_TURN = 3
OPEN_CARDS_MAX = 25
CARD_TTL = timedelta(days=7)
MESSAGE_MAX = 2000
TITLE_MAX = 200
DESCRIPTION_MAX = 2000
NAME_MAX = 120
WHY_MAX = 300
MAX_ATTENDEES = 20
HORIZON = timedelta(days=366)

_EMAIL_RE = re.compile(r"^[^@\s<>,;\"']+@[^@\s<>,;\"']+\.[^@\s<>,;\"']+$")


class ApproveRefused(Exception):
    """Why an approval was refused: an HTTP status, a code and what to tell them."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass
class CardAction:
    index: int
    kind: str
    summary: str
    # The text as it will be sent (a message) or the meeting's description.
    text: str = ""


@dataclass
class ActionCard:
    decision_id: int
    status: str
    created_at: str
    why: str
    actions: list[CardAction] = field(default_factory=list)


def _one_line(value: Any, limit: int) -> str:
    from openexecutive.utils.prompt_blocks import plain

    return " ".join(plain(str(value or "")).split())[:limit].strip()


def _text(value: Any, limit: int) -> str:
    from openexecutive.utils.prompt_blocks import plain

    return plain(str(value or "")).strip()[:limit]


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _when(value: Any) -> datetime | None:
    """An aware time, read in the workspace's timezone when it has none."""
    from openexecutive.memory.workspace_settings import get_user_timezone

    try:
        parsed = datetime.fromisoformat(str(value or "").strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=get_user_timezone())


def _shown(start: datetime, end: datetime) -> str:
    """When a meeting is, in the workspace's timezone: "Thu Oct 9, 14:00–14:30"."""
    from openexecutive.memory.workspace_settings import get_user_timezone

    zone = get_user_timezone()
    local, until = start.astimezone(zone), end.astimezone(zone)
    return f"{local:%a %b} {local.day}, {local:%H:%M}–{until:%H:%M}"


def _roster_person(person_id: int | None, *, contacts: bool) -> Any:
    """A live roster entry by id; a contact only on the principal's card."""
    from openexecutive.people.store import get_person

    if person_id is None:
        return None
    person = get_person(person_id)
    if person is None or person.archived:
        return None
    if person.kind != "team" and not contacts:
        return None
    return person


def _check_message(action: dict[str, Any], speaker: Any) -> dict[str, Any] | str:
    pid = _int(action.get("person_id"))
    person = _roster_person(pid, contacts=bool(speaker.is_principal))
    if person is None:
        return "message: person_id must be someone on the roster (lookup_person)."
    if person.id == speaker.id:
        return "message: that is the person you are speaking with; tell them here instead."
    text = _text(action.get("text"), MESSAGE_MAX + 1)
    if not text:
        return "message: pass `text`, the message as it will be sent."
    if len(text) > MESSAGE_MAX:
        return f"message: keep `text` to {MESSAGE_MAX} characters."
    return {
        "kind": "message", "input": {"person_id": person.id, "text": text},
        "summary": f"Message {_one_line(person.full_name, NAME_MAX)}", "text": text,
    }


def _check_invite(action: dict[str, Any], speaker: Any, now: datetime) -> dict[str, Any] | str:
    title = _one_line(action.get("title"), TITLE_MAX + 1)
    if not title or len(title) > TITLE_MAX:
        return f"invite: pass a `title` of at most {TITLE_MAX} characters."
    start, end = _when(action.get("start")), _when(action.get("end"))
    if start is None or end is None:
        return "invite: `start` and `end` must be dates and times like 2026-10-10T14:00."
    if start <= now:
        return "invite: that start time has passed."
    if end <= start:
        return "invite: `end` must be after `start`."
    if start > now + HORIZON:
        return "invite: that is more than a year away."
    raw_ids = action.get("attendee_person_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        return "invite: pass `attendee_person_ids`, people on the roster."
    if len(raw_ids) > MAX_ATTENDEES:
        return f"invite: at most {MAX_ATTENDEES} people."
    names: list[str] = []
    ids: list[int] = []
    include_principal = False
    for raw in raw_ids:
        person = _roster_person(_int(raw), contacts=bool(speaker.is_principal))
        if person is None or not person.email:
            return f"invite: {raw!r} is not someone on the roster with an email (lookup_person)."
        if person.id in ids:
            continue
        include_principal = include_principal or bool(person.is_principal)
        ids.append(person.id)
        names.append(_one_line(person.full_name, NAME_MAX))
    description = _text(action.get("description"), DESCRIPTION_MAX)
    tool_input: dict[str, Any] = {
        "title": title, "start": start.isoformat(), "end": end.isoformat(),
        "attendee_person_ids": ids, "include_principal": include_principal,
        "description": description, "confidence": 1.0,
    }
    return {
        "kind": "invite", "input": tool_input,
        "summary": f"Invite {', '.join(names)}: \"{title}\", {_shown(start, end)}",
        "text": description,
    }


def _check_contact(action: dict[str, Any], speaker: Any) -> dict[str, Any] | str:
    from openexecutive.delegation.gmail import normalize_email
    from openexecutive.people.store import find_person_by_email

    if not speaker.is_principal:
        return "add_contact: contacts are the owner's; only they can add one."
    name = _one_line(action.get("full_name"), NAME_MAX + 1)
    if not name or len(name) > NAME_MAX:
        return f"add_contact: pass `full_name`, at most {NAME_MAX} characters."
    email = normalize_email(str(action.get("email") or ""))
    if not _EMAIL_RE.match(email):
        return "add_contact: pass a valid `email`."
    if find_person_by_email(email, include_contacts=True) is not None:
        return f"add_contact: {email} is already on the roster."
    role = _one_line(action.get("role"), NAME_MAX)
    shown = f"Add contact {name}, {email}" + (f" ({role})" if role else "")
    return {
        "kind": "add_contact", "input": {"full_name": name, "email": email, "role": role},
        "summary": shown, "text": "",
    }


def check(actions: Any, speaker: Any, *, now: datetime) -> list[dict[str, Any]] | str:
    """``actions`` as they will be stored, or why they can't be."""
    if not isinstance(actions, list) or not actions:
        return "Pass `actions`: at least one."
    if len(actions) > MAX_ACTIONS:
        return f"At most {MAX_ACTIONS} actions on a card."
    out: list[dict[str, Any]] = []
    for action in actions:
        if not isinstance(action, dict):
            return "Each action must be an object with a `kind`."
        kind = action.get("kind")
        checked: dict[str, Any] | str
        if kind == "message":
            checked = _check_message(action, speaker)
        elif kind == "invite":
            checked = _check_invite(action, speaker, now)
        elif kind == "add_contact":
            checked = _check_contact(action, speaker)
        else:
            return f"`kind` must be one of {', '.join(KINDS)}."
        if isinstance(checked, str):
            return checked
        out.append(checked)
    return out


def _payload(card: Any) -> dict[str, Any]:
    try:
        payload = json.loads(card.proposed_payload_json)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _live(card: Any, now: datetime) -> bool:
    try:
        created = datetime.fromisoformat(card.created_at)
    except (TypeError, ValueError):
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return now - created <= CARD_TTL


def open_cards(person_id: int, *, now: datetime | None = None) -> list[Any]:
    """``person_id``'s cards still waiting, newest first."""
    from openexecutive.memory.decision_ledger import STATUS_PROPOSED, list_instances

    at = now or datetime.now(UTC)
    found = list_instances(DECISION_CLASS, status=STATUS_PROPOSED, approver_person_id=person_id, limit=1000)
    return sorted((c for c in found if _live(c, at)), key=lambda c: c.created_at, reverse=True)


def propose(speaker: Any, actions: list[dict[str, Any]], why: str, *, session_id: str | None) -> int:
    """Store a card for ``speaker`` (``actions`` already ``check``ed); returns
    its decision id. The same actions still waiting are that card."""
    from openexecutive.memory.decision_ledger import create_decision_instance

    digest = hashlib.sha256(
        json.dumps([speaker.id, [[a["kind"], a["input"]] for a in actions]], sort_keys=True).encode()
    ).hexdigest()[:32]
    for card in open_cards(speaker.id):
        if _payload(card).get("digest") == digest:
            return int(card.id)
    payload = {
        # A card is its person's alone (approver_only hides the class); this
        # keeps every reader that checks the payload in step, as reply cards do.
        "private": True,
        "person_id": speaker.id, "why": _one_line(why, WHY_MAX), "digest": digest, "actions": actions,
    }
    return create_decision_instance(
        decision_class=DECISION_CLASS,
        department="executive",
        originating_session_id=session_id,
        proposed_payload=payload,
        idempotency_key=f"{DECISION_CLASS}:{digest}:{uuid.uuid4().hex[:12]}",
        gate_mode="propose",
        approver_person_id=speaker.id,
        confidence=None,
    )


def cards(person: Any) -> list[ActionCard]:
    """``person``'s waiting cards, for ``GET /delegation/actions``."""
    out: list[ActionCard] = []
    for card in open_cards(person.id):
        payload = _payload(card)
        listed = payload.get("actions")
        actions: list[Any] = listed if isinstance(listed, list) else []
        out.append(ActionCard(
            decision_id=card.id, status=card.status, created_at=card.created_at,
            why=str(payload.get("why") or ""),
            actions=[
                CardAction(index=i, kind=str(a.get("kind") or ""), summary=str(a.get("summary") or ""),
                           text=str(a.get("text") or ""))
                for i, a in enumerate(actions) if isinstance(a, dict)
            ],
        ))
    return out


def audit(event: str, summary: str, details: dict[str, Any]) -> None:
    from openexecutive.audit import log_event

    try:
        log_event(event, summary, actor="executive", details=details, private=True,
                  private_to_person=_int(details.get("person_id")))
    except Exception:
        logger.warning("action_cards: couldn't write the audit row", exc_info=True)


def _result(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def _do(action: dict[str, Any], person: Any) -> dict[str, str]:
    """Carry out one stored action, checked again first. Never raises."""
    kind = str(action.get("kind") or "")
    given = action.get("input")
    stored: dict[str, Any] = given if isinstance(given, dict) else {}
    try:
        # Checked again: the roster or the time may have changed since.
        checked = check([{"kind": kind, **stored}], person, now=datetime.now(UTC))
        if isinstance(checked, str):
            return {"status": "failed", "detail": checked.split(": ", 1)[-1]}
        tool_input = checked[0]["input"]
        if kind == "message":
            from openexecutive.orchestrator.schedule_tools import message_person

            # A direct message or nothing: never the Briefing alert
            # message_person falls back to, which more people than them read.
            result = _result(await message_person(tool_input, alert_fallback=False))
            if result.get("status") == "sent":
                return {"status": "done", "detail": "Sent."}
            return {"status": "failed", "detail": str(result.get("error") or result.get("reason") or "Not sent.")[:300]}
        if kind == "invite":
            from openexecutive.orchestrator.calendar_tools import (
                approved_by,
                handle_create_calendar_event,
            )

            with approved_by(person.id):
                result = _result(await handle_create_calendar_event(tool_input))
            if result.get("status") == "created":
                return {"status": "done", "detail": "Booked."}
            if result.get("status") in ("proposed", "already_proposed"):
                return {"status": "waiting", "detail": "Sent to whoever approves meetings."}
            return {"status": "failed", "detail": str(result.get("error") or "Not booked.")[:300]}
        if kind == "add_contact":
            from openexecutive.people import registry as people_registry
            from openexecutive.people import store as people_store

            people_store.upsert_person(
                full_name=tool_input["full_name"], email=tool_input["email"],
                role=tool_input.get("role") or "", kind="contact",
            )
            people_registry.invalidate()
            return {"status": "done", "detail": "Added."}
    except Exception:
        logger.exception("action_cards: a %s action failed", kind)
        return {"status": "failed", "detail": "Something went wrong, so it may not have happened. Check before trying again."}
    return {"status": "failed", "detail": "Unknown action."}


async def approve(instance: Any, *, caller: Any, resolver: int | None, only: Any = None) -> list[dict[str, Any]]:
    """Carry out ``instance``'s actions (those in ``only`` when given) for
    its person, once. Raises ``ApproveRefused``."""
    from contextlib import nullcontext

    from openexecutive.audit import rows_for_person
    from openexecutive.delegation.gmail import normalize_email
    from openexecutive.delegation.settings import can_delegate, is_enabled
    from openexecutive.delegation.verified import NOT_YOURS, caller_refusal
    from openexecutive.memory.decision_ledger import (
        STATUS_APPROVED_UNCHANGED,
        STATUS_APPROVED_WITH_EDIT,
        STATUS_FAILED,
        claim_for_execution,
        finish_execution,
    )
    from openexecutive.orchestrator.people_tools import grant_contact_egress
    from openexecutive.orchestrator.schedule_tools import current_session
    from openexecutive.orchestrator.session import Session
    from openexecutive.people.store import get_person

    payload = _payload(instance)
    person = get_person(_int(payload.get("person_id")) or 0)
    if person is None or person.id is None or person.archived or person.id != instance.approver_person_id:
        raise ApproveRefused(404, "gone", "This card is no longer there.")
    refused = caller_refusal(caller, normalize_email(person.email or ""))
    if refused == NOT_YOURS:
        raise ApproveRefused(403, "not_yours", "Only the person this card is for can approve it.")
    if refused is not None:
        raise ApproveRefused(
            409, "caller_signing_required",
            "Approving needs signed sign-ins on this server, so that nobody else can approve it for you.",
        )
    if not can_delegate(person) or not is_enabled(person.id):
        raise ApproveRefused(409, "act_as_me_off", "Act as me is off, so this card can't be approved.")
    if not _live(instance, datetime.now(UTC)):
        raise ApproveRefused(409, "expired", "This card is more than a week old. Ask again if it still needs doing.")
    actions = [a for a in payload.get("actions") or [] if isinstance(a, dict)]
    chosen = set(range(len(actions)))
    if only is not None:
        if not isinstance(only, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in only):
            raise ApproveRefused(422, "bad_selection", "Pick the actions to do by their number.")
        chosen &= set(only)
        if not chosen:
            raise ApproveRefused(422, "bad_selection", "Pick at least one action.")
    if not claim_for_execution(instance.id, resolver_person_id=resolver):
        raise ApproveRefused(409, "already_handled", "This card was already handled.")
    results: list[dict[str, Any]] = []
    token = current_session.set(Session(unattended=True))
    try:
        # What the actions say came from their mail: every row the handlers
        # write (audit, the activity rail) is theirs alone, as on their own
        # turn. Only the principal reaches their contacts.
        with rows_for_person(person.id), grant_contact_egress() if person.is_principal else nullcontext():
            for i, action in enumerate(actions):
                if i not in chosen:
                    results.append({"index": i, "status": "skipped", "detail": "Left out."})
                    continue
                results.append({"index": i, **await _do(action, person)})
    finally:
        current_session.reset(token)
    any_ok = any(r["status"] in ("done", "waiting") for r in results)
    status = (STATUS_APPROVED_UNCHANGED if len(chosen) == len(actions) else STATUS_APPROVED_WITH_EDIT) if any_ok else STATUS_FAILED
    finish_execution(instance.id, status, final_payload={**payload, "results": results})
    audit("delegation_actions_approved", f"Person {person.id} approved an action card", {
        "person_id": person.id, "decision_id": instance.id,
        "outcomes": [f"{actions[r['index']].get('kind')}:{r['status']}" for r in results],
    })
    return results


async def dismissed(instance: Any) -> None:
    """After a card was dismissed: the audit row (nothing to undo)."""
    payload = _payload(instance)
    audit("delegation_actions_dismissed", f"Person {payload.get('person_id')} dismissed an action card", {
        "person_id": payload.get("person_id"), "decision_id": instance.id,
    })
