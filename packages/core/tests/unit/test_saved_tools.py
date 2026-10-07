"""Saved tools: the store (versions, on/off, rollback, run history) and
``run_script_tool`` saving a script that worked and running it again by name.

A saved tool is approved automatically because it can do no more than the
context that runs it, so these pin the limits that make that true: only a
script that succeeded is saved, a turned-off tool never runs, a workflow step
runs one only when it already allows every tool the script used, and the
global switch turns the whole thing off.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-used")

from openexecutive.workflows import (
    saved_tools,  # noqa: E402
    step_script,  # noqa: E402
)

LIST = "drive__list_items"
MOVE = "drive__move_file"

COUNT_SCRIPT = """
n = 0
for f in drive__list_items(folder=inputs["folder"]):
    n = n + 1
n
"""


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "saved.db"
    monkeypatch.setattr(saved_tools, "DB_PATH", path)
    monkeypatch.delenv("SAVED_TOOLS_ENABLED", raising=False)
    return path


class _Calls:
    def __init__(self) -> None:
        self.made: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        self.made.append((name, arguments))
        if name == LIST:
            return json.dumps([{"id": "a"}, {"id": "b"}, {"id": "c"}]), False
        return json.dumps({"ok": True}), False


async def _tool(arguments: dict[str, Any], tools: list[str] | None = None, call: Any = None,
                origin: str = "chat") -> tuple[dict[str, Any], bool]:
    final: tuple[str, bool] = ("", True)
    async for kind, payload in step_script.run_script_tool(
        arguments, tools=tools, call=call or _Calls(), origin=origin,
    ):
        if kind == "done":
            final = payload
    return json.loads(final[0]), final[1]


# --- the store ----------------------------------------------------------------


def test_save_versions_only_changed_content() -> None:
    first = saved_tools.save("count_files", "Count files.", "1", [LIST], origin="chat")
    same = saved_tools.save("count_files", "Count files.", "1", [LIST], origin="chat")
    changed = saved_tools.save("count_files", "Count the files.", "2", [LIST], origin="chat")
    assert (first.version, same.version, changed.version) == (1, 1, 2)
    assert [v["version"] for v in saved_tools.versions("count_files")] == [2, 1]
    assert saved_tools.get("count_files").script == "2"  # type: ignore[union-attr]


def test_rollback_and_turning_off() -> None:
    saved_tools.save("count_files", "v1", "1", [LIST], origin="chat")
    saved_tools.save("count_files", "v2", "2", [LIST], origin="chat")
    back = saved_tools.rollback("count_files", 1)
    assert (back.version, back.script, back.description) == (1, "1", "v1")
    off = saved_tools.set_enabled("count_files", False)
    assert not off.enabled
    assert saved_tools.list_tools(enabled_only=True) == []
    # A new version from the model doesn't turn it back on; only the owner does.
    assert not saved_tools.save("count_files", "v3", "3", [LIST], origin="chat").enabled
    with pytest.raises(saved_tools.SavedToolError):
        saved_tools.rollback("count_files", 9)
    with pytest.raises(saved_tools.SavedToolError):
        saved_tools.set_enabled("nope_tool", True)


@pytest.mark.parametrize(
    ("name", "description", "script"),
    [("Bad Name", "x", "1"), ("ok_name", "  ", "1"), ("ok_name", "x" * 501, "1"), ("ok_name", "x", " ")],
)
def test_bad_saves_are_refused(name: str, description: str, script: str) -> None:
    with pytest.raises(saved_tools.SavedToolError):
        saved_tools.save(name, description, script, [], origin="chat")


def test_run_history_is_kept_and_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(saved_tools, "_MAX_RUNS_KEPT_PER_TOOL", 3)
    for i in range(5):
        saved_tools.record_run("count_files", 1, ok=i % 2 == 0, calls=i, duration_ms=5, origin="chat")
    runs = saved_tools.runs("count_files", limit=10)
    assert [r["calls"] for r in runs] == [4, 3, 2]
    assert runs[0]["ok"] is True and runs[1]["ok"] is False


# --- saving and running through run_script ------------------------------------


@pytest.mark.asyncio
async def test_a_script_that_works_is_saved_with_the_tools_it_called() -> None:
    body, is_error = await _tool({
        "script": COUNT_SCRIPT, "inputs": {"folder": "Inbox"},
        "save_as": "count_files", "description": "Count the files in a folder.",
    })
    assert not is_error and body["result"] == 3
    assert body["saved"] == {"name": "count_files", "version": 1, "uses_tools": [LIST], "enabled": True}
    kept = saved_tools.get("count_files")
    assert kept is not None and kept.script == COUNT_SCRIPT and kept.origin == "chat"


@pytest.mark.asyncio
async def test_a_failed_script_is_not_saved() -> None:
    body, is_error = await _tool({"script": "1/0", "save_as": "broken_tool", "description": "x"})
    assert is_error and body["save_error"] == "not saved: the script failed"
    assert saved_tools.get("broken_tool") is None


@pytest.mark.asyncio
async def test_a_bad_name_is_reported_not_raised() -> None:
    body, is_error = await _tool({"script": "1", "save_as": "Bad Name", "description": "x"})
    assert not is_error and "snake_case" in body["save_error"]


@pytest.mark.asyncio
async def test_a_saved_tool_runs_again_with_new_inputs_and_is_recorded() -> None:
    saved_tools.save("count_files", "Count files.", COUNT_SCRIPT, [LIST], origin="chat")
    calls = _Calls()
    body, is_error = await _tool({"tool": "count_files", "inputs": {"folder": "Legal"}}, call=calls)
    assert not is_error and body["result"] == 3
    assert body["saved_tool"] == {"name": "count_files", "version": 1}
    assert calls.made == [(LIST, {"folder": "Legal"})]
    [run] = saved_tools.runs("count_files")
    assert run["ok"] and run["calls"] == 1 and run["origin"] == "chat"


@pytest.mark.asyncio
async def test_a_turned_off_tool_does_not_run() -> None:
    saved_tools.save("count_files", "Count files.", COUNT_SCRIPT, [LIST], origin="chat")
    saved_tools.set_enabled("count_files", False)
    calls = _Calls()
    body, is_error = await _tool({"tool": "count_files", "inputs": {"folder": "x"}}, call=calls)
    assert is_error and "is turned on" in body["error"] and calls.made == []


@pytest.mark.asyncio
async def test_a_step_runs_a_saved_tool_only_with_every_tool_it_used() -> None:
    saved_tools.save("file_all", "File everything.", "drive__move_file(file_id='a')", [LIST, MOVE],
                     origin="chat")
    calls = _Calls()
    body, is_error = await _tool({"tool": "file_all"}, tools=[LIST], call=calls)
    assert is_error and body["missing_tools"] == [MOVE] and calls.made == []
    assert [t.name for t in step_script.usable_saved_tools([LIST])] == []
    assert [t.name for t in step_script.usable_saved_tools([LIST, MOVE])] == ["file_all"]
    body, is_error = await _tool({"tool": "file_all"}, tools=[LIST, MOVE], call=calls,
                                 origin="workflow:wf/step")
    assert not is_error and calls.made == [(MOVE, {"file_id": "a"})]
    assert saved_tools.runs("file_all")[0]["origin"] == "workflow:wf/step"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({}, "either script or tool"),
        ({"script": "1", "tool": "count_files"}, "either script or tool"),
        ({"tool": "count_files", "save_as": "other_one"}, "save_as goes with a new script"),
        ({"script": "1", "inputs": ["not", "a", "dict"]}, "inputs must be an object"),
        ({"script": "1", "inputs": {"big": "x" * 30_000}}, "inputs are too large"),
    ],
)
async def test_malformed_calls_are_errors(arguments: dict[str, Any], message: str) -> None:
    saved_tools.save("count_files", "Count files.", COUNT_SCRIPT, [LIST], origin="chat")
    body, is_error = await _tool(arguments)
    assert is_error and message in body["error"]


@pytest.mark.asyncio
async def test_the_switch_turns_saving_and_running_off(monkeypatch: pytest.MonkeyPatch) -> None:
    saved_tools.save("count_files", "Count files.", COUNT_SCRIPT, [LIST], origin="chat")
    monkeypatch.setenv("SAVED_TOOLS_ENABLED", "false")
    body, is_error = await _tool({"tool": "count_files", "inputs": {"folder": "x"}})
    assert is_error and body["error"] == "saved tools are turned off"
    body, is_error = await _tool({"script": "2", "save_as": "two_tool", "description": "Two."})
    assert not is_error and body["result"] == 2 and body["save_error"] == "saved tools are turned off"
    assert saved_tools.get("two_tool") is None
    assert step_script.usable_saved_tools([LIST]) == []
    assert json.loads(step_script.list_saved_tools_result())["saved_tools"] == []


def test_listing_shows_enabled_tools_without_their_scripts() -> None:
    saved_tools.save("count_files", "Count files.", COUNT_SCRIPT, [LIST], origin="chat")
    saved_tools.save("old_tool", "Old.", "1", [], origin="chat")
    saved_tools.set_enabled("old_tool", False)
    listed = json.loads(step_script.list_saved_tools_result())["saved_tools"]
    assert listed == [
        {"name": "count_files", "description": "Count files.", "version": 1, "uses_tools": [LIST]}
    ]


def test_a_step_definition_lists_the_saved_tools_it_may_run() -> None:
    kept = saved_tools.save("count_files", "Count files.", COUNT_SCRIPT, [LIST], origin="chat")
    definition = step_script.tool_definition([LIST], [kept])
    assert "count_files" in definition["description"] and "Count files." in definition["description"]
    assert COUNT_SCRIPT not in definition["description"]


def test_a_name_with_a_trailing_newline_is_refused() -> None:
    with pytest.raises(saved_tools.SavedToolError):
        saved_tools.save("count_files\n", "Count.", "1", [], origin="chat")


@pytest.mark.asyncio
async def test_storage_errors_never_escape(monkeypatch: pytest.MonkeyPatch) -> None:
    import sqlite3

    def locked(*_a: Any, **_k: Any) -> Any:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(saved_tools, "get", locked)
    monkeypatch.setattr(saved_tools, "save", locked)
    monkeypatch.setattr(saved_tools, "list_tools", locked)
    calls = _Calls()
    body, is_error = await _tool({"tool": "count_files"}, call=calls)
    assert is_error and "couldn't be read" in body["error"] and calls.made == []
    # The script ran (its writes happened), so its result comes back.
    body, is_error = await _tool(
        {"script": COUNT_SCRIPT, "inputs": {"folder": "x"}, "save_as": "count_files", "description": "C."},
        call=calls,
    )
    assert not is_error and body["result"] == 3 and "storage error" in body["save_error"]
    assert step_script.usable_saved_tools([LIST]) == []
    assert "error" in json.loads(step_script.list_saved_tools_result())


@pytest.mark.asyncio
async def test_uses_tools_counts_every_call_but_not_refused_ones() -> None:
    script = """
for i in range(250):
    drive__list_items(folder="x")
drive__move_file(file_id="late")
try:
    call_tool("drive__delete_all", {})
except Exception:
    pass
"""

    async def refuse_outside(name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        if name not in (LIST, MOVE):
            return json.dumps({"error": "not one of this step's tools"}), True
        return json.dumps([]), False

    body, is_error = await _tool(
        {"script": script, "save_as": "late_mover", "description": "Moves late."},
        tools=[LIST, MOVE], call=refuse_outside,
    )
    assert not is_error, body
    assert body["saved"]["uses_tools"] == [LIST, MOVE]


@pytest.mark.asyncio
async def test_saving_a_turned_off_tool_says_so() -> None:
    saved_tools.save("count_files", "Count files.", "1", [], origin="chat")
    saved_tools.set_enabled("count_files", False)
    body, _ = await _tool({"script": "2", "save_as": "count_files", "description": "Count."})
    assert body["saved"]["enabled"] is False and "turned this tool off" in body["saved"]["note"]
