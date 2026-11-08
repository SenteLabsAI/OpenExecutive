"""Handle it for me: the inbox watcher sends some replies on its own.

**The switch.** A third switch under Act as me, after "Draft replies to my
inbox" (``delegation_handle_it``, one row per person, absent means off). It
has no dial of its own: Approvals decides (``orchestrator.outside_mode``,
``approvals_hold``). On Careful every reply waits on a card, since any
outside read waits there; on Balanced and Hands-off a reply goes on its own
unless one of the checks below holds it; below Hands-off that includes a
reply to someone they don't know (``stranger``). On every level the last
look (``injection_check``) holds anything it flags.

Every other email waits on a card for the person to tap Send, as it did
before. Only the person turns it on, from a session the API knows is theirs
(``reply_send._check_caller``, the rule Send uses), and nobody else can.

**Who decides.** No model decides whether a reply goes. The inbox watcher's
two model calls (``inbox_classifier`` and the ghostwriter) read the email and
have no tools: one returns a verdict, the other a reply. Text in the email can
at most change those two answers. Whether the reply is sent is this module's
plain code (``refusal``), checked again at send time
(``reply_send.send_on_its_own``). Anything it refuses becomes the usual card,
with the reason recorded.

**What it refuses.** Every one of these keeps the reply on a card:

- it's off, or Approvals is on Careful, or signed sign-ins are off on a
  server (nothing would tie the switch to the person);
- Gmail couldn't authenticate the sender;
- the classifier wasn't sure enough (``MIN_CONFIDENCE``), or the email
  isn't one of the kinds that need a reply;
- the reply goes to anyone the email didn't already go to;
- the reply is longer than ``MAX_BODY_CHARS``;
- the draft carries a flag other than ``ALLOWED_FLAGS`` (they asked whether
  it's an AI, recipients were trimmed, the draft names the Executive
  (``names_the_executive``), ...). The Executive merely being on the email
  (``executive_on_thread``) is fine: it is never a recipient of the reply;
- it already sent one in this thread in the last day, or the last thing the
  person "said" in this thread was itself sent on its own;
- the day's limit (``DELEGATION_HANDLE_IT_MAX_SENDS_PER_DAY``) is reached;
- after all of these pass, the last look (``injection_check``) finds the
  email trying to steer an assistant, or can't tell.

Never sent on its own: anything with an attachment (the watcher's drafts
have none) or a new recipient.

**Meeting invites.** It also accepts a meeting invite on its own
(``delegation.invites``, never a decline) from someone the person knows,
when nothing on their calendar clashes and their sign-in can answer
invites; it counts against the same daily limit and once-a-day-per-thread
rule (``count_refusal``), and is listed with the replies (``handled``).

**After.** A reply sent on its own is a ``delegation_reply`` decision in
status ``executed`` with ``gate_mode`` ``auto_execute``; ``handled`` lists the
person's recent ones (``GET /delegation/handled``), theirs alone, with the
questions the reply left for them to answer. No History note is taken from it:
those come from the person's own words only.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from openexecutive.delegation.schema import HANDLE_IT_TABLE, HANDLED_TABLE, ensure_schema

logger = logging.getLogger(__name__)

KIND_REPLY_KNOWN = "reply_known"
KIND_REPLY_STRANGER = "reply_stranger"
KINDS: tuple[str, ...] = (KIND_REPLY_KNOWN, KIND_REPLY_STRANGER)

LEVEL_ASK = "ask"
LEVEL_HANDLE = "handle"

# Kept in the table's ``mode`` column by older installs; read by nothing.
DEFAULT_MODE = "balanced"

# How sure the classifier must be that a simple reply is all it needs.
MIN_CONFIDENCE = 0.8
# The longest reply or follow-up it sends on its own.
MAX_BODY_CHARS = 4000
# A follow-up is a nudge: it stays short.
FOLLOW_UP_MAX_CHARS = 600

KNOWN_RELATIONS = frozenset({"team", "contact", "correspondent"})
# The classifier kinds a reply may be sent for on its own.
REPLY_KINDS = frozenset({"question", "request", "scheduling", "introduction", "follow_up"})
THREAD_WINDOW = timedelta(days=1)
# Draft flags a reply may carry and still be sent on its own.
ALLOWED_FLAGS = frozenset({"others_on_thread", "executive_on_thread"})

# What each refusal tells the person on the card.
REASONS: dict[str, str] = {
    "level": "Approvals is on Careful, so what it would send as you waits for you.",
    "off": "Handle it for me is off.",
    "signing_off": "Sending on its own needs signed sign-ins on this server.",
    "sender_unverified": "Your mail service couldn't confirm who sent it.",
    "unsure": "It wasn't sure enough this needs only a simple reply.",
    "kind": "It isn't the kind of email it answers on its own.",
    "recipients": "The reply would go to someone the email didn't.",
    "long": "The reply is longer than the ones it sends on its own.",
    "flagged": "Something about the draft needs your eye.",
    "thread_recent": "It already replied on its own in this conversation today.",
    "in_a_row": "Its last message in this conversation was sent on its own too.",
    "daily_limit": "Today's limit of replies sent on its own is reached.",
    "uncountable": "It couldn't count today's replies, so it asked instead.",
    # Found by the last look before it sends (delegation.injection_check).
    "suspicious": "The email reads like it's trying to give an assistant instructions.",
    # Found just before sending (reply_send.send_on_its_own).
    "draft_changed": "You changed the draft in your mailbox, so it's yours to send.",
    "handle_it_off": "Handle it for me was off by the time it came to send.",
    # Meeting invites (delegation.invites).
    "stranger": "Below Hands-off it acts on its own only for people you know.",
    "clash": "It clashes with something already on your calendar.",
    "calendar_unread": "It couldn't check your calendar for clashes, so it asked instead.",
    "calendar_write": "Your calendar sign-in can't answer invites yet. Connect it again under Read my calendar.",
}


def hands_off(*, db_path: Path | None = None) -> bool:
    """Whether Approvals is on Hands-off. Never raises: unreadable is no."""
    try:
        from openexecutive.orchestrator import outside_mode

        return outside_mode.get_mode(db_path=db_path) == outside_mode.TRUST
    except Exception:
        logger.warning("delegation.handle_it: couldn't read Approvals — not Hands-off", exc_info=True)
        return False


def approvals_hold(*, db_path: Path | None = None) -> bool:
    """Whether Approvals holds what it would send as someone on its own:
    Careful does (any outside read waits there). Never raises: unreadable
    holds."""
    try:
        from openexecutive.orchestrator import outside_mode

        return outside_mode.get_mode(db_path=db_path) == outside_mode.ASK
    except Exception:
        logger.warning("delegation.handle_it: couldn't read Approvals — asking", exc_info=True)
        return True


@dataclass
class HandleIt:
    person_id: int
    enabled: bool = False


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #


def _db(db_path: Path | None) -> Path:
    if db_path is not None:
        return db_path
    from openexecutive.memory import episodic

    return Path(episodic.DB_PATH)


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db(db_path)))
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def get(person_id: int, *, db_path: Path | None = None) -> HandleIt:
    """``person_id``'s switch. Never raises: unreadable is off."""
    try:
        conn = _connect(db_path)
        try:
            row = conn.execute(
                f"SELECT enabled FROM {HANDLE_IT_TABLE} WHERE person_id = ?",  # noqa: S608 — constant table name
                (person_id,),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        logger.warning("delegation.handle_it: couldn't read the switch — treating it as off", exc_info=True)
        return HandleIt(person_id=person_id)
    if row is None:
        return HandleIt(person_id=person_id)
    return HandleIt(person_id=person_id, enabled=bool(row["enabled"]))


def set_(
    person_id: int,
    *,
    enabled: bool | None = None,
    updated_by: str,
    db_path: Path | None = None,
) -> HandleIt:
    """Change ``person_id``'s switch (callers authorize first)."""
    current = get(person_id, db_path=db_path)
    new_enabled = current.enabled if enabled is None else enabled
    now = datetime.now(UTC).isoformat()
    conn = _connect(db_path)
    try:
        conn.execute(
            f"INSERT INTO {HANDLE_IT_TABLE} (person_id, enabled, mode, updated_at, updated_by) "  # noqa: S608
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(person_id) DO UPDATE SET enabled = excluded.enabled, "
            "updated_at = excluded.updated_at, updated_by = excluded.updated_by",
            (person_id, 1 if new_enabled else 0, DEFAULT_MODE, now, updated_by),
        )
        conn.commit()
    finally:
        conn.close()
    return get(person_id, db_path=db_path)


def record_handled(
    person_id: int, thread_id: str, decision_id: int | None, sent_message_id: str | None, *,
    now: datetime, db_path: Path | None = None,
) -> None:
    conn = _connect(db_path)
    try:
        conn.execute(
            f"INSERT INTO {HANDLED_TABLE} (person_id, thread_id, decision_id, sent_message_id, sent_at) "  # noqa: S608
            "VALUES (?, ?, ?, ?, ?)",
            (person_id, thread_id, decision_id, sent_message_id, now.isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def _handled_rows(person_id: int, sql: str, params: tuple[Any, ...], db_path: Path | None) -> list[sqlite3.Row]:
    conn = _connect(db_path)
    try:
        return list(conn.execute(
            f"SELECT * FROM {HANDLED_TABLE} WHERE person_id = ? {sql}",  # noqa: S608 — constant table name
            (person_id, *params),
        ).fetchall())
    finally:
        conn.close()


def sent_today(person_id: int, now: datetime, *, db_path: Path | None = None) -> int:
    day = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return len(_handled_rows(person_id, "AND sent_at >= ?", (day.isoformat(),), db_path))


# --------------------------------------------------------------------------- #
# The rules
# --------------------------------------------------------------------------- #


def kind_for(relation: str) -> str:
    """The kind of reply this is, from the sender's handling relation
    (``inbox.handling_relation``: an unauthenticated sender is a stranger)."""
    return KIND_REPLY_KNOWN if relation in KNOWN_RELATIONS else KIND_REPLY_STRANGER


def signing_ok() -> bool:
    """Whether the API can tie the switch to the person: signed callers, or
    an API that answers this computer only. The rule Send uses."""
    from openexecutive.api.caller import signing_on
    from openexecutive.utils.deployment import is_local_login

    try:
        return bool(signing_on() or is_local_login())
    except Exception:
        return False


def _own_messages(thread: Any, own: set[str]) -> list[Any]:
    return [
        m for m in getattr(thread, "messages", []) or []
        if getattr(m, "from_addr", "") in own and "DRAFT" not in (getattr(m, "labels", None) or [])
    ]


def refusal(
    person_id: int,
    message: Any,
    thread: Any,
    reply: Any,
    verdict: Any,
    *,
    relation: str,
    own: set[str],
    exec_address: str,
    now: datetime,
    db_path: Path | None = None,
) -> str | None:
    """Why ``reply`` may not be sent on its own (a ``REASONS`` code), or None
    when it may. Plain code only; never raises (an error refuses)."""
    try:
        return _refusal(
            person_id, message, thread, reply, verdict, relation=relation, own=own,
            exec_address=exec_address, now=now, db_path=db_path,
        )
    except Exception:
        logger.warning("delegation.handle_it: the rules failed — asking instead", exc_info=True)
        return "uncountable"


def _refusal(
    person_id: int, message: Any, thread: Any, reply: Any, verdict: Any, *, relation: str, own: set[str],
    exec_address: str, now: datetime, db_path: Path | None,
) -> str | None:
    from openexecutive.delegation.threads import MAX_RECIPIENTS

    settings = get(person_id, db_path=db_path)
    if not settings.enabled:
        return "off"
    if approvals_hold(db_path=db_path):
        return "level"
    # Below Hands-off, a reply to someone they don't know waits on a card, as
    # an invite from one does (delegation.invites).
    if kind_for(relation) != KIND_REPLY_KNOWN and not hands_off(db_path=db_path):
        return "stranger"
    if not signing_ok():
        return "signing_off"
    if getattr(message, "sender_authenticated", False) is not True:
        return "sender_unverified"
    if verdict is None or verdict.kind not in REPLY_KINDS:
        return "kind"
    if float(verdict.confidence) < MIN_CONFIDENCE:
        return "unsure"

    # Exactly the people the email already went to, minus the person and the
    # Executive: the sender first, then everyone else on it.
    allowed = {message.from_addr, *message.to, *message.cc} - own - ({exec_address} if exec_address else set())
    going = [*reply.to, *reply.cc]
    if not going or len(going) > MAX_RECIPIENTS or not set(going) <= allowed or message.from_addr not in reply.to:
        return "recipients"
    if len(reply.body or "") > MAX_BODY_CHARS:
        return "long"
    if any(f not in ALLOWED_FLAGS for f in reply.flags):
        return "flagged"
    return _last_checks(person_id, thread, own, now, db_path)


def _last_checks(person_id: int, thread: Any, own: set[str], now: datetime, db_path: Path | None) -> str | None:
    """Never twice in a row in a thread, then the counted rules."""
    thread_id = str(getattr(thread, "id", "") or "")
    handled = _handled_rows(person_id, "AND thread_id = ?", (thread_id,), db_path)
    mine = _own_messages(thread, own)
    if mine and handled and mine[-1].id in {r["sent_message_id"] for r in handled}:
        return "in_a_row"
    return count_refusal(person_id, thread_id, now, db_path=db_path)


def follow_up_refusal(
    person_id: int,
    sent: Any,
    reply: Any,
    *,
    relation: str,
    own: set[str],
    exec_address: str,
    now: datetime,
    db_path: Path | None = None,
) -> str | None:
    """Why a follow-up to ``sent`` (the person's own unanswered email) may
    not go on its own (a ``REASONS`` code), or None when it may. Plain code
    only; never raises (an error refuses)."""
    try:
        return _follow_up_refusal(
            person_id, sent, reply, relation=relation, own=own, exec_address=exec_address, now=now,
            db_path=db_path,
        )
    except Exception:
        logger.warning("delegation.handle_it: the follow-up rules failed — asking instead", exc_info=True)
        return "uncountable"


def _follow_up_refusal(
    person_id: int, sent: Any, reply: Any, *, relation: str, own: set[str], exec_address: str, now: datetime,
    db_path: Path | None,
) -> str | None:
    from openexecutive.delegation.threads import MAX_RECIPIENTS

    settings = get(person_id, db_path=db_path)
    if not settings.enabled:
        return "off"
    if approvals_hold(db_path=db_path):
        return "level"
    if not signing_ok():
        return "signing_off"
    # Exactly the people their own email went to: nobody added, nobody left out.
    allowed = {*sent.to, *sent.cc} - own - ({exec_address} if exec_address else set())
    going = [*reply.to, *reply.cc]
    if not going or len(going) > MAX_RECIPIENTS or set(going) != allowed:
        return "recipients"
    if len(reply.body or "") > FOLLOW_UP_MAX_CHARS:
        return "long"
    if any(f not in ALLOWED_FLAGS for f in reply.flags):
        return "flagged"
    return count_refusal(person_id, str(getattr(sent, "thread_id", "") or ""), now, db_path=db_path)


def count_refusal(
    person_id: int, thread_id: str, now: datetime, *, db_path: Path | None = None,
) -> str | None:
    """The counted rules, checked when the card is made and again just
    before it sends: one reply on its own per thread a day, and the daily
    limit. Anything it can't count is a refusal."""
    from openexecutive.config import get_settings

    try:
        handled = _handled_rows(person_id, "AND thread_id = ?", (thread_id,), db_path)
        if any((datetime.fromisoformat(r["sent_at"]) > now - THREAD_WINDOW) for r in handled):
            return "thread_recent"
        today = sent_today(person_id, now, db_path=db_path)
    except Exception:
        return "uncountable"
    if today >= get_settings().delegation_handle_it_max_sends_per_day:
        return "daily_limit"
    return None


# --------------------------------------------------------------------------- #
# Handled for you
# --------------------------------------------------------------------------- #


@dataclass
class HandledReply:
    decision_id: int
    sent_at: str
    to_name: str
    to_email: str
    subject: str
    body: str
    open_questions: list[str]
    thread_id: str
    source: str = ""
    # One line saying what it did, for an accepted invite: "Accepted Dana's
    # Pilot review, Wed 21 Oct, 14:30".
    summary: str = ""


def handled(person_id: int, *, days: int = 7, limit: int = 50) -> list[HandledReply]:
    """``person_id``'s replies sent on its own in the last ``days``, newest
    first. Theirs alone: callers resolve the caller to ``person_id``."""
    from openexecutive.memory.decision_ledger import STATUS_EXECUTED, list_instances

    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    out: list[HandledReply] = []
    cards = list_instances(
        "delegation_reply", status=STATUS_EXECUTED, approver_person_id=person_id,
        resolved_since=since, limit=limit,
    )
    for card in cards:
        try:
            payload = json.loads(card.proposed_payload_json or "{}")
        except ValueError:
            continue
        if payload.get("person_id") != person_id:
            continue
        out.append(HandledReply(
            decision_id=card.id,
            sent_at=card.resolved_at or card.created_at,
            to_name=str(payload.get("from_name") or ""),
            to_email=str(payload.get("from_email") or ""),
            subject=str(payload.get("draft_subject") or ""),
            body=str(payload.get("draft_body") or ""),
            open_questions=[str(q) for q in payload.get("open_questions") or []],
            thread_id=str(payload.get("thread_id") or ""),
            source="follow_up" if payload.get("source") == "follow_up" else "",
        ))
    from openexecutive.delegation import invites

    out.extend(invites.handled(person_id, since=since, limit=limit))
    out.sort(key=lambda h: h.sent_at, reverse=True)
    return out[:limit]
