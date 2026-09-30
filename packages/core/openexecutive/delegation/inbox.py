"""Act as me: watch a person's own inbox and draft replies for them to review.

**The switch.** "Draft replies to my inbox" is a second switch under Act as
me (``delegation_inbox_watch``, one row per person, absent means off). It
needs Act as me on and the person's own Gmail connected, and turning it on
sets ``watch_since`` to now, every time: it never drafts for mail that came in
before. Off means no Gmail call, no row written and no prompt, tool or cache
changed anywhere.

**When it looks.** A throttled hook in the scheduler tick (``maybe_scan``),
not a ``scheduled_actions`` row (those are on ``/scheduled`` for everyone and
anyone may cancel them): every ``DELEGATION_INBOX_POLL_MINUTES`` per person,
one scan at a time, after the pause, company-profile and rotation gates, and
never while a client slot is active. "Check now" runs the same scan. Every
row a scan writes is private to the principal (``private_rows``), and the
process log carries counts and codes, never an address or text.

**What it drafts for.** The newest message per thread in the inbox since
``watch_since`` (not chats, not from the person, not in a promotions, social,
updates or forums tab), at most 25 messages and 10 threads a scan. Each is
recorded once in ``delegation_inbox_messages``, its durable cursor, with
what happened to it. It is left alone (recorded with the reason) when:

- it came before ``watch_since``, or more than three days ago;
- it is from the person or one of their send-as addresses, from the
  Executive, or one of this package's own drafts;
- it is automatic, bulk, a bounce or a calendar invite, or from a mailing
  list;
- the person is not in To or Cc, or it has more than 10 recipients;
- the person replied last, or a draft already sits in the thread.

Two kinds are not recorded, so a later scan looks again: a message younger
than 10 minutes (mail the person answers straight away never gets a card),
and one in a thread whose card is still open (``deferred``).

**Two model calls, then code.** ``inbox_classifier`` decides whether it
needs a reply, with a bar that rises the less the sender is known (their team
or contacts, someone they have written to, a stranger). The ghostwriter then
writes the reply in the person's voice, from a fixed intent this module
builds: acknowledge, restate only what the person themselves already said in
the thread, promise nothing new, and put every unanswered ask in
``open_questions``. A stranger always gets a short holding reply. The reply
goes to the sender only; others on the thread are named on the card
(``others_on_thread``).

**Limits.** Per scan 5 drafts; per day ``DELEGATION_INBOX_MAX_DRAFTS_PER_DAY``
within the shared daily limit (``delegation.caps``), 200 classifications, 2
drafts per sender (1 for a stranger) and 10 for strangers in all; and never
more than 25 open cards. A limit reached leaves the message for a later scan.

**Crash-safe order.** Record the message → classify → write → save the draft
in Gmail and store its id → create the card (a ``delegation_reply`` decision,
idempotent on the message) → mark it drafted.

**Failures.** A lapsed sign-in sets the status and is checked again every 30
minutes; a rate limit or other error backs off from 5 minutes up to 2 hours.
Neither turns the switch off.

**Cards.** A card is a ``delegation_reply`` decision, private to the
principal: never an alert, since alerts feed chat turns and would put other
people's mail into memory. ``reconcile`` closes a card that Gmail settled: the
draft sent or deleted there, the person replied, or 7 days passed (the draft
stays in Gmail). It flags a card whose thread moved on.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from openexecutive.delegation.schema import INBOX_MESSAGES_TABLE, INBOX_WATCH_TABLE, ensure_schema

logger = logging.getLogger(__name__)

DECISION_CLASS = "delegation_reply"

GRACE = timedelta(minutes=10)
STALE = timedelta(days=3)
CARD_TTL = timedelta(days=7)
SCAN_MESSAGES = 25
SCAN_THREADS = 10
DRAFTS_PER_SCAN = 5
CLASSIFICATIONS_PER_DAY = 200
DRAFTS_PER_SENDER_PER_DAY = 2
DRAFTS_PER_STRANGER_PER_DAY = 1
STRANGER_DRAFTS_PER_DAY = 10
OPEN_CARDS_MAX = 25
MAX_RECIPIENTS = 10
AUTH_RECHECK = timedelta(minutes=30)
BACKOFF_FIRST = timedelta(minutes=5)
BACKOFF_MAX = timedelta(hours=2)
_THEY_WROTE_CHARS = 2000

# What happened to a message (delegation_inbox_messages.outcome).
PROCESSING = "processing"
SKIPPED = "skipped"
NOT_NEEDED = "not_needed"
DEFERRED = "deferred"
FAILED = "failed"
DRAFTED = "drafted"
SENT = "sent"
DISMISSED = "dismissed"
CLOSED = "closed"
EXPIRED = "expired"

# Local parts of addresses nobody reads replies to.
_NO_REPLY_SENDERS = ("noreply", "no-reply", "do-not-reply", "donotreply", "mailer-daemon", "postmaster")
_TAB_LABELS = frozenset({
    "SPAM", "TRASH", "CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_UPDATES", "CATEGORY_FORUMS",
})
_EMAIL_RE = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")

INBOX_REPLY_INTENT = (
    "Reply to the newest message in <thread>, from the sender, as a first reply "
    "for them to review before anything is sent. Acknowledge what the sender "
    "wrote. Where the writer's OWN earlier messages in <thread> (marked \"(the "
    "writer)\") already answer something, you may say it again in their words. "
    "Otherwise commit to nothing: no yes or no, no dates, times, prices, figures, "
    "facts or promises of your own; say they will get back to them. Put every "
    "question or request you could not answer from the writer's own earlier "
    "words in open_questions."
)

INBOX_HOLDING_INTENT = (
    "Write a short, polite holding reply to the newest message in <thread>: "
    "thank the sender for writing and say they will get back to them. Nothing "
    "else: answer nothing, agree to nothing and commit to nothing. Put what the "
    "sender asked for in open_questions."
)


@dataclass
class InboxWatch:
    person_id: int
    enabled: bool = False
    watch_since: str | None = None
    last_poll_at: str | None = None
    status: str = "off"
    backoff_until: str | None = None
    failures: int = 0


@dataclass
class ScanResult:
    status: str
    drafted: int = 0
    skipped: int = 0
    not_needed: int = 0
    deferred: int = 0
    failed: int = 0
    closed: int = 0

    def counts(self) -> dict[str, Any]:
        return asdict(self)


# What each watch status tells the person on the Act as me card.
STATUS_MESSAGES: dict[str, str] = {
    "off": "Off.",
    "waiting": "On. It checks your inbox every few minutes.",
    "ok": "On.",
    "checking": "Checking your inbox now.",
    "daily_limit": "On, but today's limit of drafts is reached. It carries on tomorrow.",
    "backlog_full": "On, but 25 replies are already waiting for you. Send or dismiss some first.",
    "act_as_me_off": "Paused: Act as me is off.",
    "client_slot": "Paused while a client is active.",
    "rate_limited": "Gmail asked it to slow down. It tries again shortly.",
    "error": "Couldn't reach your Gmail. It tries again shortly.",
    "not_configured": "Paused: your Gmail isn't connected.",
    "needs_reconnect": "Paused: connect your Gmail again.",
    "mismatch": "Paused: the connected Gmail isn't the address on your People entry.",
    "no_email": "Paused: your People entry has no email address.",
    "shared_mailbox": "Paused: your address is the Executive's own mailbox.",
}


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


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def get_watch(person_id: int, *, db_path: Path | None = None) -> InboxWatch:
    """``person_id``'s switch and its health. Never raises: unreadable is off."""
    try:
        conn = _connect(db_path)
        try:
            row = conn.execute(
                f"SELECT * FROM {INBOX_WATCH_TABLE} WHERE person_id = ?",  # noqa: S608 — constant table name
                (person_id,),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        logger.warning("delegation.inbox: couldn't read the switch — treating it as off", exc_info=True)
        return InboxWatch(person_id=person_id)
    if row is None:
        return InboxWatch(person_id=person_id)
    return InboxWatch(
        person_id=person_id,
        enabled=bool(row["enabled"]),
        watch_since=row["watch_since"],
        last_poll_at=row["last_poll_at"],
        status=row["status"] or ("waiting" if row["enabled"] else "off"),
        backoff_until=row["backoff_until"],
        failures=int(row["failures"] or 0),
    )


def set_watch(
    person_id: int, enabled: bool, *, updated_by: str, now: datetime | None = None, db_path: Path | None = None
) -> InboxWatch:
    """Turn the switch on or off (callers authorize first). Turning it on
    starts from now: ``watch_since`` resets, so old mail is never drafted for."""
    moment = (now or datetime.now(UTC)).isoformat()
    before = get_watch(person_id, db_path=db_path)
    conn = _connect(db_path)
    try:
        if enabled and not before.enabled:
            conn.execute(
                f"INSERT INTO {INBOX_WATCH_TABLE} "  # noqa: S608 — constant table name
                "(person_id, enabled, watch_since, last_poll_at, status, backoff_until, failures, "
                "updated_at, updated_by) VALUES (?, 1, ?, NULL, 'waiting', NULL, 0, ?, ?) "
                "ON CONFLICT(person_id) DO UPDATE SET enabled = 1, watch_since = excluded.watch_since, "
                "last_poll_at = NULL, status = 'waiting', backoff_until = NULL, failures = 0, "
                "updated_at = excluded.updated_at, updated_by = excluded.updated_by",
                (person_id, moment, moment, updated_by),
            )
        elif not enabled:
            conn.execute(
                f"UPDATE {INBOX_WATCH_TABLE} SET enabled = 0, status = 'off', backoff_until = NULL, "  # noqa: S608
                "failures = 0, updated_at = ?, updated_by = ? WHERE person_id = ?",
                (moment, updated_by, person_id),
            )
        conn.commit()
    finally:
        conn.close()
    return get_watch(person_id, db_path=db_path)


def _update_watch(person_id: int, db_path: Path | None = None, **fields: Any) -> None:
    if not fields:
        return
    names = sorted(fields)
    assignments = ", ".join(f"{name} = ?" for name in names)
    conn = _connect(db_path)
    try:
        conn.execute(
            f"UPDATE {INBOX_WATCH_TABLE} SET {assignments} WHERE person_id = ? AND enabled = 1",  # noqa: S608
            (*(fields[n] for n in names), person_id),
        )
        conn.commit()
    finally:
        conn.close()


def _sender_key(address: str) -> str:
    """A stable key for a sender, so the ledger never holds an address."""
    return hashlib.sha256(address.strip().lower().encode()).hexdigest()[:16]


def _claim(
    person_id: int, message: Any, *, relation: str, outcome: str, reason: str | None = None,
    now: datetime, db_path: Path | None = None,
) -> bool:
    """Record ``message`` once; False when it already was (another scan)."""
    moment = now.isoformat()
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            f"INSERT OR IGNORE INTO {INBOX_MESSAGES_TABLE} "  # noqa: S608 — constant table name
            "(person_id, message_id, thread_id, received_at, sender_key, relation, outcome, reason, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                person_id, message.id, message.thread_id, message.received_at or None,
                _sender_key(message.from_addr) if message.from_addr else None, relation or None,
                outcome, reason, moment, moment,
            ),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def _set_outcome(
    person_id: int, message_id: str, outcome: str, *, reason: str | None = None,
    db_path: Path | None = None, **fields: Any,
) -> None:
    names = sorted(fields)
    extra = "".join(f", {name} = ?" for name in names)
    conn = _connect(db_path)
    try:
        conn.execute(
            f"UPDATE {INBOX_MESSAGES_TABLE} SET outcome = ?, reason = COALESCE(?, reason), "  # noqa: S608
            f"updated_at = ?{extra} WHERE person_id = ? AND message_id = ?",
            (outcome, reason, datetime.now(UTC).isoformat(), *(fields[n] for n in names), person_id, message_id),
        )
        conn.commit()
    finally:
        conn.close()


