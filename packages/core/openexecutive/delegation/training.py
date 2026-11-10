"""As you, in training: one switch and what it learns.

**In training** (``training``, one row in ``delegation_training``, absent
means off) sits above Approvals for everything sent as the person. While it
is on, nothing goes out as them on its own, on any level: every reply,
follow-up, invite answer and suggested action waits on its card, which says
what it would have done. Send (Accept, Approve) does it once. Ticked "Send to
them on its own next time", it also allows that person (``allow_person``,
one allowance per address): from then on replies to them, follow-ups to
them, their invites and messages or meetings with them go on their own,
where Approvals lets them (Careful still holds everything; the checks
``handle_it.refusal`` always makes still apply). Everything not allowed yet
keeps coming as a card. Off, Approvals alone decides.

**Drafts** (``drafts``): nothing is sent. When they change a reply's draft
in their mailbox before tapping Send and tick "Do it like this next time",
what they sent is kept as an example the ghostwriter follows when it next
writes to that person (``ghostwriter``'s ``<writer_example>``), from the
inbox and from chat.

What it keeps lives in Take the lead's one "What it's learned" list
(``take_the_lead.allow``), as feature ``act_as_me`` with the person's id:
theirs alone to see and remove, and never in anyone else's prompts. Keeping
happens only after a tap the API knows is the person's (``reply_send``, the
rule Send uses).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from openexecutive.delegation.schema import TRAINING_TABLE, ensure_schema
from openexecutive.orchestrator.take_the_lead import FEATURE_ACT_AS_ME, Allowed

logger = logging.getLogger(__name__)

TRAINING = "training"
DRAFTS = "drafts"
SETTINGS: tuple[str, ...] = (TRAINING, DRAFTS)

# What each kind of key teaches, by the action in it.
_SETTING_OF_ACTION = {"style": DRAFTS, "person": TRAINING}

# The longest example kept, after the quoted thread is dropped.
EXAMPLE_CHARS = 1000


@dataclass(frozen=True)
class Training:
    person_id: int
    # In training: nothing goes out as them on its own but to people they allowed.
    training: bool = False
    drafts: bool = False

    def on(self, setting: str) -> bool:
        return bool(getattr(self, setting, False)) if setting in SETTINGS else False


# --------------------------------------------------------------------------- #
# The switches
# --------------------------------------------------------------------------- #


def _db(db_path: Path | None) -> Path:
    if db_path is not None:
        return db_path
    from openexecutive.memory import episodic

    return Path(episodic.DB_PATH)


def _connect(db_path: Path | None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db(db_path)))
    ensure_schema(conn)
    return conn


def get(person_id: int, *, db_path: Path | None = None, strict: bool = False) -> Training:
    """Which of ``person_id``'s settings are in training. Never raises:
    unreadable is not (nothing kept), unless ``strict``, for a caller that
    must hold rather than guess."""
    try:
        conn = _connect(db_path)
        try:
            rows = conn.execute(
                f"SELECT setting, enabled FROM {TRAINING_TABLE} WHERE person_id = ?",  # noqa: S608 — constant
                (person_id,),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        if strict:
            raise
        logger.warning("delegation.training: couldn't read the switches", exc_info=True)
        return Training(person_id=person_id)
    on = {setting for setting, enabled in rows if enabled and setting in SETTINGS}
    return Training(person_id=person_id, **{s: s in on for s in SETTINGS})


def set_(
    person_id: int, changes: dict[str, bool], *, updated_by: str, db_path: Path | None = None,
) -> Training:
    """Turn settings in or out of training (callers authorize first). An
    unknown setting is ignored."""
    now = datetime.now(UTC).isoformat()
    conn = _connect(db_path)
    try:
        for setting, enabled in changes.items():
            if setting not in SETTINGS:
                continue
            conn.execute(
                f"INSERT INTO {TRAINING_TABLE} (person_id, setting, enabled, updated_at, updated_by) "  # noqa: S608
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(person_id, setting) DO UPDATE SET "
                "enabled = excluded.enabled, updated_at = excluded.updated_at, updated_by = excluded.updated_by",
                (person_id, setting, 1 if enabled else 0, now, updated_by),
            )
        conn.commit()
    finally:
        conn.close()
    return get(person_id, db_path=db_path)


# --------------------------------------------------------------------------- #
# What one example covers
# --------------------------------------------------------------------------- #


def _email(address: str) -> str:
    from openexecutive.delegation.gmail import normalize_email

    return normalize_email(address or "")


def _prefix(person_id: int) -> str:
    return f"{FEATURE_ACT_AS_ME}|person:{person_id}"


def style_key(person_id: int, recipient: str) -> str:
    return f"{_prefix(person_id)}|style|{_email(recipient)}"


def person_key(person_id: int, address: str) -> str:
    """What one "on its own next time" covers: everything sent as
    ``person_id`` to ``address``."""
    return f"{_prefix(person_id)}|person|{_email(address)}"


def setting_of(key: str) -> str:
    """The setting an item in the list was learned under ("" if none)."""
    parts = key.split("|")
    return _SETTING_OF_ACTION.get(parts[2], "") if len(parts) > 3 else ""


def _who(name: str, address: str) -> str:
    from openexecutive.delegation.ghostwriter import one_line

    shown = one_line(name or "", 80)
    email = _email(address)
    return f"{shown} ({email})" if shown else email


# --------------------------------------------------------------------------- #
# Reading what it learned
# --------------------------------------------------------------------------- #


def _find(person_id: int, key: str, *, db_path: Path | None) -> Allowed | None:
    """The allowance at ``key`` if it is ``person_id``'s. Never raises:
    unreadable is not allowed."""
    from openexecutive.orchestrator import take_the_lead

    try:
        found = take_the_lead.find_allowed(key, db_path=db_path)
    except Exception:
        logger.warning("delegation.training: couldn't read what it's learned", exc_info=True)
        return None
    return found if found is not None and found.person_id == person_id else None


def _text_of(found: Allowed | None) -> str | None:
    if found is None or not found.example:
        return None
    try:
        text = json.loads(found.example).get("text")
    except (ValueError, AttributeError):
        return None
    return text if isinstance(text, str) and text.strip() else None


def example_for(person_id: int, recipient: str, *, db_path: Path | None = None) -> str | None:
    """What ``person_id`` sent ``recipient`` after changing a draft, kept with
    "Do it like this next time"; None when there isn't one."""
    if not _email(recipient):
        return None
    return _text_of(_find(person_id, style_key(person_id, recipient), db_path=db_path))


