"""Handle it for me: the inbox watcher sends some replies on its own.

**The switch.** A third switch under Act as me, after "Draft replies to my
inbox" (``delegation_handle_it``, one row per person, absent means off), with
one setting, like the Agent Council's quality (``MODES``, ``RULES``):

- ``careful``: short replies to people the person knows (someone on their
  team, a contact, or someone they have written to), when it's very sure.
- ``balanced`` (where it starts): replies to people they know.
- ``bold``: also replies to strangers, longer ones, and replies with a link
  or an amount when it's very sure.

**In training.** Act as me's Replies and Follow-ups can each be put in
training (``delegation.training``): then the dial lets nothing go by itself.
Only a reply to a sender, or a follow-up to people, the person allowed with
Send + allow goes on its own, under the dial's limits (a stranger they
allowed included) and every check below. Take the lead as you doesn't lift
those limits while in training.

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

- it's off, or the sender is a stranger and the setting isn't ``bold``, or
  signed sign-ins are off on a server (nothing would tie the switch to the
  person);
- Gmail couldn't authenticate the sender;
- the classifier wasn't sure enough for the setting (``Rules``), or the
  email isn't one of the kinds that need a reply;
- the reply goes to anyone the email didn't already go to;
- the reply, the email or its subject touches money, contracts, legal,
  hiring, pay, the board, the press, health or credentials (``SENSITIVE``),
  or the reply is longer than the setting allows, or has an amount, a
  percentage or a link in it (``bold`` allows those when it's very sure);
- the draft carries a flag other than ``ALLOWED_FLAGS`` (they asked whether
  it's an AI, recipients were trimmed, the draft names the Executive
  (``names_the_executive``), ...). The Executive merely being on the email
  (``executive_on_thread``) is fine: it is never a recipient of the reply;
- it already sent one in this thread in the last day, or the last thing the
  person "said" in this thread was itself sent on its own;
- the day's limit (``DELEGATION_HANDLE_IT_MAX_SENDS_PER_DAY``) is reached.

Never sent on its own whatever the setting: anything with an attachment (the
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

MODE_CAREFUL = "careful"
MODE_BALANCED = "balanced"
MODE_BOLD = "bold"
MODES: tuple[str, ...] = (MODE_CAREFUL, MODE_BALANCED, MODE_BOLD)
DEFAULT_MODE = MODE_BALANCED


@dataclass(frozen=True)
class Rules:
    """What one setting lets go on its own."""

    strangers: bool
    max_body_chars: int
    min_confidence: float
    # How sure it must be to send a reply with a link or an amount in it;
    # None: never.
    details_confidence: float | None = None
    # Who a follow-up to an unanswered email of theirs may go to on its own
    # (``delegation.follow_ups``), by ``inbox.relation_of``.
    follow_up_to: frozenset[str] = frozenset()


RULES: dict[str, Rules] = {
    MODE_CAREFUL: Rules(strangers=False, max_body_chars=400, min_confidence=0.9),
    MODE_BALANCED: Rules(
        strangers=False, max_body_chars=1200, min_confidence=0.8, follow_up_to=frozenset({"team", "contact"}),
    ),
    MODE_BOLD: Rules(
        strangers=True, max_body_chars=2000, min_confidence=0.8, details_confidence=0.9,
        follow_up_to=frozenset({"team", "contact", "correspondent"}),
    ),
}
# A follow-up is a nudge: never longer than this, whatever the setting.
FOLLOW_UP_MAX_CHARS = 600

KNOWN_RELATIONS = frozenset({"team", "contact", "correspondent"})
# The classifier kinds a reply may be sent for on its own.
REPLY_KINDS = frozenset({"question", "request", "scheduling", "introduction", "follow_up"})
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
    "level": "Handle it for me asks you about email from people you don't know yet (Most mail sends those).",
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
    # Found just before sending (reply_send.send_on_its_own).
    "draft_changed": "You changed the draft in your mailbox, so it's yours to send.",
    "handle_it_off": "Handle it for me was off by the time it came to send.",
    # Follow-ups as you (delegation.follow_ups).
    "follow_up_level": "On this setting, a follow-up to this person waits for you to send it.",
    # Take the lead as you (orchestrator.take_the_lead).
    "lead_rule": "One of the rules you or your company added says this waits for you.",
    # In training (delegation.training).
    "training": "Replies are in training, and you haven't allowed replies to them yet.",
    "follow_up_training": "Follow-ups are in training, and you haven't allowed follow-ups to them yet.",
}

# Take the lead as you lifts the setting's limits, but not this ceiling.
LEAD_MAX_BODY_CHARS = 4000


def leading(person_id: int, *, db_path: Path | None = None) -> bool:
    """Whether Take the lead as you is on for ``person_id``: the setting's
    limits give way to the added rules. Never raises: unreadable is off."""
    from openexecutive.orchestrator import take_the_lead

    try:
        return take_the_lead.as_you_on(person_id, db_path=db_path)
    except Exception:
        return False


def allowed_in_training(person_id: int, sender: str, *, db_path: Path | None = None) -> bool:
    """Whether ``person_id`` allowed replies to ``sender`` (Send + allow).
    Never raises: unreadable is no."""
    from openexecutive.delegation import training

    return training.allowed_sender(person_id, sender, db_path=db_path) is not None


def follow_up_allowed(person_id: int, recipients: list[str], *, db_path: Path | None = None) -> bool:
    """Whether ``person_id`` allowed follow-ups to exactly ``recipients``."""
    from openexecutive.delegation import training

    return training.allowed_follow_up(person_id, recipients, db_path=db_path) is not None


def in_training(person_id: int, setting: str, *, db_path: Path | None = None) -> bool:
    """Whether Act as me's ``setting`` is in training for ``person_id``.
    Never raises (``training.get``)."""
    from openexecutive.delegation import training

    return training.get(person_id, db_path=db_path).on(setting)


def _lead_rule(person_id: int, texts: list[str], going: list[str], db_path: Path | None) -> bool:
    from openexecutive.orchestrator import take_the_lead

    return take_the_lead.reply_hit(person_id, texts, going, db_path=db_path) is not None


@dataclass
class HandleIt:
    person_id: int
    enabled: bool = False
    mode: str = DEFAULT_MODE

    @property
    def rules(self) -> Rules:
        return RULES.get(self.mode, RULES[DEFAULT_MODE])

    def level(self, kind: str) -> str:
        """``handle`` when a reply to this kind of sender may go on its own
        by the dial (Replies in training overrides it: ``refusal``)."""
        if not self.enabled or (kind != KIND_REPLY_KNOWN and not self.rules.strangers):
            return LEVEL_ASK
        return LEVEL_HANDLE

    def follows_up(self, relation: str) -> bool:
        """Whether a follow-up to someone with this ``relation`` may go on
        its own."""
        return self.enabled and relation in self.rules.follow_up_to


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
    """``person_id``'s switch and setting. Never raises: unreadable is off."""
    try:
        conn = _connect(db_path)
        try:
            row = conn.execute(
                f"SELECT enabled, mode FROM {HANDLE_IT_TABLE} WHERE person_id = ?",  # noqa: S608 — constant table name
                (person_id,),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        logger.warning("delegation.handle_it: couldn't read the switch — treating it as off", exc_info=True)
        return HandleIt(person_id=person_id)
    if row is None:
        return HandleIt(person_id=person_id)
    mode = row["mode"] if row["mode"] in MODES else DEFAULT_MODE
    return HandleIt(person_id=person_id, enabled=bool(row["enabled"]), mode=mode)


def set_(
    person_id: int,
    *,
    enabled: bool | None = None,
    mode: str | None = None,
    updated_by: str,
    db_path: Path | None = None,
) -> HandleIt:
    """Change ``person_id``'s switch and/or setting (callers authorize
    first). An unknown setting is ignored."""
    current = get(person_id, db_path=db_path)
    new_enabled = current.enabled if enabled is None else enabled
    new_mode = mode if mode in MODES else current.mode
    now = datetime.now(UTC).isoformat()
    conn = _connect(db_path)
    try:
        conn.execute(
            f"INSERT INTO {HANDLE_IT_TABLE} (person_id, enabled, mode, updated_at, updated_by) "  # noqa: S608
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(person_id) DO UPDATE SET enabled = excluded.enabled, "
            "mode = excluded.mode, updated_at = excluded.updated_at, updated_by = excluded.updated_by",
            (person_id, 1 if new_enabled else 0, new_mode, now, updated_by),
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
    from openexecutive.delegation import training
    from openexecutive.delegation.threads import MAX_RECIPIENTS
    from openexecutive.integrations.email_poller import sender_new_text

    settings = get(person_id, db_path=db_path)
    # In training only Send + allow lets a reply go: Take the lead as you
    # doesn't lift its limits.
    trained = in_training(person_id, training.REPLIES, db_path=db_path)
    lead = settings.enabled and not trained and leading(person_id, db_path=db_path)
    if trained:
        if not settings.enabled:
            return "level"
        if not allowed_in_training(person_id, message.from_addr, db_path=db_path):
            return "training"
    elif not lead and settings.level(kind_for(relation)) != LEVEL_HANDLE:
        return "level"
    if not signing_ok():
        return "signing_off"
    if getattr(message, "sender_authenticated", False) is not True:
        return "sender_unverified"
    if verdict is None or verdict.kind not in REPLY_KINDS:
        return "kind"
    rules = settings.rules
    confidence = float(verdict.confidence)
    if not lead and confidence < rules.min_confidence:
        return "unsure"

    # Exactly the people the email already went to, minus the person and the
    # Executive: the sender first, then everyone else on it.
    allowed = {message.from_addr, *message.to, *message.cc} - own - ({exec_address} if exec_address else set())
    going = [*reply.to, *reply.cc]
    if not going or len(going) > MAX_RECIPIENTS or not set(going) <= allowed or message.from_addr not in reply.to:
        return "recipients"

    body = reply.body or ""
    texts = [message.subject or "", sender_new_text(message.text or ""), reply.subject or "", body]
    if sensitive(*texts):
        return "sensitive"
    if lead:
        if _lead_rule(person_id, texts, going, db_path):
            return "lead_rule"
        # A link still waits on a tap: sent as them, it's the shape a phish takes.
        if _LINK_RE.search(body):
            return "link"
        if len(body) > LEAD_MAX_BODY_CHARS:
            return "long"
        if any(f not in ALLOWED_FLAGS for f in reply.flags):
            return "flagged"
        return _last_checks(person_id, thread, own, now, db_path)
    sure_of_details = rules.details_confidence is not None and confidence >= rules.details_confidence
    if not sure_of_details and (_AMOUNT_RE.search(body) or _AMOUNT_RE.search(reply.subject or "")):
        return "amount"
    if not sure_of_details and _LINK_RE.search(body):
        return "link"
    if len(body) > rules.max_body_chars:
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
    from openexecutive.delegation import training
    from openexecutive.delegation.threads import MAX_RECIPIENTS
    from openexecutive.integrations.email_poller import sender_new_text

    settings = get(person_id, db_path=db_path)
    trained = in_training(person_id, training.FOLLOW_UPS, db_path=db_path)
    lead = settings.enabled and not trained and leading(person_id, db_path=db_path)
    # Exactly the people their own email went to: nobody added, nobody left out.
    allowed = {*sent.to, *sent.cc} - own - ({exec_address} if exec_address else set())
    going = [*reply.to, *reply.cc]
    if trained:
        # Only follow-ups to exactly the people they allowed, whoever they are.
        if not settings.enabled:
            return "follow_up_level"
        if not follow_up_allowed(person_id, going, db_path=db_path):
            return "follow_up_training"
    elif not lead and not settings.follows_up(relation):
        return "follow_up_level"
    if not signing_ok():
        return "signing_off"
    if not going or len(going) > MAX_RECIPIENTS or set(going) != allowed:
        return "recipients"
    body = reply.body or ""
    texts = [sent.subject or "", sender_new_text(sent.text or ""), reply.subject or "", body]
    if sensitive(*texts):
        return "sensitive"
    if lead and _lead_rule(person_id, texts, going, db_path):
        return "lead_rule"
    if _AMOUNT_RE.search(body) or _AMOUNT_RE.search(reply.subject or ""):
        return "amount"
    if _LINK_RE.search(body):
        return "link"
    if len(body) > min(settings.rules.max_body_chars, FOLLOW_UP_MAX_CHARS):
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
    out.sort(key=lambda h: h.sent_at, reverse=True)
    return out[:limit]