def _add_flag(person_id: int, message_id: str, flag: str, *, db_path: Path | None = None) -> None:
    conn = _connect(db_path)
    try:
        row = conn.execute(
            f"SELECT flags FROM {INBOX_MESSAGES_TABLE} WHERE person_id = ? AND message_id = ?",  # noqa: S608
            (person_id, message_id),
        ).fetchone()
        if row is None:
            return
        try:
            flags = json.loads(row["flags"] or "[]")
        except ValueError:
            flags = []
        if flag in flags:
            return
        conn.execute(
            f"UPDATE {INBOX_MESSAGES_TABLE} SET flags = ? WHERE person_id = ? AND message_id = ?",  # noqa: S608
            (json.dumps([*flags, flag]), person_id, message_id),
        )
        conn.commit()
    finally:
        conn.close()


def ledger_flags(person_id: int, message_ids: list[str], *, db_path: Path | None = None) -> dict[str, list[str]]:
    """The flags the reconciler added, by message id."""
    if not message_ids:
        return {}
    conn = _connect(db_path)
    try:
        marks = ",".join("?" for _ in message_ids)
        rows = conn.execute(
            f"SELECT message_id, flags FROM {INBOX_MESSAGES_TABLE} "  # noqa: S608 — constant table name
            f"WHERE person_id = ? AND message_id IN ({marks})",
            (person_id, *message_ids),
        ).fetchall()
    finally:
        conn.close()
    out: dict[str, list[str]] = {}
    for row in rows:
        try:
            flags = json.loads(row["flags"] or "[]")
        except ValueError:
            flags = []
        out[str(row["message_id"])] = [str(f) for f in flags if isinstance(f, str)]
    return out