def allowed_person(person_id: int, address: str, *, db_path: Path | None = None) -> Allowed | None:
    """``person_id``'s allowance for ``address``, if they gave one."""
    if not _email(address):
        return None
    return _find(person_id, person_key(person_id, address), db_path=db_path)


def all_allowed(person_id: int, addresses: list[str], *, db_path: Path | None = None) -> list[Allowed] | None:
    """The allowances covering every one of ``addresses``; None when any is
    missing (or there is nobody to cover)."""
    found = [allowed_person(person_id, a, db_path=db_path) for a in addresses]
    if not found or any(f is None for f in found):
        return None
    return [f for f in found if f is not None]


def reply_goes_to(payload: dict[str, Any]) -> list[str]:
    """Who a reply card's send reaches: everyone the draft goes to
    (``draft_to``), else the sender it answers."""
    to = payload.get("draft_to")
    going = [str(v) for v in to if isinstance(v, str) and v] if isinstance(to, list) else []
    if going:
        return going
    sender = str(payload.get("from_email") or "")
    return [sender] if sender else []


def can_allow_reply(person_id: int, payload: dict[str, Any], trained: Training, *, db_path: Path | None = None) -> bool:
    """Whether a reply card offers "on its own next time": In training is
    on and someone it goes to isn't allowed yet."""
    if not trained.training:
        return False
    going = reply_goes_to(payload)
    return bool(going) and all_allowed(person_id, going, db_path=db_path) is None


