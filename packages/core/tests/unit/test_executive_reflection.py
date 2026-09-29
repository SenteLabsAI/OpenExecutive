"""Tests for the executive_reflection workflow + scheduler plumbing.

Covers:
- Workflow registration + input model.
- Scheduler seeding is idempotent and includes the new reflection kind.
- Env-var override of PRINCIPAL_REFLECTION_TIME.
- Chain-next math: `_enqueue_next_principal_brief("executive_reflection", …)`
  advances to the next occurrence.
- Smoke run: end-to-end workflow execution with a stubbed LLM that
  emits a no-tool plain-text response. Verifies the workflow yields
  step events + artifact + done without crashing.
- Action chips: the new outbound tool set is unchanged, so the chip
  drift tests still pass (verified by import).
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from openexecutive.memory import episodic
from openexecutive.scheduler import runner
from openexecutive.workflows import WORKFLOW_REGISTRY
from openexecutive.workflows.executive_reflection import (
    ExecutiveReflectionInput,
    ExecutiveReflectionWorkflow,
)


def _setup_isolated_db(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(episodic, "DB_PATH", db)
    episodic.initialize_db(db)


# ---------------------------------------------------------------------------
# Workflow registration
# ---------------------------------------------------------------------------

def test_executive_reflection_registered() -> None:
    assert "executive_reflection" in WORKFLOW_REGISTRY
    wf = WORKFLOW_REGISTRY["executive_reflection"]
    assert wf.title == "Executive Reflection"
    assert len(wf.steps()) == 3
    step_ids = [s.id for s in wf.steps()]
    assert step_ids == ["gather_signals", "decide", "emit_artifact"]


def test_input_model_accepts_empty_period_label() -> None:
    # Scheduler fires the workflow with no args — the input model must
    # accept that with all-default values.
    inputs = ExecutiveReflectionInput()
    assert inputs.period_label == ""


def test_input_model_accepts_explicit_period_label() -> None:
    inputs = ExecutiveReflectionInput(period_label="2026-05-26")
    assert inputs.period_label == "2026-05-26"


# ---------------------------------------------------------------------------
# Scheduler seeding includes executive_reflection
# ---------------------------------------------------------------------------

def test_seed_principal_briefs_includes_reflection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same seed function that handles morning_brief + EoD digest
    should now also enqueue an executive_reflection row."""
    _setup_isolated_db(tmp_path / "refl.db", monkeypatch)

    inserted = runner.seed_principal_briefs()
    assert inserted == 3  # morning + eod + reflection

    actions = episodic.list_scheduled_actions(status="pending", limit=10)
    kinds = {a.kind for a in actions}
    assert "executive_reflection" in kinds
    assert "principal_brief_morning" in kinds
    assert "principal_brief_eod" in kinds


def test_reflection_seed_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_isolated_db(tmp_path / "refl2.db", monkeypatch)
    runner.seed_principal_briefs()

    # Second call must not duplicate.
    inserted = runner.seed_principal_briefs()
    assert inserted == 0

    reflection_rows = [
        a for a in episodic.list_scheduled_actions(status="pending", limit=10)
        if a.kind == "executive_reflection"
    ]
    assert len(reflection_rows) == 1


def test_reflection_uses_env_var_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_isolated_db(tmp_path / "refl3.db", monkeypatch)
    monkeypatch.setenv("PRINCIPAL_REFLECTION_TIME", "06:45")

    runner.seed_principal_briefs()
    row = next(
        a for a in episodic.list_scheduled_actions(status="pending", limit=10)
        if a.kind == "executive_reflection"
    )
    dt = datetime.fromisoformat(row.run_at)
    assert (dt.hour, dt.minute) == (6, 45)


