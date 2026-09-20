"""The agent loop must cache its own transcript as it grows.

Without an intra-turn breakpoint the loop caches only the static
tools+system prefix, so every iteration re-sends the whole accumulated
transcript at full input price. The runtime signature of that bug is
`cache_read_input_tokens` pinned flat while the prompt grows iteration
over iteration.

A fake provider cannot produce real cache reads, so this module pins the
*structure* that produces them — one marker, on the newest tool result,
moving each iteration — and the live check (a real two-iteration tool
loop, asserting cache_read grows) covers the wire behaviour. Simulating
cache accounting here would pass while production stayed broken, which is
precisely the silent regression being guarded against.

The contract pinned here:
  1. the newest tool result carries the marker, and it is the last block
     of the last user message;
  2. exactly one tool_result is marked per request — the marker MOVES,
     it does not accumulate (15 accumulated markers would blow the
     4-breakpoint API limit);
  3. total cache_control markers across system + tools + messages stay
     within the API limit of 4;
  4. the loop marker is 5m (no `ttl` key) — the turn is over in seconds,
     so a 1h TTL would only double the write premium;
  5. `enable_caching=False` emits no marker at all;
  6. the caller's `messages` list is never mutated.
"""
from __future__ import annotations

import asyncio
import copy
from typing import Any
from unittest.mock import patch

import pytest

from openexecutive.orchestrator.executive import Executive

# --------------------------------------------------------------------- #
# Fake provider plumbing (same shape as test_executive_tool_error_isolation)
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
    """Records a deep snapshot of every request.

    The loop mutates one `current_messages` list in place and passes it by
    reference, so storing `kwargs` as-is would make every recorded call
    alias the same final state — and every per-call assertion below would
    silently pass regardless of what happened mid-loop. Snapshot at call
    time so "what did iteration N actually send" is a real question.
    """

    def __init__(self, final_msgs: list[_FinalMsg]) -> None:
        self._final_msgs = list(final_msgs)
        self.calls: list[dict[str, Any]] = []

    def messages_stream(self, **kwargs: Any) -> _FakeStream:
        self.calls.append(copy.deepcopy(kwargs))
        return _FakeStream(self._final_msgs.pop(0))


@pytest.fixture(autouse=True)
def _no_audit_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """`audit.log_event` otherwise writes to ./episodic_memory.db and leaks
    rows that break other modules' assertions in a full-suite run."""
    monkeypatch.setattr(
        "openexecutive.orchestrator.executive.audit_log",
        lambda *_a, **_k: None,
    )


def _scripted_three_iterations() -> _ScriptedProvider:
    """tool_use -> tool_use -> text, i.e. two tool rounds then an answer."""
    return _ScriptedProvider(
        [
            _FinalMsg([_ToolUseBlock("tu_1", "search_knowledge", {"q": "a"})], "tool_use"),
            _FinalMsg([_ToolUseBlock("tu_2", "search_knowledge", {"q": "b"})], "tool_use"),
            _FinalMsg([_TextBlock("done")], "end_turn"),
        ]
    )


def _run_loop(
    provider: _ScriptedProvider,
    messages: list[dict[str, Any]] | None = None,
    executive: Executive | None = None,
    system_blocks: list[dict[str, Any]] | None = None,
) -> list[Any]:
    exec_ = executive or Executive()
    msgs = messages if messages is not None else [
        {"role": "user", "content": "do the thing"}
    ]

    async def _go() -> list[Any]:
        items: list[Any] = []
        with patch(
            "openexecutive.orchestrator.executive.get_provider",
            return_value=provider,
        ):
            async for item in exec_._stream_agent_loop(
                system_blocks=system_blocks if system_blocks is not None else [],
                messages=msgs,
                model="claude-test",
            ):
                items.append(item)
        return items

    return asyncio.run(_go())


