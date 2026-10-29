"""Approval cards in a chat app, with Approve and Skip buttons.

The web chat shows the cards a reply leaves (a meeting to book, a card of
actions from ``propose_actions``) under that reply, for the person chatting
to tap. A chat app that can prove who taps a button shows them the same way:
the adapter collects the reply's cards (``Executive.chat(cards=...)``), sends
each as a short message with two buttons, and passes a tap to
``api.routes.decisions.resolve_in_chat``, which applies the web chat's rule
for who may decide it.

Where a tap proves who made it, the same bar as a verified speaker there
(``people_tools._is_verified_speaker_surface``):

- Telegram: a private chat (the chat is the person; ``from.id`` must equal
  the chat id) on a webhook only Telegram can call (``TELEGRAM_WEBHOOK_SECRET``
  set and valid).
- Slack: a DM, with ``SLACK_CARD_BUTTONS`` on (Interactivity turned on in
  the Slack app). Slack signs the tap (Socket Mode, or the signing secret
  over HTTP), and it counts only from the user whose DM with the bot it is.
- Discord: a DM. The tap comes over the bot's own gateway connection, and
  counts only from the user whose DM with the bot it is.

Anywhere else no card is shown and the Executive is told the card is on Today.

A card that waits for someone outside any chat (a reply the inbox watcher
drafted, a meeting invite, an action Take the lead or a department held) is
sent to their chat app once it has waited ``PUSH_AFTER``
(:func:`push_waiting`, from the scheduler's tick): the first chat app in
their reach order that proves taps, outside their quiet hours. A reply card
has Send and Skip, an invite Accept and Skip.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# A button's data (Telegram callback_data, a Slack button value, a Discord
# custom_id): "card:<decision id>:a" (Approve) or ":s" (Skip).
_PREFIX = "card"
APPROVE = "a"
SKIP = "s"
# A reply leaves at most this many cards (action_cards.CARDS_PER_TURN plus a meeting).
MAX_CARDS = 4

# What a second tap gets while the first is still deciding the card; the
# card itself is left for the first tap to update.
BUSY = "Already on it."
# Cards a tap is deciding right now (one process serves a company's taps).
_resolving: set[int] = set()

_CLASS_LABEL = {
    "meeting_scheduling": "Meeting to book",
    "delegation_actions": "Waiting for your approval",
    "app_action": "Waiting for your OK",
    "delegation_reply": "Reply ready to send",
    "delegation_invite": "Meeting invite",
    "take_the_lead_action": "Waiting for your OK",
    "department_action": "Waiting for your approval",
}
# The approve button's label and what the card says once it is done, where
# they aren't Approve and Approved.
_APPROVE_LABEL = {"delegation_reply": "Send", "delegation_invite": "Accept"}
_DONE_TEXT = {"delegation_reply": "Sent.", "delegation_invite": "Accepted."}
# A card waiting outside any chat is shown whole or not at all: its tap
# sends or does exactly what is stored, so a card cut short would approve
# what it never showed. This fits every chat app's message (Discord's 2000
# is the smallest).
CARD_MAX = 1800
_WHOLE_CLASSES = frozenset({"delegation_reply", "take_the_lead_action", "department_action"})


def telegram_proves_taps() -> bool:
    """Whether a Telegram update can only come from Telegram."""
    from openexecutive.config import get_settings

    s = get_settings()
    return bool(s.telegram_bot_token and s.telegram_webhook_secret and s.telegram_webhook_secret_valid)


def _dm_proves_taps(channel: str) -> bool:
    """Whether a Slack or Discord DM here can show cards with buttons."""
    from openexecutive.config import get_settings

    s = get_settings()
    if channel == "slack":
        return bool(s.slack_bot_token and s.slack_card_buttons)
    return bool(s.discord_bot_token)


def shows_cards(session: Any) -> bool:
    """Whether this turn's chat shows its cards with buttons: a private
    Telegram chat with someone on the roster, on a webhook only Telegram can
    call, or a Slack or Discord DM with someone on the roster. Fails closed."""
    channel = str(getattr(session, "origin_channel", "") or "") if session is not None else ""
    if getattr(session, "caller_person_id", None) is None:
        return False
    try:
        if channel == "telegram":
            ref = str(getattr(session, "origin_channel_ref", "") or "")
            return ref.isdigit() and telegram_proves_taps()
        if channel in ("slack", "discord"):
            from openexecutive.delegation.settings import private_conversation

            return private_conversation(session) and _dm_proves_taps(channel)
    except Exception:
        logger.warning("channel_cards: couldn't read the %s settings", channel, exc_info=True)
    return False


def card_here(session: Any) -> bool:
    """Whether a card left on this turn is shown in the chat it was left in:
    the web chat (and the phone app, which uses it), or a chat app that
    ``shows_cards``."""
    return bool(getattr(session, "from_web_chat", False)) or shows_cards(session)


def callback_data(decision_id: int, choice: str) -> str:
    return f"{_PREFIX}:{decision_id}:{choice}"


def parse_callback(data: Any) -> tuple[int, bool] | None:
    """(decision id, approve) from a button's callback data, or None."""
    if not isinstance(data, str):
        return None
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != _PREFIX or parts[2] not in (APPROVE, SKIP):
        return None
    if not parts[1].isdigit() or len(parts[1]) > 12:
        return None
    return int(parts[1]), parts[2] == APPROVE