def test_reflection_default_time_is_0730_utc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without an env override, reflection should fire at 07:30 UTC —
    30 minutes before the 08:00 morning brief default."""
    _setup_isolated_db(tmp_path / "refl4.db", monkeypatch)
    monkeypatch.delenv("PRINCIPAL_REFLECTION_TIME", raising=False)

    runner.seed_principal_briefs()
    row = next(
        a for a in episodic.list_scheduled_actions(status="pending", limit=10)
        if a.kind == "executive_reflection"
    )
    dt = datetime.fromisoformat(row.run_at)
    assert (dt.hour, dt.minute) == (7, 30)


# ---------------------------------------------------------------------------
# Chain-next
# ---------------------------------------------------------------------------

def test_enqueue_next_reflection_advances_24h(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_isolated_db(tmp_path / "refl5.db", monkeypatch)
    monkeypatch.setenv("PRINCIPAL_REFLECTION_TIME", "07:30")

    after = datetime(2026, 5, 26, 12, 0, tzinfo=UTC)
    aid = runner._enqueue_next_principal_brief(
        "executive_reflection", after=after,
    )
    assert aid is not None

    row = episodic.get_scheduled_action(aid)
    assert row is not None
    next_dt = datetime.fromisoformat(row.run_at)
    assert next_dt > after
    assert next_dt.hour == 7 and next_dt.minute == 30


def test_enqueue_next_unknown_kind_returns_none() -> None:
    """Defensive: the recurring-kind lookup table must return None for an
    unknown kind rather than crash."""
    after = datetime(2026, 5, 26, 12, 0, tzinfo=UTC)
    result = runner._enqueue_next_principal_brief("not_a_real_kind", after=after)
    assert result is None


# ---------------------------------------------------------------------------
# Smoke run with stubbed LLM
# ---------------------------------------------------------------------------

class _FakeTextBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _FakeResponse:
    """Mimics enough of Anthropic's response shape for the workflow's
    content + stop_reason iteration."""

    def __init__(self, text: str, stop_reason: str = "end_turn") -> None:
        self.content = [_FakeTextBlock(text)]
        self.stop_reason = stop_reason


class _FakeProvider:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def messages_create(self, **_kwargs: Any) -> _FakeResponse:
        return self._response


@pytest.mark.asyncio
async def test_workflow_runs_end_to_end_with_stubbed_llm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Smoke: run the workflow with a stubbed provider that returns a
    plain-text response (no tool calls). The workflow should emit
    step_start / step_done / artifact / done events without crashing.

    The provider is stubbed via `providers.get_provider` to avoid any
    real Anthropic API call.
    """
    _setup_isolated_db(tmp_path / "refl_smoke.db", monkeypatch)

    from openexecutive import providers

    fake = _FakeProvider(_FakeResponse(
        "**Acted on:**\n- DM'd Sara on Slack\n\n**Quiet:** Nothing else.",
        stop_reason="end_turn",
    ))
    monkeypatch.setattr(providers, "get_provider", lambda _model: fake)

    workflow = ExecutiveReflectionWorkflow()
    inputs = ExecutiveReflectionInput()

    # ChromaDBStore is not used by this workflow (we don't pass through
    # retrieve), but Workflow.run requires a store argument. Pass a
    # sentinel — the workflow body never touches it.
    events: list[Any] = []
    async for event in workflow.run(inputs=inputs, store=None):  # type: ignore[arg-type]
        events.append(event)

    event_types = [e.type for e in events]
    # Three step_start, three step_done, one artifact, one done.
    assert event_types.count("step_start") == 3
    assert event_types.count("step_done") == 3
    assert event_types.count("artifact") == 1
    assert event_types.count("done") == 1

    artifact_event = next(e for e in events if e.type == "artifact")
    assert artifact_event.content
    # The stub's text appears verbatim in the artifact (no tool log
    # appended because no tools were called).
    assert "Acted on" in artifact_event.content


class _FakeToolUseBlock:
    def __init__(self, name: str, input: dict[str, Any], id: str = "tu_1") -> None:
        self.type = "tool_use"
        self.id = id
        self.name = name
        self.input = input


class _FakeMixedResponse:
    """A response that returns ONE tool_use block then one text block —
    Anthropic's typical shape when the model wants to call a tool then
    summarize."""

    def __init__(self, tool_name: str, tool_input: dict[str, Any], text: str) -> None:
        self.content = [
            _FakeToolUseBlock(tool_name, tool_input),
            _FakeTextBlock(text),
        ]
        self.stop_reason = "tool_use"


class _FakeFinalResponse:
    """Follow-up turn after tool_result — model emits a plain-text
    summary with no further tool calls."""

    def __init__(self, text: str) -> None:
        self.content = [_FakeTextBlock(text)]
        self.stop_reason = "end_turn"


