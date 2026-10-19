"""A turn's text across tool rounds (orchestrator/executive.py): each round's
text starts a new paragraph, so the model's words before a tool call and its
answer never run together ("...stock.Yes. ITEM-1..."), and nothing it wrote is
dropped. Found by evals against a live install (2026-10-19)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from openexecutive.orchestrator.executive import Executive, _round_separator
from openexecutive.orchestrator.schedule_tools import set_session
from openexecutive.orchestrator.session import Session

from ._agent_loop_fakes import FakeStream, FinalMsg, ScriptedProvider, TextBlock, ToolUseBlock


class StreamingFake(FakeStream):
    """Streams each text block of the final message as a text delta, as the
    API does, before the final message."""

    def __init__(self, final_msg: FinalMsg) -> None:
        super().__init__(final_msg)
        self._deltas = [
            SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text=b.text))
            for b in final_msg.content
            if getattr(b, "type", "") == "text"
        ]

    async def __anext__(self) -> Any:
        if not self._deltas:
            raise StopAsyncIteration
        return self._deltas.pop(0)


class StreamingProvider(ScriptedProvider):
    def messages_stream(self, **kwargs: Any) -> FakeStream:
        self.calls.append(kwargs)
        return StreamingFake(self._final_msgs.pop(0))


def _reply(rounds: list[FinalMsg]) -> str:
    provider = StreamingProvider(rounds)
    text: list[str] = []

    async def go() -> None:
        with (
            patch("openexecutive.orchestrator.executive.get_provider", return_value=provider),
            patch("openexecutive.orchestrator.executive.audit_log", lambda *_a, **_k: None),
            set_session(Session()),
        ):
            async for item in Executive()._stream_agent_loop(
                system_blocks=[], messages=[{"role": "user", "content": "x"}],
                model="claude-test", workspace_mode="team", principal_role_tag="", turn_id="t-1",
            ):
                if isinstance(item, str) and item != Executive._THINKING:
                    text.append(item)

    asyncio.run(go())
    return "".join(text)


@pytest.mark.parametrize(("tail", "text", "separator"), [
    ("stock.", "Yes.", "\n\n"),
    ("stock.\n", "Yes.", "\n"),
    ("stock.", "\nYes.", "\n"),
    ("stock.\n\n", "Yes.", ""),
    ("stock.", "\n\nYes.", ""),
])
def test_a_new_round_starts_a_new_paragraph(tail: str, text: str, separator: str) -> None:
    assert _round_separator(tail, text) == separator


def test_a_turn_keeps_every_round_in_its_own_paragraph() -> None:
    reply = _reply([
        FinalMsg([TextBlock("Check actual stock."), ToolUseBlock("tu1", "no_such_tool", {})], "tool_use"),
        FinalMsg([TextBlock("Yes. ITEM-1 has 12 in Stores.")], "end_turn"),
    ])
    assert reply == "Check actual stock.\n\nYes. ITEM-1 has 12 in Stores."


def test_one_round_is_untouched_and_a_silent_round_adds_nothing() -> None:
    assert _reply([FinalMsg([TextBlock("ITEM-1 has 12.")], "end_turn")]) == "ITEM-1 has 12."
    # A round that only calls a tool writes nothing, so no break before the answer.
    reply = _reply([
        FinalMsg([ToolUseBlock("tu1", "no_such_tool", {})], "tool_use"),
        FinalMsg([TextBlock("ITEM-1 has 12.")], "end_turn"),
    ])
    assert reply == "ITEM-1 has 12."
