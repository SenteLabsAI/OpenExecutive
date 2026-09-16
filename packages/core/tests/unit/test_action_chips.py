"""Unit tests for the action_chips classifier.

Covers:
- read-only tools never produce a chip
- side-effecting tools produce a chip on success
- handler-reported errors suppress the chip (no false-positive ✓)
- per-tool summaries shape the chip body correctly
- the SIDE_EFFECTING_TOOLS set keeps in sync with the orchestrator registries
"""
from __future__ import annotations

import json

import pytest

from openexecutive.orchestrator.action_chips import (
    SIDE_EFFECTING_TOOLS,
    summarize_action,
)

# ---------------------------------------------------------------------------
# Read-only tools — must never produce a chip.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "tool_name",
    [
        "consult_specialist",
        "lookup_person",
        "ask_about_person",
        "list_people",
        "search_skills",
        "load_skill",
        "search_tools",
        "web_search",
    ],
)
def test_read_only_tools_never_chip(tool_name: str) -> None:
    chip = summarize_action(
        tool_name=tool_name, tool_input={}, tool_result=json.dumps({"ok": True})
    )
    assert chip is None
    # And the read-only tool isn't in the SIDE_EFFECTING_TOOLS set.
    assert tool_name not in SIDE_EFFECTING_TOOLS


# ---------------------------------------------------------------------------
# Side-effecting tools — happy paths.
# ---------------------------------------------------------------------------

def test_send_slack_dm_chip() -> None:
    chip = summarize_action(
        tool_name="send_slack_dm",
        tool_input={"user_id": "U123", "text": "hi"},
        tool_result=json.dumps({"status": "sent", "user_id": "U123"}),
    )
    assert chip is not None
    assert chip["type"] == "action_taken"
    assert chip["tool"] == "send_slack_dm"
    assert "Slack" in chip["summary"]
    assert chip["target"] == "U123"


def test_send_discord_dm_chip() -> None:
    chip = summarize_action(
        tool_name="send_discord_dm",
        tool_input={"discord_user_id": "1234", "text": "hi"},
        tool_result=json.dumps({"status": "sent", "discord_user_id": "1234"}),
    )
    assert chip is not None
    assert "Discord" in chip["summary"]


def test_send_telegram_message_chip() -> None:
    chip = summarize_action(
        tool_name="send_telegram_message",
        tool_input={"chat_id": 99, "text": "hi"},
        tool_result=json.dumps({"status": "sent", "chat_id": 99}),
    )
    assert chip is not None
    assert "Telegram" in chip["summary"]


def test_schedule_followup_chip() -> None:
    chip = summarize_action(
        tool_name="schedule_followup",
        tool_input={
            "run_at": "2026-06-15T15:00:00+00:00",
            "channel": "slack_dm",
            "channel_ref": "U123",
            "intent": "check on Q3",
        },
        tool_result=json.dumps({"status": "scheduled", "id": 42}),
    )
    assert chip is not None
    assert "follow-up" in chip["summary"].lower()
    assert "slack_dm" in chip["summary"]


def test_suggest_workflow_chip() -> None:
    chip = summarize_action(
        tool_name="suggest_workflow",
        tool_input={
            "workflow_name": "board_prep",
            "run_at": "2026-06-15T15:00:00+00:00",
            "channel": "email",
            "channel_ref": "alice@example.com",
            "reason": "board next week",
        },
        tool_result=json.dumps({"status": "scheduled", "id": 42, "workflow_name": "board_prep"}),
    )
    assert chip is not None
    assert "board_prep" in chip["summary"]
    assert chip["target"] == "board_prep"


def test_upsert_person_chip_with_link() -> None:
    chip = summarize_action(
        tool_name="upsert_person",
        tool_input={"full_name": "Alex Stone", "role": "Ops"},
        tool_result=json.dumps({"id": 7, "full_name": "Alex Stone"}),
    )
    assert chip is not None
    assert chip["target"] == "Alex Stone"
    assert chip["link"] == "/people/7"


def test_set_department_head_chip_with_link() -> None:
    chip = summarize_action(
        tool_name="set_department_head",
        tool_input={"department_slug": "marketing", "person_id": 7},
        tool_result=json.dumps({"ok": True}),
    )
    assert chip is not None
    assert chip["link"] == "/departments/marketing"


