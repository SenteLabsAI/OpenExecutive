"""Unit tests for openexecutive.orchestrator.workflow_run_tools.

These chat tools let the Executive list and launch any workflow from a chat
turn. ``list_workflows`` and the validation/refusal branches of ``run_workflow``
run against the real registry; the happy path and the approval-gate path use a
stub workflow + a tmp run DB so no Anthropic API or vector store is needed.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from openexecutive.orchestrator.workflow_run_tools import (
    handle_list_workflows,
    handle_run_workflow,
)
from openexecutive.workflows.base import WorkflowEvent
from openexecutive.workflows.wait_for_human import WaitForHumanEvent


@pytest.fixture(autouse=True)
def _no_audit_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep `_audit` off the default ./episodic_memory.db.

    Every run_workflow path writes a tool_invocation row. Unpatched, those
    rows land in the repo-root DB and leak into other modules' assertions on a
    full-suite run (see the audit-log note in CLAUDE.md).
    """
    monkeypatch.setattr(
        "openexecutive.orchestrator.workflow_run_tools.audit_log",
        lambda *_a, **_kw: None,
    )


def _call(fn: Callable[[dict[str, Any]], Awaitable[str]], payload: dict[str, Any]) -> dict[str, Any]:
    return json.loads(asyncio.run(fn(payload)))


# --------------------------------------------------------------------------- #
# Stub workflow used for the happy / gate paths
# --------------------------------------------------------------------------- #


class _StubInputs(BaseModel):
    topic: str


class _StubWorkflow:
    name = "stub"
    title = "Stub Workflow"

    def input_model(self) -> type[BaseModel]:
        return _StubInputs

    async def run(self, inputs: BaseModel, store: Any) -> AsyncIterator[WorkflowEvent]:
        yield WorkflowEvent(type="step_start", step_id="s1", step_title="Work")
        yield WorkflowEvent(type="artifact", content="# Stub artifact\n\nDone.")


class _GateWorkflow:
    name = "gate"
    title = "Gate Workflow"

    def input_model(self) -> type[BaseModel]:
        return _StubInputs

    async def run(self, inputs: BaseModel, store: Any) -> AsyncIterator[WorkflowEvent]:
        # Pause at an approval gate before producing any artifact.
        yield WaitForHumanEvent(person_id=3, question="Approve the plan?", timeout_hours=24)


class _ErrorThenArtifactWorkflow:
    name = "mixed"
    title = "Mixed Workflow"

    def input_model(self) -> type[BaseModel]:
        return _StubInputs

    async def run(self, inputs: BaseModel, store: Any) -> AsyncIterator[WorkflowEvent]:
        # A non-fatal error event followed by a real artifact: the artifact must win.
        yield WorkflowEvent(type="error", message="non-fatal note")
        yield WorkflowEvent(type="artifact", content="# Recovered artifact")


class _ErrorOnlyWorkflow:
    name = "boom"
    title = "Boom Workflow"

    def input_model(self) -> type[BaseModel]:
        return _StubInputs

    async def run(self, inputs: BaseModel, store: Any) -> AsyncIterator[WorkflowEvent]:
        yield WorkflowEvent(type="error", message="boom")


