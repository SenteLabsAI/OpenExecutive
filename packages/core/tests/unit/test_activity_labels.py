"""Unit tests for `orchestrator.activity_labels.summarize_activity`.

Covers:
  * one label per round, chosen by priority rank, never by concatenation
  * the one-voice invariant — a specialist round never names a specialist
  * MCP `call_tool` resolving to the underlying tool name, and its hardening
  * the "Working…" fallback for tools with no entry in the map
  * drift: every tool the Executive can actually call has a label
"""
from __future__ import annotations

import pytest

from openexecutive.orchestrator import activity_labels as al
from openexecutive.orchestrator.activity_labels import (
    FALLBACK_LABEL,
    SPECIALIST_LABEL,
    summarize_activity,
)


def _tu(tool_name: str, **input_kwargs: object) -> dict[str, object]:
    """One tool_use block shaped like the agent loop builds them.

    The first argument is the *tool* name; keyword arguments become the tool
    input — including MCP's own `name` field, which is why this parameter is
    not called `name`.
    """
    return {
        "id": f"toolu_{tool_name}",
        "name": tool_name,
        "input": dict(input_kwargs),
    }


# --------------------------------------------------------------------------
# Specialist rounds stay generic
# --------------------------------------------------------------------------

def test_specialist_round_uses_the_generic_label() -> None:
    evt = summarize_activity([_tu("consult_specialist", specialist="finance")])
    assert evt is not None
    assert evt["label"] == SPECIALIST_LABEL


def test_specialist_label_never_names_the_specialist() -> None:
    """Guards the CLAUDE.md one-voice invariant against a 'helpful' future edit."""
    evt = summarize_activity([
        _tu("consult_specialist", specialist="finance", query="runway?"),
        _tu("consult_specialist", specialist="legal", query="risk?"),
    ])
    assert evt is not None
    blob = " ".join(str(v) for v in evt.values()).lower()
    for leaked in ("finance", "legal", "runway", "risk"):
        assert leaked not in blob


def test_specialist_wins_a_mixed_round_regardless_of_block_order() -> None:
    """Specialist is rank 0, so it wins even when it is not the first block."""
    evt = summarize_activity([
        _tu("create_calendar_event", title="sync"),
        _tu("consult_specialist", specialist="finance"),
    ])
    assert evt is not None
    assert evt["label"] == SPECIALIST_LABEL


# --------------------------------------------------------------------------
# Non-specialist rounds — the bug this module exists to fix
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("tool_name", "expected"),
    [
        ("create_calendar_event", "Putting time on the calendar…"),
        ("list_people", "Looking up people…"),
        ("send_slack_dm", "Sending a Slack DM…"),
        ("run_workflow", "Running a workflow…"),
        ("run_executive_research", "Researching…"),
        ("create_alert", "Flagging something for review…"),
        ("search_tools", "Looking for the right tool…"),
        ("load_mcp_server", "Connecting a tool server…"),
        ("propose_form_values", "Filling in the form…"),
        ("list_workflows", "Checking available workflows…"),
    ],
)
def test_single_tool_round_is_named(tool_name: str, expected: str) -> None:
    evt = summarize_activity([_tu(tool_name)])
    assert evt is not None
    assert evt["label"] == expected
    assert evt["tool"] == tool_name
    assert SPECIALIST_LABEL not in evt["label"]


def test_unranked_tool_alone_still_gets_its_own_label() -> None:
    """A tool in no priority bucket falls back to block order, not to 'Working…'."""
    evt = summarize_activity([_tu("list_watchlist")])
    assert evt is not None
    assert evt["label"] == "Checking the watchlist…"


def test_nameable_tool_beats_an_unknown_one_in_block_position_zero() -> None:
    """An unmapped tool must not shadow a nameable one behind it."""
    evt = summarize_activity([_tu("some_future_tool"), _tu("list_workflows")])
    assert evt is not None
    assert evt["label"] == "Checking available workflows…"
    assert evt["tool"] == "list_workflows"


def test_round_with_nothing_nameable_falls_back() -> None:
    evt = summarize_activity([_tu("aaa_unknown"), _tu("bbb_unknown")])
    assert evt is not None
    assert evt["label"] == FALLBACK_LABEL


def test_fallback_activity_is_a_well_formed_event() -> None:
    """The loop pairs every sentinel with one of these when labelling fails."""
    evt = al.fallback_activity(iteration=2)
    assert evt == {
        "type": "activity",
        "label": FALLBACK_LABEL,
        "tool": "",
        "iteration": 2,
    }
    assert "iteration" not in al.fallback_activity()