def _count(sql: str, params: tuple[Any, ...], db_path: Path | None = None) -> int:
    conn = _connect(db_path)
    try:
        row = conn.execute(sql, params).fetchone()
    finally:
        conn.close()
    return int(row[0]) if row else 0


def _day_start(now: datetime) -> str:
    return now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def _recorded(person_id: int, message_id: str, db_path: Path | None = None) -> bool:
    return _count(
        f"SELECT COUNT(*) FROM {INBOX_MESSAGES_TABLE} WHERE person_id = ? AND message_id = ?",  # noqa: S608
        (person_id, message_id), db_path,
    ) > 0


# --------------------------------------------------------------------------- #
# Cards
# --------------------------------------------------------------------------- #


def open_cards(person_id: int) -> list[Any]:
    """``person_id``'s open reply cards (proposed or being sent), newest first."""
    from openexecutive.memory.decision_ledger import OPEN_STATUSES, list_instances

    cards = list_instances(DECISION_CLASS, limit=200)
    return [c for c in cards if c.approver_person_id == person_id and c.status in OPEN_STATUSES]


def card_payload(card: Any) -> dict[str, Any]:
    try:
        payload = json.loads(card.proposed_payload_json)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _open_card_for_thread(person_id: int, thread_id: str) -> bool:
    return any(card_payload(c).get("thread_id") == thread_id for c in open_cards(person_id))


# --------------------------------------------------------------------------- #
# Deciding what to draft for
# --------------------------------------------------------------------------- #


def skip_reason(
    message: Any,
    thread: Any,
    *,
    own: set[str],
    exec_address: str,
    watch_since: datetime,
    now: datetime,
) -> str | None:
    """Why ``message`` gets no reply drafted, or None. ``own`` is the
    person's addresses (primary and send-as)."""
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
    local_part = sender.partition("@")[0]
    if (
        message.auto_generated or message.bulk or message.delivery_report or message.calendar_invite
        or any(mark in local_part for mark in _NO_REPLY_SENDERS)
    ):
        return "automatic"
    if message.mailing_list:
        return "mailing_list"
    if set(message.labels) & _TAB_LABELS:
        return "category"
    addressed = {*message.to, *message.cc}
    if not addressed & own:
        return "not_addressed"
    if len(addressed) > MAX_RECIPIENTS:
        return "too_many_recipients"
    shown = [m for m in thread.messages if "DRAFT" not in m.labels]
    if shown and shown[-1].from_addr in own:
        return "you_replied"
    if any("DRAFT" in m.labels for m in thread.messages):
        return "draft_exists"
    return None


