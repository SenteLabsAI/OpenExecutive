"""Act as me, in training: each setting can be put in training on its own.

A person turns training on per setting (``SETTINGS``, one row each in
``delegation_training``, absent means not in training), from the Training
card on the Act as me page:

- **Replies** (``replies``): every reply the inbox watcher drafts waits on its
  card, whatever Handle it for me's dial says. The card adds **Send + allow**:
  it sends that reply on the person's tap, as Send does, and from then on
  replies to that same sender may go on their own while Handle it for me is
  on, under the dial's limits and every check Handle it always makes
  (``handle_it.refusal``), strangers included.
- **Follow-ups** (``follow_ups``): every follow-up waits; Send + allow on a
  follow-up card lets follow-ups to exactly those people go on their own.
- **Suggested actions** (``actions``): an action card from someone's mail
  (``delegation.action_cards``) adds **Approve + allow**: from then on a card
  whose every action is one they allowed (a message to that person, a meeting
  with exactly those people) is carried out at once, under the checks in
  ``action_cards.on_its_own_refusal``. A new contact always waits.
- **Drafts** (``drafts``): nothing is sent. When they change a reply's draft
  in their mailbox before tapping Send and tick "Do it like this next time",
  what they sent is kept as an example the ghostwriter follows when it next
  writes to that person (``ghostwriter``'s ``<writer_example>``), from the
  inbox and from chat.

What it learns lives in Take the lead's one "What it's learned" list
(``take_the_lead.allow``), as feature ``act_as_me`` with the person's id:
theirs alone to see and remove, and never in anyone else's prompts. Each
item's setting is read from its key (``setting_of``). Allowing happens only
after a tap the API knows is the person's (``reply_send``, the rule Send
uses; ``action_cards.approve``).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openexecutive.delegation.schema import TRAINING_TABLE, ensure_schema
from openexecutive.orchestrator.take_the_lead import FEATURE_ACT_AS_ME, Allowed

logger = logging.getLogger(__name__)

REPLIES = "replies"
FOLLOW_UPS = "follow_ups"
ACTIONS = "actions"
DRAFTS = "drafts"
SETTINGS: tuple[str, ...] = (REPLIES, FOLLOW_UPS, ACTIONS, DRAFTS)

# What each kind of key teaches, by the action in it.
_SETTING_OF_ACTION = {
    "reply": REPLIES, "follow_up": FOLLOW_UPS, "message": ACTIONS, "invite": ACTIONS, "style": DRAFTS,
}

# The longest example kept, after the quoted thread is dropped.
EXAMPLE_CHARS = 1000


@dataclass(frozen=True)
class Training:
    person_id: int
    replies: bool = False
    follow_ups: bool = False
    actions: bool = False
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


def get(person_id: int, *, db_path: Path | None = None) -> Training:
    """Which of ``person_id``'s settings are in training. Never raises:
    unreadable is in training for Replies and Follow-ups (they wait) and not
    for Suggested actions (they wait too) or Drafts (nothing kept)."""
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
        logger.warning("delegation.training: couldn't read the switches", exc_info=True)
        return Training(person_id=person_id, replies=True, follow_ups=True)
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
# What one allow covers
# --------------------------------------------------------------------------- #


def _email(address: str) -> str:
    from openexecutive.delegation.gmail import normalize_email

    return normalize_email(address or "")


def _prefix(person_id: int) -> str:
    return f"{FEATURE_ACT_AS_ME}|person:{person_id}"


def reply_key(person_id: int, sender: str) -> str:
    return f"{_prefix(person_id)}|reply|{_email(sender)}"


def follow_up_key(person_id: int, recipients: list[str]) -> str:
    going = sorted({_email(a) for a in recipients if _email(a)})
    return f"{_prefix(person_id)}|follow_up|{','.join(going)}"


def style_key(person_id: int, recipient: str) -> str:
    return f"{_prefix(person_id)}|style|{_email(recipient)}"


def setting_of(key: str) -> str:
    """The setting an item in the list was learned under ("" if none)."""
    parts = key.split("|")
    return _SETTING_OF_ACTION.get(parts[2], "") if len(parts) > 3 else ""


def _who(name: str, address: str) -> str:
    from openexecutive.delegation.ghostwriter import one_line

    shown = one_line(name or "", 80)
    email = _email(address)
    return f"{shown} ({email})" if shown else email


def reply_label(name: str, sender: str) -> str:
    return f"Reply to {_who(name, sender)}"


def _person_name(person_id: int) -> str | None:
    from openexecutive.people.store import get_person

    person = get_person(person_id)
    if person is None or person.archived:
        return None
    return str(person.full_name or person.email or "") or None


def action_key(person_id: int, action: dict[str, Any]) -> tuple[str, str] | None:
    """What allowing one stored action card action covers, ``(key, label)``:
    a message to that person, or a meeting with exactly those people (the
    person themselves left out). None for anything else: a new contact
    always waits."""
    kind = action.get("kind")
    given = action.get("input")
    stored: dict[str, Any] = given if isinstance(given, dict) else {}
    try:
        if kind == "message":
            target = int(stored["person_id"])
            name = _person_name(target)
            if name is None:
                return None
            return f"{_prefix(person_id)}|message|person:{target}", f"Message {name}"[:200]
        if kind == "invite":
            raw = stored.get("attendee_person_ids")
            if not isinstance(raw, list):
                return None
            ids = sorted({int(i) for i in raw if not isinstance(i, bool)} - {person_id})
            names = [_person_name(i) for i in ids]
            if not ids or any(n is None for n in names):
                return None
            who = ", ".join(n for n in names if n)
            return f"{_prefix(person_id)}|invite|people:{','.join(str(i) for i in ids)}", f"Meetings with {who}"[:200]
    except (KeyError, TypeError, ValueError):
        return None
    return None


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


def allowed_sender(person_id: int, sender: str, *, db_path: Path | None = None) -> Allowed | None:
    """The allowance for ``person_id``'s replies to ``sender``, if they gave one."""
    if not _email(sender):
        return None
    return _find(person_id, reply_key(person_id, sender), db_path=db_path)