def test_priority_rank_decides_a_mixed_non_specialist_round() -> None:
    evt = summarize_activity([_tu("upsert_person"), _tu("run_workflow")])
    assert evt is not None
    assert evt["label"] == "Running a workflow…"


def test_repeated_same_tool_yields_one_label() -> None:
    evt = summarize_activity([_tu("send_slack_dm"), _tu("send_slack_dm")])
    assert evt is not None
    assert evt["label"] == "Sending a Slack DM…"


# --------------------------------------------------------------------------
# MCP
# --------------------------------------------------------------------------

def test_mcp_call_tool_uses_the_underlying_tool_name() -> None:
    evt = summarize_activity(
        [_tu("call_tool", name="google_workspace__send_gmail_message", arguments={})]
    )
    assert evt is not None
    assert evt["label"] == "Using google_workspace__send_gmail_message…"
    assert evt["tool"] == "google_workspace__send_gmail_message"


@pytest.mark.parametrize("bad", [None, 42, "", "   ", "<<<>>>"])
def test_mcp_call_tool_falls_back_when_the_name_is_unusable(bad: object) -> None:
    tu = {"id": "t", "name": "call_tool", "input": {"name": bad}}
    evt = summarize_activity([tu])
    assert evt is not None
    assert evt["label"] == "Using a connected tool…"
    assert evt["tool"] == "call_tool"


def test_mcp_call_tool_handles_a_missing_input_dict() -> None:
    evt = summarize_activity([{"id": "t", "name": "call_tool", "input": None}])
    assert evt is not None
    assert evt["label"] == "Using a connected tool…"


def test_mcp_tool_name_is_sanitized_and_truncated() -> None:
    """Model-authored text on its way to the DOM: strip and cap it here."""
    evt = summarize_activity(
        [_tu("call_tool", name="<b>evil</b>\nnewline " + "x" * 200)]
    )
    assert evt is not None
    label = evt["label"]
    assert "<" not in label and ">" not in label and "\n" not in label
    assert len(label) <= 60


# --------------------------------------------------------------------------
# Fallback, shape, and defensive inputs
# --------------------------------------------------------------------------

def test_unmapped_tool_falls_back_to_working() -> None:
    evt = summarize_activity([_tu("some_future_tool")])
    assert evt is not None
    assert evt["label"] == FALLBACK_LABEL


def test_non_string_tool_name_falls_back() -> None:
    evt = summarize_activity([{"id": "t", "name": None, "input": {}}])
    assert evt is not None
    assert evt["label"] == FALLBACK_LABEL


def test_empty_round_returns_none() -> None:
    assert summarize_activity([]) is None


def test_iteration_is_echoed_when_given_and_omitted_when_not() -> None:
    with_it = summarize_activity([_tu("list_people")], iteration=3)
    without = summarize_activity([_tu("list_people")])
    assert with_it is not None and without is not None
    assert with_it["iteration"] == 3
    assert "iteration" not in without
    assert with_it["type"] == "activity"


# --------------------------------------------------------------------------
# Drift guards
# --------------------------------------------------------------------------

def test_every_label_is_present_progressive_and_fits_one_line() -> None:
    for tool, label in al._LABELS.items():
        assert label.endswith("…"), tool
        assert label == label.strip(), tool
        assert len(label) <= 60, (tool, len(label))
    assert FALLBACK_LABEL.endswith("…")


def test_every_priority_entry_has_a_label() -> None:
    """A tool can't be ranked without being nameable."""
    ranked = {name for bucket in al._PRIORITY for name in bucket}
    assert ranked - set(al._LABELS) == {"call_tool"}


def test_every_registered_executive_tool_has_a_label() -> None:
    """The real drift guard: adding a tool without a label is a silent regression.

    `_ALL_SKILL_HANDLERS` is the merged source of truth for every handler-backed
    tool the Executive can call; MCP and the specialist tool sit outside it.
    """
    from openexecutive.orchestrator.executive import _ALL_SKILL_HANDLERS
    from openexecutive.orchestrator.mcp_gateway import MCP_TOOL_NAMES

    registered = set(_ALL_SKILL_HANDLERS) | set(MCP_TOOL_NAMES) | {"consult_specialist"}
    # `call_tool` is labelled dynamically from the tool it wraps.
    missing = registered - set(al._LABELS) - {"call_tool"}
    assert not missing, f"tools with no activity label: {sorted(missing)}"