def _roster_relation(address: str) -> str | None:
    from openexecutive.people.store import list_people

    for person in list_people(include_contacts=True):
        if address in {a.lower() for a in [person.email, *person.email_aliases] if a}:
            return "team" if person.kind == "team" else "contact"
    return None


async def relation_of(address: str, gmail: Any) -> str:
    """Who the sender is to the person: on their team, one of their
    contacts, someone they have written to before, or a stranger."""
    try:
        found = _roster_relation(address)
    except Exception:
        logger.warning("delegation.inbox: roster lookup failed — treating the sender as unknown", exc_info=True)
        found = None
    if found is not None:
        return found
    if not _EMAIL_RE.fullmatch(address):
        return "stranger"
    written_to = await gmail.list_message_ids(f"in:sent to:{address}", max_results=1)
    return "correspondent" if written_to else "stranger"


def _cap_reason(person_id: int, message: Any, relation: str, now: datetime, db_path: Path | None = None) -> str | None:
    """A daily limit this message would pass, or None. Checked before the
    message is recorded, so a limit only delays it."""
    from openexecutive.config import get_settings
    from openexecutive.delegation import caps, drafts

    day = _day_start(now)
    if _count(
        f"SELECT COUNT(*) FROM {INBOX_MESSAGES_TABLE} "  # noqa: S608 — constant table name
        "WHERE person_id = ? AND classified = 1 AND created_at >= ?",
        (person_id, day), db_path,
    ) >= CLASSIFICATIONS_PER_DAY:
        return "classify_limit"
    try:
        inbox_today = drafts.count_since(person_id, datetime.fromisoformat(day), source=drafts.SOURCE_INBOX)
    except Exception:
        return "uncountable"
    settings = get_settings()
    if inbox_today >= settings.delegation_inbox_max_drafts_per_day:
        return "inbox_limit"
    shared = caps.drafts_today(person_id)
    if shared is None:
        return "uncountable"
    if shared >= settings.delegation_max_drafts_per_day:
        return "daily_limit"
    per_sender = DRAFTS_PER_STRANGER_PER_DAY if relation == "stranger" else DRAFTS_PER_SENDER_PER_DAY
    if _count(
        f"SELECT COUNT(*) FROM {INBOX_MESSAGES_TABLE} "  # noqa: S608 — constant table name
        "WHERE person_id = ? AND sender_key = ? AND draft_id IS NOT NULL AND created_at >= ?",
        (person_id, _sender_key(message.from_addr), day), db_path,
    ) >= per_sender:
        return "sender_limit"
    if relation == "stranger" and _count(
        f"SELECT COUNT(*) FROM {INBOX_MESSAGES_TABLE} "  # noqa: S608 — constant table name
        "WHERE person_id = ? AND relation = 'stranger' AND draft_id IS NOT NULL AND created_at >= ?",
        (person_id, day), db_path,
    ) >= STRANGER_DRAFTS_PER_DAY:
        return "stranger_limit"
    return None


# Limits that hold for the rest of the scan, not just this sender.
_SCAN_STOPPING_LIMITS = frozenset({"classify_limit", "inbox_limit", "daily_limit", "uncountable"})


def card_flags(message: Any, plan: dict[str, Any], *, own: set[str], exec_address: str) -> list[str]:
    """What the card warns about, besides the draft's own flags."""
    flags = [f for f in plan.get("flags", []) if f != "you_replied_last"]
    addressed = {*message.to, *message.cc}
    if addressed - own - {message.from_addr} - ({exec_address} if exec_address else set()):
        flags.append("others_on_thread")
    if exec_address and exec_address in addressed:
        flags.append("executive_on_thread")
    if not message.sender_authenticated:
        flags.append("sender_unverified")
    return flags


# --------------------------------------------------------------------------- #
# Writing one reply
# --------------------------------------------------------------------------- #


@dataclass
class Reply:
    """A reply written for one message: what the card shows."""

    to: list[str]
    subject: str
    body: str
    open_questions: list[str]
    flags: list[str]
    in_reply_to: str | None
    references: str | None


