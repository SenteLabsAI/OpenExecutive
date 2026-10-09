"""A single tool result must not be able to dominate a turn.

An unbounded result (a large document fetch) lands in the context and is
then re-sent on every remaining iteration of the tool loop, so one call
can cost more than the rest of the turn combined. `_cap_tool_result` is a
circuit breaker against that — deliberately set high enough that ordinary
tool output never reaches it.

The contract pinned here:
  1. under-limit results pass through byte-identically (the common case);
  2. an over-limit result is cut to within the budget, marker included;
  3. the marker names the tool, says the result is incomplete, and gives
     both character counts — silent truncation is worse than none, since
     the model reads the cut text as the whole answer;
  4. the limit comes from settings, not a literal;
  5. the cap sits DOWNSTREAM of the propose_form_values JSON parse, so an
     over-limit JSON result still produces its form_patch event. Capping
     at the producer instead would feed truncated JSON to json.loads and
     silently kill the SSE event.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from openexecutive.orchestrator.executive import (
    _TOOL_NAME_MARKER_MAX,
    Executive,
    _cap_tool_result,
)

# --------------------------------------------------------------------- #
# _cap_tool_result unit behaviour
# --------------------------------------------------------------------- #


def test_under_limit_result_is_unchanged() -> None:
    text = "a" * 100
    assert _cap_tool_result(text, tool_name="some_tool", limit=50_000) == text


def test_non_string_result_passes_through() -> None:
    payload = {"not": "a string"}
    assert _cap_tool_result(payload, tool_name="some_tool", limit=10) is payload


@pytest.mark.parametrize("limit", [1_000, 2_000, 9_999, 50_000])
def test_over_limit_result_never_exceeds_the_budget(limit: int) -> None:
    """The marker is reserved inside the budget, so the capped result is
    always <= limit — including when pct rounds up to three digits."""
    out = _cap_tool_result("x" * 500_000, tool_name="fetch_document", limit=limit)
    assert len(out) <= limit
    assert out.startswith("x" * 100)


def test_marker_names_the_tool_and_both_counts() -> None:
    out = _cap_tool_result("x" * 50_000, tool_name="fetch_document", limit=2_000)

    assert "TRUNCATED" in out
    assert "fetch_document" in out, "the model must know which call to re-issue"
    assert "50,000" in out, "the original size must be stated"
    assert "NOT the full result" in out, "a bare ellipsis is reliably ignored"
    assert "narrower" in out, "a naive retry would re-trigger the cut"


def test_limit_is_honoured_not_hardcoded() -> None:
    text = "y" * 5_000
    assert _cap_tool_result(text, tool_name="t", limit=50_000) == text
    assert _cap_tool_result(text, tool_name="t", limit=1_000) != text


@pytest.mark.parametrize(
    "size",
    [
        500,     # a short JSON status result
        4_000,   # a specialist's analysis
        15_000,  # a long email thread
        20_000,  # a substantial document excerpt (the shipped default)
    ],
)
def test_default_limit_does_not_fire_on_ordinary_output(size: int) -> None:
    """The breaker is insurance, not a routine clipper.

    Exercised behaviourally at the shipped default, across the range of
    result sizes real tools actually produce — a config-floor assertion
    alone would not notice the default being tuned down into the range
    where everyday document reads start getting cut.
    """
    from openexecutive.config import get_settings

    limit = get_settings().tool_result_max_chars
    text = "o" * size
    assert _cap_tool_result(text, tool_name="search_drive_files", limit=limit) == text


# --------------------------------------------------------------------- #
# Placement: downstream of the form_patch JSON parse
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
    usage = None

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
        import copy

        self.calls.append(copy.deepcopy(kwargs))
        return _FakeStream(self._final_msgs.pop(0))


@pytest.fixture(autouse=True)
def _no_audit_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "openexecutive.orchestrator.executive.audit_log",
        lambda *_a, **_k: None,
    )


def test_oversized_json_result_still_emits_form_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression this placement exists to prevent.

    propose_form_values does `json.loads(result)` on the *uncapped* value.
    Capping upstream of that parse would raise JSONDecodeError, which the
    handler swallows — dropping the form_patch SSE event with no error
    anywhere. Capping at the prompt boundary keeps both correct.
    """
    from openexecutive.orchestrator.executive import PROPOSE_FORM_VALUES

    exec_ = Executive()
    monkeypatch.setattr(exec_._settings, "tool_result_max_chars", 1_000)

    # A valid-JSON handler result that comfortably exceeds the cap.
    big_values = {f"field_{i}": "v" * 100 for i in range(50)}
    handler_result = json.dumps({"values": big_values})
    assert len(handler_result) > 1_000

    provider = _ScriptedProvider(
        [
            _FinalMsg(
                [_ToolUseBlock("tu_1", PROPOSE_FORM_VALUES, {"values": big_values})],
                "tool_use",
            ),
            _FinalMsg([_TextBlock("done")], "end_turn"),
        ]
    )

    async def _fake_handler(*_a: Any, **_k: Any) -> str:
        return handler_result

    async def _go() -> list[Any]:
        items: list[Any] = []
        with patch(
            "openexecutive.orchestrator.executive.get_provider",
            return_value=provider,
        ), patch.dict(
            "openexecutive.orchestrator.executive._ALL_SKILL_HANDLERS",
            {PROPOSE_FORM_VALUES: _fake_handler},
        ):
            async for item in exec_._stream_agent_loop(
                system_blocks=[],
                messages=[{"role": "user", "content": "fill the form"}],
                model="claude-test",
            ):
                items.append(item)
        return items

    items = asyncio.run(_go())

    # The form_patch event survived the cap.
    patches = [
        it
        for it in items
        if isinstance(it, dict) and it.get("type") == "form_patch"
    ]
    assert patches, "capping upstream of json.loads would have dropped this"

    # ...and the prompt still got the capped text.
    sent = provider.calls[1]["messages"][-1]["content"][0]["content"]
    assert len(sent) <= 1_000
    assert "TRUNCATED" in sent


