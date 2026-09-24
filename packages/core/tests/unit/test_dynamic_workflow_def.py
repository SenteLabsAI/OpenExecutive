"""Validation tests for user-created (dynamic) workflow definitions."""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-used")

from openexecutive.workflows.dynamic_models import (  # noqa: E402
    DynamicWorkflowDef,
    validate_definition,
)


def _specialist(id_: str, goal: str = "Do {topic}.", specialist: str = "cso") -> dict:
    return {"kind": "specialist", "id": id_, "title": id_.title(), "specialist": specialist, "goal": goal}


def _synthesis(id_: str = "assemble") -> dict:
    return {"kind": "synthesis", "id": id_, "title": "Assemble"}


def _valid_def(**overrides) -> DynamicWorkflowDef:
    base = {
        "name": "weekly_watch",
        "title": "Weekly Watch",
        "input_fields": [{"name": "topic", "label": "Topic", "required": True}],
        "steps": [_specialist("research"), _synthesis()],
    }
    base.update(overrides)
    return DynamicWorkflowDef.model_validate(base)


def test_valid_definition_passes() -> None:
    assert validate_definition(_valid_def()) == []


def test_rejects_bad_name() -> None:
    errs = validate_definition(_valid_def(name="Bad Name!"))
    assert any("snake_case" in e for e in errs)


def test_rejects_builtin_name_collision() -> None:
    errs = validate_definition(_valid_def(name="board_prep"))
    assert any("collides with a built-in" in e for e in errs)


def test_rejects_duplicate_field_names() -> None:
    errs = validate_definition(
        _valid_def(
            input_fields=[
                {"name": "topic", "label": "A"},
                {"name": "topic", "label": "B"},
            ]
        )
    )
    assert any("duplicate input field" in e for e in errs)


def test_rejects_unknown_specialist() -> None:
    errs = validate_definition(
        _valid_def(steps=[_specialist("research", specialist="wizard"), _synthesis()])
    )
    assert any("unknown specialist" in e for e in errs)


def test_rejects_missing_synthesis() -> None:
    errs = validate_definition(_valid_def(steps=[_specialist("research")]))
    assert any("exactly one synthesis step" in e for e in errs)


def test_rejects_non_terminal_synthesis() -> None:
    errs = validate_definition(
        _valid_def(steps=[_synthesis("assemble"), _specialist("research")])
    )
    assert any("must be the last step" in e for e in errs)


def test_rejects_unknown_placeholder() -> None:
    errs = validate_definition(
        _valid_def(
            input_fields=[{"name": "topic", "label": "Topic"}],
            steps=[_specialist("research", goal="Analyze {not_a_field}."), _synthesis()],
        )
    )
    assert any("unknown input field" in e for e in errs)


def test_known_placeholder_ok() -> None:
    assert validate_definition(
        _valid_def(steps=[_specialist("research", goal="Analyze {topic}."), _synthesis()])
    ) == []


def test_rejects_attribute_access_placeholder() -> None:
    # {topic.__class__} would otherwise leak object internals at format time.
    errs = validate_definition(
        _valid_def(steps=[_specialist("research", goal="Leak {topic.__class__}"), _synthesis()])
    )
    assert any("attribute/index access" in e for e in errs)


def test_rejects_index_access_placeholder() -> None:
    errs = validate_definition(
        _valid_def(steps=[_specialist("research", goal="Leak {topic[0]}"), _synthesis()])
    )
    assert any("attribute/index access" in e for e in errs)


def test_rejects_positional_placeholder() -> None:
    errs = validate_definition(
        _valid_def(steps=[_specialist("research", goal="Leak {0}"), _synthesis()])
    )
    assert any("positional/empty placeholder" in e for e in errs)


def test_rejects_cadence_with_approval_gate() -> None:
    # A scheduled run can't pause for a human, so gates are forbidden with cadence.
    errs = validate_definition(
        _valid_def(
            input_fields=[],
            cadence="daily@09:00",
            cadence_person_id=99999999,
            steps=[
                _specialist("research"),
                {"kind": "approval_gate", "id": "gate", "title": "Approve",
                 "person_id": 99999999, "question": "ok?"},
                _synthesis(),
            ],
        )
    )
    assert any("must not contain approval-gate steps" in e for e in errs)


def test_requires_at_least_one_specialist_or_action_step() -> None:
    errs = validate_definition(_valid_def(steps=[_synthesis()]))
    assert any("at least one specialist or action step" in e for e in errs)