async def compose_reply(person: Any, message: Any, thread: Any, *, relation: str, own: set[str]) -> Reply | str:
    """The reply to ``message``, written as ``person``, or why there is none
    (a code). Raises ``ComposeError`` when the composer returns nothing."""
    from openexecutive.config import get_settings
    from openexecutive.delegation.ghostwriter import Recipient, asks_if_ai, compose
    from openexecutive.delegation.threads import plan_reply, thread_text
    from openexecutive.delegation.voice import composer_model, get_voice, render_voice_block

    email = (person.email or "").strip().lower()
    plan = plan_reply(thread, email, False)
    if isinstance(plan, str) or plan["to"] != [message.from_addr]:
        return "no_reply_target"
    if asks_if_ai(plan.pop("last_text", "") or ""):
        plan["flags"].append("asks_if_ai")
    stored = get_voice(person.id)
    names = (person.full_name or "").split()
    exec_address = (get_settings().exec_email_address or "").strip().lower()
    relation_text = {
        "team": "on their team",
        "contact": "one of their contacts",
        "correspondent": "someone they have written to before",
        "stranger": "someone they have not written to before",
    }.get(relation, "")
    composed = await compose(
        writer_name=" ".join(names) or email,
        voice_block=render_voice_block(stored.profile, first_name=names[0] if names else "them"),
        thread_text=thread_text(thread, email),
        reply_subject=plan["subject"],
        intent=INBOX_HOLDING_INTENT if relation == "stranger" else INBOX_REPLY_INTENT,
        recipients=[Recipient(email=message.from_addr, name=message.from_name, relation=relation_text)],
        signature=stored.profile.signature,
        exec_name=get_settings().exec_display_name,
        model=composer_model(),
    )
    if not composed.subject:
        return "no_subject"
    questions = list(composed.open_questions)
    flags = [*card_flags(message, plan, own=own, exec_address=exec_address), *composed.flags]
    if "asks_if_ai" in flags:
        questions.append("They asked whether they're talking to an AI — answer that yourself.")
    return Reply(
        to=plan["to"],
        subject=composed.subject,
        body=composed.body,
        open_questions=questions,
        flags=list(dict.fromkeys(flags)),
        in_reply_to=plan["in_reply_to"],
        references=plan["references"],
    )


async def reply_for(
    person: Any, message: Any, thread: Any, *, relation: str, own: set[str]
) -> tuple[Any, Reply | str | None]:
    """The watcher's two model calls for one message, without the mailbox or
    the ledger: the verdict, and the reply (or why none, a code; None when no
    reply is wanted). The evals run this; a scan runs the same two steps with
    its limits in between."""
    from openexecutive.delegation.ghostwriter import ComposeError
    from openexecutive.delegation.inbox_classifier import classify, wants_draft

    verdict = await classify(message, relation=relation)
    if verdict is None or not wants_draft(verdict, relation):
        return verdict, None
    try:
        return verdict, await compose_reply(person, message, thread, relation=relation, own=own)
    except ComposeError:
        return verdict, "compose_failed"


def _card_payload(
    person: Any, message: Any, thread: Any, reply: Reply, draft: Any, *, relation: str, verdict: Any
) -> dict[str, Any]:
    from openexecutive.delegation.ghostwriter import one_line
    from openexecutive.integrations.email_poller import sender_new_text

    return {
        # A card is the principal's alone (DecisionClassSpec.principal_only
        # hides the class; this keeps every reader that checks the payload
        # in step).
        "private": True,
        "person_id": person.id,
        "message_id": message.id,
        "thread_id": thread.id,
        "draft_id": draft.draft_id,
        "draft_message_id": draft.message_id,
        "from_name": one_line(message.from_name, 120),
        "from_email": message.from_addr,
        "relation": relation,
        "sender_verified": bool(message.sender_authenticated),
        "subject": one_line(message.subject, 200),
        "received_at": message.received_at,
        "they_wrote": sender_new_text(message.text or "")[:_THEY_WROTE_CHARS],
        "draft_to": reply.to,
        "draft_subject": reply.subject,
        "draft_body": reply.body,
        "open_questions": reply.open_questions,
        "flags": reply.flags,
        "kind": verdict.kind,
        "confidence": verdict.confidence,
    }


# --------------------------------------------------------------------------- #
# The scan
# --------------------------------------------------------------------------- #

_SCANNING: set[int] = set()
_scan_task: asyncio.Task[None] | None = None


def scanning(person_id: int) -> bool:
    return person_id in _SCANNING


def _backoff(failures: int) -> timedelta:
    return min(BACKOFF_FIRST * (2 ** max(0, failures - 1)), BACKOFF_MAX)


def _client_slot_active() -> bool:
    try:
        from openexecutive.clients.slots import get_active_client
        from openexecutive.config import get_settings

        return get_active_client(get_settings()) is not None
    except Exception:
        logger.warning("delegation.inbox: client-slot check failed — not scanning", exc_info=True)
        return True


def _audit(event_type: str, summary: str, details: dict[str, Any]) -> None:
    from openexecutive.audit import log_event

    log_event(event_type, summary, actor="executive", details=details, private=True)


