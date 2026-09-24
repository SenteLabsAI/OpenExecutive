"""Name the tool round that is currently in flight, for the chat progress line.

The agent loop yields one `_THINKING` sentinel per tool-use iteration, and the
UI turns that into a single muted line beside the bouncing dots. That line used
to read "Consulting specialists…" unconditionally, so an MCP call or a calendar
write looked like a specialist fan-out. This module supplies the label that
makes it honest: one `activity` SSE event per tool round, naming what actually
ran.

Sibling of `action_chips.py`, which does the same tool -> phrase mapping in the
*past* tense for side-effecting calls only. This one is present-progressive and
covers every tool, read-only included, because the point is to describe work
that has not finished yet.

Invariant: a real `consult_specialist` round gets exactly one generic label —
"Consulting specialists…". Individual specialist names are NEVER put in a
user-facing label (CLAUDE.md: the internal agent architecture is never exposed
to the user; there is one voice).
"""
from __future__ import annotations

import re
from typing import Any

# Shown when a round contains only tools with no entry in `_LABELS`. Going
# silent is not an option — a round with no label is the exact failure this
# event exists to fix, and the UI would fall back to its own placeholder.
FALLBACK_LABEL = "Working…"

SPECIALIST_LABEL = "Consulting specialists…"

# Longest underlying MCP tool name rendered into a label. MCP names are
# model-authored text on their way to the DOM, so they are capped and stripped
# here rather than trusted. React escapes the value, but a 4KB name or an
# embedded newline would still wreck a one-line indicator.
_MCP_NAME_MAX = 48
_MCP_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_.:\- ]")

# Canonical tool name -> present-progressive phrase. Every entry ends in an
# ellipsis and is short enough for one line next to the dots; the drift test in
# tests/unit/test_activity_labels.py enforces both, and also enforces that every
# registered Executive tool appears here.
_LABELS: dict[str, str] = {
    # Specialist fan-out — deliberately generic. See the module docstring.
    "consult_specialist": SPECIALIST_LABEL,

    # Skills
    "search_skills": "Looking through saved skills…",
    "load_skill": "Opening a saved skill…",
    "create_skill": "Drafting a new playbook…",
    "update_skill": "Drafting a playbook change…",
    "delete_skill": "Proposing a playbook deletion…",

    # Scheduling and outbound messages
    "schedule_followup": "Scheduling a follow-up…",
    "suggest_workflow": "Queuing a workflow suggestion…",
    "send_telegram_message": "Sending a Telegram message…",
    "send_slack_dm": "Sending a Slack DM…",
    "send_discord_dm": "Sending a Discord DM…",
    "message_person": "Sending a message…",
    "lookup_person": "Looking up people…",
    "ack_alert": "Updating a proposal…",

    # Calendar. These write, so none of them says "checking" — the label is
    # emitted before the call runs and must not promise a read.
    "create_calendar_event": "Putting time on the calendar…",
    "create_instant_meeting": "Spinning up a meeting…",
    "cancel_calendar_event": "Clearing time from the calendar…",

    # People and org chart
    "list_people": "Looking up people…",
    "upsert_person": "Updating the people roster…",
    "archive_person": "Updating the people roster…",
    "set_department_head": "Updating the org chart…",
    "ask_about_person": "Checking what I know about someone…",
    "list_open_loops": "Checking what people owe…",
    "close_open_loop": "Closing an open loop…",

    # Departments
    "list_department_goals": "Reviewing department goals…",
    "update_department_goal": "Updating a department goal…",

    # Broadcast channels
    "send_department_message": "Posting to a department channel…",
    "send_company_broadcast": "Sending a company-wide note…",

    # Watchlist
    "add_watchlist_entry": "Adding to the watchlist…",
    "list_watchlist": "Checking the watchlist…",
    "remove_watchlist_entry": "Updating the watchlist…",
    "tune_watchlist_entry": "Tuning the watchlist…",

    # Research, alerts, artifacts
    "run_executive_research": "Researching…",
    "create_alert": "Flagging something for review…",
    "draft_artifact": "Writing that up…",
    "list_artifacts": "Looking through earlier work…",
    "get_artifact": "Rereading that document…",

    # Workflows
    "draft_workflow": "Drafting a workflow…",
    "save_workflow": "Saving the workflow…",
    "list_workflows": "Checking available workflows…",
    "run_workflow": "Running a workflow…",

    # Ask OE panel form fill
    "propose_form_values": "Filling in the form…",

    # MCP gateway. `call_tool` is dynamic and handled in `_label_for`.
    "search_tools": "Looking for the right tool…",
    "load_mcp_server": "Connecting a tool server…",
}

