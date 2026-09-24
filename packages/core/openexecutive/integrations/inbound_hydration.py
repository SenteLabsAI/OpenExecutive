"""Hydrate an inbound DM reply with the context of the outbound DM it answers.

When the Executive ("oe") DMs a rostered person mid-conversation — e.g. the
principal says "DM Alex about the pizza thing" — the send is fire-and-forget
and the recipient's reply otherwise lands in a fresh per-person session with no
history. ``schedule_tools._record_outbound_context`` persists a linkage row at
send time (recipient + outbound text + originating session). This module is the
read side: each DM bot calls :func:`hydrate_user_message` right before handing
the reply to ``Executive.chat`` so oe knows what the person is replying about.

Per the prompt-caching invariant (CLAUDE.md), this context goes in the **user
turn** (prepended to the message text), never the cached system block.

The block is context for the LLM turn, never the person's own words. Peer
memory (``memory.honcho_client``: the turn sync, the department sync and the
prefetch query) strips it with :func:`strip_outbound_reply_context` before
recording what the person said; otherwise the Executive's own DM gets
attributed to the person who replied to it.
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta

from openexecutive.memory.episodic import (
    find_open_outbound_context,
    mark_outbound_context_consumed,
)
from openexecutive.memory.session_store import load_messages

logger = logging.getLogger(__name__)

# How far back an unconsumed outbound DM stays eligible to explain a reply.
# Beyond this, a reply is treated as unrelated (stale backstory would be noise).
_DEFAULT_WINDOW_HOURS = 72
# Turns of the originating conversation to quote as backstory, newest-first
# selection but rendered oldest-first.
_EXCERPT_TURNS = 4
# Hard cap on the rendered excerpt so a long originating session can't dump
# unbounded history into the recipient-facing reasoning context.
_EXCERPT_MAX_CHARS = 800
# Per-message cap inside the excerpt so one long turn can't crowd out the rest.
_PER_MESSAGE_CHARS = 300

OUTBOUND_REPLY_CONTEXT_TAG = "outbound_reply_context"
_DM_QUOTE_PREFIX = "You (oe) recently sent this person a DM: "
# Exactly the block ``hydrate_user_message`` prepends — opening tag, the DM
# quote line, through the closing tag and the blank line after it — and only
# at the start of the message. Anchoring on the quote line means a block the
# person typed themselves (no hydration ran) is content and stays; matching
# the builder's own "\n\n" rather than any whitespace keeps the message's
# leading indentation intact.
_LEADING_BLOCK_RE = re.compile(
    rf"\A<{re.escape(OUTBOUND_REPLY_CONTEXT_TAG)}>\n{re.escape(_DM_QUOTE_PREFIX)}"
    rf".*?\n</{re.escape(OUTBOUND_REPLY_CONTEXT_TAG)}>\n\n",
    re.DOTALL,
)


def _neutralize_closing_tag(text: str) -> str:
    """Interpolated content (the outbound DM, the backstory) must not be able
    to close the block early; a literal closing tag inside it is defanged."""
    return text.replace(f"</{OUTBOUND_REPLY_CONTEXT_TAG}>", f"<\\/{OUTBOUND_REPLY_CONTEXT_TAG}>")


def strip_outbound_reply_context(text: str) -> str:
    """Return ``text`` without the leading block :func:`hydrate_user_message`
    prepends — the person's own words only. Unchanged when the message does
    not start with that exact block (no hydration, a block the person wrote,
    a missing closing tag: better to keep scaffolding than to eat the
    message)."""
    return _LEADING_BLOCK_RE.sub("", text)


def _build_backstory_excerpt(originating_session_id: str | None) -> str:
    """Return a short, oldest-first excerpt of the originating conversation.

    Empty string when there is no originating session or it has no messages —
    the caller then emits a block with the outbound text alone.
    """
    if not originating_session_id:
        return ""
    messages = load_messages(originating_session_id)
    if not messages:
        return ""
    recent = messages[-_EXCERPT_TURNS:]
    lines: list[str] = []
    for msg in recent:
        role = "You (oe)" if msg.get("role") == "assistant" else "Principal"
        content = str(msg.get("content", "")).strip().replace("\n", " ")
        if not content:
            continue
        if len(content) > _PER_MESSAGE_CHARS:
            content = content[:_PER_MESSAGE_CHARS] + "…"
        lines.append(f"{role}: {content}")
    excerpt = "\n".join(lines)
    if len(excerpt) > _EXCERPT_MAX_CHARS:
        # Lines are rendered oldest-first, so drop from the tail to keep the
        # opening of the backstory intact rather than starting mid-sentence.
        excerpt = excerpt[:_EXCERPT_MAX_CHARS]
    return excerpt


def hydrate_user_message(
    *,
    channel: str,
    channel_ref: str,
    user_message: str,
    within_hours: int = _DEFAULT_WINDOW_HOURS,
) -> str:
    """Prepend outbound-reply context to ``user_message`` if a linkage matches.

    Looks for an ``open`` outbound linkage to ``(channel, channel_ref)`` within
    ``within_hours``. On a hit, prepends an ``<outbound_reply_context>`` block
    (the outbound DM text + a backstory excerpt) and one-shot-consumes the
    linkage so a later, unrelated message from the same person doesn't re-fire
    stale context. Returns ``user_message`` unchanged on a miss.

    Never raises — any lookup/format failure logs and returns the original
    message so a bookkeeping problem can't block the reply.
    """
    try:
        row = find_open_outbound_context(
            channel=channel,
            channel_ref=channel_ref,
            within=timedelta(hours=within_hours),
        )
        if row is None or row.id is None:
            return user_message

        # Build the block BEFORE consuming. If excerpt-building fails, the
        # linkage stays open for a later retry instead of being silently
        # consumed-without-injection.
        excerpt = _neutralize_closing_tag(_build_backstory_excerpt(row.originating_session_id))
        backstory = (
            f"Backstory from the originating conversation:\n{excerpt}"
            if excerpt
            else "No further backstory is available."
        )
        block = (
            f"<{OUTBOUND_REPLY_CONTEXT_TAG}>\n"
            f'{_DM_QUOTE_PREFIX}"{_neutralize_closing_tag(row.outbound_text)}"\n'
            "This incoming message MAY be their reply to it. "
            f"{backstory}\n"
            f"</{OUTBOUND_REPLY_CONTEXT_TAG}>\n\n"
        )

        # Consume last + race-safe: only the writer that flips open→consumed
        # injects. A concurrent reply that lost the race passes through, so the
        # context is never double-injected. A successful flip now guarantees we
        # return the block we just built.
        if not mark_outbound_context_consumed(row.id):
            return user_message

        logger.info(
            "inbound_hydration: injected outbound context id=%s for channel=%s ref=%s",
            row.id, channel, channel_ref,
        )
        return block + user_message
    except Exception:
        logger.exception(
            "inbound_hydration: failed for channel=%s ref=%s — passing message through",
            channel, channel_ref,
        )
        return user_message