def test_chip_suppressed_on_not_found() -> None:
    # The generic `status: not_found` suppression: a write that matched no row
    # changed nothing, so it must not paint a green ✓. Here archive_person
    # against an id that doesn't exist (people_tools returns that shape).
    chip = summarize_action(
        tool_name="archive_person",
        tool_input={"person_id": 9999},
        tool_result=json.dumps({"status": "not_found", "person_id": 9999}),
    )
    assert chip is None


def test_run_workflow_chip_with_link() -> None:
    chip = summarize_action(
        tool_name="run_workflow",
        tool_input={"workflow": "board_prep", "inputs": {"quarter_label": "Q2"}},
        tool_result=json.dumps({"ok": True, "run_id": "run123", "artifact": "# Deck"}),
    )
    assert chip is not None
    assert chip["summary"] == "Ran board prep workflow"
    assert chip["link"] == "/jobs/runs/run123"


def test_run_workflow_chip_awaiting_human() -> None:
    chip = summarize_action(
        tool_name="run_workflow",
        tool_input={"workflow": "comp_refresh", "inputs": {}},
        tool_result=json.dumps({"status": "awaiting_human", "run_id": "r9", "person_id": 3}),
    )
    assert chip is not None
    assert chip["summary"] == "Started comp refresh — awaiting sign-off"


def test_run_workflow_chip_suppressed_on_error() -> None:
    chip = summarize_action(
        tool_name="run_workflow",
        tool_input={"workflow": "board_prep", "inputs": {}},
        tool_result=json.dumps({"error": "workflow error: boom"}),
    )
    assert chip is None


def test_create_alert_chip() -> None:
    chip = summarize_action(
        tool_name="create_alert",
        tool_input={"headline": "Burn trending high", "severity": "high"},
        tool_result=json.dumps({"alert_id": 9}),
    )
    assert chip is not None
    assert "Burn trending high" in chip["summary"]


def test_send_department_message_chip() -> None:
    chip = summarize_action(
        tool_name="send_department_message",
        tool_input={"department_slug": "marketing", "integration": "slack", "text": "FYI"},
        tool_result=json.dumps({"status": "sent", "channel": "C123"}),
    )
    assert chip is not None
    assert "marketing" in chip["summary"]
    assert "Slack" in chip["summary"]
    assert chip["target"] == "marketing"
    assert chip["link"] == "/departments/marketing"


def test_send_department_message_chip_suppressed_on_no_channel() -> None:
    chip = summarize_action(
        tool_name="send_department_message",
        tool_input={"department_slug": "marketing", "integration": "slack", "text": "FYI"},
        tool_result=json.dumps({"error": "department 'marketing' has no slack channel configured"}),
    )
    assert chip is None


def test_send_company_broadcast_chip() -> None:
    chip = summarize_action(
        tool_name="send_company_broadcast",
        tool_input={"integration": "slack", "text": "Q3 shipped"},
        tool_result=json.dumps({"status": "sent", "channel": "C99GENERAL"}),
    )
    assert chip is not None
    assert "Broadcast" in chip["summary"]
    assert "Slack" in chip["summary"]
    assert chip["target"] == "slack"


def test_call_tool_chip_uses_underlying_name() -> None:
    chip = summarize_action(
        tool_name="call_tool",
        tool_input={"name": "google_workspace__send_gmail_message", "arguments": {}},
        tool_result=json.dumps({"ok": True}),
    )
    assert chip is not None
    # `tool` field reflects the actual MCP tool, not "call_tool" — UI can map.
    assert chip["tool"] == "google_workspace__send_gmail_message"
    assert "google_workspace__send_gmail_message" in chip["summary"]


# ---------------------------------------------------------------------------
# Error paths — no chip when the tool failed.
# ---------------------------------------------------------------------------

def test_failed_send_suppresses_chip() -> None:
    chip = summarize_action(
        tool_name="send_slack_dm",
        tool_input={"user_id": "U123", "text": "hi"},
        tool_result=json.dumps({"error": "slack is not configured"}),
    )
    # Don't paint a green ✓ over a red outcome.
    assert chip is None


def test_failed_schedule_suppresses_chip() -> None:
    chip = summarize_action(
        tool_name="schedule_followup",
        tool_input={
            "run_at": "2020-01-01T00:00:00+00:00",
            "channel": "slack_dm",
            "channel_ref": "U123",
            "intent": "past",
        },
        tool_result=json.dumps({"error": "run_at must be in the future"}),
    )
    assert chip is None