# One `_THINKING` per iteration means exactly one label per iteration, so a
# mixed round has to collapse. It collapses by rank rather than by joining
# clauses: the indicator is a single muted line, and two joined phrases both
# overflow on a narrow panel and read like generated grammar.
#
# Lower rank wins. The order tracks what the user most wants to know is
# happening, which in practice tracks how long the call blocks the turn.
# Rank 0 is also what keeps a mixed specialist round generic.
#
# A tool in no bucket is not unreachable: when nothing in the round is ranked,
# `_pick` falls back to the first block it can name, so a read like
# `list_workflows` still gets its own label even alongside a tool the map has
# never heard of. Ties inside a bucket break on block order, which the
# Anthropic response preserves, so the choice is deterministic.
_PRIORITY: tuple[frozenset[str], ...] = (
    frozenset({"consult_specialist"}),
    frozenset({"run_executive_research"}),
    frozenset({"run_workflow", "draft_workflow", "save_workflow"}),
    frozenset({"call_tool", "load_mcp_server", "search_tools"}),
    frozenset({
        "create_calendar_event",
        "create_instant_meeting",
        "cancel_calendar_event",
    }),
    frozenset({
        "send_slack_dm",
        "send_discord_dm",
        "send_telegram_message",
        "message_person",
        "send_department_message",
        "send_company_broadcast",
        "schedule_followup",
        "suggest_workflow",
    }),
    frozenset({
        "upsert_person",
        "archive_person",
        "set_department_head",
        "update_department_goal",
        "close_open_loop",
    }),
    frozenset({
        "create_alert",
        "ack_alert",
        "draft_artifact",
        "add_watchlist_entry",
        "remove_watchlist_entry",
        "tune_watchlist_entry",
    }),
    frozenset({
        "create_skill",
        "update_skill",
        "delete_skill",
        "propose_form_values",
    }),
)


def _mcp_label(tool_input: Any) -> tuple[str, str]:
    """Label an MCP `call_tool` from the underlying tool it wraps.

    The real tool name lives in `tool_input["name"]` — the same field
    `action_chips.summarize_action` reads to surface the true tool on an MCP
    chip. A static label here would be useless, which matters because MCP is
    the case that made the old indicator wrong most visibly.
    """
    raw = tool_input.get("name") if isinstance(tool_input, dict) else None
    if not isinstance(raw, str):
        return "Using a connected tool…", "call_tool"
    name = _MCP_NAME_UNSAFE.sub("", raw).strip()[:_MCP_NAME_MAX].strip()
    if not name:
        return "Using a connected tool…", "call_tool"
    return f"Using {name}…", name


def _label_for(tool_use: dict[str, Any]) -> tuple[str, str]:
    """Return `(label, canonical_tool_name)` for one tool_use block."""
    name = tool_use.get("name")
    if not isinstance(name, str):
        return FALLBACK_LABEL, ""
    if name == "call_tool":
        return _mcp_label(tool_use.get("input"))
    return _LABELS.get(name, FALLBACK_LABEL), name


def _is_nameable(tool_use: dict[str, Any]) -> bool:
    return tool_use.get("name") in _LABELS or tool_use.get("name") == "call_tool"


def _pick(tool_uses: list[dict[str, Any]]) -> dict[str, Any]:
    """Choose the one tool_use block whose label represents the round.

    Ranked tools win first. Failing that, prefer any block we can actually
    name over block order — otherwise an unlabelled tool sitting in position
    zero would shadow a nameable one behind it and the round would render
    "Working…" despite having something to say. Only a round where nothing is
    nameable falls back to the first block.
    """
    for bucket in _PRIORITY:
        for tool_use in tool_uses:
            if tool_use.get("name") in bucket:
                return tool_use
    for tool_use in tool_uses:
        if _is_nameable(tool_use):
            return tool_use
    return tool_uses[0]


def fallback_activity(*, iteration: int | None = None) -> dict[str, Any]:
    """The unnamed-round event, for when a label could not be produced.

    The agent loop pairs every `_THINKING` sentinel with exactly one `activity`
    event. Yielding nothing here would break that pairing, and a client that
    keeps the last label it saw would then caption this round with the previous
    round's work — a stale label is worse than an honest "Working…".
    """
    payload: dict[str, Any] = {
        "type": "activity",
        "label": FALLBACK_LABEL,
        "tool": "",
    }
    if iteration is not None:
        payload["iteration"] = iteration
    return payload


def summarize_activity(
    tool_uses: list[dict[str, Any]],
    *,
    iteration: int | None = None,
) -> dict[str, Any] | None:
    """Build the `activity` SSE event for one tool round, or None.

    `tool_uses` holds the `{"id", "name", "input"}` dicts the agent loop
    collected from the model's tool_use blocks. Returns None only for an empty
    round — defensive, since the loop reaches this point only on
    `stop_reason == "tool_use"`. Anything else always produces an event; an
    unmapped tool falls back to `FALLBACK_LABEL` rather than going silent.

    The returned dict is the SSE event body; `api/routes/chat.py` forwards it
    verbatim. It is progress only and is never persisted with the assistant
    message, unlike an `action_taken` chip.
    """
    if not tool_uses:
        return None

    label, tool = _label_for(_pick(tool_uses))
    payload: dict[str, Any] = {"type": "activity", "label": label, "tool": tool}
    if iteration is not None:
        payload["iteration"] = iteration
    return payload
