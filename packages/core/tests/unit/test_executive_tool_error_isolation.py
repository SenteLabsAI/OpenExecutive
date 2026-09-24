"""One crashing tool handler must not take the whole turn down.

Tool handlers are supposed to return a JSON `{"error": ...}` string rather
than raise. When one raises anyway, `asyncio.gather` used to propagate it out
of the agent loop, past the adapter, and into the channel's catch-all — where
Slack answered "I encountered an error processing your request" with no audit
row and no way to tell which tool failed (#136).

The contract pinned here:
  1. a raising skill handler becomes an error tool_result, the sibling tool's
     real result survives, and the loop continues to the next iteration;
  2. the failure is audited with `ok=False` so /audit shows which tool broke;
  3. `asyncio.CancelledError` is NOT swallowed — `return_exceptions=True`
     captures it like any other exception, and eating it would silently break
     turn-timeout and client-disconnect handling in api/routes/chat.py;
  4. the MCP dispatch path behaves identically.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch

import pytest

from openexecutive.orchestrator.executive import Executive

# --------------------------------------------------------------------- #
# Fake provider plumbing (same shape as test_executive_form_patch.py)
# --------------------------------------------------------------------- #


class _TextBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _ToolUseBlock:
    type = "tool_use"

    def __init__(self, id_: str, name: str, input_: dict[str, Any]) -> None:
        self.id = id_
        self.name = name
        self.input = input_


class _FinalMsg:
    usage = None  # _emit_cache_event no-ops on usage=None

    def __init__(self, content: list[Any], stop_reason: str) -> None:
        self.content = content
        self.stop_reason = stop_reason


class _FakeStream:
    def __init__(self, final_msg: _FinalMsg) -> None:
        self._final_msg = final_msg

    async def __aenter__(self) -> _FakeStream:
        return self

    async def __aexit__(self, *_a: Any) -> None:
        return None

    def __aiter__(self) -> _FakeStream:
        return self

    async def __anext__(self) -> Any:
        raise StopAsyncIteration

    async def get_final_message(self) -> _FinalMsg:
        return self._final_msg


class _ScriptedProvider:
    def __init__(self, final_msgs: list[_FinalMsg]) -> None:
        self._final_msgs = list(final_msgs)
        self.calls: list[dict[str, Any]] = []

    def messages_stream(self, **kwargs: Any) -> _FakeStream:
        self.calls.append(kwargs)
        return _FakeStream(self._final_msgs.pop(0))


@pytest.fixture(autouse=True)
def _no_audit_writes(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture audit calls instead of writing them.

    `audit.log_event` otherwise writes to the default ./episodic_memory.db and
    leaks rows that break other modules' assertions in a full-suite run.
    """
    captured: list[dict[str, Any]] = []

    def _fake_audit(event_type: str, summary: str, **kwargs: Any) -> None:
        captured.append({"event_type": event_type, "summary": summary, **kwargs})

    monkeypatch.setattr(
        "openexecutive.orchestrator.executive.audit_log", _fake_audit
    )
    return captured


def _run_loop(
    provider: _ScriptedProvider, executive: Executive | None = None
) -> list[Any]:
    exec_ = executive or Executive()

    async def _go() -> list[Any]:
        items: list[Any] = []
        with patch(
            "openexecutive.orchestrator.executive.get_provider",
            return_value=provider,
        ):
            async for item in exec_._stream_agent_loop(
                system_blocks=[],
                messages=[{"role": "user", "content": "do the thing"}],
                model="claude-test",
            ):
                items.append(item)
        return items

    return asyncio.run(_go())


def _tool_results(provider: _ScriptedProvider, call_index: int) -> dict[str, str]:
    """Map tool_use_id -> tool_result content from the Nth API call's messages."""
    turn = provider.calls[call_index]["messages"][-1]
    assert turn["role"] == "user"
    return {b["tool_use_id"]: b["content"] for b in turn["content"]}


# --------------------------------------------------------------------- #
# Skill dispatch
# --------------------------------------------------------------------- #


