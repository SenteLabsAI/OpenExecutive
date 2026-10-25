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
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
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
}


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
    return "\n".join(lines)


def outcome_text(approved: bool, error: str | None) -> str:
    if error:
        return f"Not done: {error}"
    return "Approved." if approved else "Skipped."


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

    # The buttons stay until the card is edited after this returns, so a
    # second tap can land while the first is still booking: it is turned
    # away rather than racing it to a second invite.
    if decision_id in _resolving:
        return BUSY
    _resolving.add(decision_id)
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
    return outcome_text(approve, None)


__all__ = [
    "MAX_CARDS",
    "callback_data",
    "card_here",
    "card_text",
    "parse_callback",
    "resolve",
    "shows_cards",
    "telegram_proves_taps",
]
