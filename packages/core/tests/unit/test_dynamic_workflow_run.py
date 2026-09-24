"""Tests for the generic DynamicWorkflow engine: input-model synthesis + run()."""
from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-used")

from openexecutive.workflows import dynamic as dyn  # noqa: E402
from openexecutive.workflows.dynamic import DynamicWorkflow  # noqa: E402
from openexecutive.workflows.dynamic_models import DynamicWorkflowDef  # noqa: E402
from openexecutive.workflows.wait_for_human import WaitForHumanEvent  # noqa: E402


@pytest.fixture(autouse=True)
def _stub_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """No real profile/RAG/specialist calls in unit tests."""
    fake_profile = MagicMock()
    fake_profile.name = "Acme"
    fake_profile.is_empty.return_value = True
    monkeypatch.setattr(dyn, "load_or_create_profile", lambda: fake_profile)
    monkeypatch.setattr(dyn, "retrieve", lambda **kw: "RAG[" + kw.get("query", "") + "]")


def _def(**overrides: Any) -> DynamicWorkflowDef:
    base = {
        "name": "weekly_watch",
        "title": "Weekly Watch",
        "input_fields": [
            {"name": "topic", "label": "Topic", "required": True},
            {"name": "extra", "label": "Extra", "required": False},
        ],
        "steps": [
            {"kind": "specialist", "id": "research", "title": "Research",
             "specialist": "cso", "goal": "Analyze {topic}.", "rag_query": "about {topic}"},
            {"kind": "synthesis", "id": "assemble", "title": "Assemble"},
        ],
    }
    base.update(overrides)
    return DynamicWorkflowDef.model_validate(base)


def test_input_model_synthesis() -> None:
    wf = DynamicWorkflow(_def())
    model = wf.input_model()
    fields = model.model_fields
    assert set(fields) == {"topic", "extra"}
    assert fields["topic"].is_required()
    assert not fields["extra"].is_required()
    # Cached on second call.
    assert wf.input_model() is model


def test_meta_marks_custom() -> None:
    assert DynamicWorkflow(_def()).meta().is_custom is True


async def _collect(wf: DynamicWorkflow, inputs: Any) -> list[Any]:
    return [e async for e in wf.run(inputs=inputs, store=MagicMock())]