def in_training(person_id: int, *, db_path: Path | None = None, strict: bool = False) -> bool:
    """Whether ``person_id``'s As you is in training. Never raises unless ``strict``."""
    return get(person_id, db_path=db_path, strict=strict).training


def room_for(person_id: int, key: str, *, db_path: Path | None = None) -> bool:
    from openexecutive.orchestrator import take_the_lead

    return take_the_lead.room_for(key, person_id=person_id, db_path=db_path)


def room_for_all(person_id: int, addresses: Iterable[str], *, db_path: Path | None = None) -> bool:
    """Whether ``person_id`` can allow every one of ``addresses`` at once:
    the ones not yet allowed must fit together under the list's cap."""
    from openexecutive.orchestrator import take_the_lead

    keys = {person_key(person_id, a) for a in addresses if _email(a)}
    return take_the_lead.room_for_all(keys, person_id=person_id, db_path=db_path)


# --------------------------------------------------------------------------- #
# Learning (callers check it's the person's own tap first)
# --------------------------------------------------------------------------- #


def _allow(
    person_id: int, key: str, label: str, *, decision_id: int | None, example: dict[str, str] | None = None,
    db_path: Path | None,
) -> Allowed:
    from openexecutive.orchestrator import take_the_lead

    return take_the_lead.allow(
        key, label, example=example, created_by=f"person:{person_id}", decision_id=decision_id,
        person_id=person_id, db_path=db_path,
    )


def allow_person(
    person_id: int, address: str, name: str, *, decision_id: int | None, db_path: Path | None = None,
) -> Allowed | None:
    """"Send to them on its own next time": let everything sent as
    ``person_id`` to ``address`` go on its own, where Approvals lets it.
    None when there is no address. Raises ``take_the_lead.RuleError`` when
    their list is full."""
    if not _email(address):
        return None
    return _allow(
        person_id, person_key(person_id, address), f"Send to {_who(name, address)} on its own",
        decision_id=decision_id, db_path=db_path,
    )


def keep_style(
    person_id: int, recipient: str, name: str, text: str, *, decision_id: int | None, db_path: Path | None = None,
) -> Allowed | None:
    """Keep ``text``, what ``person_id`` sent ``recipient`` after changing the
    draft, as how they write to them. None when there is nothing to keep."""
    from openexecutive.integrations.email_poller import sender_new_text

    kept = sender_new_text(text or "").strip()[:EXAMPLE_CHARS]
    if not kept or not _email(recipient):
        return None
    return _allow(
        person_id, style_key(person_id, recipient), f"Writing to {_who(name, recipient)}",
        example={"text": kept}, decision_id=decision_id, db_path=db_path,
    )


def learned(person_id: int, *, db_path: Path | None = None) -> list[Allowed]:
    """``person_id``'s own As you items (who it may send to on its own, and
    how they write), newest first."""
    from openexecutive.orchestrator import take_the_lead

    return [
        a for a in take_the_lead.list_allowed(feature=FEATURE_ACT_AS_ME, person_id=person_id, db_path=db_path)
        if setting_of(a.key) in SETTINGS
    ]


def forget(person_id: int, allowed_id: int, *, db_path: Path | None = None) -> bool:
    """Ask again for one of ``person_id``'s own; anyone else's stays."""
    from openexecutive.orchestrator import take_the_lead

    return take_the_lead.disallow(allowed_id, person_id=person_id, shared=False, db_path=db_path)


def used(allowance: Allowed, *, db_path: Path | None = None) -> None:
    from openexecutive.orchestrator import take_the_lead

    take_the_lead._used(allowance.id, db_path=db_path)


