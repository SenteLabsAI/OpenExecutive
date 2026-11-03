"""A turn that uses up its steps still answers.

The round before the last gets LAST_ROUND_NOTE after its tool results, and
the last round is offered no tool (tool_choice none), so a long task ends
on an answer from what it gathered rather than on "unable to complete".
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from openexecutive.orchestrator.executive import LAST_ROUND_NOTE, Executive

from ._agent_loop_fakes import FinalMsg, ScriptedProvider, TextBlock, ToolUseBlock


class _StubGateway:
    def __init__(self) -> None:
        self.calls = 0

    async def call_tool(self, _input: dict[str, Any]) -> str:
        self.calls += 1
        return '{"status": "ok"}'

    async def search_tools(self, _input: dict[str, Any]) -> str:
        return '{"tools": []}'

    async def load_mcp_server(self, _input: dict[str, Any]) -> str:
        return '{"status": "ok"}'


@pytest.fixture(autouse=True)
def _no_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openexecutive.orchestrator.executive.audit_log", lambda *a, **k: None)


def _tool_round(tool_id: str) -> FinalMsg:
    return FinalMsg(
        [ToolUseBlock(tool_id, "call_tool", {"name": "drive__read_file", "arguments": {}})],
        "tool_use",
    )


def _run(provider: ScriptedProvider, max_iterations: int) -> tuple[list[Any], _StubGateway]:
    executive = Executive()
    gateway = _StubGateway()
    executive._mcp_gateway = gateway  # type: ignore[assignment]

    async def _go() -> list[Any]:
        items: list[Any] = []
        with patch("openexecutive.orchestrator.executive.get_provider", return_value=provider):
            async for item in executive._stream_agent_loop(
                system_blocks=[],
                messages=[{"role": "user", "content": "review the whole contract"}],
                model="claude-test",
                max_iterations=max_iterations,
            ):
                items.append(item)
        return items

    return asyncio.run(_go()), gateway


def _text(items: list[Any]) -> str:
    return "".join(i for i in items if isinstance(i, str))


def test_last_round_runs_no_tool_and_answers() -> None:
    provider = ScriptedProvider(
        [
            _tool_round("toolu_1"),
            _tool_round("toolu_2"),
            FinalMsg([TextBlock("Here is the full review.")], "end_turn"),
        ]
    )
    items, gateway = _run(provider, max_iterations=3)

    assert gateway.calls == 2
    assert len(provider.calls) == 3
    # Only the last round is tools-off.
    assert "tool_choice" not in provider.calls[0]
    assert "tool_choice" not in provider.calls[1]
    assert provider.calls[2]["tool_choice"] == {"type": "none"}
    # The tool list is the same on every round, so its cached prefix holds.
    assert provider.calls[2]["tools"] == provider.calls[1]["tools"]
    # The note rides after the results of the round before the last, once.
    messages = provider.calls[2]["messages"]
    last_user = messages[-1]["content"]
    assert last_user[0]["type"] == "tool_result"
    assert last_user[-1] == {"type": "text", "text": LAST_ROUND_NOTE}
    notes = [
        b for m in messages if isinstance(m["content"], list)
        for b in m["content"] if b == {"type": "text", "text": LAST_ROUND_NOTE}
    ]
    assert len(notes) == 1
    assert "unable to complete" not in _text(items)


def test_a_turn_that_finishes_early_is_unchanged() -> None:
    provider = ScriptedProvider(
        [_tool_round("toolu_1"), FinalMsg([TextBlock("Done.")], "end_turn")]
    )
    items, _gateway = _run(provider, max_iterations=15)

    assert len(provider.calls) == 2
    assert all("tool_choice" not in c for c in provider.calls)
    note = {"type": "text", "text": LAST_ROUND_NOTE}
    assert all(
        note not in m["content"]
        for m in provider.calls[-1]["messages"] if isinstance(m["content"], list)
    )
    assert "unable to complete" not in _text(items)


def test_a_claim_check_retry_on_the_last_round_is_told_tools_are_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _claim(*_a: Any, **_k: Any) -> str:
        return "said the invoice was sent"

    monkeypatch.setattr(
        "openexecutive.orchestrator.executive.claim_check.unbacked_claim", _claim
    )
    provider = ScriptedProvider(
        [
            FinalMsg([TextBlock("I sent the invoice.")], "end_turn"),
            FinalMsg([TextBlock("I couldn't send it here.")], "end_turn"),
        ]
    )
    _run(provider, max_iterations=2)

    assert len(provider.calls) == 2
    assert provider.calls[1]["tool_choice"] == {"type": "none"}
    retry = provider.calls[1]["messages"][-1]["content"]
    assert retry[-1] == {"type": "text", "text": LAST_ROUND_NOTE}
