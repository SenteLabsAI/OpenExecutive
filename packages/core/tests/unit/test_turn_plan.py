"""A long turn's plan, shown up front (orchestrator.turn_plan).

The unit half checks how one ``plan_steps`` call is answered; the loop half
drives the real ``_stream_agent_loop`` and checks the ``plan`` event comes
before the round's activity, and that the plan call never names the round.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch

from openexecutive.orchestrator import turn_plan
from openexecutive.orchestrator.executive import Executive, builtin_tool_names
from openexecutive.workflows import chat_tools

from ._agent_loop_fakes import FinalMsg, ScriptedProvider, TextBlock, ToolUseBlock

STEPS = ["Find the deals", "Read the emails", "Write the summary"]


def test_a_plan_is_shown_with_its_current_step() -> None:
    result, event, shown = turn_plan.apply({"steps": STEPS, "current": 1}, None)
    assert event == {"type": "plan", "steps": STEPS, "current": 1}
    assert shown == STEPS
    assert result == "Shown: step 2 of 3."


def test_moving_on_without_steps_keeps_the_plan() -> None:
    _, event, shown = turn_plan.apply({"current": 2}, STEPS)
    assert event is not None and event["steps"] == STEPS and event["current"] == 2
    assert shown == STEPS


def test_a_changed_plan_replaces_the_old_one() -> None:
    new = [*STEPS[:2], "Check Thursday's meeting", STEPS[2]]
    _, event, shown = turn_plan.apply({"steps": new, "current": 2}, STEPS)
    assert event is not None and event["steps"] == new
    assert shown == new


def test_no_steps_and_no_plan_shows_nothing() -> None:
    result, event, shown = turn_plan.apply({"steps": [], "current": 0}, None)
    assert event is None and shown is None
    assert "error" in json.loads(result)


def test_steps_are_cleaned_capped_and_current_clamped() -> None:
    raw = ["  Read   the\nemails ", "", 3, "x" * 200, *[f"s{i}" for i in range(20)]]
    _, event, _ = turn_plan.apply({"steps": raw, "current": 99}, None)
    assert event is not None
    steps = event["steps"]
    assert steps[0] == "Read the emails"
    assert len(steps[1]) == turn_plan.MAX_STEP_CHARS and steps[1].endswith("…")
    assert len(steps) == turn_plan.MAX_STEPS
    assert event["current"] == turn_plan.MAX_STEPS - 1
    _, event, _ = turn_plan.apply({"steps": STEPS, "current": "two"}, None)
    assert event is not None and event["current"] == 0
    _, event, _ = turn_plan.apply({"steps": STEPS, "current": -3}, None)
    assert event is not None and event["current"] == 0


def test_the_tool_is_known_to_the_loop_and_labelled_for_workflows() -> None:
    assert turn_plan.PLAN_STEPS in builtin_tool_names()
    assert chat_tools.label(turn_plan.PLAN_STEPS) == chat_tools.COVERED


def _run(provider: ScriptedProvider) -> list[Any]:
    async def _go() -> list[Any]:
        items: list[Any] = []
        with patch("openexecutive.orchestrator.executive.get_provider", return_value=provider):
            async for item in Executive()._stream_agent_loop(
                system_blocks=[],
                messages=[{"role": "user", "content": "go"}],
                model="claude-test",
            ):
                items.append(item)
        return items

    return asyncio.run(_go())


def _of(items: list[Any], kind: str) -> list[dict[str, Any]]:
    return [i for i in items if isinstance(i, dict) and i.get("type") == kind]


def test_the_loop_sends_the_plan_before_the_rounds_activity() -> None:
    provider = ScriptedProvider([
        FinalMsg([
            ToolUseBlock("p1", "plan_steps", {"steps": STEPS, "current": 0}),
            ToolUseBlock("t1", "list_people", {}),
        ], "tool_use"),
        FinalMsg([
            ToolUseBlock("p2", "plan_steps", {"steps": STEPS, "current": 1}),
            ToolUseBlock("t2", "list_people", {}),
        ], "tool_use"),
        FinalMsg([TextBlock("done")], "end_turn"),
    ])
    items = _run(provider)

    plans = _of(items, "plan")
    assert [p["current"] for p in plans] == [0, 1]
    acts = _of(items, "activity")
    # The round is named by its real work, never by the plan call.
    assert [a["label"] for a in acts] == ["Looking up people…", "Looking up people…"]
    assert items.index(plans[0]) < items.index(acts[0]) < items.index(plans[1])


def test_a_round_with_only_the_plan_is_still_paired_with_an_activity() -> None:
    provider = ScriptedProvider([
        FinalMsg([ToolUseBlock("p1", "plan_steps", {"steps": STEPS, "current": 0})], "tool_use"),
        FinalMsg([TextBlock("done")], "end_turn"),
    ])
    items = _run(provider)
    assert len(_of(items, "plan")) == 1
    assert len(_of(items, "activity")) == items.count(Executive._THINKING) == 1


def test_a_plan_call_on_its_own_is_told_to_ride_with_the_steps_calls() -> None:
    result, _, _ = turn_plan.apply({"steps": STEPS, "current": 0}, None, alone=True)
    assert result.endswith(turn_plan.ALONE_NOTE)
    result, _, _ = turn_plan.apply({"steps": STEPS, "current": 0}, None)
    assert turn_plan.ALONE_NOTE not in result