# ---------------------------------------------------------------------------
# Non-JSON tool result — handler returned raw text; chip still surfaces.
# ---------------------------------------------------------------------------

def test_non_json_result_still_surfaces() -> None:
    chip = summarize_action(
        tool_name="create_alert",
        tool_input={"headline": "Hello"},
        tool_result="alert created successfully",  # raw text, not JSON
    )
    assert chip is not None
    # We can't see an "error" key in unparseable text, so we err on the
    # side of surfacing the action. The user's prose will catch any nuance.


# ---------------------------------------------------------------------------
# Registry consistency — guard against drift.
# ---------------------------------------------------------------------------

# Allowlist of tools the orchestrator registers but which produce no
# user-visible side effect (pure reads). Maintained alongside
# SIDE_EFFECTING_TOOLS — every handler not in this list MUST be a member
# of SIDE_EFFECTING_TOOLS, or it's a silent omission (the user fires the
# tool and the chip never renders).
_KNOWN_READ_ONLY_TOOLS: frozenset[str] = frozenset({
    # schedule_tools
    "lookup_person",
    # people_tools
    "list_people",
    "ask_about_person",
    # department_tools
    "list_department_goals",
    # skills_tools
    "search_skills",
    "load_skill",
    # mcp_gateway
    "search_tools",
    # workflow_run_tools — catalog read (run_workflow is in SIDE_EFFECTING_TOOLS)
    "list_workflows",
})


def _all_registered_tool_names() -> set[str]:
    """Union of every tool name OE can call through the agentic loop.

    Kept here as a helper so both drift tests use the exact same source
    of truth — drift between them would defeat the guard.
    """
    from openexecutive.orchestrator.artifact_tools import DRAFT_ARTIFACT_TOOL_HANDLERS
    from openexecutive.orchestrator.broadcast_tools import BROADCAST_TOOL_HANDLERS
    from openexecutive.orchestrator.department_tools import DEPARTMENT_TOOL_HANDLERS
    from openexecutive.orchestrator.mcp_gateway import MCP_TOOL_NAMES
    from openexecutive.orchestrator.people_tools import PEOPLE_TOOL_HANDLERS
    from openexecutive.orchestrator.schedule_tools import SCHEDULE_TOOL_HANDLERS
    from openexecutive.orchestrator.skills_tools import SKILL_TOOL_HANDLERS
    from openexecutive.orchestrator.workflow_run_tools import WORKFLOW_RUN_TOOL_HANDLERS

    return (
        set(SCHEDULE_TOOL_HANDLERS)
        | set(PEOPLE_TOOL_HANDLERS)
        | set(DEPARTMENT_TOOL_HANDLERS)
        | set(SKILL_TOOL_HANDLERS)
        | set(BROADCAST_TOOL_HANDLERS)
        | set(DRAFT_ARTIFACT_TOOL_HANDLERS)
        | set(MCP_TOOL_NAMES)
        | set(WORKFLOW_RUN_TOOL_HANDLERS)
        # `create_alert` is in chat module rather than a HANDLERS dict.
        | {"create_alert"}
    )


def test_side_effecting_tools_set_matches_registries() -> None:
    """Every side-effecting tool we instrument must be a real registered tool.

    Catches typos and drift between the chip module and the actual tool
    handler registries.
    """
    unknown = SIDE_EFFECTING_TOOLS - _all_registered_tool_names()
    assert not unknown, f"SIDE_EFFECTING_TOOLS references unknown tools: {unknown}"


def test_no_silent_omission_from_side_effecting_tools() -> None:
    """A new handler added to a registry MUST be classified — either as
    side-effecting (chip emitted) or in `_KNOWN_READ_ONLY_TOOLS` (chip
    deliberately skipped).

    This catches the silent-omission failure mode that the inbound check
    above misses: someone adds a new mutating tool and the user never sees
    a chip for it.
    """
    classified = SIDE_EFFECTING_TOOLS | _KNOWN_READ_ONLY_TOOLS
    unclassified = _all_registered_tool_names() - classified
    assert not unclassified, (
        f"Tools registered but unclassified — add to SIDE_EFFECTING_TOOLS "
        f"(to emit a chip) or _KNOWN_READ_ONLY_TOOLS (to deliberately skip): "
        f"{sorted(unclassified)}"
    )