# --------------------------------------------------------------------------- #
# Could go on its own: people whose replies they sent unchanged
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Offer:
    """Replies to one person ``person_id`` sent unchanged ``approvals`` times
    in training, with nobody allowed for that address yet."""

    key: str
    address: str
    name: str
    approvals: int
    last_at: str

    @property
    def label(self) -> str:
        return f"Replies to {self.name or self.address}"


def offers(person_id: int, *, db_path: Path | None = None) -> list[Offer]:
    """Who ``person_id`` could let replies go to on its own: someone they
    sent ``take_the_lead.SUGGEST_AFTER`` reply cards to unchanged in the last
    ``SUGGEST_DAYS`` days, not allowed yet. Only while As you is in training:
    outside it Approvals alone decides, and allowing someone changes
    nothing. After Keep asking, or a removal, only sends since count. Never
    raises: unreadable is none."""
    from openexecutive.delegation.inbox import DECISION_CLASS
    from openexecutive.memory.decision_ledger import STATUS_APPROVED_UNCHANGED, list_instances
    from openexecutive.orchestrator import take_the_lead

    try:
        if not in_training(person_id, db_path=db_path):
            return []
        since = (datetime.now(UTC) - timedelta(days=take_the_lead.SUGGEST_DAYS)).isoformat()
        after = take_the_lead.counted_since(db_path=db_path)
        sent = list_instances(
            DECISION_CLASS, status=STATUS_APPROVED_UNCHANGED, approver_person_id=person_id,
            resolved_since=since, limit=500, db_path=db_path,
        )
    except Exception:
        logger.warning("delegation.training: couldn't read what could go on its own", exc_info=True)
        return []
    counts: dict[str, int] = {}
    names: dict[str, str] = {}
    last: dict[str, str] = {}
    # Each key's address as it was counted, never parsed back out of the key.
    addresses: dict[str, str] = {}
    for card in sent:
        try:
            payload = json.loads(card.proposed_payload_json or "{}")
        except ValueError:
            continue
        if not isinstance(payload, dict) or payload.get("person_id") != person_id:
            continue
        at = card.resolved_at or ""
        sender = _email(str(payload.get("from_email") or ""))
        for address in {_email(a) for a in reply_goes_to(payload)} - {""}:
            key = person_key(person_id, address)
            if key in after and at <= after[key]:
                continue
            counts[key] = counts.get(key, 0) + 1
            last[key] = max(last.get(key, ""), at)
            addresses[key] = address
            if address == sender and payload.get("from_name") and key not in names:
                from openexecutive.delegation.ghostwriter import one_line

                names[key] = one_line(str(payload["from_name"]), 80)
    out: list[Offer] = []
    for key, n in counts.items():
        if n < take_the_lead.SUGGEST_AFTER:
            continue
        address = addresses[key]
        if allowed_person(person_id, address, db_path=db_path) is not None:
            continue
        out.append(Offer(key=key, address=address, name=names.get(key, ""), approvals=n, last_at=last[key]))
    return sorted(out, key=lambda o: (-o.approvals, o.label))


def let_go(person_id: int, key: str, *, db_path: Path | None = None) -> Allowed | None:
    """"Let it go" on one of ``person_id``'s current offers: allow that
    person, as ticking "on its own next time" would. None when ``key``
    isn't a current offer. Raises ``take_the_lead.RuleError`` when their
    list is full."""
    match = next((o for o in offers(person_id, db_path=db_path) if o.key == key), None)
    if match is None:
        return None
    return allow_person(person_id, match.address, match.name, decision_id=None, db_path=db_path)


def keep_asking(person_id: int, key: str, *, db_path: Path | None = None) -> bool:
    """"Keep asking" on one of ``person_id``'s current offers. False when
    ``key`` isn't one."""
    from openexecutive.orchestrator import take_the_lead

    if not any(o.key == key for o in offers(person_id, db_path=db_path)):
        return False
    take_the_lead.dismiss(key, person_id=person_id, db_path=db_path)
    return True
