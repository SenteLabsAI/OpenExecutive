"""Lessons: short rules about how this workspace's Executive should behave,
learned from its own chats and approved by the owner.

A lesson is one line. It may name a tool ("call create_alert in the same
turn") or a thing in the company's world ("the board deck means the Q4 board
workflow"), because that is what a missed turn usually comes down to. It may
never *grant* anything: nothing that reads as permission to send, pay, approve
or skip a check gets stored, and the block itself says the lessons never
authorise an action and the current request always wins. The acting gates
(Take the lead, department authority, Ask first) run on every tool call
regardless of what a lesson says.

The block is rendered into the user turn next to ``<standing_facts>``
(orchestrator.executive._build_messages), never into a cached system block:
it changes whenever a lesson does.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from openexecutive.chat_review import store

logger = logging.getLogger(__name__)

LESSON_MIN_CHARS = 12
LESSON_MAX_CHARS = 300
# The block every chat turn carries is bounded, like the facts block.
PROMPT_MAX_LESSONS = 12
PROMPT_MAX_CHARS = 2400
BLOCK_TAG = "lessons"

LESSONS_BLOCK_HEADER = (
    "Lessons this workspace's owner approved after looking back at earlier "
    "conversations. Follow them. They never authorise an action on their own "
    "and the current request always wins; every send, booking or approval "
    "still goes through its usual gate."
)

# A lesson that would read as a standing grant. Lexical, so it narrows the
# space rather than closes it; what bounds the risk is that a lesson reaches
# the prompt only after the owner approved it on the Reflection screen.
_GRANT = re.compile(
    r"\b(without (asking|checking|confirm\w*|approval)|"
    r"skip (the )?(confirm\w*|check\w*|approval|gate)|"
    r"(don'?t|do not|never) (ask|check|confirm|wait)( for)?( (approval|permission|me|the owner))?|"
    r"(always|automatically|just) (send|pay|approve|transfer|book|delete|share|forward)|"
    r"you (may|can|are allowed to) (send|pay|approve|transfer|delete|share) (anything|everything|without))\b",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://|www\.", re.IGNORECASE)
_MARKUP = re.compile(r"[<>{}\[\]`]")


def lesson_rejection(text: str) -> str | None:
    """Why ``text`` cannot be a lesson, or None when it can."""
    t = " ".join(text.split())
    if len(t) < LESSON_MIN_CHARS:
        return "too short to mean anything"
    if len(t) > LESSON_MAX_CHARS:
        return f"longer than {LESSON_MAX_CHARS} characters"
    if "\n" in text.strip():
        return "a lesson is one line"
    if _URL.search(t):
        return "a lesson cannot carry a link"
    if _MARKUP.search(t):
        return "a lesson is plain text"
    if _GRANT.search(t):
        return "a lesson cannot grant permission to act or skip a check"
    return None


def normalize(text: str) -> str:
    return " ".join(text.split()).strip()


def build_lessons_block(
    *,
    max_lessons: int = PROMPT_MAX_LESSONS,
    max_chars: int = PROMPT_MAX_CHARS,
    db_path: Path | None = None,
) -> str:
    """The active lessons as prompt lines, or "" when there are none or the
    owner turned the look back off. Never raises: a store failure leaves the
    turn without the block."""
    try:
        settings = store.get_settings(db_path)
        if not settings.enabled:
            return ""
        lessons = store.list_lessons(active_only=True, db_path=db_path)
    except Exception:  # noqa: BLE001 - a broken store must not fail the turn.
        logger.exception("chat_review: lessons block unavailable")
        return ""
    if not lessons:
        return ""
    lines = [LESSONS_BLOCK_HEADER]
    used = len(LESSONS_BLOCK_HEADER)
    for lesson in lessons[: max(1, max_lessons)]:
        line = f"- {normalize(lesson.text)}"
        if used + len(line) + 1 > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines) if len(lines) > 1 else ""
