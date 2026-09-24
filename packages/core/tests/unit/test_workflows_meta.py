"""Smoke tests for workflow metadata and sample inputs.

Covers structural invariants that hold without making any Anthropic API
calls: every workflow has a section, every sample validates against its
own input model, and steps lists are non-empty and unique.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-used")

from openexecutive.workflows import (  # noqa: E402
    WORKFLOW_REGISTRY,
    WorkflowSection,
    list_workflows,
)


@pytest.mark.parametrize("workflow", list_workflows(), ids=lambda w: w.name)
def test_meta_has_section_and_steps(workflow) -> None:
    meta = workflow.meta()
    # Section must be one of the known UI groupings.
    assert isinstance(meta.section, WorkflowSection)
    # Steps must be present and have unique IDs.
    step_ids = [s.id for s in meta.steps]
    assert step_ids, f"{workflow.name} has no steps"
    assert len(step_ids) == len(set(step_ids)), (
        f"{workflow.name} has duplicate step ids: {step_ids}"
    )
    # Estimated minutes must be a positive int.
    assert meta.estimated_minutes > 0


@pytest.mark.parametrize("workflow", list_workflows(), ids=lambda w: w.name)
def test_sample_inputs_validate(workflow) -> None:
    sample = workflow.sample_inputs()
    if sample is None:
        pytest.skip(f"{workflow.name} declares no sample_inputs")
    # Sample must validate against the workflow's own input model.
    model = workflow.input_model().model_validate(sample)
    # Round-trip: model dump should be a strict subset of (or equal to) sample.
    dumped = model.model_dump()
    for key, value in sample.items():
        assert dumped.get(key) == value, (
            f"{workflow.name}: sample field {key!r} did not round-trip"
        )


def test_section_enum_values_are_strings() -> None:
    # Pydantic serialization relies on `str, Enum` mixin → values are strings.
    for s in WorkflowSection:
        assert isinstance(s.value, str)


def test_every_workflow_has_section() -> None:
    # Every registered workflow declares a UI section.
    for workflow in WORKFLOW_REGISTRY.values():
        assert isinstance(workflow.section, WorkflowSection), (
            f"{workflow.name} is missing a section"
        )


def test_meta_serializes_section_as_string() -> None:
    # The HTTP layer dumps meta via model_dump(); the section must come
    # through as a plain string for the UI.
    meta = WORKFLOW_REGISTRY["board_prep"].meta().model_dump()
    assert meta["section"] == "Board"


_BACKGROUND = {"morning_brief", "end_of_day_digest", "executive_reflection", "executive_research"}


def test_background_flag_marks_only_the_system_workflows() -> None:
    """The /jobs catalog files these under "System" instead of the default view."""
    flagged = {name for name, wf in WORKFLOW_REGISTRY.items() if wf.meta().background}
    assert flagged == _BACKGROUND