def test_rejects_bad_cadence_spec() -> None:
    errs = validate_definition(
        _valid_def(
            input_fields=[],
            cadence="hourly@nope",
            cadence_person_id=1,
        )
    )
    assert any("not a valid spec" in e for e in errs)


def test_cadence_with_required_fields_rejected() -> None:
    # A cadence run supplies no inputs, so required fields can't be satisfied.
    errs = validate_definition(
        _valid_def(cadence="daily@09:00", cadence_person_id=99999999)
    )
    assert any("must not have required input fields" in e for e in errs)


def test_cadence_without_person_rejected() -> None:
    errs = validate_definition(_valid_def(input_fields=[], cadence="daily@09:00"))
    assert any("requires cadence_person_id" in e for e in errs)


def test_cadence_person_set_without_cadence_rejected() -> None:
    errs = validate_definition(_valid_def(input_fields=[], cadence_person_id=1))
    assert any("cadence_person_id is set but cadence is not" in e for e in errs)


@pytest.mark.parametrize("minutes", [0, 1000])
def test_estimated_minutes_bounds(minutes: int) -> None:
    errs = validate_definition(_valid_def(estimated_minutes=minutes))
    assert any("estimated_minutes" in e for e in errs)


# --- action steps ------------------------------------------------------------


def _action(id_: str = "file_bills", tools: list[str] | None = None, **extra) -> dict:
    return {
        "kind": "action",
        "id": id_,
        "title": "File bills",
        "goal": "Add each {topic} bill to the tracker sheet.",
        "tools": ["google_workspace__append_table_rows", "oe__read_file"] if tools is None else tools,
        **extra,
    }


def test_action_step_alone_satisfies_the_work_step_rule() -> None:
    assert validate_definition(_valid_def(steps=[_action(), _synthesis()])) == []


def test_action_step_needs_goal_and_tools() -> None:
    errs = validate_definition(
        _valid_def(steps=[_action(tools=[], goal=""), _synthesis()])
    )
    assert any("must have a goal" in e for e in errs)
    assert any("at least one tool" in e for e in errs)


@pytest.mark.parametrize("meta", ["search_tools", "call_tool", "load_mcp_server"])
def test_action_step_may_not_use_gateway_meta_tools(meta: str) -> None:
    """call_tool would reach every downstream tool — the allowlist would be moot."""
    errs = validate_definition(_valid_def(steps=[_action(tools=[meta]), _synthesis()]))
    assert any(f"may not use {meta!r}" in e for e in errs)


@pytest.mark.parametrize("bad", ["has space", "x" * 65, "semi;colon", ""])
def test_action_step_rejects_malformed_tool_names(bad: str) -> None:
    errs = validate_definition(_valid_def(steps=[_action(tools=[bad]), _synthesis()]))
    assert any("invalid tool name" in e for e in errs)


def test_action_step_rejects_duplicate_and_too_many_tools() -> None:
    dupes = validate_definition(
        _valid_def(steps=[_action(tools=["oe__read_file", "oe__read_file"]), _synthesis()])
    )
    assert any("more than once" in e for e in dupes)
    many = validate_definition(
        _valid_def(steps=[_action(tools=[f"srv__tool_{i}" for i in range(17)]), _synthesis()])
    )
    assert any("too many tools" in e for e in many)


@pytest.mark.parametrize("calls", [0, 51])
def test_action_step_bounds_max_tool_calls(calls: int) -> None:
    errs = validate_definition(
        _valid_def(steps=[_action(max_tool_calls=calls), _synthesis()])
    )
    assert any("max_tool_calls must be 1-50" in e for e in errs)


def test_action_step_placeholders_must_be_declared() -> None:
    errs = validate_definition(
        _valid_def(steps=[_action(goal="Use {undeclared}."), _synthesis()])
    )
    assert any("undeclared" in e for e in errs)


def test_cadence_workflow_may_have_action_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Action steps need no human, so a scheduled run can do them."""
    monkeypatch.setattr(
        "openexecutive.workflows.dynamic_models._person_exists", lambda pid: True
    )
    defn = _valid_def(
        input_fields=[{"name": "topic", "label": "Topic", "required": False}],
        steps=[_action(), _synthesis()],
        cadence="daily@09:00",
        cadence_person_id=1,
    )
    assert validate_definition(defn) == []
