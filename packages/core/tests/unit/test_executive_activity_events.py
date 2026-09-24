"""The agent loop names each tool round before it signals one is in flight.

The chat progress line used to read "Consulting specialists…" for every tool
round, including rounds with no specialist in them. These tests drive the real
`_stream_agent_loop` and assert on what it yields:

1. an `activity` event per tool round, immediately before the `_THINKING`
   sentinel, naming what actually ran;
2. a real specialist round still collapsing to the generic label, with no
   specialist name anywhere in the event;
3. no `activity` at all when the turn takes no tool round;
4. a labelling failure degrading to the old behaviour rather than killing
   the turn.

Fake-provider scaffolding comes from the shared `_agent_loop_fakes` module.
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

from openexecutive.orchestrator import activity_labels as al
from openexecutive.orchestrator.executive import Executive

from ._agent_loop_fakes import (
    FinalMsg,
    ScriptedProvider,
    TextBlock,
    ToolUseBlock,
)


class _StubGateway:
    """Minimal MCP gateway.

    All three handlers are needed even for a `call_tool`-only round: the loop
    builds its dispatch dict eagerly from every MCP tool name.
    """

    async def call_tool(self, _input: dict[str, Any]) -> str:
        return '{"status": "ok"}'

    async def search_tools(self, _input: dict[str, Any]) -> str:
        return '{"tools": []}'

    async def load_mcp_server(self, _input: dict[str, Any]) -> str:
        return '{"status": "ok"}'


def _run_loop(provider: ScriptedProvider, executive: Executive) -> list[Any]:
    async def _go() -> list[Any]:
        items: list[Any] = []
        with patch(
            "openexecutive.orchestrator.executive.get_provider",
            return_value=provider,
        ):
            async for item in executive._stream_agent_loop(
                system_blocks=[],
                messages=[{"role": "user", "content": "go"}],
                model="claude-test",
            ):
                items.append(item)
        return items

    return asyncio.run(_go())


def _activities(items: list[Any]) -> list[dict[str, Any]]:
    return [i for i in items if isinstance(i, dict) and i.get("type") == "activity"]


def _two_turn_script(tool_block: ToolUseBlock) -> ScriptedProvider:
    """One tool round, then a plain text turn that ends the loop."""
    return ScriptedProvider([
        FinalMsg([tool_block], "tool_use"),
        FinalMsg([TextBlock("done")], "end_turn"),
    ])


def test_mcp_round_is_named_by_the_underlying_tool_before_thinking() -> None:
    executive = Executive()
    executive._mcp_gateway = _StubGateway()  # type: ignore[assignment]
    items = _run_loop(
        _two_turn_script(
            ToolUseBlock(
                "toolu_1",
                "call_tool",
                {"name": "google_workspace__send_gmail_message", "arguments": {}},
            )
        ),
        executive,
    )

    acts = _activities(items)
    assert len(acts) == 1
    assert acts[0]["label"] == "Using google_workspace__send_gmail_message…"
    assert acts[0]["tool"] == "google_workspace__send_gmail_message"
    assert acts[0]["iteration"] == 1
    # This is the regression: an MCP round must not claim a specialist fan-out.
    assert "specialist" not in acts[0]["label"].lower()
    assert items.index(acts[0]) < items.index(Executive._THINKING)


def test_specialist_round_is_generic_and_names_no_specialist() -> None:
    executive = Executive()
    with patch(
        "openexecutive.orchestrator.executive.route_parallel",
        new=_async_return(["analysis"]),
    ):
        items = _run_loop(
            _two_turn_script(
                ToolUseBlock(
                    "toolu_1",
                    "consult_specialist",
                    {"specialist": "finance", "query": "runway?"},
                )
            ),
            executive,
        )

    acts = _activities(items)
    assert len(acts) == 1
    assert acts[0]["label"] == "Consulting specialists…"
    assert "finance" not in " ".join(str(v) for v in acts[0].values()).lower()


def test_no_tool_round_yields_no_activity_and_no_sentinel() -> None:
    items = _run_loop(
        ScriptedProvider([FinalMsg([TextBlock("hi")], "end_turn")]),
        Executive(),
    )
    assert _activities(items) == []
    assert Executive._THINKING not in items


def test_each_round_of_a_multi_round_turn_is_named() -> None:
    """One activity per round, each naming its own round — not the last one's."""
    executive = Executive()
    executive._mcp_gateway = _StubGateway()  # type: ignore[assignment]
    provider = ScriptedProvider([
        FinalMsg([ToolUseBlock("t1", "send_slack_dm", {"user_id": "U1"})], "tool_use"),
        FinalMsg([ToolUseBlock("t2", "list_people", {})], "tool_use"),
        FinalMsg([TextBlock("done")], "end_turn"),
    ])
    items = _run_loop(provider, executive)

    acts = _activities(items)
    assert [a["label"] for a in acts] == [
        "Sending a Slack DM…",
        "Looking up people…",
    ]
    assert [a["iteration"] for a in acts] == [1, 2]


def test_a_failed_label_mid_turn_does_not_leave_the_previous_one_standing() -> None:
    """Every sentinel is paired with an activity, so no round inherits a label.

    Without the pairing, a client that keeps the last label it saw would
    caption round 2's work with round 1's — "Sending a Slack DM…" while a
    people lookup runs.
    """
    executive = Executive()
    provider = ScriptedProvider([
        FinalMsg([ToolUseBlock("t1", "send_slack_dm", {"user_id": "U1"})], "tool_use"),
        FinalMsg([ToolUseBlock("t2", "list_people", {})], "tool_use"),
        FinalMsg([TextBlock("done")], "end_turn"),
    ])

    real = al.summarize_activity
    calls = {"n": 0}

    def _fail_second(*a: Any, **k: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("labeller exploded on round 2")
        return real(*a, **k)

    with patch(
        "openexecutive.orchestrator.executive.summarize_activity", new=_fail_second
    ):
        items = _run_loop(provider, executive)

    acts = _activities(items)
    assert [a["label"] for a in acts] == ["Sending a Slack DM…", "Working…"]
    # One activity per sentinel, always.
    assert len(acts) == items.count(Executive._THINKING)


def test_labelling_failure_does_not_break_the_turn() -> None:
    """A bug in the labeller must cost the label, not the response."""
    executive = Executive()
    executive._mcp_gateway = _StubGateway()  # type: ignore[assignment]

    def _boom(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise RuntimeError("labeller exploded")

    provider = _two_turn_script(
        ToolUseBlock("toolu_1", "call_tool", {"name": "x__y", "arguments": {}})
    )
    with patch(
        "openexecutive.orchestrator.executive.summarize_activity", new=_boom
    ):
        items = _run_loop(provider, executive)

    # The label degrades to the honest fallback rather than vanishing.
    assert [a["label"] for a in _activities(items)] == ["Working…"]
    # The round still signals itself, and the loop still makes its follow-up
    # model call with the tool result — the specific label is all that was lost.
    assert Executive._THINKING in items
    assert len(provider.calls) == 2


def _async_return(value: Any) -> Any:
    async def _inner(*_a: Any, **_k: Any) -> Any:
        return value

    return _inner