@pytest.fixture
def run_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point workflow-run persistence + the vector store at throwaway stand-ins."""
    from openexecutive.knowledge import store as knowledge_store
    from openexecutive.workflows import persistence

    db = tmp_path / "runs.db"
    monkeypatch.setattr(persistence, "DB_PATH", db)
    # The store is never touched by the stub workflows; a no-op stand-in keeps
    # the handler from constructing a real (heavy) ChromaDB instance.
    monkeypatch.setattr(knowledge_store, "ChromaDBStore", lambda *a, **k: object())
    return db


# --------------------------------------------------------------------------- #
# list_workflows
# --------------------------------------------------------------------------- #


def test_list_workflows_returns_builtins_with_inputs() -> None:
    out = _call(handle_list_workflows, {})
    assert out["count"] >= 1
    names = {w["name"] for w in out["workflows"]}
    # A representative built-in is present, with the compact metadata shape.
    assert "board_prep" in names
    sample = next(w for w in out["workflows"] if w["name"] == "board_prep")
    assert {"name", "title", "description", "section", "estimated_minutes", "inputs"} <= sample.keys()
    assert isinstance(sample["inputs"], dict)


def test_list_workflows_excludes_blocklist() -> None:
    out = _call(handle_list_workflows, {})
    names = {w["name"] for w in out["workflows"]}
    assert "executive_research" not in names


# --------------------------------------------------------------------------- #
# run_workflow — refusal / validation branches (real registry, no API)
# --------------------------------------------------------------------------- #


def test_run_workflow_missing_name() -> None:
    out = _call(handle_run_workflow, {"inputs": {}})
    assert "error" in out


def test_run_workflow_unknown_name() -> None:
    out = _call(handle_run_workflow, {"workflow": "not_a_workflow", "inputs": {}})
    assert "error" in out
    assert "unknown workflow" in out["error"]


def test_run_workflow_blocklisted_name_refused() -> None:
    out = _call(handle_run_workflow, {"workflow": "executive_research", "inputs": {}})
    assert "error" in out
    assert "run_executive_research" in out["error"]


def test_run_workflow_inputs_must_be_object(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive import workflows as wf_pkg

    monkeypatch.setattr(wf_pkg, "get_workflow", lambda name: _StubWorkflow())
    out = _call(handle_run_workflow, {"workflow": "stub", "inputs": "nope"})
    assert "error" in out
    assert "inputs must be an object" in out["error"]


def test_run_workflow_invalid_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive import workflows as wf_pkg

    monkeypatch.setattr(wf_pkg, "get_workflow", lambda name: _StubWorkflow())
    # _StubInputs requires `topic`; an empty object must fail validation.
    out = _call(handle_run_workflow, {"workflow": "stub", "inputs": {}})
    assert "error" in out
    assert "invalid inputs" in out["error"]


# --------------------------------------------------------------------------- #
# run_workflow — happy path + approval gate (stub workflow + tmp run DB)
# --------------------------------------------------------------------------- #


def test_run_workflow_happy_path(monkeypatch: pytest.MonkeyPatch, run_db: Path) -> None:
    from openexecutive import workflows as wf_pkg
    from openexecutive.workflows import persistence

    monkeypatch.setattr(wf_pkg, "get_workflow", lambda name: _StubWorkflow())

    out = _call(handle_run_workflow, {"workflow": "stub", "inputs": {"topic": "x"}})
    assert out["ok"] is True
    assert out["artifact"].startswith("# Stub artifact")
    run = persistence.get_run(out["run_id"], db_path=run_db)
    assert run is not None
    assert run["status"] == "done"


def test_run_workflow_awaiting_human(monkeypatch: pytest.MonkeyPatch, run_db: Path) -> None:
    from openexecutive import workflows as wf_pkg
    from openexecutive.workflows import persistence

    monkeypatch.setattr(wf_pkg, "get_workflow", lambda name: _GateWorkflow())

    out = _call(handle_run_workflow, {"workflow": "gate", "inputs": {"topic": "x"}})
    assert out["status"] == "awaiting_human"
    assert out["person_id"] == 3
    # The run is checkpointed (not completed or failed).
    run = persistence.get_run(out["run_id"], db_path=run_db)
    assert run is not None
    assert run["status"] == "awaiting_human"


def test_run_workflow_artifact_wins_over_error_event(
    monkeypatch: pytest.MonkeyPatch, run_db: Path
) -> None:
    """A non-fatal `error` event must not discard a produced artifact."""
    from openexecutive import workflows as wf_pkg
    from openexecutive.workflows import persistence

    monkeypatch.setattr(wf_pkg, "get_workflow", lambda name: _ErrorThenArtifactWorkflow())

    out = _call(handle_run_workflow, {"workflow": "mixed", "inputs": {"topic": "x"}})
    assert out["ok"] is True
    assert out["artifact"].startswith("# Recovered artifact")
    run = persistence.get_run(out["run_id"], db_path=run_db)
    assert run is not None
    assert run["status"] == "done"


def test_run_workflow_error_only_fails_with_message(
    monkeypatch: pytest.MonkeyPatch, run_db: Path
) -> None:
    from openexecutive import workflows as wf_pkg
    from openexecutive.workflows import persistence

    monkeypatch.setattr(wf_pkg, "get_workflow", lambda name: _ErrorOnlyWorkflow())

    out = _call(handle_run_workflow, {"workflow": "boom", "inputs": {"topic": "x"}})
    assert "error" in out
    assert "boom" in out["error"]
    run = persistence.get_run(out["run_id"], db_path=run_db)
    assert run is not None
    assert run["status"] == "error"


def test_run_workflow_create_run_failure_fails_fast(
    monkeypatch: pytest.MonkeyPatch, run_db: Path
) -> None:
    """If the run row can't be created, refuse rather than run an untracked
    (and potentially orphaned) workflow."""
    from openexecutive import workflows as wf_pkg
    from openexecutive.workflows import persistence

    monkeypatch.setattr(wf_pkg, "get_workflow", lambda name: _GateWorkflow())

    def _boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(persistence, "create_run", _boom)

    out = _call(handle_run_workflow, {"workflow": "gate", "inputs": {"topic": "x"}})
    assert "error" in out
    assert "could not start run" in out["error"]
    # No awaiting_human claim was made.
    assert out.get("status") != "awaiting_human"


def test_run_workflow_reports_knowledge_store_failure_and_fails_the_run(
    monkeypatch: pytest.MonkeyPatch, run_db: Path
) -> None:
    """A store that won't construct must not escape this handler.

    `ChromaDBStore(...)` used to sit outside the try, so an init failure
    propagated out of the tool handler, through the orchestrator's tool gather,
    and surfaced to the user as a generic "I encountered an error" with the run
    row stranded at 'running' (#136). It must come back as a tool error, and
    the run it already created must be marked failed.
    """
    from openexecutive import workflows as wf_pkg
    from openexecutive.workflows import persistence

    monkeypatch.setattr(wf_pkg, "get_workflow", lambda name: _StubWorkflow())

    def _explode(**_kwargs: Any) -> Any:
        raise RuntimeError("chroma refused to open")

    monkeypatch.setattr(
        "openexecutive.knowledge.store.ChromaDBStore", _explode
    )

    out = _call(handle_run_workflow, {"workflow": "stub", "inputs": {"topic": "x"}})
    assert "error" in out
    assert "knowledge store unavailable" in out["error"]

    # The run row exists and is failed, not left at 'running'.
    runs = persistence.list_runs(workflow_name="stub", db_path=run_db)
    assert runs, "create_run should have written a row before the store failed"
    assert runs[0]["status"] == "error"


def test_run_workflow_description_does_not_claim_the_briefs_dispatch() -> None:
    """The description told the model that morning_brief and
    end_of_day_digest DM the principal when run, and to confirm before firing
    them. `handle_run_workflow` delivers nothing — delivery is scheduler-only
    — so the model solicited a confirmation on a false premise (#136).

    `executive_reflection` genuinely does execute tool calls, so the warning
    has to be narrowed to it, not deleted.
    """
    from openexecutive.orchestrator.workflow_run_tools import RUN_WORKFLOW_TOOL

    desc = RUN_WORKFLOW_TOOL["description"]
    assert "morning_brief and end_of_day_digest DM the principal" not in desc
    assert "does NOT deliver DMs" in desc.replace("\n", " ")
    assert "executive_reflection" in desc


def test_awaiting_human_checkpoint_carries_routing_fields(
    monkeypatch: pytest.MonkeyPatch, run_db: Path
) -> None:
    """state_json used to be written with empty channel/channel_ref, so the
    inbound resolver's channel filter could never match a reply (#136)."""
    import json as _json

    from openexecutive import workflows as wf_pkg
    from openexecutive.workflows import persistence

    monkeypatch.setattr(wf_pkg, "get_workflow", lambda name: _GateWorkflow())

    async def _fake_deliver(event: Any, **_kw: Any) -> tuple[Any, str]:
        return (
            event.model_copy(
                update={
                    "channel": "slack",
                    "channel_ref": "U123",
                    "outbound_message_id": "1700000000.5",
                }
            ),
            "sent",
        )

    monkeypatch.setattr(
        "openexecutive.workflows.gate_delivery.deliver_gate_question", _fake_deliver
    )

    out = _call(handle_run_workflow, {"workflow": "gate", "inputs": {"topic": "x"}})
    assert out["status"] == "awaiting_human"
    assert out["delivery"] == "sent"

    run = persistence.get_run(out["run_id"], db_path=run_db)
    assert run is not None
    state = _json.loads(run["state_json"])
    assert state["channel"] == "slack"
    assert state["channel_ref"] == "U123"
    assert state["outbound_message_id"] == "1700000000.5"


def test_undelivered_gate_is_not_reported_as_waiting_on_them(
    monkeypatch: pytest.MonkeyPatch, run_db: Path
) -> None:
    """When the question never reached the approver, the presentation hint
    must not tell the principal it is waiting on their reply."""
    from openexecutive import workflows as wf_pkg

    monkeypatch.setattr(wf_pkg, "get_workflow", lambda name: _GateWorkflow())

    async def _suppressed(event: Any, **_kw: Any) -> tuple[Any, str]:
        return event.model_copy(), "suppressed"

    monkeypatch.setattr(
        "openexecutive.workflows.gate_delivery.deliver_gate_question", _suppressed
    )

    out = _call(handle_run_workflow, {"workflow": "gate", "inputs": {"topic": "x"}})

    assert out["delivery"] == "suppressed"
    assert "NOT been asked" in out["presentation_hint"]


def test_every_delivery_status_has_a_presentation_hint() -> None:
    """A new DeliveryStatus must not fall through to the "could not be
    delivered" fallback and silently mis-describe itself."""
    from typing import get_args

    from openexecutive.orchestrator.workflow_run_tools import (
        _AWAITING_HINTS,
        _assert_hints_cover_every_delivery_status,
    )
    from openexecutive.workflows.gate_delivery import DeliveryStatus

    assert set(get_args(DeliveryStatus)) <= set(_AWAITING_HINTS)

    # And the guard actually bites.
    with patch.dict(
        "openexecutive.orchestrator.workflow_run_tools._AWAITING_HINTS",
        {k: v for k, v in _AWAITING_HINTS.items() if k != "alerted"},
        clear=True,
    ), pytest.raises(RuntimeError, match="alerted"):
        _assert_hints_cover_every_delivery_status()


class _ResumableGateWorkflow:
    """A gate that carries a resume payload, i.e. a real dynamic workflow."""

    name = "resumable_gate"
    title = "Resumable Gate Workflow"

    def input_model(self) -> type[BaseModel]:
        return _StubInputs

    async def run(self, inputs: BaseModel, store: Any) -> AsyncIterator[Any]:
        from openexecutive.workflows.wait_for_human import WorkflowResumeState

        yield WaitForHumanEvent(
            person_id=3,
            question="Approve the plan?",
            timeout_hours=24,
            resume_state=WorkflowResumeState(
                workflow_name="resumable_gate",
                gate_step_id="gate",
                gate_step_index=1,
                next_step_index=2,
                outputs={"research": ("Research", "body")},
            ),
        )


def _stub_sent_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _deliver(event: Any, **_kw: Any) -> tuple[Any, str]:
        return event.model_copy(update={"channel": "slack", "channel_ref": "U1"}), "sent"

    monkeypatch.setattr(
        "openexecutive.workflows.gate_delivery.deliver_gate_question", _deliver
    )


def test_pause_only_gate_is_reported_as_not_resuming(
    monkeypatch: pytest.MonkeyPatch, run_db: Path
) -> None:
    """`_GateWorkflow` yields a bare WaitForHumanEvent — no resume payload —
    which is what every caller got before resume existed. The run must be
    checkpointed as un-resumable and described that way, so the Executive does
    not promise the principal work that will never happen."""
    from openexecutive import workflows as wf_pkg
    from openexecutive.workflows import persistence

    monkeypatch.setattr(wf_pkg, "get_workflow", lambda name: _GateWorkflow())
    _stub_sent_delivery(monkeypatch)

    out = _call(handle_run_workflow, {"workflow": "gate", "inputs": {"topic": "x"}})

    assert out["resumable"] is False
    assert "does not continue past the gate" in out["presentation_hint"]
    run = persistence.get_run(out["run_id"], db_path=run_db)
    assert run is not None
    assert run["resume_state_json"] is None


def test_resumable_gate_is_reported_as_continuing_on_its_own(
    monkeypatch: pytest.MonkeyPatch, run_db: Path
) -> None:
    from openexecutive import workflows as wf_pkg
    from openexecutive.workflows import persistence

    monkeypatch.setattr(wf_pkg, "get_workflow", lambda name: _ResumableGateWorkflow())
    _stub_sent_delivery(monkeypatch)

    out = _call(
        handle_run_workflow, {"workflow": "resumable_gate", "inputs": {"topic": "x"}}
    )

    assert out["status"] == "awaiting_human"
    assert out["resumable"] is True
    assert "picks up where it left off" in out["presentation_hint"]

    run = persistence.get_run(out["run_id"], db_path=run_db)
    assert run is not None
    assert json.loads(run["resume_state_json"])["gate_step_id"] == "gate"
    # The payload must not leak into the checkpoint the resolver reads.
    assert "resume_state" not in json.loads(run["state_json"])


def test_an_undelivered_resumable_gate_says_both_things(
    monkeypatch: pytest.MonkeyPatch, run_db: Path
) -> None:
    """Delivery and resumability are independent, and the hint has to be
    honest about each: nobody was asked, AND the run will continue once they
    are."""
    from openexecutive import workflows as wf_pkg

    monkeypatch.setattr(wf_pkg, "get_workflow", lambda name: _ResumableGateWorkflow())

    async def _failed(event: Any, **_kw: Any) -> tuple[Any, str]:
        return event.model_copy(), "failed"

    monkeypatch.setattr(
        "openexecutive.workflows.gate_delivery.deliver_gate_question", _failed
    )

    out = _call(
        handle_run_workflow, {"workflow": "resumable_gate", "inputs": {"topic": "x"}}
    )

    hint = out["presentation_hint"]
    assert "could not be delivered" in hint
    assert "picks up where it left off" in hint


# --------------------------------------------------------------------------- #
# Principal-only workflows: the weekly review (both modes) and the solo
# morning brief carry the principal's own data, so only they may run one.
# --------------------------------------------------------------------------- #


@pytest.fixture
def principal_roster(
    tmp_path: Path, run_db: Path, monkeypatch: pytest.MonkeyPatch
) -> Any:
    from types import SimpleNamespace

    from openexecutive import workflows as wf_pkg
    from openexecutive.memory import episodic
    from openexecutive.people import registry as people_registry
    from openexecutive.people import store as people_store
    from openexecutive.workflows.morning_brief import MorningBriefWorkflow
    from openexecutive.workflows.weekly_review import WeeklyReviewWorkflow

    db = tmp_path / "roster.db"
    monkeypatch.setattr(episodic, "DB_PATH", db)
    monkeypatch.setattr(people_store, "DB_PATH", db)
    episodic.initialize_db(db)
    people_store.initialize_db(db)
    people_registry.invalidate()

    # Real classes (so the real principal_only_modes apply) with a stub run.
    class _Weekly(WeeklyReviewWorkflow):
        async def run(self, inputs: Any, store: Any):  # type: ignore[override]
            yield WorkflowEvent(type="artifact", content="# Weekly review")

    class _Morning(MorningBriefWorkflow):
        async def run(self, inputs: Any, store: Any):  # type: ignore[override]
            yield WorkflowEvent(type="artifact", content="# Morning brief")

    stubs = {"weekly_review": _Weekly(), "morning_brief": _Morning()}
    monkeypatch.setattr(wf_pkg, "get_workflow", lambda name: stubs[name])
    yield SimpleNamespace(
        principal=people_store.upsert_person(full_name="Pat Lee", is_principal=True),
        teammate=people_store.upsert_person(full_name="Sam Ortiz"),
    )
    people_registry.invalidate()


def _unverified(principal: int, teammate: int) -> dict[str, Any]:
    from openexecutive.orchestrator.session import Session

    return {
        "no session": None,
        "email from the principal's address": Session(caller_person_id=principal),
        "Google Chat": Session(origin_channel="google_chat", origin_channel_ref="spaces/x",
                               caller_person_id=principal),
        "Telegram without a webhook secret": Session(origin_channel="telegram",
                                                     origin_channel_ref="555",
                                                     caller_person_id=principal),
        "a teammate in the web chat": Session(from_web_chat=True, caller_person_id=teammate),
        "a teammate on Slack": Session(origin_channel="slack", origin_channel_ref="U2",
                                       caller_person_id=teammate),
        "an unattended run": Session(unattended=True),
    }


def _run_as(session: Any, workflow: str, mode: str) -> dict[str, Any]:
    from openexecutive.memory import workspace_settings as ws
    from openexecutive.orchestrator.schedule_tools import set_session

    # The install-wide mode (what a session-less turn sees).
    ws.restore_workspace_settings(ws.WorkspaceSettings(mode=mode))  # type: ignore[arg-type]
    with set_session(session):
        return _call(handle_run_workflow, {"workflow": workflow, "inputs": {}})


@pytest.mark.parametrize(("workflow", "mode"), [
    ("weekly_review", "solo"), ("weekly_review", "team"), ("morning_brief", "solo"),
])
def test_principal_only_workflows_refuse_everyone_else(
    principal_roster: Any, monkeypatch: pytest.MonkeyPatch, workflow: str, mode: str
) -> None:
    from unittest.mock import MagicMock

    from openexecutive.config import get_settings
    from openexecutive.workflows.persistence import list_runs

    monkeypatch.setattr(type(get_settings()), "telegram_webhook_secret_valid", False,
                        raising=False)
    fake_audit = MagicMock()
    monkeypatch.setattr("openexecutive.orchestrator.workflow_run_tools.audit_log", fake_audit)
    sessions = _unverified(principal_roster.principal, principal_roster.teammate)
    for label, session in sessions.items():
        out = _run_as(session, workflow, mode)
        assert "error" in out, (label, out)
        # A plain refusal that does not spell out the rule.
        assert "principal" not in out["error"] and "verified" not in out["error"]
        assert "artifact" not in out
    assert list_runs(workflow_name=workflow) == []
    refusals = [c.kwargs["details"] for c in fake_audit.call_args_list
                if c.kwargs.get("details", {}).get("refused") is True]
    assert len(refusals) == len(sessions)
    assert refusals[0]["workflow"] == workflow and refusals[0]["workspace_mode"] == mode


@pytest.mark.parametrize(("workflow", "mode"), [
    ("weekly_review", "solo"), ("weekly_review", "team"), ("morning_brief", "solo"),
])
def test_the_principal_on_a_verified_surface_may_run_them(
    principal_roster: Any, workflow: str, mode: str
) -> None:
    from openexecutive.orchestrator.session import Session

    web = _run_as(Session(from_web_chat=True, caller_person_id=principal_roster.principal),
                  workflow, mode)
    assert web.get("ok") is True and web["artifact"].startswith("#"), web
    slack = _run_as(Session(origin_channel="slack", origin_channel_ref="U1",
                            caller_person_id=principal_roster.principal), workflow, mode)
    assert slack.get("ok") is True, slack


def test_the_team_morning_brief_is_unchanged(principal_roster: Any) -> None:
    """In team mode anyone who can chat may still run the morning brief."""
    from openexecutive.orchestrator.session import Session

    for session in (
        Session(from_web_chat=True, caller_person_id=principal_roster.teammate),
        Session(caller_person_id=principal_roster.principal),  # email
        None,
    ):
        out = _run_as(session, "morning_brief", "team")
        assert out.get("ok") is True and out["artifact"] == "# Morning brief", out


def test_a_failing_principal_check_refuses(
    principal_roster: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.orchestrator.session import Session

    def _boom(_session: Any) -> bool:
        raise RuntimeError("roster unreadable")

    monkeypatch.setattr(
        "openexecutive.orchestrator.people_tools.is_principal_on_verified_surface", _boom
    )
    out = _run_as(Session(from_web_chat=True, caller_person_id=principal_roster.principal),
                  "weekly_review", "solo")
    assert "error" in out


def test_only_the_weekly_review_and_solo_brief_are_principal_only() -> None:
    from openexecutive.workflows import WORKFLOW_REGISTRY

    gated = {n: set(wf.principal_only_modes) for n, wf in WORKFLOW_REGISTRY.items()
             if wf.principal_only_modes}
    assert gated == {"weekly_review": {"solo", "team"}, "morning_brief": {"solo"}}