def _when(start: Any, end: Any) -> str:
    try:
        s = datetime.fromisoformat(str(start))
        e = datetime.fromisoformat(str(end))
    except ValueError:
        return ""
    day = s.strftime("%a %d %b").replace(" 0", " ")
    return f"{day}, {s.strftime('%H:%M')}-{e.strftime('%H:%M')}"


def card_text(decision_id: int) -> str | None:
    """What the card says, or None when it isn't one a chat shows or isn't
    waiting any more."""
    from openexecutive.api.routes.decisions import shown_in_chat
    from openexecutive.memory.decision_ledger import STATUS_PROPOSED, get_decision_instance

    instance = get_decision_instance(decision_id)
    if instance is None or not shown_in_chat(instance):
        return None
    if instance.status != STATUS_PROPOSED:
        return None
    try:
        payload = json.loads(instance.proposed_payload_json or "{}")
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    lines = [_CLASS_LABEL[instance.decision_class]]
    if instance.decision_class == "meeting_scheduling":
        title = str(payload.get("title") or "Meeting").strip()[:200]
        lines.append(title)
        when = _when(payload.get("start"), payload.get("end"))
        if when:
            lines.append(when)
        who = [str(a) for a in payload.get("attendee_emails") or [] if a][:10]
        if who:
            lines.append("With " + ", ".join(who))
    elif instance.decision_class == "delegation_reply":
        sender = str(payload.get("from_name") or payload.get("from_email") or "").strip()[:120]
        if sender:
            lines.append(f"To {sender}")
        to = [str(a) for a in payload.get("draft_to") or [] if a][:10]
        if to:
            lines.append("Goes to " + ", ".join(to))
        subject = str(payload.get("draft_subject") or payload.get("subject") or "").strip()[:200]
        if subject:
            lines.append(subject)
        body = str(payload.get("draft_body") or "").strip()
        if body:
            lines.append("")
            lines.append(body)
    elif instance.decision_class == "delegation_invite":
        lines.append(str(payload.get("title") or "Meeting").strip()[:200])
        when = _when(payload.get("start"), payload.get("end"))
        if when:
            lines.append(when)
        sender = str(payload.get("from_name") or payload.get("from_email") or "").strip()[:120]
        if sender:
            lines.append(f"From {sender}")
        if payload.get("clashes"):
            lines.append("It clashes with something on your calendar.")
    elif instance.decision_class in ("take_the_lead_action", "department_action"):
        if payload.get("requested_by"):
            lines.append(f"{str(payload['requested_by'])[:120]} asked for this.")
        lines.append(str(payload.get("summary") or payload.get("tool") or "").strip()[:400])
        reason = str(payload.get("reason") or "").strip()[:300]
        if reason:
            lines.append(f"It waited because {reason}.".replace("..", "."))
        # Everything the call says, as the web card shows it before Approve.
        tool_input = payload.get("input")
        if not isinstance(tool_input, dict):
            return None
        lines.append("")
        for key, value in tool_input.items():
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
            lines.append(f"{key}: {text}")
    elif instance.decision_class == "app_action":
        # Written by the server from the change itself (workflow_change.card_text).
        # Never cut: workflow_change stages only a card that fits.
        lines.append(str(payload.get("card") or "").strip())
    else:
        why = str(payload.get("why") or "").strip()[:300]
        if why:
            lines.append(why)
        for action in payload.get("actions") or []:
            if isinstance(action, dict) and action.get("summary"):
                lines.append(f"- {str(action['summary'])[:400]}")
    text = "\n".join(lines)
    if instance.decision_class in _WHOLE_CLASSES and len(text) > CARD_MAX:
        # Too long to show whole: it waits in the app instead.
        return None
    return text