class _SequenceProvider:
    """Hands out a different response on each `messages_create` call —
    lets the test simulate a 2-iteration loop (tool call → summary)."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)

    async def messages_create(self, **_kwargs: Any) -> Any:
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_workflow_invokes_tool_handler_during_decide(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The decide loop should: see a tool_use block → invoke the
    matching handler in _ALL_SKILL_HANDLERS → feed the tool_result
    back to the model → consume the model's follow-up text summary →
    emit an artifact containing both the summary and the structured
    tool-call log.

    Regression test for the round-1 review finding that no test
    exercised the tool-execution branch of the decide loop.
    """
    _setup_isolated_db(tmp_path / "refl_tool.db", monkeypatch)

    captured_input: dict[str, Any] = {}

    async def _stub_create_alert(payload: dict[str, Any]) -> str:
        captured_input.update(payload)
        return '{"alert_id": 42, "status": "created"}'

    # Patch the handler in the registry the workflow imports from
    # executive.py — this is the actual lookup target.
    from openexecutive.orchestrator import executive
    monkeypatch.setitem(executive._ALL_SKILL_HANDLERS, "create_alert", _stub_create_alert)

    from openexecutive import providers
    seq = _SequenceProvider([
        _FakeMixedResponse(
            "create_alert",
            {"headline": "Burn trending high", "severity": "high"},
            "",  # no narrative in the tool-call turn
        ),
        _FakeFinalResponse("**Acted on:**\n- Flagged burn alert\n\n**Quiet:** done."),
    ])
    monkeypatch.setattr(providers, "get_provider", lambda _model: seq)

    workflow = ExecutiveReflectionWorkflow()
    inputs = ExecutiveReflectionInput()
    events: list[Any] = []
    async for event in workflow.run(inputs=inputs, store=None):  # type: ignore[arg-type]
        events.append(event)

    # Handler was actually called with the model's args.
    assert captured_input == {"headline": "Burn trending high", "severity": "high"}

    # Workflow emitted an artifact + done; no error event.
    types = [e.type for e in events]
    assert "error" not in types
    assert types.count("artifact") == 1
    assert types.count("done") == 1

    # The artifact contains both the model summary AND the tool-call log.
    artifact_event = next(e for e in events if e.type == "artifact")
    assert "Flagged burn alert" in artifact_event.content
    assert "create_alert" in artifact_event.content
    # Success mark in the tool log.
    assert "✓" in artifact_event.content


@pytest.mark.asyncio
async def test_workflow_handles_failing_tool_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a tool handler returns {"error": ...}, the workflow should
    still complete — the model gets to see the failure summary in the
    tool_result and decide what to do next; the tool-call log marks
    the row with ✗."""
    _setup_isolated_db(tmp_path / "refl_toolfail.db", monkeypatch)

    async def _failing_handler(_payload: dict[str, Any]) -> str:
        return '{"error": "slack is not configured"}'

    from openexecutive.orchestrator import executive
    monkeypatch.setitem(executive._ALL_SKILL_HANDLERS, "send_slack_dm", _failing_handler)

    from openexecutive import providers
    seq = _SequenceProvider([
        _FakeMixedResponse(
            "send_slack_dm",
            {"user_id": "U123", "text": "hi"},
            "",
        ),
        _FakeFinalResponse("**Quiet:** Slack misconfigured, skipping."),
    ])
    monkeypatch.setattr(providers, "get_provider", lambda _model: seq)

    workflow = ExecutiveReflectionWorkflow()
    inputs = ExecutiveReflectionInput()
    events: list[Any] = []
    async for event in workflow.run(inputs=inputs, store=None):  # type: ignore[arg-type]
        events.append(event)

    assert next(e for e in events if e.type == "artifact").content
    artifact_content = next(e for e in events if e.type == "artifact").content
    assert "✗" in artifact_content
    assert "send_slack_dm" in artifact_content


@pytest.mark.asyncio
async def test_workflow_handles_provider_failure_gracefully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the LLM provider raises, the workflow must emit an `error`
    event rather than letting the exception escape into the
    scheduler. The scheduler's outer try/except would still catch a
    bare raise but the workflow should produce a clean signal."""
    _setup_isolated_db(tmp_path / "refl_fail.db", monkeypatch)

    from openexecutive import providers

    class _FailingProvider:
        async def messages_create(self, **_kwargs: Any) -> _FakeResponse:
            raise RuntimeError("simulated provider failure")

    monkeypatch.setattr(providers, "get_provider", lambda _model: _FailingProvider())

    workflow = ExecutiveReflectionWorkflow()
    inputs = ExecutiveReflectionInput()
    events: list[Any] = []
    async for event in workflow.run(inputs=inputs, store=None):  # type: ignore[arg-type]
        events.append(event)
    types = [e.type for e in events]
    assert "error" in types
    error = next(e for e in events if e.type == "error")
    assert error.message and "provider failure" in error.message.lower()


