"""Handle it for me: the inbox watcher sends some replies on its own.

**The switch.** A third switch under Act as me, after "Draft replies to my
inbox" (``delegation_handle_it``, one row per person, absent means off). Each
kind of reply has a level, ``off``, ``ask`` or ``handle``:

- ``reply_known``: a reply to someone on the person's team, one of their
  contacts, or someone they have written to. ``handle`` when first turned on.
- ``reply_stranger``: a stranger's short holding reply. ``ask`` when first
  turned on.

``ask`` is today's behaviour: a card waits for the person to tap Send.
``off`` is the same as ``ask`` for now (the watcher never drafts less than it
did). Only the person turns it on, from a session the API knows is theirs
(``reply_send._check_caller``, the rule Send uses), and nobody else can.

**Who decides.** No model decides whether a reply goes. The inbox watcher's
two model calls (``inbox_classifier`` and the ghostwriter) read the email and
have no tools: one returns a verdict, the other a reply. Text in the email can
at most change those two answers. Whether the reply is sent is this module's
plain code (``refusal``), checked again at send time
(``reply_send.send_on_its_own``). Anything it refuses becomes the usual card,
with the reason recorded.

**What it refuses.** Every one of these keeps the reply on a card:

- the level for this kind of sender isn't ``handle``, or signed sign-ins are
  off on a server (nothing would tie the switch to the person);
- Gmail couldn't authenticate the sender;
- the classifier wasn't sure (``MIN_CONFIDENCE``), or the email isn't one of
  the kinds that need a reply;
- the reply goes to anyone the email didn't already go to;
- the reply, the email or its subject touches money, contracts, legal,
  hiring, pay, the board, the press, health or credentials (``SENSITIVE``),
  or the reply has an amount, a percentage or a link in it, or is long;
- the draft carries a flag other than ``ALLOWED_FLAGS`` (they asked whether
  it's an AI, recipients were trimmed, the draft names the Executive
  (``names_the_executive``), ...). The Executive merely being on the email
  (``executive_on_thread``) is fine: it is never a recipient of the reply;
- it already sent one in this thread in the last day, or the last thing the
  person "said" in this thread was itself sent on its own;
- the day's limit (``DELEGATION_HANDLE_IT_MAX_SENDS_PER_DAY``) is reached.

Never sent on its own whatever the level: anything with an attachment (the
watcher's drafts have none), a new recipient, or a sensitive topic.

**After.** A reply sent on its own is a ``delegation_reply`` decision in
status ``executed`` with ``gate_mode`` ``auto_execute``; ``handled`` lists the
person's recent ones (``GET /delegation/handled``), theirs alone, with the
questions the reply left for them to answer. No History note is taken from it:
those come from the person's own words only.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from openexecutive.delegation.schema import HANDLE_IT_TABLE, HANDLED_TABLE, ensure_schema

logger = logging.getLogger(__name__)

KIND_REPLY_KNOWN = "reply_known"
KIND_REPLY_STRANGER = "reply_stranger"
KINDS: tuple[str, ...] = (KIND_REPLY_KNOWN, KIND_REPLY_STRANGER)

LEVEL_OFF = "off"
LEVEL_ASK = "ask"
LEVEL_HANDLE = "handle"
LEVELS: tuple[str, ...] = (LEVEL_OFF, LEVEL_ASK, LEVEL_HANDLE)

# The levels a person starts with when they first turn it on.
DEFAULT_LEVELS: dict[str, str] = {KIND_REPLY_KNOWN: LEVEL_HANDLE, KIND_REPLY_STRANGER: LEVEL_ASK}

KNOWN_RELATIONS = frozenset({"team", "contact", "correspondent"})
# The classifier kinds a reply may be sent for on its own.
REPLY_KINDS = frozenset({"question", "request", "scheduling", "introduction", "follow_up"})
MIN_CONFIDENCE = 0.8
MAX_BODY_CHARS = 1200
THREAD_WINDOW = timedelta(days=1)
# Draft flags a reply may carry and still be sent on its own.
ALLOWED_FLAGS = frozenset({"others_on_thread", "executive_on_thread"})

# Topics that always wait for the person, matched as whole words in the email,
# its subject and the reply. Deliberately broad: a false match costs a tap.
SENSITIVE = (
    "access token", "acquisition", "agreement", "api key", "attorney", "bank", "board", "bonus",
    "budget", "compensation", "compliance", "confidential", "contract", "contracts", "counsel",
    "court", "credentials", "diagnosis", "discount", "equity", "fire", "fired", "firing", "gdpr",
    "health", "hire", "hiring", "investor", "investors", "invoice", "invoices", "journalist",
    "lawsuit", "lawyer", "layoff", "layoffs", "legal", "login", "medical", "merger", "nda",
    "offer letter", "paid", "passcode", "password", "passwords", "pay", "payment", "payments",
    "payroll", "press", "price", "prices", "pricing", "quote", "refund", "reporter", "resign",
    "resignation", "salary", "secret", "secrets", "settlement", "social security", "ssn",
    "subpoena", "term sheet", "terminate", "termination", "token", "tokens", "wire",
)
_SENSITIVE_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(w).replace(r"\ ", r"\s+") for w in SENSITIVE) + r")\b", re.IGNORECASE,
)
# A scheme, www., a bare host with a path ("bit.ly/x9Z"), or a bare host on
# a common top-level domain. A false match only costs the person a tap.
_LINK_RE = re.compile(
    r"(?:https?://|www\.|\b[\w-]+(?:\.[\w-]+)+/\S*"
    r"|\b[\w-]+(?:\.[\w-]+)*\.(?:com|net|org|io|co|ly|ai|app|dev|me|info|biz|us|uk|link|xyz|gl|to|site|online)\b)",
    re.IGNORECASE,
)
_AMOUNT_RE = re.compile(
    r"[$€£¥₹]\s*\d|\d\s*%|\b\d[\d,.]*\s*(?:usd|eur|gbp|dollars?|euros?|pounds?|percent)\b",
    re.IGNORECASE,
)

# What each refusal tells the person on the card.
REASONS: dict[str, str] = {
    "level": "Handle it for me is set to ask for this kind of email.",
    "signing_off": "Sending on its own needs signed sign-ins on this server.",
    "sender_unverified": "Your mail service couldn't confirm who sent it.",
    "unsure": "It wasn't sure enough this needs only a simple reply.",
    "kind": "It isn't the kind of email it answers on its own.",
    "recipients": "The reply would go to someone the email didn't.",
    "sensitive": "It touches a topic that always waits for you.",
    "amount": "The reply mentions an amount or a percentage.",
    "link": "The reply has a link in it.",
    "long": "The reply is longer than the ones it sends on its own.",
    "flagged": "Something about the draft needs your eye.",
    "thread_recent": "It already replied on its own in this conversation today.",
    "in_a_row": "Its last message in this conversation was sent on its own too.",
    "daily_limit": "Today's limit of replies sent on its own is reached.",
    "uncountable": "It couldn't count today's replies, so it asked instead.",
}


@dataclass
class HandleIt:
    person_id: int
    enabled: bool = False
    levels: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_LEVELS))

    def level(self, kind: str) -> str:
        if not self.enabled:
            return LEVEL_ASK
        return self.levels.get(kind, LEVEL_ASK)


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


def _clean_levels(raw: Any) -> dict[str, str]:
    levels = dict(DEFAULT_LEVELS)
    if isinstance(raw, dict):
        for kind, level in raw.items():
            if kind in KINDS and level in LEVELS:
                levels[kind] = level
    return levels


def get(person_id: int, *, db_path: Path | None = None) -> HandleIt:
    """``person_id``'s switch and levels. Never raises: unreadable is off."""
    try:
        conn = _connect(db_path)
        try:
            row = conn.execute(
                f"SELECT enabled, levels FROM {HANDLE_IT_TABLE} WHERE person_id = ?",  # noqa: S608 — constant table name
                (person_id,),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        logger.warning("delegation.handle_it: couldn't read the switch — treating it as off", exc_info=True)
        return HandleIt(person_id=person_id)
    if row is None:
        return HandleIt(person_id=person_id)
    try:
        levels = _clean_levels(json.loads(row["levels"] or "{}"))
    except ValueError:
        levels = dict(DEFAULT_LEVELS)
    return HandleIt(person_id=person_id, enabled=bool(row["enabled"]), levels=levels)


def set_(
    person_id: int,
    *,
    enabled: bool | None = None,
    levels: dict[str, str] | None = None,
    updated_by: str,
    db_path: Path | None = None,
) -> HandleIt:
    """Change ``person_id``'s switch and/or levels (callers authorize first).
    Unknown kinds and levels are ignored."""
    current = get(person_id, db_path=db_path)
    new_enabled = current.enabled if enabled is None else enabled
    new_levels = _clean_levels({**current.levels, **(levels or {})})
    now = datetime.now(UTC).isoformat()
    conn = _connect(db_path)
    try:
        conn.execute(
            f"INSERT INTO {HANDLE_IT_TABLE} (person_id, enabled, levels, updated_at, updated_by) "  # noqa: S608
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(person_id) DO UPDATE SET enabled = excluded.enabled, "
            "levels = excluded.levels, updated_at = excluded.updated_at, updated_by = excluded.updated_by",
            (person_id, 1 if new_enabled else 0, json.dumps(new_levels, sort_keys=True), now, updated_by),
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


def sensitive(*texts: str) -> bool:
    return any(_SENSITIVE_RE.search(t or "") for t in texts)


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
    from openexecutive.integrations.email_poller import sender_new_text

    settings = get(person_id, db_path=db_path)
    if settings.level(kind_for(relation)) != LEVEL_HANDLE:
        return "level"
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

    body = reply.body or ""
    if sensitive(message.subject or "", sender_new_text(message.text or ""), reply.subject or "", body):
        return "sensitive"
    if _AMOUNT_RE.search(body) or _AMOUNT_RE.search(reply.subject or ""):
        return "amount"
    if _LINK_RE.search(body):
        return "link"
    if len(body) > MAX_BODY_CHARS:
        return "long"
    if any(f not in ALLOWED_FLAGS for f in reply.flags):
        return "flagged"

    thread_id = str(getattr(thread, "id", "") or "")
    handled = _handled_rows(person_id, "AND thread_id = ?", (thread_id,), db_path)
    mine = _own_messages(thread, own)
    if mine and handled and mine[-1].id in {r["sent_message_id"] for r in handled}:
        return "in_a_row"
    return count_refusal(person_id, thread_id, now, db_path=db_path)


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
        ))
    out.sort(key=lambda h: h.sent_at, reverse=True)
    return out[:limit]