def card_buttons(decision_id: int) -> tuple[str, str]:
    """The labels of a card's two buttons: Send, Accept or Approve, and Skip."""
    from openexecutive.memory.decision_ledger import get_decision_instance

    instance = get_decision_instance(decision_id)
    label = _APPROVE_LABEL.get(instance.decision_class, "Approve") if instance is not None else "Approve"
    return label, "Skip"


def outcome_text(approved: bool, error: str | None, decision_class: str = "") -> str:
    if error:
        return f"Not done: {error}"
    return _DONE_TEXT.get(decision_class, "Approved.") if approved else "Skipped."


def outcome_of(line: str) -> str:
    """``done``, ``busy`` or ``refused``, for the audit row of a tap."""
    if line == BUSY:
        return "busy"
    return "refused" if line.startswith("Not done") else "done"


async def resolve(decision_id: int, person: Any, *, approve: bool) -> str:
    """Approve or skip a card for ``person``, the proven tapper. Returns the
    line to show under the card."""
    from fastapi import HTTPException

    from openexecutive.api.routes.decisions import resolve_in_chat
    from openexecutive.memory.decision_ledger import get_decision_instance

    # The buttons stay until the card is edited after this returns, so a
    # second tap can land while the first is still booking: it is turned
    # away rather than racing it to a second invite.
    if decision_id in _resolving:
        return BUSY
    _resolving.add(decision_id)
    instance = get_decision_instance(decision_id)
    kind = instance.decision_class if instance is not None else ""
    try:
        await resolve_in_chat(decision_id, person, approve=approve)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else "it couldn't be done."
        return outcome_text(approve, detail)
    except Exception:
        logger.exception("channel_cards: resolving card %d failed", decision_id)
        return outcome_text(approve, "something went wrong. Check Today before trying again.")
    finally:
        _resolving.discard(decision_id)
    return outcome_text(approve, None, kind)


# --------------------------------------------------------------------------- #
# Cards that wait outside any chat, sent to the person's chat app
# --------------------------------------------------------------------------- #

# A card waits this long first, so one handled on the web right away is not
# sent too, and is sent only while it is this young.
PUSH_AFTER = timedelta(minutes=2)
PUSH_WITHIN = timedelta(hours=24)
SCAN_EVERY = timedelta(minutes=1)
# At most this many cards to one person in a pass; the rest go on the next.
PUSH_PER_PASS = 5

_TABLE = """
    CREATE TABLE IF NOT EXISTS chat_card_pushes (
        decision_id INTEGER PRIMARY KEY,
        person_id INTEGER NOT NULL,
        channel TEXT NOT NULL,
        pushed_at TEXT NOT NULL
    )
"""

_last_scan_at: datetime | None = None
_task: asyncio.Task[int] | None = None


def _connect(db_path: Path | None) -> sqlite3.Connection:
    from openexecutive.memory import episodic

    conn = sqlite3.connect(str(db_path or episodic.DB_PATH))
    conn.execute(_TABLE)
    return conn


def _pushed(decision_ids: list[int], db_path: Path | None) -> set[int]:
    if not decision_ids:
        return set()
    conn = _connect(db_path)
    try:
        marks = ",".join("?" * len(decision_ids))
        rows = conn.execute(
            f"SELECT decision_id FROM chat_card_pushes WHERE decision_id IN ({marks})", decision_ids,
        ).fetchall()
    finally:
        conn.close()
    return {int(r[0]) for r in rows}


def _mark_pushed(decision_id: int, person_id: int, channel: str, now: datetime, db_path: Path | None) -> bool:
    """Claim the card for sending; False when it was claimed already."""
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO chat_card_pushes (decision_id, person_id, channel, pushed_at) VALUES (?,?,?,?)",
            (decision_id, person_id, channel, now.isoformat()),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def chat_for(person: Any) -> str | None:
    """The chat app a card for ``person`` goes to: the first in their reach
    order (``people.reach``) whose taps prove who made them, or None."""
    from openexecutive.people.reach import reach_order

    for channel in reach_order(person, email=False):
        try:
            if channel == "telegram" and str(person.telegram_chat_id or "").isdigit() and telegram_proves_taps():
                return channel
            if channel in ("slack", "discord") and _dm_proves_taps(channel):
                return channel
        except Exception:
            logger.warning("channel_cards: couldn't read the %s settings", channel, exc_info=True)
    return None


