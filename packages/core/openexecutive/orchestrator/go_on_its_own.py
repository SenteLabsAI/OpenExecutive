"""Delegate → Approvals, what the level did lately and what could go on its own.

Reads only; nothing here is a gate. Approvals (``levels``), Your rules and In
training still decide every step, in the same order.

**Last 7 days** (``week``): for the person reading, how many things went on
their own and how many waited for them, As you (their reply cards) and As me
(the Executive's own actions: its done log for the owner, and the held cards
routed to the reader). Of those that waited, how many they approved and
declined, and the most common reason, in their words.

**Could go on its own** (``offers``): things the reader approved
``take_the_lead.SUGGEST_AFTER`` times unchanged, not allowed yet. As me: the
owner's training suggestions (``take_the_lead.suggestions``). As you: people
whose reply cards they sent unchanged in training (``training.offers``).
"Let it go" adds it to What it's learned, exactly as ticking "on its own next
time" would; "Keep asking" drops the offer until that many more unchanged
approvals (``take_the_lead.dismiss``).
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

AS_YOU = "as_you"
AS_ME = "as_me"
DAYS = 7
# The most it lists under See what waited.
ITEMS_MAX = 50

APPROVED = "approved"
DECLINED = "declined"
WAITING = "waiting"
# Settled some other way: sent or deleted in the mailbox, or it lapsed.
CLOSED = "closed"

# Why it waited, short enough for "N waited because …", by As you's
# handle_it.REASONS code and As me's hold kind. Anything else has no line.
_AS_YOU_BECAUSE: dict[str, str] = {
    "training": "In training is on",
    "level": "Approvals is on Careful",
    "stranger": "they went to someone outside People",
    "work_stranger": "they went to someone outside People",
    "sensitive": "they were about money, contracts or passwords",
    "amount": "they had an amount in them",
    "link": "they had a link in them",
    "rule": "one of your rules says to wait",
    "unsure": "it wasn't sure enough",
    "suspicious": "the email tried to give it instructions",
}
_AS_ME_BECAUSE: dict[str, str] = {
    "training": "In training is on",
    "level": "Approvals is on Careful",
    "rule": "one of your rules says to wait",
    "money": "they were about money",
    "contracts": "they were about contracts",
    "people_decisions": "they were about hiring or people decisions",
    "delete_share": "they deleted, cancelled or shared something",
    "someone_new": "they went to someone outside People",
    "big_send": "they went to many people at once",
}


@dataclass(frozen=True)
class Waited:
    at: str
    side: str
    label: str
    # Why it waited, as a sentence ('' when it didn't say).
    why: str
    outcome: str


@dataclass(frozen=True)
class Week:
    went: int = 0
    waited: int = 0
    approved: int = 0
    declined: int = 0
    # How many of those that waited share the most common reason, and it.
    because_count: int = 0
    because: str = ""
    items: list[Waited] = field(default_factory=list)


@dataclass(frozen=True)
class Offer:
    key: str
    side: str
    label: str
    approvals: int
    last_at: str
    # As you: the address replies would go to on their own; '' for As me.
    address: str = ""
    name: str = ""


def _outcome(status: str) -> str:
    from openexecutive.memory import decision_ledger as ledger

    if status in (ledger.STATUS_APPROVED_UNCHANGED, ledger.STATUS_APPROVED_WITH_EDIT):
        return APPROVED
    if status == ledger.STATUS_REJECTED:
        return DECLINED
    if status in ledger.OPEN_STATUSES:
        return WAITING
    return CLOSED


def week(
    person_id: int | None, *, as_you: bool, as_me_done: bool, days: int = DAYS, db_path: Path | None = None,
) -> Week:
    """The last ``days`` for ``person_id``: their As you reply cards when
    ``as_you``, the As me cards routed to them, and (``as_me_done``, the
    owner) what the Executive did in its own name. Never raises: what can't
    be read counts as nothing."""
    from openexecutive.delegation.handle_it import REASONS
    from openexecutive.delegation.inbox import DECISION_CLASS as REPLY_CLASS
    from openexecutive.memory import decision_ledger as ledger
    from openexecutive.orchestrator import take_the_lead

    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    went = 0
    items: list[Waited] = []
    reasons: Counter[str] = Counter()
    if as_me_done:
        try:
            went += sum(
                1 for d in take_the_lead.done([take_the_lead.SCOPE_EXECUTIVE], days=days, limit=5000, db_path=db_path)
                if d.status == "done"
            )
        except Exception:
            logger.warning("go_on_its_own: couldn't read what it did as itself", exc_info=True)
    classes = ([REPLY_CLASS] if as_you else []) + [take_the_lead.DECISION_CLASS]
    cards = []
    if person_id is not None:
        try:
            cards = ledger.list_for_approver_since(classes, person_id, since, db_path=db_path)
        except Exception:
            logger.warning("go_on_its_own: couldn't read the cards", exc_info=True)
    for card in cards:
        mine = card.decision_class == REPLY_CLASS
        if card.status == ledger.STATUS_EXECUTED:
            if mine and (card.resolved_at or "") >= since:
                went += 1
            continue
        if card.status == ledger.STATUS_FAILED or card.created_at < since:
            continue
        if card.gate_mode == "auto_execute" and card.status == ledger.STATUS_EXECUTING:
            continue  # going on its own right now
        try:
            payload = json.loads(card.proposed_payload_json or "{}")
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        if mine:
            code = str(payload.get("handle_it_reason") or "")
            who = str(payload.get("from_name") or payload.get("from_email") or "").strip()
            subject = str(payload.get("subject") or "").strip()
            label = f"Reply to {who}" + (f": {subject}" if subject else "") if who else (subject or "A reply")
            why = REASONS.get(code, "")
            because = _AS_YOU_BECAUSE.get(code, "")
        else:
            code = str(payload.get("kind") or "")
            label = str(payload.get("headline") or payload.get("summary") or "Something it wanted to do")
            reason = str(payload.get("reason") or "").strip()
            why = f"It waited because {reason}." if reason else ""
            because = _AS_ME_BECAUSE.get(code, "")
        if because:
            reasons[because] += 1
        items.append(Waited(
            at=card.created_at, side=AS_YOU if mine else AS_ME, label=label[:200], why=why,
            outcome=_outcome(card.status),
        ))
    top = reasons.most_common(1)
    return Week(
        went=went,
        waited=len(items),
        approved=sum(1 for i in items if i.outcome == APPROVED),
        declined=sum(1 for i in items if i.outcome == DECLINED),
        because_count=top[0][1] if top else 0,
        because=top[0][0] if top else "",
        items=items[:ITEMS_MAX],
    )


def offers(person_id: int | None, *, as_you: bool, as_me: bool, db_path: Path | None = None) -> list[Offer]:
    """What ``person_id`` could let go on its own: their As you people when
    ``as_you``, and the Executive's own suggestions when ``as_me`` (the
    owner). Never raises."""
    from openexecutive.delegation import training
    from openexecutive.orchestrator import take_the_lead

    out: list[Offer] = []
    if as_me:
        try:
            out += [
                Offer(key=s.key, side=AS_ME, label=s.label, approvals=s.approvals, last_at=s.last_at)
                for s in take_the_lead.suggestions(db_path=db_path)
            ]
        except Exception:
            logger.warning("go_on_its_own: couldn't read its suggestions", exc_info=True)
    if as_you and person_id is not None:
        out += [
            Offer(
                key=o.key, side=AS_YOU, label=o.label, approvals=o.approvals, last_at=o.last_at,
                address=o.address, name=o.name,
            )
            for o in training.offers(person_id, db_path=db_path)
        ]
    return sorted(out, key=lambda o: o.last_at, reverse=True)


def side_of(key: str) -> str:
    """Which side an offer's key belongs to ('' for neither)."""
    from openexecutive.orchestrator import take_the_lead

    if key.startswith(f"{take_the_lead.FEATURE}|"):
        return AS_ME
    if key.startswith(f"{take_the_lead.FEATURE_ACT_AS_ME}|"):
        return AS_YOU
    return ""


def let_go_as_me(key: str, *, created_by: str, db_path: Path | None = None) -> str | None:
    """Allow one of the Executive's current suggestions; its label, or None
    when ``key`` isn't one. Raises ``take_the_lead.RuleError`` when full."""
    from openexecutive.orchestrator import take_the_lead

    match = next((s for s in take_the_lead.suggestions(db_path=db_path) if s.key == key), None)
    if match is None:
        return None
    take_the_lead.allow(match.key, match.label, created_by=created_by, db_path=db_path)
    return match.label


def keep_asking_as_me(key: str, *, db_path: Path | None = None) -> bool:
    from openexecutive.orchestrator import take_the_lead

    if not any(s.key == key for s in take_the_lead.suggestions(db_path=db_path)):
        return False
    take_the_lead.dismiss(key, db_path=db_path)
    return True