def _marked_tool_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every tool_result block in ``messages`` carrying a cache marker."""
    out: list[dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_result"
                and isinstance(block.get("cache_control"), dict)
            ):
                out.append(block)
    return out


def _count_all_markers(call: dict[str, Any]) -> int:
    """cache_control markers across system + tools + messages for one request."""
    n = 0
    for block in call.get("system") or []:
        if isinstance(block, dict) and isinstance(block.get("cache_control"), dict):
            n += 1
    for tool in call.get("tools") or []:
        if isinstance(tool, dict) and isinstance(tool.get("cache_control"), dict):
            n += 1
    for msg in call.get("messages") or []:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("cache_control"), dict):
                n += 1
    return n


# --------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------- #


def test_newest_tool_result_is_marked_and_is_the_last_block() -> None:
    provider = _scripted_three_iterations()
    _run_loop(provider)

    # Call 2 is the first request that carries a tool_result at all.
    messages = provider.calls[1]["messages"]
    marked = _marked_tool_results(messages)
    assert len(marked) == 1

    last_msg = messages[-1]
    assert last_msg["role"] == "user"
    assert last_msg["content"][-1]["tool_use_id"] == marked[0]["tool_use_id"], (
        "the breakpoint must sit on the newest tool result, otherwise the "
        "bytes after it are re-sent uncached on the next iteration"
    )


def test_marker_moves_instead_of_accumulating() -> None:
    """The regression that blows the 4-breakpoint limit at max_iterations=15."""
    provider = _scripted_three_iterations()
    _run_loop(provider)

    call2_marked = _marked_tool_results(provider.calls[1]["messages"])
    call3_marked = _marked_tool_results(provider.calls[2]["messages"])

    assert len(call2_marked) == 1
    assert len(call3_marked) == 1, (
        "a second marker means the marker accumulated rather than moved"
    )
    # The block marked on call 2 must have been swept clean by call 3.
    assert call3_marked[0]["tool_use_id"] == "tu_2"
    assert call2_marked[0]["tool_use_id"] == "tu_1"
    prior = [
        b
        for msg in provider.calls[2]["messages"]
        if isinstance(msg.get("content"), list)
        for b in msg["content"]
        if isinstance(b, dict) and b.get("tool_use_id") == "tu_1"
    ]
    assert prior and "cache_control" not in prior[0]


def test_total_markers_are_exactly_the_four_the_api_allows() -> None:
    """The live budget has ZERO headroom, so assert the real number.

    Passing `system_blocks=[]` would make this vacuous — 0 system + 1 tool
    + 1 loop = 2, and `<= 4` could never fail however the layout changed.
    Using the real `build_system_blocks` output pins the actual shipped
    budget: 2 system + 1 tool + 1 intra-turn = 4. A future third system
    block, or a second message-level marker, ships a 5-breakpoint request
    that Anthropic rejects outright — and this test goes red first.
    """
    from openexecutive.memory.company_profile import CompanyProfile
    from openexecutive.prompts.cache_manager import build_system_blocks

    system_blocks = build_system_blocks(
        company_profile=CompanyProfile(name="Test Co", industry="Testing"),
    )
    system_markers = sum(
        1
        for b in system_blocks
        if isinstance(b, dict) and isinstance(b.get("cache_control"), dict)
    )
    assert system_markers == 2, (
        "cache_manager's documented budget is 2 system blocks; if this "
        "changed, the loop marker no longer fits under the API limit"
    )

    provider = _scripted_three_iterations()
    _run_loop(provider, system_blocks=system_blocks)

    # The iteration that carries a tool_result is the one at full budget.
    assert _count_all_markers(provider.calls[1]) == 4
    for i, call in enumerate(provider.calls):
        assert _count_all_markers(call) <= 4, f"call {i} exceeded the 4-block limit"


def test_loop_marker_uses_the_default_five_minute_ttl() -> None:
    """A 1h TTL doubles the write premium; the turn is over in seconds."""
    provider = _scripted_three_iterations()
    _run_loop(provider)
    marked = _marked_tool_results(provider.calls[1]["messages"])
    assert marked[0]["cache_control"] == {"type": "ephemeral"}
    assert "ttl" not in marked[0]["cache_control"]


def test_no_marker_when_caching_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    exec_ = Executive()
    monkeypatch.setattr(exec_._settings, "enable_caching", False)
    provider = _scripted_three_iterations()
    _run_loop(provider, executive=exec_)
    for call in provider.calls:
        assert _marked_tool_results(call["messages"]) == []


def test_caller_messages_are_not_mutated() -> None:
    """The loop shallow-copies the caller's list; marking must not alias it.

    The caller message must contain a tool_result carrying a pre-existing
    marker — a flat-string message is skipped by the helper's
    `isinstance(content, list)` guard, so it would pass even if the sweep
    aggressively rewrote caller-owned blocks, which is the actual hazard
    the call-site comment reasons about.
    """
    caller_messages: list[dict[str, Any]] = [
        {"role": "user", "content": "do the thing"},
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "caller_tu",
                    "content": "caller-owned result",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        },
    ]
    before = copy.deepcopy(caller_messages)
    provider = _scripted_three_iterations()
    _run_loop(provider, messages=caller_messages)
    assert caller_messages == before


def test_build_messages_marks_no_history_turn() -> None:
    """The removed rolling-history marker was a latent 5th breakpoint.

    It lived in `_build_messages`, so this must assert on that method's
    output. Asserting on `_stream_agent_loop` (which takes `messages`
    pre-built) would pass with the deleted code fully restored — and
    restoring it ships a 5-breakpoint request Anthropic rejects outright.
    """
    from openexecutive.orchestrator.session import Session

    session = Session(session_id="s-hist")
    session.add_user_message("earlier question")
    session.add_assistant_message("earlier answer")
    session.add_user_message("second question")
    session.add_assistant_message("second answer")

    built = Executive()._build_messages(session, "do the thing")

    marked = [
        b
        for msg in built
        if isinstance(msg.get("content"), list)
        for b in msg["content"]
        if isinstance(b, dict) and "cache_control" in b
    ]
    assert marked == [], (
        "history turns must carry no cache_control — the budget's four "
        "slots are system x2 + tools + the intra-turn loop marker"
    )


def test_marker_falls_back_when_the_newest_results_are_empty() -> None:
    """An all-empty iteration must still get a breakpoint.

    Losing it re-sends the whole accumulated transcript at full price for
    that step, which is the exact cost this feature exists to remove.
    """
    from openexecutive.orchestrator.executive import _apply_loop_cache_marker

    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "real"}
            ],
        },
        {"role": "assistant", "content": [{"type": "text", "text": "x"}]},
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t2", "content": ""}],
        },
    ]
    _apply_loop_cache_marker(messages)

    marked = [
        b
        for msg in messages
        if isinstance(msg.get("content"), list)
        for b in msg["content"]
        if isinstance(b, dict) and "cache_control" in b
    ]
    assert len(marked) == 1
    assert marked[0]["tool_use_id"] == "t1"


def test_non_string_tool_result_content_is_still_markable() -> None:
    """A typed/image result is real bytes in the prefix, so it must be
    able to carry the breakpoint — the cap deliberately passes non-str
    results through untouched, and dropping the marker for them would
    silently lose the cache hit for that whole iteration."""
    from openexecutive.orchestrator.executive import _apply_loop_cache_marker

    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "t1",
                    "content": [{"type": "text", "text": "typed result"}],
                }
            ],
        }
    ]
    _apply_loop_cache_marker(messages)
    assert messages[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