async def scan_person(person: Any, *, gmail: Any = None, now: datetime | None = None) -> ScanResult:
    """One scan of ``person``'s inbox. Never raises; the switch's status
    records how it went."""
    from openexecutive.audit import private_rows
    from openexecutive.delegation.gmail import (
        GmailAuthError,
        GmailError,
        GmailRateLimited,
        gmail_for,
        gmail_status,
    )
    from openexecutive.delegation.settings import can_delegate, is_enabled

    moment = now or datetime.now(UTC)
    if person is None or person.id is None:
        return ScanResult("off")
    watch = get_watch(person.id)
    if not watch.enabled:
        return ScanResult("off")
    if person.id in _SCANNING:
        return ScanResult("checking")
    if not can_delegate(person) or not is_enabled(person.id):
        _update_watch(person.id, status="act_as_me_off", last_poll_at=moment.isoformat())
        return ScanResult("act_as_me_off")
    if _client_slot_active():
        _update_watch(person.id, status="client_slot", last_poll_at=moment.isoformat())
        return ScanResult("client_slot")
    email = (person.email or "").strip().lower()
    client = gmail if gmail is not None else gmail_for(email)
    _SCANNING.add(person.id)
    try:
        with private_rows(True):
            status = await gmail_status(email, gmail=client)
            if status != "connected":
                _update_watch(
                    person.id, status=status, last_poll_at=moment.isoformat(),
                    backoff_until=(moment + AUTH_RECHECK).isoformat(),
                )
                return ScanResult(status)
            try:
                result = await _scan(person, client, watch, moment)
            except GmailAuthError:
                _update_watch(
                    person.id, status="needs_reconnect", last_poll_at=moment.isoformat(),
                    backoff_until=(moment + AUTH_RECHECK).isoformat(),
                )
                return ScanResult("needs_reconnect")
            except GmailRateLimited:
                failures = watch.failures + 1
                _update_watch(
                    person.id, status="rate_limited", failures=failures, last_poll_at=moment.isoformat(),
                    backoff_until=(moment + _backoff(failures)).isoformat(),
                )
                return ScanResult("rate_limited")
            except GmailError:
                logger.warning("delegation.inbox: scan failed (gmail)")
                failures = watch.failures + 1
                _update_watch(
                    person.id, status="error", failures=failures, last_poll_at=moment.isoformat(),
                    backoff_until=(moment + _backoff(failures)).isoformat(),
                )
                return ScanResult("error")
            _update_watch(
                person.id, status=result.status, failures=0, backoff_until=None,
                last_poll_at=moment.isoformat(),
            )
            _audit("delegation_inbox_scanned", f"Checked person {person.id}'s inbox", {
                "person_id": person.id, **result.counts(),
            })
            logger.info("delegation.inbox: scan %s", result.counts())
            return result
    except Exception:
        logger.exception("delegation.inbox: scan crashed")
        _update_watch(person.id, status="error", last_poll_at=moment.isoformat(),
                      backoff_until=(moment + BACKOFF_FIRST).isoformat())
        return ScanResult("error")
    finally:
        _SCANNING.discard(person.id)


async def _scan(person: Any, client: Any, watch: InboxWatch, now: datetime) -> ScanResult:
    from openexecutive.config import get_settings

    email = (person.email or "").strip().lower()
    own = {email, *await client.send_as_addresses()}
    exec_address = (get_settings().exec_email_address or "").strip().lower()
    result = ScanResult("ok")
    result.closed = await reconcile(person, client, now=now, own=own)
    if len(open_cards(person.id)) >= OPEN_CARDS_MAX:
        result.status = "backlog_full"
        return result
    since = _parse(watch.watch_since) or now
    query = (
        "in:inbox -in:chats -from:me -category:promotions -category:social "
        f"-category:updates -category:forums after:{int(since.timestamp())}"
    )
    listed = await client.list_message_ids(query, max_results=SCAN_MESSAGES)
    threads: list[tuple[str, str]] = []
    seen: set[str] = set()
    for message_id, thread_id in listed:  # newest first: the first one per thread
        if thread_id in seen:
            continue
        seen.add(thread_id)
        threads.append((message_id, thread_id))
        if len(threads) >= SCAN_THREADS:
            break
    for message_id, thread_id in threads:
        if result.drafted >= DRAFTS_PER_SCAN:
            break
        if _recorded(person.id, message_id):
            continue
        stop = await _consider(person, client, message_id, thread_id, own, exec_address, since, now, result)
        if stop:
            break
    return result