def _waiting(now: datetime) -> list[Any]:
    """Open cards made outside any chat that have waited ``PUSH_AFTER`` and
    are younger than ``PUSH_WITHIN``, oldest first."""
    from openexecutive.api.routes.decisions import CHAT_CARD_CLASSES, shown_in_chat
    from openexecutive.memory.decision_ledger import STATUS_PROPOSED, list_instances

    cards = []
    for decision_class in sorted(CHAT_CARD_CLASSES):
        for card in list_instances(decision_class, status=STATUS_PROPOSED, limit=200):
            # A card made on its own person's turn was shown there; a
            # department's is made on the asker's turn, never its approver's.
            shown = card.originating_session_id and decision_class != "department_action"
            if shown or card.gate_mode != "propose" or not shown_in_chat(card):
                continue
            try:
                made = datetime.fromisoformat(card.created_at)
            except ValueError:
                continue
            if made.tzinfo is None:
                made = made.replace(tzinfo=UTC)
            if now - PUSH_WITHIN <= made <= now - PUSH_AFTER:
                cards.append(card)
    return sorted(cards, key=lambda c: c.id)


async def _send(channel: str, person: Any, decision_id: int, text: str, labels: tuple[str, str]) -> None:
    from openexecutive.config import get_settings

    if channel == "telegram":
        from openexecutive.integrations.telegram_bot import send_card

        await send_card(
            str(get_settings().telegram_bot_token or ""), int(person.telegram_chat_id), decision_id, text,
            labels=labels, raise_on_error=True,
        )
    elif channel == "slack":
        from openexecutive.integrations.slack_bot import send_dm_card

        await send_dm_card(str(person.slack_user_id), decision_id, text, labels)
    elif channel == "discord":
        from openexecutive.integrations.discord_bot import send_dm_card as discord_card

        await discord_card(str(person.discord_user_id), decision_id, text, labels)


async def push_waiting(now: datetime | None = None, *, db_path: Path | None = None) -> int:
    """Send each card waiting outside any chat to its person's chat app, once.
    Returns how many went. Never raises."""
    from openexecutive.audit import log_event
    from openexecutive.people.channel import is_within_availability
    from openexecutive.people.store import find_principal_person, get_person

    now = now or datetime.now(UTC)
    sent = 0
    try:
        cards = _waiting(now)
        done = _pushed([c.id for c in cards], db_path)
        principal = find_principal_person() if any(c.approver_person_id is None for c in cards) else None
    except Exception:
        logger.warning("channel_cards: couldn't read the waiting cards", exc_info=True)
        return 0
    per_person: dict[int, int] = {}
    for card in cards:
        if card.id in done:
            continue
        person = get_person(card.approver_person_id) if card.approver_person_id is not None else principal
        if person is None or person.id is None or person.archived:
            continue
        if per_person.get(person.id, 0) >= PUSH_PER_PASS:
            continue
        channel = chat_for(person)
        if channel is None or not is_within_availability(person, now=now, quiet_hours=True):
            continue
        text = card_text(card.id)
        if not text or not _mark_pushed(card.id, person.id, channel, now, db_path):
            continue
        try:
            await _send(channel, person, card.id, text, card_buttons(card.id))
        except Exception:
            logger.warning("channel_cards: couldn't send card %d to %s", card.id, channel, exc_info=True)
            continue
        sent += 1
        per_person[person.id] = per_person.get(person.id, 0) + 1
        try:
            log_event(
                "chat_card_sent", f"Sent card {card.id} to {channel}", actor="executive",
                details={"channel": channel, "decision_id": card.id, "person_id": person.id},
                private=True, private_to_person=person.id,
            )
        except Exception:
            logger.warning("channel_cards: couldn't audit card %d", card.id, exc_info=True)
    return sent


def maybe_push(now: datetime) -> bool:
    """Start a pass of :func:`push_waiting` when ``SCAN_EVERY`` has passed and
    none is running. True when one started. Never raises."""
    global _last_scan_at, _task
    try:
        if _task is not None and not _task.done():
            return False
        if _last_scan_at is not None and now - _last_scan_at < SCAN_EVERY:
            return False
        _last_scan_at = now
        _task = asyncio.get_running_loop().create_task(push_waiting(now))
        return True
    except Exception:
        logger.warning("channel_cards: couldn't start a pass", exc_info=True)
        return False


__all__ = [
    "MAX_CARDS",
    "callback_data",
    "card_buttons",
    "card_here",
    "card_text",
    "chat_for",
    "maybe_push",
    "parse_callback",
    "push_waiting",
    "resolve",
    "shows_cards",
    "telegram_proves_taps",
]
