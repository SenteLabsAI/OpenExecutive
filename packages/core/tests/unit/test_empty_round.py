"""A round that ends on a tool call the loop has nothing to answer for
must not send an empty user message: the API refuses it, and the whole
turn fails after its earlier steps already ran."""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from openexecutive.orchestrator.executive import EMPTY_ROUND_NOTE, Executive
from tests.unit.test_tool_result_cap import _FinalMsg, _ScriptedProvider, _TextBlock


@pytest.fixture(autouse=True)
def _no_audit_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openexecutive.orchestrator.executive.audit_log", lambda *_a, **_k: None)


def test_round_with_no_tool_call_to_answer_carries_on() -> None:
    provider = _ScriptedProvider(
        [
            # stop_reason says a tool comes, but no tool_use block did.
            _FinalMsg([_TextBlock("Adding the last one.")], "tool_use"),
            _FinalMsg([_TextBlock("Done.")], "end_turn"),
        ]
    )

    async def _go() -> list[Any]:
        items: list[Any] = []
        with patch("openexecutive.orchestrator.executive.get_provider", return_value=provider):
            async for item in Executive()._stream_agent_loop(
                system_blocks=[], messages=[{"role": "user", "content": "add them"}], model="claude-test",
            ):
                items.append(item)
        return items

    asyncio.run(_go())

    assert len(provider.calls) == 2
    sent = provider.calls[1]["messages"]
    assert all(m["content"] for m in sent), "no message may go out empty"
    last = sent[-1]
    assert last["role"] == "user"
    assert last["content"][0]["text"] == EMPTY_ROUND_NOTE
