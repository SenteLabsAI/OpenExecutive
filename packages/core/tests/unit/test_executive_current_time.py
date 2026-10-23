"""A chat turn says when it is. Without a <current_time> hint the model has no
date at all and guesses one (from the newest record a tool returned, say), so
"yesterday" or "this month" resolve against the wrong day. _build_messages
puts it in the user turn, in the workspace's timezone, never in a cached
system block. See openexecutive.orchestrator.executive.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-used")


def _time_text(content: list[dict]) -> str | None:
    for part in content:
        text = part.get("text", "")
        if "<current_time>" in text:
            return text
    return None


def test_the_block_names_the_day_date_time_and_zone() -> None:
    from openexecutive.orchestrator.executive import _current_time_block

    noon_utc = datetime(2026, 10, 20, 12, 35, tzinfo=UTC)
    with patch("openexecutive.memory.workspace_settings.get_user_timezone", return_value=ZoneInfo("Europe/Prague")):
        block = _current_time_block(noon_utc)
    assert block.startswith("Tuesday 20 October 2026, 14:35 (Europe/Prague, UTC+02:00).")
    assert "yesterday" in block


def test_the_date_is_where_the_workspace_is_not_utc() -> None:
    # Late evening in Los Angeles is already tomorrow in UTC: "today" is
    # the workspace's day.
    from openexecutive.orchestrator.executive import _current_time_block

    late_la = datetime(2026, 10, 21, 5, 30, tzinfo=UTC)
    with patch(
        "openexecutive.memory.workspace_settings.get_user_timezone", return_value=ZoneInfo("America/Los_Angeles")
    ):
        block = _current_time_block(late_la)
    assert block.startswith("Tuesday 20 October 2026, 22:30 (America/Los_Angeles, UTC-07:00).")


def test_every_chat_turn_carries_it_first_in_the_user_turn() -> None:
    from openexecutive.orchestrator.executive import Executive
    from openexecutive.orchestrator.session import Session

    messages = Executive()._build_messages(Session(session_id="t-time-1"), "what were yesterday's sales?")
    content = messages[-1]["content"]
    block = _time_text(content)
    assert block is not None, "no <current_time> block in the user turn"
    assert content[0]["text"] == block
    assert block.rstrip().endswith("</current_time>")
    # The person's words still come last.
    assert content[-1]["text"] == "what were yesterday's sales?"


def test_it_never_goes_in_the_cached_system_prompt() -> None:
    # The time changes every turn: in a cached block it would break caching,
    # so the blocks built a minute apart are the same.
    from openexecutive.prompts.cache_manager import build_system_blocks

    first = build_system_blocks()
    with patch("openexecutive.orchestrator.executive.datetime") as clock:
        clock.now.return_value = datetime(2030, 1, 1, tzinfo=UTC)
        later = build_system_blocks()
    assert first == later
    assert all("<current_time>" not in block.get("text", "") for block in first)