async def _consider(
    person: Any, client: Any, message_id: str, thread_id: str, own: set[str], exec_address: str,
    since: datetime, now: datetime, result: ScanResult,
) -> bool:
    """Handle one message. True when the rest of the scan should stop."""
    from openexecutive.config import get_settings
    from openexecutive.delegation import caps, drafts
    from openexecutive.delegation.ghostwriter import ComposeError
    from openexecutive.delegation.gmail import DraftSpec
    from openexecutive.delegation.inbox_classifier import classify, wants_draft

    thread = await client.get_thread(thread_id)
    message = next((m for m in thread.messages if m.id == message_id), None)
    if message is None:
        return False
    received = _parse(message.received_at)
    if received is not None and now - received < GRACE:
        return False  # not yet: mail they answer straight away never gets a card
    if _open_card_for_thread(person.id, thread.id):
        # Not recorded: looked at again once that card is settled (the
        # reconciler flags the card meanwhile).
        result.deferred += 1
        return False
    reason = skip_reason(message, thread, own=own, exec_address=exec_address, watch_since=since, now=now)
    if reason is not None:
        if _claim(person.id, message, relation="", outcome=SKIPPED, reason=reason, now=now):
            result.skipped += 1
        return False
    relation = await relation_of(message.from_addr, client)
    limit = _cap_reason(person.id, message, relation, now)
    if limit is not None:
        # Not recorded: a later scan tries again once the limit allows.
        if limit in _SCAN_STOPPING_LIMITS:
            result.status = "daily_limit"
            return True
        return False
    if not _claim(person.id, message, relation=relation, outcome=PROCESSING, now=now):
        return False
    verdict = await classify(message, relation=relation)
    _set_outcome(person.id, message.id, PROCESSING, classified=1)
    if verdict is None:
        _set_outcome(person.id, message.id, FAILED, reason="classify_failed")
        result.failed += 1
        return False
    if not wants_draft(verdict, relation):
        _set_outcome(person.id, message.id, NOT_NEEDED, reason=verdict.kind)
        result.not_needed += 1
        return False
    settings = get_settings()
    if caps.reserve(person.id, settings.delegation_max_drafts_per_day) is not None:
        _set_outcome(person.id, message.id, DEFERRED, reason="daily_limit")
        result.status = "daily_limit"
        return True
    saved = False
    try:
        try:
            reply = await compose_reply(person, message, thread, relation=relation, own=own)
        except ComposeError:
            reply = "compose_failed"
        if isinstance(reply, str):
            _set_outcome(person.id, message.id, FAILED, reason=reply)
            result.failed += 1
            return False
        names = (person.full_name or "").split()
        draft = await client.create_draft(DraftSpec(
            to=reply.to,
            subject=reply.subject,
            body=reply.body,
            thread_id=thread.id,
            in_reply_to=reply.in_reply_to,
            references=reply.references,
            from_name=" ".join(names),
        ))
        saved = True
        _set_outcome(person.id, message.id, PROCESSING, draft_id=draft.draft_id)
        try:
            drafts.record(
                person.id, source=drafts.SOURCE_INBOX, thread_id=thread.id,
                draft_id=draft.draft_id, message_id=draft.message_id, now=now,
            )
        except Exception:
            logger.warning("delegation.inbox: couldn't record the draft", exc_info=True)
    finally:
        caps.release(person.id, saved=saved)
    decision_id = _create_card(person, message, thread, reply, draft, relation=relation, verdict=verdict)
    _set_outcome(person.id, message.id, DRAFTED, decision_id=decision_id)
    result.drafted += 1
    _audit("delegation_reply_drafted", f"Drafted a reply as person {person.id} for review", {
        "person_id": person.id,
        "thread_id": thread.id,
        "draft_id": draft.draft_id,
        "decision_id": decision_id,
        "relation": relation,
        "kind": verdict.kind,
        "flags": reply.flags,
    })
    return False


def _create_card(
    person: Any, message: Any, thread: Any, reply: Reply, draft: Any, *, relation: str, verdict: Any
) -> int | None:
    """The card for this reply, made once per message (a crash between the
    draft and here, then a rescan, finds the one it made)."""
    from openexecutive.memory.decision_ledger import create_decision_instance, get_live_by_idem

    key = f"{DECISION_CLASS}:{person.id}:{message.id}"
    try:
        return create_decision_instance(
            decision_class=DECISION_CLASS,
            department="",
            originating_session_id=None,
            proposed_payload=_card_payload(person, message, thread, reply, draft, relation=relation, verdict=verdict),
            idempotency_key=key,
            gate_mode="propose",
            approver_person_id=person.id,
            confidence=verdict.confidence,
        )
    except sqlite3.IntegrityError:
        existing = get_live_by_idem(key)
        return existing.id if existing is not None else None


# --------------------------------------------------------------------------- #
# Settling cards Gmail already settled
# --------------------------------------------------------------------------- #


# Cards being sent right now in this process (delegation.reply_send adds a
# card before claiming it and removes it once the send is settled): the
# reconciler leaves them alone.
SENDING: set[int] = set()


def later_messages(thread: Any, payload: dict[str, Any], *, now: datetime) -> list[Any]:
    """The thread's messages after the one a card answers (drafts left out)."""
    inbound_at = _parse(str(payload.get("received_at") or ""))
    return [
        m for m in thread.messages
        if "DRAFT" not in m.labels and (inbound_at is None or ((_parse(m.received_at) or now) > inbound_at))
    ]


def _close_card(person_id: int, card_id: int, message_id: str, reason: str, outcome: str) -> bool:
    """Close a card Gmail settled (compare-and-set), unless it is being sent."""
    from openexecutive.memory.decision_ledger import close_externally

    if card_id in SENDING or not close_externally(card_id, reason=reason):
        return False
    _set_outcome(person_id, message_id, outcome, reason=reason)
    _audit("delegation_reply_closed", f"Closed a reply card for person {person_id}", {
        "person_id": person_id, "decision_id": card_id, "reason": reason,
    })
    return True


