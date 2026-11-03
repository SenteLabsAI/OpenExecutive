"""What a long chat turn shows while it works: the task, what it is doing
right now, what it has used and what it has found so far.

On work that takes several steps, the Executive calls ``show_progress``
alongside that work's tool calls: the task in a few words, what it is doing
now with real names ("Comparing the LOI terms with Dana's March 4 email"),
the things it has read since the last call, by name, and anything worth
knowing it found. The loop answers the call itself (nothing runs) and sends
the web chat a ``progress`` event with the whole picture so far, which the
chat shows as one card that stays the same size however long the turn runs.

A quick answer never calls it. Without it the card still has something true
to show: each round's ``activity`` label as the live line, and the ``reads``
this module counts from the round's own calls ("3 emails", "2 files").
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

SHOW_PROGRESS = "show_progress"

# One line on the card.
MAX_LINE_CHARS = 90
# Kept for the turn; the card shows only the newest few.
MAX_USED = 20
MAX_FOUND = 12

SHOW_PROGRESS_TOOL: dict[str, Any] = {
    "name": SHOW_PROGRESS,
    "description": (
        "Show the person how the work is going when a request takes several "
        "steps (roughly three or more rounds of tools). Call it in the same "
        "response as your tool calls, never on its own, and again each round "
        "something changes. `now`: what you are doing right now, naming the "
        "real thing (\"Comparing the LOI terms with Dana's March 4 email\"), "
        "never tool or system names. `task` (first call): the whole job in a "
        "few words (\"Reviewing the Harbor Point LOI\"). `used`: what you read "
        "since the last call, by name (a file name, an email subject, a "
        "person, a web page); leave out what you already listed. `found`: at "
        "most two new findings worth knowing early, one short line each; "
        "leave it out when there is nothing new. Skip it for a quick answer, "
        "a single lookup or a plain reply. Nothing runs: it only updates what "
        "the person sees."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "The whole job in a few words."},
            "now": {
                "type": "string",
                "description": "What you are doing right now, with real names.",
            },
            "used": {
                "type": "array",
                "items": {"type": "string"},
                "description": "What you read since the last call, by name.",
            },
            "found": {
                "type": "array",
                "items": {"type": "string"},
                "description": "New findings worth knowing early, one short line each.",
            },
        },
        "required": ["now"],
    },
}


def _line(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    line = " ".join(raw.split())
    if len(line) > MAX_LINE_CHARS:
        line = line[: MAX_LINE_CHARS - 1].rstrip() + "…"
    return line


def _lines(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [line for line in (_line(item) for item in raw) if line]


@dataclass
class Progress:
    """The turn's progress so far, as the chat last saw it."""

    task: str = ""
    # This call's own line, empty when it gave none.
    now: str = ""
    used: list[str] = field(default_factory=list)
    found: list[str] = field(default_factory=list)

    def event(self) -> dict[str, Any]:
        return {
            "type": "progress",
            "task": self.task,
            "now": self.now,
            "used": list(self.used),
            "found": list(self.found),
        }


def _add(kept: list[str], new: list[str], cap: int, *, newest: bool) -> list[str]:
    seen = {item.casefold() for item in kept}
    out = list(kept)
    for item in new:
        if item.casefold() not in seen:
            seen.add(item.casefold())
            out.append(item)
    # Sources keep the first ones (what the person asked about, which the card
    # shows first, and a dropped one can't come back as new); findings keep the
    # newest (the card shows those).
    return out[-cap:] if newest else out[:cap]


# Said when the call came on its own: that cost a round with no work in it.
ALONE_NOTE = " Next time, make this call in the same response as your tool calls."


def apply(
    tool_input: Any, progress: Progress | None, *, alone: bool = False
) -> tuple[str, dict[str, Any] | None, Progress | None]:
    """Answer one ``show_progress`` call.

    Returns (the tool result for the model, the ``progress`` event for the
    chat or None, the turn's progress from now on). ``used`` and ``found``
    add to what the turn already showed; ``task`` replaces it, and a call
    without one keeps the old one; ``now`` is only ever this call's. A first call with nothing to show
    shows nothing and says why. ``alone`` (no other call in the round) adds a
    reminder to send it with the round's own calls.
    """
    data = tool_input if isinstance(tool_input, dict) else {}
    prev = progress or Progress()
    task = _line(data.get("task")) or prev.task
    # Only this call's own words: an old `now` sent again would pin the live
    # line to an earlier round instead of falling back to its activity label.
    now = _line(data.get("now"))
    used = _add(prev.used, _lines(data.get("used")), MAX_USED, newest=False)
    found = _add(prev.found, _lines(data.get("found"))[:2], MAX_FOUND, newest=True)
    if not (task or now or used or found):
        return json.dumps({"error": "Say what you are doing now in `now`."}), None, progress
    shown = Progress(task=task, now=now, used=used, found=found)
    result = "Shown."
    return result + (ALONE_NOTE if alone else ""), shown.event(), shown


# What a call reads, by kind, for the card's fallback when the turn names
# nothing itself: "3 emails · 2 files". Only reads count, never searches or
# writes: a search finds things, it doesn't use them.
_READ_KINDS: dict[str, str] = {
    "read_document": "file",
    "read_my_email_attachment": "file",
    "get_artifact": "document",
    "read_my_email": "email",
    "my_email_read_before": "email",
    "my_calendar": "calendar",
    "lookup_person": "person",
    "ask_about_person": "person",
    "run_executive_research": "web search",
    "search_past_chats": "earlier chat",
    "recall_history": "note",
    "read_user_guide": "guide page",
    "call_tool": "connected app",
}


@dataclass
class Reads:
    """Every distinct read the turn made, so a repeat isn't counted twice."""

    seen: set[tuple[str, str]] = field(default_factory=set)

    def add(self, tool_uses: list[dict[str, Any]]) -> dict[str, int]:
        """Count this round's new reads by kind ({} when none)."""
        counts: dict[str, int] = {}
        for tu in tool_uses:
            kind = _READ_KINDS.get(str(tu.get("name") or ""))
            if kind is None:
                continue
            try:
                key = json.dumps(tu.get("input"), sort_keys=True, default=str)
            except (TypeError, ValueError):
                key = repr(tu.get("input"))
            if (kind, key) in self.seen:
                continue
            self.seen.add((kind, key))
            counts[kind] = counts.get(kind, 0) + 1
        return counts