@pytest.mark.asyncio
async def test_run_specialist_then_synthesis(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    async def fake_route(**kwargs: Any) -> str:
        calls.append(kwargs)
        return f"## Output\nDraft for {kwargs['query']}"

    monkeypatch.setattr(dyn, "route_to_specialist", fake_route)

    wf = DynamicWorkflow(_def())
    inputs = wf.input_model()(topic="pricing", extra="")
    events = await _collect(wf, inputs)

    types = [e.type for e in events]
    assert types[0] == "step_start" and events[0].step_id == "context"
    # placeholder rendered into the goal, RAG fetched
    assert calls[0]["query"] == "Analyze pricing."
    assert "RAG[about pricing]" in calls[0]["retrieved_knowledge"]
    # final artifact present and contains the step output
    artifact_events = [e for e in events if e.type == "artifact"]
    assert len(artifact_events) == 1
    assert "Weekly Watch" in artifact_events[0].content
    assert "Draft for Analyze pricing." in artifact_events[0].content


@pytest.mark.asyncio
async def test_run_pauses_at_approval_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_route(**kwargs: Any) -> str:
        return "section"

    monkeypatch.setattr(dyn, "route_to_specialist", fake_route)

    d = _def(
        input_fields=[{"name": "topic", "label": "Topic", "required": True}],
        steps=[
            {"kind": "specialist", "id": "research", "title": "Research",
             "specialist": "cso", "goal": "Analyze {topic}."},
            {"kind": "approval_gate", "id": "gate", "title": "Approve",
             "person_id": 7, "question": "OK to proceed on {topic}?"},
            {"kind": "synthesis", "id": "assemble", "title": "Assemble"},
        ],
    )
    wf = DynamicWorkflow(d)
    inputs = wf.input_model()(topic="layoffs")
    events = await _collect(wf, inputs)

    gates = [e for e in events if isinstance(e, WaitForHumanEvent)]
    assert len(gates) == 1
    assert gates[0].person_id == 7
    assert gates[0].question == "OK to proceed on layoffs?"
    # Run stops at the gate — no artifact emitted.
    assert not any(getattr(e, "type", None) == "artifact" for e in events)

    # The gate carries everything needed to pick the run back up: which step
    # it stopped at, where to continue, and what the earlier steps produced.
    state = gates[0].resume_state
    assert state is not None
    assert state.workflow_name == "weekly_watch"
    assert state.gate_step_id == "gate"
    assert state.gate_step_index == 1
    assert state.outputs == {"research": ("Research", "section")}
    # Pins the whole step list, not just the gate: a definition edited during
    # the pause must not be able to swap the steps the approval lands on.
    assert state.steps_fingerprint


@pytest.mark.asyncio
async def test_run_synthesis_with_instructions_calls_specialist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_route(**kwargs: Any) -> str:
        calls.append(kwargs)
        return "polished doc"

    monkeypatch.setattr(dyn, "route_to_specialist", fake_route)
    d = _def(
        steps=[
            {"kind": "specialist", "id": "research", "title": "Research",
             "specialist": "cso", "goal": "Analyze {topic}."},
            {"kind": "synthesis", "id": "assemble", "title": "Assemble",
             "instructions": "Tighten it into one page", "specialist": "cfo"},
        ],
    )
    wf = DynamicWorkflow(d)
    inputs = wf.input_model()(topic="x", extra="")
    events = await _collect(wf, inputs)
    artifact = next(e for e in events if e.type == "artifact").content
    assert artifact.strip() == "polished doc"
    # research (cso) + synthesis (cfo) both consulted, in order.
    assert [c["specialist_name"] for c in calls] == ["cso", "cfo"]
    # The synthesis call must actually carry the instructions AND the prior
    # step's output — otherwise "synthesis with instructions" does nothing.
    synth_query = calls[1]["query"]
    assert "Tighten it into one page" in synth_query
    assert "polished doc" in synth_query  # the research step's output is fed in


@pytest.mark.asyncio
async def test_run_reports_placeholder_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_route(**kwargs: Any) -> str:  # pragma: no cover - shouldn't run
        return "x"

    monkeypatch.setattr(dyn, "route_to_specialist", fake_route)
    # Build a def that passes model validation but whose goal references a
    # field missing at runtime (simulate by constructing inputs without it).
    d = _def(
        input_fields=[{"name": "topic", "label": "Topic", "required": False}],
        steps=[
            {"kind": "specialist", "id": "research", "title": "Research",
             "specialist": "cso", "goal": "Analyze {topic}."},
            {"kind": "synthesis", "id": "assemble", "title": "Assemble"},
        ],
    )
    wf = DynamicWorkflow(d)
    # Pass an input object missing 'topic' entirely.
    bad_inputs = MagicMock()
    bad_inputs.model_dump.return_value = {}
    events = await _collect(wf, bad_inputs)
    errors = [e for e in events if getattr(e, "type", None) == "error"]
    assert errors and "placeholder error" in errors[0].message


def _never_route(called: list[str]) -> Any:
    async def fake_route(**kwargs: Any) -> str:  # pragma: no cover - must not run
        called.append(kwargs["specialist_name"])
        return "Unknown specialist: talent"
    return fake_route


@pytest.mark.asyncio
async def test_run_fails_loudly_when_stored_specialist_no_longer_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Definitions are validated on create/update, not on load, so a stored
    definition can name a specialist that has since been removed (the `talent`
    key). The run must emit an error event before doing anything — not feed
    "Unknown specialist" into the synthesis step as if it were analysis."""
    called: list[str] = []
    monkeypatch.setattr(dyn, "route_to_specialist", _never_route(called))
    d = _def(
        steps=[
            {"kind": "specialist", "id": "screen", "title": "Screen",
             "specialist": "talent", "goal": "Assess {topic}."},
            {"kind": "synthesis", "id": "assemble", "title": "Assemble"},
        ],
    )
    wf = DynamicWorkflow(d)
    inputs = wf.input_model()(topic="a candidate")
    events = await _collect(wf, inputs)
    types = [getattr(e, "type", None) for e in events]
    # Pre-flight: the error is the ONLY event — not even the context step ran.
    assert types == ["error"]
    assert "'talent'" in events[0].message and "'screen'" in events[0].message
    assert "artifact" not in types
    assert called == []


@pytest.mark.asyncio
async def test_run_fails_when_synthesis_step_names_missing_specialist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A synthesis step with `instructions` consults its own `specialist` and
    returns that consult as the WHOLE artifact — the same hole, one field over."""
    called: list[str] = []
    monkeypatch.setattr(dyn, "route_to_specialist", _never_route(called))
    d = _def(
        steps=[
            {"kind": "synthesis", "id": "assemble", "title": "Assemble",
             "instructions": "Polish this.", "specialist": "talent"},
        ],
    )
    wf = DynamicWorkflow(d)
    events = await _collect(wf, wf.input_model()(topic="x"))
    assert [getattr(e, "type", None) for e in events] == ["error"]
    assert "'assemble'" in events[0].message
    assert called == []


@pytest.mark.asyncio
async def test_synthesis_without_instructions_ignores_its_specialist_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without `instructions` the synthesis step never consults anyone, so a
    stale default there must not fail an otherwise-valid workflow."""
    async def fake_route(**kwargs: Any) -> str:
        return "analysis"
    monkeypatch.setattr(dyn, "route_to_specialist", fake_route)
    d = _def(
        steps=[
            {"kind": "specialist", "id": "research", "title": "Research",
             "specialist": "cso", "goal": "Analyze {topic}."},
            {"kind": "synthesis", "id": "assemble", "title": "Assemble",
             "specialist": "talent"},
        ],
    )
    wf = DynamicWorkflow(d)
    events = await _collect(wf, wf.input_model()(topic="x"))
    types = [getattr(e, "type", None) for e in events]
    assert "error" not in types and "artifact" in types


@pytest.mark.asyncio
async def test_stale_specialist_fails_before_an_earlier_approval_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check is pre-flight: a gate BEFORE the stale step must not pause the
    run and DM a human for sign-off on a workflow that cannot complete."""
    called: list[str] = []
    monkeypatch.setattr(dyn, "route_to_specialist", _never_route(called))
    d = _def(
        steps=[
            {"kind": "approval_gate", "id": "gate", "title": "Gate",
             "person_id": 7, "question": "Proceed with {topic}?"},
            {"kind": "specialist", "id": "screen", "title": "Screen",
             "specialist": "talent", "goal": "Assess {topic}."},
            {"kind": "synthesis", "id": "assemble", "title": "Assemble"},
        ],
    )
    wf = DynamicWorkflow(d)
    events = await _collect(wf, wf.input_model()(topic="x"))
    assert not any(isinstance(e, WaitForHumanEvent) for e in events)
    assert [getattr(e, "type", None) for e in events] == ["error"]
    assert called == []
