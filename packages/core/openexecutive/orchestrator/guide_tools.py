"""Anthropic tool definition + handler for reading the user guide.

``read_user_guide`` lets the Executive answer "how does X work in Open
Executive?" from the same hand-written guide the ``/guide`` page shows
(``guide/prebuilt/<id>.json``), for any part of the app, not only the page
the Ask OE panel is open on. The panel still sends the current page's
section inline (``api/routes/chat.py`` ``_build_page_context_block``); this
tool covers questions about every other part of the product.

Cache note: the definition is static (built once at import from
``GUIDE_SECTIONS``) and always offered, so it never changes the cached
tools prefix between turns. A read is a plain file read: no model call.
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from openexecutive.guide.prebuilt import get_prebuilt
from openexecutive.guide.sections import GUIDE_SECTIONS, GuideSection

READ_USER_GUIDE = "read_user_guide"


def answerable_sections() -> list[GuideSection]:
    """The guide sections the Executive may read and describe."""
    return list(GUIDE_SECTIONS)


def _tool_definition() -> dict[str, Any]:
    sections = answerable_sections()
    index = "\n".join(f"- {s.id}: {s.title}" for s in sections)
    return {
        "name": READ_USER_GUIDE,
        "description": (
            "Read one section of the Open Executive user guide: what a part of "
            "the app is, what each control does, who can see or use it, and "
            "how to use it. Call this when someone asks how Open Executive "
            "itself works (a page, a button, a setting, sharing, who sees "
            "what) and the answer is not already in a <page_context> block. "
            "Answer from what the section says; if it does not cover the "
            "question, say so plainly. Sections:\n" + index
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "section_id": {
                    "type": "string",
                    "enum": [s.id for s in sections],
                    "description": "The section to read.",
                },
            },
            "required": ["section_id"],
        },
    }


READ_USER_GUIDE_TOOL: dict[str, Any] = _tool_definition()

GUIDE_TOOLS: list[dict[str, Any]] = [READ_USER_GUIDE_TOOL]


async def handle_read_user_guide(tool_input: dict[str, Any]) -> str:
    section_id = str(tool_input.get("section_id", "")).strip()
    known = {s.id: s for s in answerable_sections()}
    if section_id not in known:
        return json.dumps(
            {"error": f"unknown section {section_id!r}", "sections": sorted(known)}
        )
    content = get_prebuilt(section_id)
    if not content or not content.get("markdown"):
        return json.dumps({"error": f"section {section_id!r} has no content"})
    return json.dumps(
        {
            "section_id": section_id,
            "title": known[section_id].title,
            "markdown": content["markdown"],
        }
    )


GUIDE_TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[str]]] = {
    READ_USER_GUIDE: handle_read_user_guide,
}


__all__ = [
    "GUIDE_TOOLS",
    "GUIDE_TOOL_HANDLERS",
    "READ_USER_GUIDE",
    "READ_USER_GUIDE_TOOL",
    "answerable_sections",
    "handle_read_user_guide",
]
