"""Where the Executive reaches a person: one rule for every message it sends.

``reach_order`` puts the channels a person has an address or id for in the
order to try:

1. Their preferred channel, when they picked one.
2. Otherwise (preference ``any``) the chat app they last talked to the
   Executive in, from their own account (``note_talked``).
3. Then the other chat apps: Slack, Discord, Telegram.
4. Email last, as the backup, unless it was the one picked.

Briefs and reminders (``scheduler.runner.delivery_order``), messages
(``orchestrator.schedule_tools.message_person``), follow-ups and nudges
(``people.channel``) all take their order from here; each still checks that
its channel is set up and the stored id is usable before sending.
"""
from __future__ import annotations

import logging
from typing import Any

from openexecutive.people.models import Person

logger = logging.getLogger(__name__)

CHAT_ORDER: tuple[str, ...] = ("slack", "discord", "telegram")
# The chat apps whose messages can tell where a person last talked.
_TALK_CHANNELS = frozenset(CHAT_ORDER)


def channel_id(person: Person, channel: str) -> str | None:
    """The person's stored address or id for ``channel``."""
    if channel == "email":
        return person.email
    if channel == "slack":
        return person.slack_user_id
    if channel == "discord":
        return person.discord_user_id
    if channel == "telegram":
        return person.telegram_chat_id
    return None


def first_choice(person: Person) -> str | None:
    """The channel to try first: the one they picked, else where they last
    talked."""
    pref = (person.preferred_channel or "any").lower()
    if pref != "any":
        return pref
    last = (person.last_channel or "").lower()
    return last if last in _TALK_CHANNELS else None


def _private_telegram(person: Person) -> bool:
    return str(person.telegram_chat_id or "").isdigit()


def reach_order(person: Person, *, email: bool = True, telegram_groups: bool = False) -> list[str]:
    """The channels ``person`` has an address or id for, in the order to try
    (see the module docstring). ``email=False`` leaves email out. A Telegram
    group id (negative: everyone in the group reads it) counts only with
    ``telegram_groups``, for a message someone asked to send there."""
    chat = [
        c for c in CHAT_ORDER
        if channel_id(person, c) and (c != "telegram" or telegram_groups or _private_telegram(person))
    ]
    first = first_choice(person)
    order = ([first] if first in chat else []) + [c for c in chat if c != first]
    if email and person.email:
        if first == "email":
            order.insert(0, "email")
        else:
            order.append("email")
    return order


def note_talked(session: Any) -> bool:
    """Remember the chat app this turn came in on as where its person last
    talked: only when the turn is from their own account there (the stored
    Slack or Discord user id, or their private Telegram chat). Sync (SQLite);
    never raises. True when it recorded it."""
    channel = str(getattr(session, "origin_channel", "") or "")
    person_id = getattr(session, "caller_person_id", None)
    ref = str(getattr(session, "origin_channel_ref", "") or "")
    if channel not in _TALK_CHANNELS or not isinstance(person_id, int) or not ref:
        return False
    try:
        from openexecutive.people.store import get_person, note_last_channel

        person = get_person(person_id)
        if person is None or person.archived or channel_id(person, channel) != ref:
            return False
        if channel == "telegram":
            if not ref.isdigit():
                return False  # a group chat isn't theirs alone
            from openexecutive.integrations.channel_cards import telegram_proves_taps

            # Without a valid webhook secret anyone can post a Telegram update.
            if not telegram_proves_taps():
                return False
        return note_last_channel(person_id, channel)
    except Exception:
        logger.warning("reach: couldn't record where person %s last talked", person_id, exc_info=True)
        return False