# --------------------------------------------------------------------- #
# tool_name is model-supplied, therefore untrusted
# --------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "hostile_name",
    [
        "evil_{shown}_tool",          # would substitute a local via str.format
        "evil_{0}_tool",              # positional field reference
        "{",                          # unbalanced brace -> ValueError
        "}",
        "{limit}{pct}",
        "T" * 3_000,                  # would inflate the marker past the budget
    ],
)
def test_hostile_tool_name_neither_injects_nor_breaks_the_budget(
    hostile_name: str,
) -> None:
    """`tool_name` comes from the model's tool_use block, so prompt
    injection reaches it. It must never be interpreted as a format string
    (substitution or a raised ValueError that kills the turn), and must
    not be able to push the marker past the budget it fits inside."""
    out = _cap_tool_result("x" * 50_000, tool_name=hostile_name, limit=1_000)

    # Two things carry the weight here: no exception escaped (a raised
    # ValueError/KeyError from str.format would unwind the agent loop and
    # fail the whole turn), and the budget still held.
    assert len(out) <= 1_000, "a long tool name must not inflate the marker"
    assert "TRUNCATED" in out
    # The name is echoed as literal text, never interpreted as a field.
    # With str.format the brace forms below either raised or substituted;
    # as literals they survive verbatim (bounded to _TOOL_NAME_MARKER_MAX).
    literal = hostile_name[:_TOOL_NAME_MARKER_MAX]
    if "{" in hostile_name or "}" in hostile_name:
        assert literal in out, "braces must be literal text, not a format field"


def test_hostile_tool_name_is_truncated_not_echoed_whole() -> None:
    out = _cap_tool_result("x" * 50_000, tool_name="N" * 500, limit=2_000)
    assert "N" * 500 not in out
    assert "N" * 80 in out


# --------------------------------------------------------------------- #
# The turn's reading budget (TOOL_RESULTS_TURN_MAX_CHARS)
# --------------------------------------------------------------------- #


