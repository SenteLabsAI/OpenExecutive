"""The model side of chat topics (store: ``memory.chat_topics``).

- ``name_chat``: a new chat's first exchange gets its title and its topic in
  the one utility-fast call that used to give only the title.
- ``tidy_new_topic``: when that call makes a new topic, one short call over
  the topic names alone checks it against the owner's other topics and merges
  it into one that means the same. No call on a turn that made no topic.
- ``backfill``: chats from before topics (or whose naming failed) are filed
  in batches, from their titles only, the first time the owner opens the
  Topic view.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from typing import Any

from openexecutive.memory import chat_topics as store
from openexecutive.utils.session_title import clean_title

logger = logging.getLogger(__name__)

_NAME_SYSTEM = (
    "You name a chat thread and file it under a topic. Reply with exactly two "
    "lines and nothing else:\n"
    "Title: <a concise 4-6 word title>\n"
    "Topic: <topic>\n"
    "For Topic, reuse one of the person's existing topics, written exactly as "
    "listed, when the chat is about the same matter. Otherwise give a new "
    "2-4 word topic naming the matter (a project, deal, hire, customer, "
    "person or area of the business), broad enough that later chats about it "
    "fit too. Write none for small talk or a one-off question with no lasting "
    "matter."
)

_TIDY_SYSTEM = (
    "You keep a person's list of chat topics free of duplicates. You get one "
    "new topic and their other topics. If the new topic is about the same "
    "matter as one of the others, reply with that other topic exactly as "
    "listed. Otherwise reply none. Reply with the topic or none only."
)

_BACKFILL_SYSTEM = (
    "You file a person's chats under topics, from their titles. Reply with one "
    "line per chat, in the form <number>: <topic>, and nothing else. Reuse an "
    "existing topic, written exactly as listed, when a chat is about the same "
    "matter. Otherwise give a new 2-4 word topic naming the matter (a project, "
    "deal, hire, customer, person or area of the business), and put chats "
    "about the same matter under the same topic. Write none for small talk or "
    "a one-off question with no lasting matter."
)

# Titles per backfill call: a few thousand tokens in, one short line each out.
BACKFILL_BATCH = 80
# After a backfill fails, the Topic view doesn't start another for a while.
BACKFILL_RETRY_S = 600.0

_tasks: set[asyncio.Task[Any]] = set()
_backfilling: set[int] = set()
_backfill_failed_at: dict[int, float] = {}


def _keep(task: asyncio.Task[Any]) -> None:
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def _topic_list(names: list[str]) -> str:
    return "\n".join(f"- {n}" for n in names) if names else "(none yet)"


async def _ask(system: str, content: str, *, max_tokens: int, actor: str) -> str:
    from openexecutive.agents.utility_fast import get_fast_model
    from openexecutive.audit.usage import log_model_usage
    from openexecutive.providers import get_provider

    model = get_fast_model()
    response = await get_provider(model).messages_create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": content}],
    )
    log_model_usage(response, model=model, actor=actor)
    return "".join(
        getattr(b, "text", "") for b in response.content if getattr(b, "type", "") == "text"
    ).strip()


def parse_title_and_topic(raw: str) -> tuple[str | None, str | None]:
    """``Title: …`` / ``Topic: …`` lines. A reply without the labels is
    taken as a bare title, so a model that ignores the format still names
    the chat."""
    title: str | None = None
    topic: str | None = None
    for line in raw.splitlines():
        label, _, rest = line.partition(":")
        key = label.strip().lower()
        if key == "title" and title is None:
            title = clean_title(rest)
        elif key == "topic" and topic is None:
            topic = store.clean_topic_name(rest)
    if title is None and topic is None and raw.strip():
        title = clean_title(raw.strip().splitlines()[0])
    return title, topic


async def _title_and_topic(
    user_msg: str, assistant_msg: str, names: list[str]
) -> tuple[str | None, str | None]:
    """The one call that names a chat: its title and its topic."""
    raw = await _ask(
        _NAME_SYSTEM,
        f"EXISTING TOPICS:\n{_topic_list(names)}\n\n"
        f"USER MESSAGE:\n{user_msg[:2000]}\n\n"
        f"EXECUTIVE RESPONSE:\n{assistant_msg[:2000]}",
        max_tokens=48,
        actor="session_title",
    )
    return parse_title_and_topic(raw)


async def name_chat(session_id: str, user_msg: str, assistant_msg: str) -> str | None:
    """Title a chat from its first exchange and file it under a topic.
    Returns the title (None when the call failed; the caller keeps its
    placeholder). A chat with no owner only gets a title."""
    from openexecutive.memory.session_store import get_session_owner, update_session_title
    from openexecutive.utils.session_title import generate_session_title

    # Two small indexed reads, done inline so the call starts on this step:
    # the stop path dispatches naming as a task and must see it under way.
    try:
        _, owner = get_session_owner(session_id)
    except Exception:
        logger.exception("name_chat: couldn't read the chat's owner")
        owner = None
    if owner is None:
        title = await generate_session_title(user_msg, assistant_msg)
        if title:
            await asyncio.to_thread(update_session_title, session_id, title)
        return title
    try:
        names = store.topic_names(owner)
        title, topic = await _title_and_topic(user_msg, assistant_msg, names)
    except Exception:
        logger.exception("name_chat: failed")
        return None
    if title:
        await asyncio.to_thread(update_session_title, session_id, title)
    topic_id, made = await asyncio.to_thread(store.set_auto_topic, session_id, owner, topic)
    if made and topic_id is not None and len(names) > 0:
        with contextlib.suppress(RuntimeError):  # no running loop
            _keep(asyncio.get_running_loop().create_task(tidy_new_topic(owner, topic_id)))
    return title


async def tidy_new_topic(owner: int, topic_id: int) -> int | None:
    """Merge a just-made topic into one of the owner's others that means the
    same. Returns the topic it was merged into, if any. Leaves it alone once
    the owner has touched it (renamed, pinned, filed a chat under it)."""
    try:
        new = await asyncio.to_thread(store.owned_topic, owner, topic_id)
        if new is None or new["kept"]:
            return None
        others = [n for n in await asyncio.to_thread(store.topic_names, owner)
                  if n.lower() != str(new["name"]).lower()]
        if not others:
            return None
        raw = await _ask(
            _TIDY_SYSTEM,
            f"NEW TOPIC: {new['name']}\n\nOTHER TOPICS:\n{_topic_list(others)}",
            max_tokens=24,
            actor="chat_topics",
        )
        match = store.clean_topic_name(raw.splitlines()[0] if raw else "")
        if not match:
            return None
        by_name = {n.lower(): n for n in others}
        if match.lower() not in by_name:
            return None

        def _merge() -> int | None:
            current = store.owned_topic(owner, topic_id)
            if current is None or current["kept"]:
                return None
            into_id = store.topic_id_by_name(owner, by_name[match.lower()])
            if into_id is None:
                return None
            return into_id if store.merge_topics(owner, topic_id, into_id) else None

        return await asyncio.to_thread(_merge)
    except Exception:
        logger.exception("tidy_new_topic: failed")
        return None


_LINE = re.compile(r"^\s*(\d+)\s*[:.)\-]\s*(.*)$")


def parse_backfill(raw: str, count: int) -> dict[int, str | None]:
    """``<number>: <topic>`` lines, 1-based, for numbers up to ``count``."""
    out: dict[int, str | None] = {}
    for line in raw.splitlines():
        m = _LINE.match(line)
        if not m:
            continue
        n = int(m.group(1))
        if 1 <= n <= count and n not in out:
            out[n] = store.clean_topic_name(m.group(2))
    return out


def backfill_running(owner: int) -> bool:
    return owner in _backfilling


def start_backfill(owner: int) -> bool:
    """Start filing the owner's untagged chats in the background, unless it
    is running already or failed a short while ago. True when it runs."""
    if owner in _backfilling:
        return True
    failed = _backfill_failed_at.get(owner)
    if failed is not None and time.monotonic() - failed < BACKFILL_RETRY_S:
        return False
    try:
        task = asyncio.get_running_loop().create_task(backfill(owner))
    except RuntimeError:  # pragma: no cover - no running loop
        return False
    _backfilling.add(owner)
    task.add_done_callback(lambda _t: _backfilling.discard(owner))
    _keep(task)
    return True


async def backfill(owner: int) -> int:
    """File the owner's untagged chats, newest first, a batch per call.
    Returns how many were filed. A failed call stops the run (the rest wait
    for the next one); a chat the reply skips is filed under no topic, so it
    isn't asked about again."""
    filed = 0
    try:
        pending = await asyncio.to_thread(store.untagged_sessions, owner)
        for at in range(0, len(pending), BACKFILL_BATCH):
            batch = pending[at:at + BACKFILL_BATCH]
            names = await asyncio.to_thread(store.topic_names, owner)
            titles = "\n".join(f"{i}. {c['title'][:120]}" for i, c in enumerate(batch, start=1))
            raw = await _ask(
                _BACKFILL_SYSTEM,
                f"EXISTING TOPICS:\n{_topic_list(names)}\n\nCHATS:\n{titles}",
                max_tokens=16 * len(batch) + 32,
                actor="chat_topics",
            )
            picks = parse_backfill(raw, len(batch))

            def _file(batch: list[dict[str, str]] = batch, picks: dict[int, str | None] = picks) -> int:
                n = 0
                for i, chat in enumerate(batch, start=1):
                    store.set_auto_topic(chat["session_id"], owner, picks.get(i))
                    n += 1
                return n

            filed += await asyncio.to_thread(_file)
        _backfill_failed_at.pop(owner, None)
    except Exception:
        _backfill_failed_at[owner] = time.monotonic()
        logger.exception("chat_topics.backfill: failed owner=%s", owner)
    return filed