# ---------------------------------------------------------------------------
# Drift guard — action_chips registry stays in sync
# ---------------------------------------------------------------------------

def test_action_chips_registry_drift_still_passes() -> None:
    """Shift 5 doesn't add new outbound tools — the bidirectional drift
    test from Shift 4 should still pass."""
    from openexecutive.orchestrator.action_chips import SIDE_EFFECTING_TOOLS
    from tests.unit.test_action_chips import (
        _KNOWN_READ_ONLY_TOOLS,
        _all_registered_tool_names,
    )

    classified = SIDE_EFFECTING_TOOLS | _KNOWN_READ_ONLY_TOOLS
    unclassified = _all_registered_tool_names() - classified
    assert not unclassified, (
        f"Shift 5 changes left tools unclassified: {sorted(unclassified)}"
    )


def test_previous_reflection_artifact_reads_last_done_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.workflows import persistence as wf_persistence
    from openexecutive.workflows.executive_reflection import _previous_reflection_artifact

    db = tmp_path / "runs.db"
    monkeypatch.setattr(wf_persistence, "DB_PATH", db)
    wf_persistence.initialize_runs_db(db)
    assert _previous_reflection_artifact() is None

    wf_persistence.create_run("r1", "executive_reflection", "Reflection day 1", {})
    wf_persistence.complete_run("r1", "**Acted on:** DM'd Dana")
    wf_persistence.create_run("r2", "morning_brief", "Brief", {})
    wf_persistence.complete_run("r2", "BRIEF TEXT")
    wf_persistence.create_run("r3", "executive_reflection", "Reflection running", {})
    assert _previous_reflection_artifact() == "**Acted on:** DM'd Dana"

    wf_persistence.create_run("r4", "executive_reflection", "Reflection empty", {})
    wf_persistence.complete_run("r4", "(no artifact)")
    assert _previous_reflection_artifact() is None


# ---------------------------------------------------------------------------
# Grounding: outward prose and flags must name only what the input holds
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reflection_refuses_ungrounded_alert_and_drops_ungrounded_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_isolated_db(tmp_path / "refl_ground.db", monkeypatch)
    from openexecutive import providers
    from openexecutive.briefing import grounding
    from openexecutive.orchestrator import executive

    monkeypatch.setattr(grounding, "profile_sources", lambda: [])
    monkeypatch.setattr(grounding, "org_sources", lambda: [])
    monkeypatch.setattr(grounding, "grounding_mode", lambda: "enforce")
    audits: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "openexecutive.audit.log_event",
        lambda event_type, summary, **kw: audits.append({"type": event_type, **kw}),
    )
    called: list[dict[str, Any]] = []

    async def _stub_create_alert(payload: dict[str, Any]) -> str:
        called.append(payload)
        return '{"status": "alert_scheduled"}'

    monkeypatch.setitem(executive._ALL_SKILL_HANDLERS, "create_alert", _stub_create_alert)
    seq = _SequenceProvider([
        _FakeMixedResponse(
            "create_alert",
            {"subject": "Lease renewal", "body": "Marcus Lee says the renewal is 52 units."},
            "",
        ),
        _FakeFinalResponse(
            "**Acted on:**\n- Tried an alert\n\n"
            "**Flagged for the brief:**\n- Marcus Lee wants a call\n- Nothing else pressing"
        ),
    ])
    monkeypatch.setattr(providers, "get_provider", lambda _model: seq)

    events = [
        e async for e in ExecutiveReflectionWorkflow().run(
            inputs=ExecutiveReflectionInput(), store=None,  # type: ignore[arg-type]
        )
    ]
    artifact = next(e for e in events if e.type == "artifact").content

    assert called == []
    assert "✗ `create_alert`" in artifact and "Not sent" in artifact
    assert "Marcus Lee wants a call" not in artifact
    assert "- Nothing else pressing" in artifact
    assert "_Grounding: held back 1 flag naming" in artifact
    tools = [a for a in audits if a["type"] == "grounding" and "tool" in a["details"]]
    assert tools and tools[0]["details"]["names_bad"] == ["Marcus Lee"]

    # What the next brief reads stops at the tool log.
    from openexecutive.briefing.brief_state import _FLAGGED_RE

    match = _FLAGGED_RE.search(artifact)
    assert match is not None
    assert "Tool calls" not in match.group(1)
    assert match.group(1).strip() == "- Nothing else pressing"
