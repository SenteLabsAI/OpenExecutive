"""Tools the Executive opens when it needs them, instead of carrying them all.

Every model call in a chat turn re-sends the whole tool list, and the list is
the first thing in the cached prefix. Most answers use none of the less
common tools (goals, the watch list, workflows, documents, …), yet their
descriptions were most of that prefix. Here those tools sit in named groups:
the turn offers two small tools in their place,

  * ``open_tools(group)`` returns the group's tools (name, description, input
    schema) as a tool result, and
  * ``use_tool(name, input)`` runs one of them,

so the offered list stays the same on every call of every turn (a group
opening never changes the cached prefix) and a group's descriptions are paid
for only on a turn that needs them. A ``use_tool`` call is rewritten into the
tool's own call before dispatch (``unwrap``), so it meets exactly the guards,
handlers, chips and audit rows a direct call would.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

OPEN_TOOLS = "open_tools"
USE_TOOL = "use_tool"

# group -> (what it is for, the tools in it). Each tool belongs to one group.
# Everything not listed here stays on the direct list.
GROUPS: dict[str, tuple[str, tuple[str, ...]]] = {
    "calendar": (
        "create, start or cancel calendar events and meetings",
        ("cancel_calendar_event", "create_calendar_event", "create_instant_meeting"),
    ),
    "people": (
        "add, change or archive people on the roster, department heads, roster requests",
        ("archive_person", "resolve_roster_request", "set_department_head", "upsert_person"),
    ),
    "goals": (
        "department goals, decision outcomes, open loops",
        (
            "assign_open_loop",
            "close_open_loop",
            "create_goal",
            "list_department_goals",
            "list_open_loops",
            "record_decision_outcome",
            "update_department_goal",
        ),
    ),
    "watch_and_alerts": (
        "keep an eye on topics, people or competitors (the watch list), alerts, executive research runs",
        (
            "ack_alert",
            "add_watchlist_entry",
            "create_alert",
            "find_alerts",
            "list_watchlist",
            "remove_watchlist_entry",
            "run_executive_research",
            "tune_watchlist_entry",
        ),
    ),
    "workflows": (
        "draft, save, list, suggest or run workflows; Python jobs (analysis, charts, documents; with or without files)",
        ("draft_workflow", "list_workflows", "run_python_job", "run_workflow", "save_workflow", "suggest_workflow"),
    ),
    "documents": (
        "write, read back or list documents such as memos, reports and plans (artifacts)",
        ("draft_artifact", "get_artifact", "list_artifacts"),
    ),
    "messaging": (
        "Slack, Telegram and Discord messages, department messages, company broadcasts",
        (
            "send_company_broadcast",
            "send_department_message",
            "send_discord_dm",
            "send_slack_dm",
            "send_telegram_message",
        ),
    ),
    "skills": (
        "create, change or delete skills",
        ("create_skill", "delete_skill", "update_skill"),
    ),
}

DEFERRED: dict[str, str] = {tool: group for group, (_, tools) in GROUPS.items() for tool in tools}


def add_to_group(group: str, purpose: str | None, tool: str) -> None:
    """Put an extension's tool in ``group`` (orchestrator/extensions.py),
    creating the group when ``purpose`` says what it is for. Done once at
    startup, so every turn still sees one fixed set of groups."""
    if tool in DEFERRED:
        raise ValueError(f"{tool!r} is already in group {DEFERRED[tool]!r}")
    if group in GROUPS:
        current_purpose, tools = GROUPS[group]
        GROUPS[group] = (current_purpose, (*tools, tool))
    else:
        if not (purpose or "").strip():
            raise ValueError(f"new group {group!r} needs a purpose")
        GROUPS[group] = (" ".join((purpose or "").split()), (tool,))
    DEFERRED[tool] = group


def remove_from_group(group: str, tool: str) -> None:
    """Undo add_to_group (tests)."""
    if DEFERRED.get(tool) != group or group not in GROUPS:
        return
    purpose, tools = GROUPS[group]
    rest = tuple(t for t in tools if t != tool)
    if rest:
        GROUPS[group] = (purpose, rest)
    else:
        del GROUPS[group]
    del DEFERRED[tool]


def open_tools_tool(offered: Iterable[str]) -> dict[str, Any]:
    """The open_tools definition for a turn whose deferred tools are
    ``offered``: it names only the groups and tools that turn may use, so a
    tool a guard withholds is never named. The text depends only on that set,
    which is fixed for each kind of turn, so each keeps its one cached list."""
    names = set(offered)
    groups = {
        group: (purpose, [t for t in tools if t in names])
        for group, (purpose, tools) in GROUPS.items()
    }
    groups = {group: entry for group, entry in groups.items() if entry[1]}
    lines = "\n".join(f"- {group}: {purpose} ({', '.join(tools)})" for group, (purpose, tools) in groups.items())
    return {
        "name": OPEN_TOOLS,
        "description": (
            "Open a group of further tools. You have more tools than the ones listed "
            "directly; they sit in the groups below. When a request needs one, open "
            "its group: the result gives each tool's description and input, and you "
            "then call it with use_tool. Open a group only when the request needs it. "
            "Before you tell the person you can't do something, check these groups: "
            "if one covers it, open it.\n"
            + lines
        ),
        "input_schema": {
            "type": "object",
            "properties": {"group": {"type": "string", "enum": sorted(groups)}},
            "required": ["group"],
        },
    }


USE_TOOL_TOOL: dict[str, Any] = {
    "name": USE_TOOL,
    "description": (
        "Run a tool from a group opened with open_tools: its name, and its input "
        "as the object its input schema describes."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The tool's name, as open_tools listed it."},
            "input": {"type": "object", "description": "The tool's input."},
        },
        "required": ["name", "input"],
    },
}


def split(tools: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """The turn's offered tools as (the direct list, the deferred tools by
    name). The direct list carries open_tools and use_tool whenever any tool
    is deferred, so it depends only on what the turn offers, never on which
    groups get opened."""
    direct: list[dict[str, Any]] = []
    deferred: dict[str, dict[str, Any]] = {}
    for tool in tools:
        if tool["name"] in DEFERRED:
            deferred[tool["name"]] = tool
        else:
            direct.append(tool)
    if deferred:
        direct += [open_tools_tool(deferred), USE_TOOL_TOOL]
    return direct, deferred


def _offered_groups(deferred: dict[str, dict[str, Any]]) -> set[str]:
    return {DEFERRED[name] for name in deferred if name in DEFERRED}


def open_result(tool_input: Any, deferred: dict[str, dict[str, Any]]) -> str:
    """The tool result for an ``open_tools`` call: the group's tools this turn
    offers, for use_tool."""
    group = tool_input.get("group") if isinstance(tool_input, dict) else None
    if not isinstance(group, str) or group not in GROUPS:
        return json.dumps({"error": f"No such group. Groups: {', '.join(sorted(_offered_groups(deferred)))}."})
    tools = [
        {"name": t["name"], "description": t.get("description", ""), "input_schema": t.get("input_schema", {})}
        for name in GROUPS[group][1]
        if (t := deferred.get(name)) is not None
    ]
    if not tools:
        return json.dumps({"error": f"No such group. Groups: {', '.join(sorted(_offered_groups(deferred)))}."})
    return json.dumps({"group": group, "call_with": USE_TOOL, "tools": tools})


def unwrap(tool_use: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """A ``use_tool`` call as the call it stands for: (the tool's own call,
    same id, None), or (None, the error tool result) when it names no
    deferred tool or carries no input object. The rewritten call goes through
    the turn's usual guards, so a tool the turn doesn't offer is refused there
    exactly as a direct call to it is."""
    raw = tool_use.get("input")
    name = raw.get("name") if isinstance(raw, dict) else None
    if not isinstance(name, str) or name not in DEFERRED:
        return None, json.dumps({
            "error": "use_tool runs only the tools open_tools lists. Call the others directly by name."
        })
    args = raw.get("input") if isinstance(raw, dict) else None
    if isinstance(args, str):
        # Some models send the object as JSON text.
        try:
            args = json.loads(args)
        except ValueError:
            args = None
    if not isinstance(args, dict):
        return None, json.dumps({"error": f"use_tool needs {name}'s input as an object."})
    return {**tool_use, "name": name, "input": args}, None
