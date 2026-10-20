"""Agent-loop and message-build behavior for the Ask OE form-fill tier.

Covers the two invariants the feature hangs on:

1. ``propose_form_values`` tool_use → the loop yields a ``form_patch``
   dict (delivered to the panel via SSE) and feeds the handler's
   acknowledgement back as the tool_result.
2. Cache stability: the tool is ALWAYS in the sorted client tool list —
   tool defs sit at the top of the cached prompt prefix, so a
   conditionally-present tool would invalidate the cache whenever panel
   and non-panel turns alternate.

Plus: ``<page_context>`` placement in the user turn of _build_messages.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch

from openexecutive.memory.company_profile import CompanyProfile
from openexecutive.orchestrator.executive import (
    _ALL_SKILL_TOOLS,
    Executive,
)
from openexecutive.orchestrator.form_tools import PROPOSE_FORM_VALUES
from openexecutive.orchestrator.router import SPECIALIST_TOOLS
from openexecutive.orchestrator.session import Session

# --------------------------------------------------------------------- #
# Fake provider plumbing (mirrors test_executive_response_audit.py)
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
        raise StopAsyncIteration  # no text deltas; content rides on final_msg

    async def get_final_message(self) -> _FinalMsg:
        return self._final_msg


class _ScriptedProvider:
    """Returns one scripted final message per messages_stream call."""

    def __init__(self, final_msgs: list[_FinalMsg]) -> None:
        self._final_msgs = list(final_msgs)
        self.calls: list[dict[str, Any]] = []

    def messages_stream(self, **kwargs: Any) -> _FakeStream:
        self.calls.append(kwargs)
        return _FakeStream(self._final_msgs.pop(0))


def _run_loop(provider: _ScriptedProvider) -> list[Any]:
    async def _go() -> list[Any]:
        items: list[Any] = []
        with patch(
            "openexecutive.orchestrator.executive.get_provider",
            return_value=provider,
        ):
            async for item in Executive()._stream_agent_loop(
                system_blocks=[],
                messages=[{"role": "user", "content": "fill the form"}],
                model="claude-test",
            ):
                items.append(item)
        return items

    return asyncio.run(_go())


def test_propose_form_values_yields_form_patch_and_tool_result() -> None:
    tool_input = {
        "form_id": "add_person",
        "fields": {"full_name": "Sara Chen", "role": "CFO"},
        "rationale": "From the user's description.",
    }
    provider = _ScriptedProvider(
        [
            _FinalMsg(
                [_ToolUseBlock("tu-1", PROPOSE_FORM_VALUES, tool_input)],
                stop_reason="tool_use",
            ),
            _FinalMsg([_TextBlock("Done — review and save.")], stop_reason="end_turn"),
        ]
    )

    items = _run_loop(provider)

    patches = [
        i for i in items if isinstance(i, dict) and i.get("type") == "form_patch"
    ]
    assert len(patches) == 1
    assert patches[0]["form_id"] == "add_person"
    assert patches[0]["fields"] == {"full_name": "Sara Chen", "role": "CFO"}
    assert patches[0]["iteration"] == 1

    # No action chip — a proposal mutates nothing.
    assert not any(
        isinstance(i, dict) and i.get("type") == "action_taken" for i in items
    )

    # Second API call must carry the handler's acknowledgement as the
    # tool_result so the model knows the proposal was delivered, not saved.
    second_messages = provider.calls[1]["messages"]
    tool_result_turn = second_messages[-1]
    assert tool_result_turn["role"] == "user"
    result_content = tool_result_turn["content"][0]
    assert result_content["tool_use_id"] == "tu-1"
    ack = json.loads(result_content["content"])
    assert ack["status"] == "delivered"


def test_invalid_proposal_yields_no_form_patch() -> None:
    """Shape errors go back to the model as the tool_result; the client
    must NOT receive a form_patch for them."""
    provider = _ScriptedProvider(
        [
            _FinalMsg(
                [_ToolUseBlock("tu-1", PROPOSE_FORM_VALUES, {"form_id": "x", "fields": {}})],
                stop_reason="tool_use",
            ),
            _FinalMsg([_TextBlock("ok")], stop_reason="end_turn"),
        ]
    )

    items = _run_loop(provider)

    assert not any(
        isinstance(i, dict) and i.get("type") == "form_patch" for i in items
    )
    ack = json.loads(provider.calls[1]["messages"][-1]["content"][0]["content"])
    assert "error" in ack


def test_tool_list_is_cache_stable_and_includes_form_tool() -> None:
    """The sorted client tool list must contain propose_form_values and be
    identical regardless of request content — it's part of the cached
    prompt prefix (see executive.py _stream_agent_loop)."""
    provider = _ScriptedProvider(
        [
            _FinalMsg([_TextBlock("a")], stop_reason="end_turn"),
        ]
    )
    _run_loop(provider)
    sent_names = [t["name"] for t in provider.calls[0]["tools"]]

    from openexecutive.orchestrator import tool_groups
    from openexecutive.workflows import python_job

    # run_python_job is offered only on the principal's own turns where its
    # sandbox is installed; this turn isn't the principal's.
    # The less common tools are behind open_tools/use_tool (tool_groups).
    direct, _ = tool_groups.split(
        t for t in [*SPECIALIST_TOOLS, *_ALL_SKILL_TOOLS] if t["name"] != python_job.TOOL_NAME
    )
    expected = sorted(t["name"] for t in direct)
    assert PROPOSE_FORM_VALUES in sent_names
    # Anthropic server-side tools (web_search) are appended AFTER the cached
    # client prefix — only the prefix participates in the cache key.
    assert sent_names[: len(expected)] == expected, (
        "cached client tool prefix must be the static sorted roster"
    )

    # And it is request-independent: a second loop run sends a byte-identical
    # tool list (names AND schemas/cache markers — the whole cache prefix),
    # not merely the same names.
    provider2 = _ScriptedProvider(
        [_FinalMsg([_TextBlock("b")], stop_reason="end_turn")]
    )
    _run_loop(provider2)
    assert json.dumps(provider2.calls[0]["tools"], sort_keys=True) == json.dumps(
        provider.calls[0]["tools"], sort_keys=True
    )


def test_build_messages_page_context_placement() -> None:
    executive = Executive()
    session = Session(session_id="s1", company_profile=CompanyProfile())

    messages = executive._build_messages(
        session, "fill it in", page_context_block="PAGE: /people — \"People\""
    )
    user_parts = messages[-1]["content"]
    page_parts = [
        p for p in user_parts if p.get("text", "").startswith("<page_context>")
    ]
    assert len(page_parts) == 1
    assert 'PAGE: /people — "People"' in page_parts[0]["text"]
    # The literal user message stays the final part.
    assert user_parts[-1]["text"] == "fill it in"

    # Empty block → no <page_context> part at all (byte-identical old turn).
    messages_plain = executive._build_messages(session, "hi")
    assert not any(
        "<page_context>" in p.get("text", "")
        for p in messages_plain[-1]["content"]
    )