async def reconcile(person: Any, client: Any, *, now: datetime, own: set[str]) -> int:
    """Close the person's open cards that Gmail settled; returns how many."""
    from openexecutive.delegation import drafts
    from openexecutive.delegation.gmail import GmailNotFound
    from openexecutive.memory.decision_ledger import STATUS_EXECUTING, STATUS_PROPOSED

    closed = 0
    for card in open_cards(person.id):
        if card.id in SENDING:
            continue  # the send path's, right now
        payload = card_payload(card)
        message_id = str(payload.get("message_id") or "")
        if card.status == STATUS_EXECUTING:
            closed += await _settle_unconfirmed_send(person, client, card, payload, own=own, now=now)
            continue
        if card.status != STATUS_PROPOSED:
            continue
        created = _parse(card.created_at)
        if created is not None and now - created > CARD_TTL:
            closed += _close_card(person.id, card.id, message_id, "expired", EXPIRED)
            continue
        draft = await client.get_draft(str(payload.get("draft_id") or ""))
        try:
            thread = await client.get_thread(str(payload.get("thread_id") or ""))
        except GmailNotFound:
            closed += _close_card(person.id, card.id, message_id, "thread_gone", CLOSED)
            continue
        later = later_messages(thread, payload, now=now)
        sent_by_them = [m for m in later if "SENT" in m.labels and m.from_addr in own]
        if draft is None:
            if sent_by_them:
                if _close_card(person.id, card.id, message_id, "sent_in_gmail", SENT):
                    drafts.mark_sent(person.id, str(payload.get("draft_id")), sent_by_them[-1].id)
                    closed += 1
            else:
                closed += _close_card(person.id, card.id, message_id, "draft_deleted", CLOSED)
            continue
        if sent_by_them:
            closed += _close_card(person.id, card.id, message_id, "you_replied", CLOSED)
            continue
        if any(m.from_addr not in own and "SENT" not in m.labels for m in later):
            _add_flag(person.id, message_id, "thread_moved_on")
    return closed


async def _settle_unconfirmed_send(
    person: Any, client: Any, card: Any, payload: dict[str, Any], *, own: set[str], now: datetime
) -> int:
    """A card left ``executing`` by a send whose outcome was unclear (a
    timeout, a 5xx, a crash mid-send). Gmail deletes a draft it sends, so the
    draft still being there means it was not sent: the card goes back for the
    person to try again. Gone, with their reply in the thread: it was sent.
    Gone without one: it was deleted in Gmail. Never sends anything itself."""
    from openexecutive.delegation import drafts
    from openexecutive.delegation.gmail import GmailNotFound
    from openexecutive.memory.decision_ledger import (
        STATUS_APPROVED_UNCHANGED,
        STATUS_APPROVED_WITH_EDIT,
        finish_execution,
        release_claim,
    )

    message_id = str(payload.get("message_id") or "")
    draft_id = str(payload.get("draft_id") or "")
    draft = await client.get_draft(draft_id)
    if card.id in SENDING:
        return 0
    if draft is not None:
        if release_claim(card.id):
            _add_flag(person.id, message_id, "send_failed")
        return 0
    try:
        thread = await client.get_thread(str(payload.get("thread_id") or ""))
    except GmailNotFound:
        thread = None
    if card.id in SENDING:
        return 0
    later = later_messages(thread, payload, now=now) if thread is not None else []
    sent = [m for m in later if "SENT" in m.labels and m.from_addr in own]
    if not sent:
        return int(_close_card(person.id, card.id, message_id, "draft_deleted", CLOSED))
    edited = "edited_in_gmail" in ledger_flags(person.id, [message_id]).get(message_id, [])
    status = STATUS_APPROVED_WITH_EDIT if edited else STATUS_APPROVED_UNCHANGED
    if not finish_execution(
        card.id, status, final_payload={"sent_message_id": sent[-1].id, "confirmed_later": True},
        external_event_id=sent[-1].id or None,
    ):
        return 0
    _set_outcome(person.id, message_id, SENT, reason="sent")
    drafts.mark_sent(person.id, draft_id, sent[-1].id)
    _audit("delegation_reply_sent", f"Sent a reply as person {person.id}", {
        "person_id": person.id, "decision_id": card.id, "thread_id": str(payload.get("thread_id") or ""),
        "sent_message_id": sent[-1].id, "edited": edited, "confirmed_later": True,
    })
    return 1


# --------------------------------------------------------------------------- #
# The scheduler hook
# --------------------------------------------------------------------------- #


def _due(now: datetime) -> list[int]:
    """People whose switch is on and whose next check is due."""
    from openexecutive.config import get_settings

    interval = timedelta(minutes=get_settings().delegation_inbox_poll_minutes)
    try:
        conn = _connect()
        try:
            rows = conn.execute(
                f"SELECT person_id, last_poll_at, backoff_until FROM {INBOX_WATCH_TABLE} "  # noqa: S608
                "WHERE enabled = 1"
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        logger.warning("delegation.inbox: couldn't read who to check", exc_info=True)
        return []
    due: list[int] = []
    for row in rows:
        last = _parse(row["last_poll_at"])
        backoff = _parse(row["backoff_until"])
        if backoff is not None and backoff > now:
            continue
        if last is None or now - last >= interval:
            due.append(int(row["person_id"]))
    return due


async def _scan_due(person_ids: list[int], now: datetime) -> None:
    from openexecutive.people.store import get_person

    for person_id in person_ids:
        try:
            person = get_person(person_id)
        except Exception:
            logger.warning("delegation.inbox: couldn't read person %s", person_id, exc_info=True)
            continue
        if person is None or person.archived:
            continue
        await scan_person(person, now=now)


def maybe_scan(now: datetime) -> bool:
    """Start the due scans as one task when none is running. True when it
    started one. Never raises."""
    global _scan_task
    try:
        if _scan_task is not None and not _scan_task.done():
            return False
        due = _due(now)
        if not due:
            return False
        from openexecutive.audit.context import unscoped_audit_rows

        with unscoped_audit_rows():
            _scan_task = asyncio.create_task(_scan_due(due, now))
        return True
    except Exception:
        logger.exception("delegation.inbox: couldn't start a scan")
        return False