def test_raising_skill_handler_becomes_error_result_and_turn_survives(
    _no_audit_writes: list[dict[str, Any]],
) -> None:
    async def _boom(_input: dict[str, Any]) -> str:
        raise RuntimeError("chroma exploded")

    async def _fine(_input: dict[str, Any]) -> str:
        return json.dumps({"matches": []})

    provider = _ScriptedProvider(
        [
            _FinalMsg(
                [
                    _ToolUseBlock("tu-bad", "lookup_person", {"query": "x"}),
                    _ToolUseBlock("tu-ok", "list_people", {}),
                ],
                stop_reason="tool_use",
            ),
            _FinalMsg([_TextBlock("Recovered.")], stop_reason="end_turn"),
        ]
    )

    with patch.dict(
        "openexecutive.orchestrator.executive._ALL_SKILL_HANDLERS",
        {"lookup_person": _boom, "list_people": _fine},
    ):
        items = _run_loop(provider)

    # The turn continued to a second API call instead of raising out of the
    # loop. (The scripted stream emits no text deltas, so `items` carries only
    # the in-flight sentinel — the second call is the real survival signal.)
    assert len(provider.calls) == 2
    assert items is not None

    results = _tool_results(provider, 1)
    failed = json.loads(results["tu-bad"])
    assert "error" in failed
    assert "lookup_person failed" in failed["error"]
    assert "RuntimeError" in failed["error"]
    # The sibling tool's real result is untouched.
    assert json.loads(results["tu-ok"]) == {"matches": []}

    # And the failure is on the audit trail, flagged not-ok.
    failures = [
        row
        for row in _no_audit_writes
        if row["event_type"] == "tool_invocation"
        and row.get("details", {}).get("ok") is False
    ]
    assert len(failures) == 1
    assert failures[0]["details"]["tool"] == "lookup_person"
    assert failures[0]["details"]["kind"] == "skill"
    assert "RuntimeError" in failures[0]["details"]["error"]


def test_cancelled_error_in_skill_handler_propagates() -> None:
    """Cancellation is not a tool failure. Swallowing it would break the
    turn-timeout and client-disconnect paths that cancel this coroutine."""

    async def _cancelled(_input: dict[str, Any]) -> str:
        raise asyncio.CancelledError()

    provider = _ScriptedProvider(
        [
            _FinalMsg(
                [_ToolUseBlock("tu-1", "lookup_person", {"query": "x"})],
                stop_reason="tool_use",
            ),
            _FinalMsg([_TextBlock("unreachable")], stop_reason="end_turn"),
        ]
    )

    with (
        patch.dict(
            "openexecutive.orchestrator.executive._ALL_SKILL_HANDLERS",
            {"lookup_person": _cancelled},
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        _run_loop(provider)


# --------------------------------------------------------------------- #
# MCP dispatch
# --------------------------------------------------------------------- #


class _ExplodingGateway:
    """Stands in for MCPGateway; only the three dispatched methods matter."""

    async def search_tools(self, _input: dict[str, Any]) -> str:
        return json.dumps({"tools": []})

    async def call_tool(self, _input: dict[str, Any]) -> str:
        raise ConnectionError("stdio pipe closed")

    async def load_mcp_server(self, _input: dict[str, Any]) -> str:
        return json.dumps({"ok": True})


def test_raising_mcp_tool_becomes_error_result_and_turn_survives(
    _no_audit_writes: list[dict[str, Any]],
) -> None:
    provider = _ScriptedProvider(
        [
            _FinalMsg(
                [
                    _ToolUseBlock(
                        "tu-mcp", "call_tool", {"name": "gcal__list", "arguments": "{}"}
                    )
                ],
                stop_reason="tool_use",
            ),
            _FinalMsg([_TextBlock("Recovered.")], stop_reason="end_turn"),
        ]
    )

    _run_loop(provider, Executive(mcp_gateway=_ExplodingGateway()))  # type: ignore[arg-type]

    assert len(provider.calls) == 2

    failed = json.loads(_tool_results(provider, 1)["tu-mcp"])
    assert "error" in failed
    # Labelled with the underlying MCP tool name, not the meta-tool.
    assert "gcal__list failed" in failed["error"]
    assert "ConnectionError" in failed["error"]

    failures = [
        row
        for row in _no_audit_writes
        if row["event_type"] == "tool_invocation"
        and row.get("details", {}).get("ok") is False
    ]
    assert len(failures) == 1
    assert failures[0]["details"]["kind"] == "mcp"
    assert failures[0]["details"]["tool"] == "gcal__list"
