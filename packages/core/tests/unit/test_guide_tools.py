"""read_user_guide: the Executive reads any user-guide section on demand."""
from __future__ import annotations

import asyncio
import json

from openexecutive.guide.sections import GUIDE_SECTIONS
from openexecutive.orchestrator import executive
from openexecutive.orchestrator.guide_tools import (
    READ_USER_GUIDE,
    READ_USER_GUIDE_TOOL,
    handle_read_user_guide,
)


def _read(section_id: object) -> dict:
    return json.loads(asyncio.run(handle_read_user_guide({"section_id": section_id})))


def test_tool_offers_every_guide_section() -> None:
    enum = READ_USER_GUIDE_TOOL["input_schema"]["properties"]["section_id"]["enum"]
    assert enum == [s.id for s in GUIDE_SECTIONS]
    for s in GUIDE_SECTIONS:
        assert f"- {s.id}: {s.title}" in READ_USER_GUIDE_TOOL["description"]


def test_reads_a_section_markdown() -> None:
    out = _read("chat")
    assert out["section_id"] == "chat"
    assert "**How to use:**" in out["markdown"]


def test_unknown_or_path_like_section_is_refused() -> None:
    for bad in ("nope", "../architecture/prebuilt/api", "", None):
        out = _read(bad)
        assert "error" in out
        assert "chat" in out["sections"]


def test_executive_offers_and_handles_it() -> None:
    assert any(t["name"] == READ_USER_GUIDE for t in executive._ALL_SKILL_TOOLS)
    assert READ_USER_GUIDE in executive._ALL_SKILL_HANDLERS


def test_sharing_question_is_answerable_from_the_chat_section() -> None:
    # Ask OE once could not say what sharing a chat does: the guide must.
    markdown = _read("chat")["markdown"].lower()
    assert "share" in markdown and "write in it with you" in markdown
