"""What the chat adapters share about conversations (``memory.conversations``).

Each adapter computes its stream's base id as before, then asks
``conversation_for`` which conversation a message belongs to, answers
``/new`` with ``start_fresh``, and calls ``title_after_turn`` once a turn is
saved so a conversation's first exchange gives it a title, as a web chat's
does. ``record_delivery`` saves what the Executive sent on its own (a brief,
a digest, a workflow's report) into the conversation, so a reply to it has
it in view.
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

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


def record_delivery(
    base_id: str,
    text: str,
    *,
    owner_person_id: int | None,
    title: str,
    db_path: Path | None = None,
) -> str | None:
    """Save ``text``, which the Executive just sent into ``base_id``'s stream
    on its own, as its message in the conversation a reply would join.

    Without it the reply's turn has nothing before it: "can you fix it?"
    after the evening digest reads as a question out of nowhere, and when the
    stream was quiet for a while the reply even starts a new conversation.
    Saving it also counts as activity, so the reply lands in this same
    conversation. ``title`` names a conversation this message starts; an
    existing one keeps its name. Returns the conversation's id, or None when
    it couldn't be saved. Sync (SQLite); never raises.
    """
    if not is_rolling(base_id) or not (text or "").strip():
        return None
    from datetime import UTC, datetime

    from openexecutive.memory import episodic
    from openexecutive.memory.conversations import current_conversation
    from openexecutive.memory.session_store import (
        create_session,
        save_message,
        update_session_timestamp,
    )

    path = db_path or episodic.DB_PATH
    try:
        session_id = current_conversation(base_session_id(base_id), db_path=path)
        create_session(
            session_id, title, datetime.now(UTC).isoformat(),
            caller_person_id=owner_person_id, db_path=path,
        )
        save_message(session_id, "assistant", text, db_path=path)
        update_session_timestamp(session_id, db_path=path)
    except Exception:
        logger.exception("conversations: couldn't save a delivery into %s", base_id)
        return None
    return session_id


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
    from openexecutive.utils.chat_topics import name_chat

    meta = await asyncio.to_thread(get_session_metadata, session_id)
    # Only a conversation's first exchange names it; later turns and a title
    # someone already gave it are left alone. Naming also files it under a
    # topic on its owner's Chats page.
    if meta is None or int(meta.get("message_count") or 0) != 2:
        return
    if not await name_chat(session_id, user_text, reply):
        title = first_line_title(user_text)
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
