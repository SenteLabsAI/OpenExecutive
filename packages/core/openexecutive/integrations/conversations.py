"""What the chat adapters share about conversations (``memory.conversations``).

Each adapter computes its stream's base id as before, then asks
``conversation_for`` which conversation a message belongs to, answers
``/new`` with ``start_fresh``, and calls ``title_after_turn`` once a turn is
saved so a conversation's first exchange gives it a title, as a web chat's
does.
"""
from __future__ import annotations

import asyncio
import logging
import re

from openexecutive.memory.conversation_ids import base_session_id, is_rolling

logger = logging.getLogger(__name__)

# A whole message asking for a fresh conversation. Slack and Discord keep
# `/…` for their own commands, so the words work too.
_NEW_COMMAND_RE = re.compile(r"(?:/new(?:@\w+)?|new (?:chat|conversation))[.!]?", re.IGNORECASE)
_NEW_COMMAND_MAX = 64

_title_tasks: set[asyncio.Task[None]] = set()


def is_new_conversation_command(text: str) -> bool:
    text = (text or "").strip()
    return len(text) <= _NEW_COMMAND_MAX and bool(_NEW_COMMAND_RE.fullmatch(text))


def conversation_for(base_id: str) -> str:
    """The conversation a message in ``base_id``'s stream belongs to. Sync
    (SQLite); never raises — on a read failure the message stays in the
    stream's first conversation, as before conversations existed."""
    if not is_rolling(base_id):
        return base_id
    from openexecutive.memory.conversations import current_conversation

    try:
        return current_conversation(base_id)
    except Exception:
        logger.exception("conversations: couldn't pick the conversation for %s", base_id)
        return base_id


def start_fresh(base_id: str, owner_person_id: int | None) -> str:
    """Start a new conversation in ``base_id``'s stream and return the reply
    to send. Sync (SQLite)."""
    from openexecutive.memory.conversations import (
        NEW_CONVERSATION_TITLE,
        start_new_conversation,
    )

    try:
        _new_id, ended = start_new_conversation(
            base_session_id(base_id), owner_person_id=owner_person_id
        )
    except Exception:
        logger.exception("conversations: couldn't start a new conversation for %s", base_id)
        return "I couldn't start a new conversation just now. Please try again."
    if ended and ended != NEW_CONVERSATION_TITLE:
        return (
            "Started a fresh conversation. The last one is saved in Chats as "
            f"“{ended}”."
        )
    return "Started a fresh conversation."


async def _title(session_id: str, user_text: str, reply: str) -> None:
    from openexecutive.memory.conversations import first_line_title
    from openexecutive.memory.session_store import (
        get_session_metadata,
        update_session_title,
    )
    from openexecutive.utils.session_title import generate_session_title

    meta = await asyncio.to_thread(get_session_metadata, session_id)
    # Only a conversation's first exchange names it; later turns and a title
    # someone already gave it are left alone.
    if meta is None or int(meta.get("message_count") or 0) != 2:
        return
    title = await generate_session_title(user_text, reply) or first_line_title(user_text)
    if title:
        await asyncio.to_thread(update_session_title, session_id, title)


def title_after_turn(session_id: str, user_text: str, reply: str) -> None:
    """Name a chat-app conversation from its first exchange, in the
    background. Call after the turn is saved; a no-op on any later turn or
    for a conversation that isn't a chat-app stream's."""
    if not is_rolling(session_id):
        return
    try:
        task = asyncio.get_running_loop().create_task(_title(session_id, user_text, reply))
    except RuntimeError:
        return
    _title_tasks.add(task)

    def _done(t: asyncio.Task[None]) -> None:
        _title_tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.error(
                "conversations: titling %s failed", session_id, exc_info=t.exception()
            )

    task.add_done_callback(_done)