def test_turn_budget_limit() -> None:
    from openexecutive.orchestrator.executive import _TURN_BUDGET_FLOOR, _turn_result_limit

    # Plenty left: the per-result cap applies, not the budget.
    assert _turn_result_limit("read_document", per_result=20_000, turn_budget=40_000, used=0) == (20_000, False)
    # Partly used: what is left.
    assert _turn_result_limit("read_document", per_result=20_000, turn_budget=40_000, used=30_000) == (10_000, True)
    # Used up: still a floor, so the model sees what it got.
    assert _turn_result_limit("read_document", per_result=20_000, turn_budget=40_000, used=60_000) == (
        _TURN_BUDGET_FLOOR, True,
    )
    # Never more than the per-result cap, even at the floor.
    assert _turn_result_limit("x", per_result=2_000, turn_budget=40_000, used=60_000) == (2_000, True)
    # Off, and specialists exempt.
    assert _turn_result_limit("read_document", per_result=20_000, turn_budget=0, used=10**6) == (20_000, False)
    assert _turn_result_limit("consult_specialist", per_result=20_000, turn_budget=40_000, used=10**6) == (
        20_000, False,
    )
    # A built tool's result lists the writes that already ran: never cut short.
    assert _turn_result_limit("run_script", per_result=20_000, turn_budget=40_000, used=10**6) == (20_000, False)


def test_budget_marker_says_answer_from_what_you_have() -> None:
    out = _cap_tool_result("x" * 50_000, tool_name="read_document", limit=3_000, budget_spent=True)
    assert len(out) <= 3_000
    assert "already read as much as it can" in out
    # A read cut by the budget must never be the basis of an edit: the
    # model would write back a document missing everything past the cut.
    assert "Do not edit, rewrite or replace anything from this cut result" in out
    plain = _cap_tool_result("x" * 50_000, tool_name="read_document", limit=3_000)
    assert "already read as much as it can" not in plain


def test_a_turns_reads_share_one_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Three long reads across two rounds: the first two fit the budget, the
    third comes back at the floor with the budget note. A specialist's
    answer after that is neither counted nor cut."""
    exec_ = Executive()
    monkeypatch.setattr(exec_._settings, "tool_result_max_chars", 20_000)
    monkeypatch.setattr(exec_._settings, "tool_results_turn_max_chars", 40_000)

    provider = _ScriptedProvider(
        [
            _FinalMsg([_ToolUseBlock("r1", "read_document", {"n": 1}),
                       _ToolUseBlock("r2", "read_document", {"n": 2})], "tool_use"),
            _FinalMsg([_ToolUseBlock("r3", "read_document", {"n": 3}),
                       _ToolUseBlock("s1", "consult_specialist", {"specialist": "cfo", "query": "q"})],
                      "tool_use"),
            _FinalMsg([_TextBlock("done")], "end_turn"),
        ]
    )

    async def _read(*_a: Any, **_k: Any) -> str:
        return "d" * 19_000

    async def _go() -> None:
        with patch(
            "openexecutive.orchestrator.executive.get_provider", return_value=provider,
        ), patch.dict(
            "openexecutive.orchestrator.executive._ALL_SKILL_HANDLERS", {"read_document": _read},
        ), patch(
            "openexecutive.orchestrator.executive.route_parallel",
            new=AsyncMock(return_value={"s1": "a" * 15_000}),
        ):
            async for _ in exec_._stream_agent_loop(
                system_blocks=[], messages=[{"role": "user", "content": "x"}], model="claude-test",
            ):
                pass

    asyncio.run(_go())
    first = {b["tool_use_id"]: b["content"] for b in provider.calls[1]["messages"][-1]["content"]}
    assert len(first["r1"]) == 19_000 and len(first["r2"]) == 19_000
    second = {b["tool_use_id"]: b["content"] for b in provider.calls[2]["messages"][-1]["content"]}
    assert len(second["r3"]) <= 3_000
    assert "already read as much as it can" in second["r3"]
