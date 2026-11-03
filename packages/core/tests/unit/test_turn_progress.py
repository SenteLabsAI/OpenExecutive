"""What a long turn shows while it works (orchestrator.turn_progress).

The unit half checks how one ``show_progress`` call is answered and how reads
are counted; the loop half drives the real ``_stream_agent_loop`` and checks
the ``progress`` event comes before the round's activity, that the progress
call never names the round, and that the activity carries the round's reads.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch

from openexecutive.orchestrator import turn_progress
from openexecutive.orchestrator.executive import Executive, builtin_tool_names
from openexecutive.workflows import chat_tools

from ._agent_loop_fakes import FinalMsg, ScriptedProvider, TextBlock, ToolUseBlock

TASK = "Reviewing the Harbor Point LOI"


def test_a_first_call_shows_the_task_and_what_it_is_doing() -> None:
    result, event, shown = turn_progress.apply(
        {"task": TASK, "now": "Reading the LOI", "used": ["Harbor Point LOI v3.pdf"]}, None
    )
    assert event == {
        "type": "progress",
        "task": TASK,
        "now": "Reading the LOI",
        "used": ["Harbor Point LOI v3.pdf"],
        "found": [],
    }
    assert shown is not None and shown.task == TASK
    assert result == "Shown."


def test_later_calls_add_used_and_found_and_keep_the_task() -> None:
    _, _, shown = turn_progress.apply(
        {"task": TASK, "now": "Reading the LOI", "used": ["LOI.pdf"]}, None
    )
    _, event, shown = turn_progress.apply(
        {
            "now": "Checking the rent schedule",
            "used": ["loi.pdf", "Rent model.xlsx"],
            "found": ["Term is 7 years; the March email agreed 10."],
        },
        shown,
    )
    assert event is not None
    assert event["task"] == TASK
    assert event["now"] == "Checking the rent schedule"
    # The same name in another case is one source.
    assert event["used"] == ["LOI.pdf", "Rent model.xlsx"]
    assert event["found"] == ["Term is 7 years; the March email agreed 10."]


def test_a_call_without_now_leaves_the_live_line_to_the_activity() -> None:
    _, _, shown = turn_progress.apply({"task": TASK, "now": "Reading the LOI"}, None)
    _, event, _ = turn_progress.apply({"used": ["Q3.xlsx"]}, shown)
    assert event is not None and event["now"] == "" and event["task"] == TASK


def test_findings_keep_the_newest() -> None:
    shown = None
    for i in range(turn_progress.MAX_FOUND + 2):
        _, _, shown = turn_progress.apply({"now": "x", "found": [f"f{i}"]}, shown)
    assert shown is not None
    assert shown.found[-1] == f"f{turn_progress.MAX_FOUND + 1}"
    assert len(shown.found) == turn_progress.MAX_FOUND


def test_nothing_to_show_shows_nothing() -> None:
    result, event, shown = turn_progress.apply({"now": "  "}, None)
    assert event is None and shown is None
    assert "error" in json.loads(result)


def test_lines_are_cleaned_and_lists_capped() -> None:
    raw_used = [f"file {i}.pdf" for i in range(30)]
    _, event, shown = turn_progress.apply(
        {"now": "  Reading \n the  files ", "used": [*raw_used, 3, ""], "found": ["a", "b", "c"]},
        None,
    )
    assert event is not None
    assert event["now"] == "Reading the files"
    # The first sources are kept.
    assert event["used"] == raw_used[: turn_progress.MAX_USED]
    # At most two findings a call.
    assert event["found"] == ["a", "b"]
    _, event, _ = turn_progress.apply({"now": "x" * 300}, shown)
    assert event is not None
    assert len(event["now"]) == turn_progress.MAX_LINE_CHARS and event["now"].endswith("…")


def test_reads_count_each_distinct_read_once_by_kind() -> None:
    reads = turn_progress.Reads()
    first = reads.add([
        {"name": "read_my_email", "input": {"message_id": "m1"}},
        {"name": "read_my_email", "input": {"message_id": "m2"}},
        {"name": "read_document", "input": {"file": "drive:abc"}},
        {"name": "search_my_email", "input": {"query": "LOI"}},
    ])
    assert first == {"email": 2, "file": 1}
    # Reading the same email again isn't a new one.
    again = reads.add([
        {"name": "read_my_email", "input": {"message_id": "m1"}},
        {"name": "my_calendar", "input": {}},
    ])
    assert again == {"calendar": 1}
    assert reads.add([{"name": "save_file", "input": {}}]) == {}


def test_the_tool_is_known_to_the_loop_and_labelled_for_workflows() -> None:
    assert turn_progress.SHOW_PROGRESS in builtin_tool_names()
    assert chat_tools.label(turn_progress.SHOW_PROGRESS) == chat_tools.COVERED


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


def test_the_loop_sends_progress_before_the_rounds_activity() -> None:
    provider = ScriptedProvider([
        FinalMsg([
            ToolUseBlock("p1", "show_progress", {"task": TASK, "now": "Finding the team"}),
            ToolUseBlock("t1", "list_people", {}),
        ], "tool_use"),
        FinalMsg([
            ToolUseBlock("p2", "show_progress", {"now": "Checking the roster again"}),
            ToolUseBlock("t2", "list_people", {}),
        ], "tool_use"),
        FinalMsg([TextBlock("done")], "end_turn"),
    ])
    items = _run(provider)

    shown = _of(items, "progress")
    assert [p["now"] for p in shown] == ["Finding the team", "Checking the roster again"]
    assert shown[1]["task"] == TASK
    acts = _of(items, "activity")
    # The round is named by its real work, never by the progress call.
    assert [a["label"] for a in acts] == ["Looking up people…", "Looking up people…"]
    assert items.index(shown[0]) < items.index(acts[0]) < items.index(shown[1])


def test_a_round_with_only_progress_is_still_paired_with_an_activity() -> None:
    provider = ScriptedProvider([
        FinalMsg([ToolUseBlock("p1", "show_progress", {"now": "Starting"})], "tool_use"),
        FinalMsg([TextBlock("done")], "end_turn"),
    ])
    items = _run(provider)
    assert len(_of(items, "progress")) == 1
    assert len(_of(items, "activity")) == items.count(Executive._THINKING) == 1


def test_the_activity_carries_the_rounds_new_reads() -> None:
    provider = ScriptedProvider([
        FinalMsg([ToolUseBlock("t1", "lookup_person", {"name": "Dana"})], "tool_use"),
        FinalMsg([ToolUseBlock("t2", "lookup_person", {"name": "Dana"})], "tool_use"),
        FinalMsg([TextBlock("done")], "end_turn"),
    ])
    acts = _of(_run(provider), "activity")
    assert acts[0]["reads"] == {"person": 1}
    # The same lookup again adds nothing.
    assert "reads" not in acts[1]


def test_a_progress_call_on_its_own_is_told_to_ride_with_the_rounds_calls() -> None:
    result, _, _ = turn_progress.apply({"now": "Starting"}, None, alone=True)
    assert result.endswith(turn_progress.ALONE_NOTE)
    result, _, _ = turn_progress.apply({"now": "Starting"}, None)
    assert turn_progress.ALONE_NOTE not in result