def allowed_follow_up(person_id: int, recipients: list[str], *, db_path: Path | None = None) -> Allowed | None:
    """The allowance for ``person_id``'s follow-ups to exactly ``recipients``."""
    if not any(_email(a) for a in recipients):
        return None
    return _find(person_id, follow_up_key(person_id, recipients), db_path=db_path)


def allowed_action(person_id: int, action: dict[str, Any], *, db_path: Path | None = None) -> Allowed | None:
    """The allowance covering one stored action card action, if any."""
    found = action_key(person_id, action)
    return _find(person_id, found[0], db_path=db_path) if found is not None else None


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
    "Do it like this next time"; None when there isn't one. An example kept
    on a reply allowance before Drafts had its own switch still counts."""
    if not _email(recipient):
        return None
    return _text_of(_find(person_id, style_key(person_id, recipient), db_path=db_path)) or _text_of(
        allowed_sender(person_id, recipient, db_path=db_path)
    )


def room_for(person_id: int, key: str, *, db_path: Path | None = None) -> bool:
    from openexecutive.orchestrator import take_the_lead

    return take_the_lead.room_for(key, person_id=person_id, db_path=db_path)


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


def allow_sender(
    person_id: int, sender: str, name: str, *, decision_id: int | None, db_path: Path | None = None,
) -> Allowed:
    """Let replies to ``sender`` go on their own for ``person_id``. Raises
    ``take_the_lead.RuleError`` when their list is full."""
    return _allow(person_id, reply_key(person_id, sender), reply_label(name, sender),
                  decision_id=decision_id, db_path=db_path)


def allow_follow_up(
    person_id: int, recipients: list[str], *, decision_id: int | None, db_path: Path | None = None,
) -> Allowed:
    """Let follow-ups to exactly ``recipients`` go on their own."""
    from openexecutive.delegation.ghostwriter import one_line

    going = sorted({_email(a) for a in recipients if _email(a)})
    label = one_line(f"Follow up with {', '.join(going)}", 200)
    return _allow(person_id, follow_up_key(person_id, going), label, decision_id=decision_id, db_path=db_path)


def allow_action(
    person_id: int, action: dict[str, Any], *, decision_id: int | None, db_path: Path | None = None,
) -> Allowed | None:
    """Let actions like this one happen on their own; None when it can't be
    allowed (a new contact, someone no longer on the roster)."""
    found = action_key(person_id, action)
    if found is None:
        return None
    return _allow(person_id, found[0], found[1], decision_id=decision_id, db_path=db_path)


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
    """``person_id``'s own Act as me items, newest first."""
    from openexecutive.orchestrator import take_the_lead

    return take_the_lead.list_allowed(feature=FEATURE_ACT_AS_ME, person_id=person_id, db_path=db_path)


def forget(person_id: int, allowed_id: int, *, db_path: Path | None = None) -> bool:
    """Ask again for one of ``person_id``'s own; anyone else's stays."""
    from openexecutive.orchestrator import take_the_lead

    return take_the_lead.disallow(allowed_id, person_id=person_id, shared=False, db_path=db_path)


def used(allowance: Allowed, *, db_path: Path | None = None) -> None:
    from openexecutive.orchestrator import take_the_lead

    take_the_lead._used(allowance.id, db_path=db_path)
