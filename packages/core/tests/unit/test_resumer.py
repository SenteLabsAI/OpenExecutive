"""Tests for workflows/resumer.py."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from openexecutive.alerts import store as alert_store
from openexecutive.departments import store as dept_store
from openexecutive.memory import episodic
from openexecutive.people import store as people_store
from openexecutive.workflows import persistence as wf_persistence
from openexecutive.workflows.resumer import _handle_timeout, _tick, apply_resolution
from openexecutive.workflows.wait_for_human import WaitForHumanResolution


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "test.db"
    monkeypatch.setattr(episodic, "DB_PATH", db)
    monkeypatch.setattr(wf_persistence, "DB_PATH", db)
    monkeypatch.setattr(alert_store, "DB_PATH", db)
    monkeypatch.setattr(dept_store, "DB_PATH", db)
    monkeypatch.setattr(people_store, "DB_PATH", db)

    episodic.initialize_db(db)
    wf_persistence.initialize_runs_db(db)
    alert_store.initialize_db(db)
    dept_store.initialize_db(db)
    people_store.initialize_db(db)
    yield


def _seed_awaiting(
    run_id: str,
    person_id: int,
    on_timeout: str = "escalate",
    awaiting_until: datetime | None = None,
    *,
    department: str = "",
    resume_state: str | None = None,
    workflow_name: str = "test_wf",
    inputs: dict | None = None,
) -> None:
    wf_persistence.create_run(run_id, workflow_name, "Test run", inputs or {})
    state = json.dumps({
        "on_timeout": on_timeout,
        "channel": "slack",
        "channel_ref": "U123",
        "expected_reply_shape": "approve_reject",
        "question": "Please approve.",
        "department": department,
    })
    until = awaiting_until or datetime.now(UTC) - timedelta(minutes=1)  # already expired
    wf_persistence.save_checkpoint(
        run_id, state, person_id, until, resume_state_json=resume_state
    )


# ---------------------------------------------------------------------------
# apply_resolution
# ---------------------------------------------------------------------------

def test_apply_resolution_marks_resolved() -> None:
    _seed_awaiting("run-1", person_id=5)
    resolution = WaitForHumanResolution(
        run_id="run-1",
        reply_text="approved",
        source_channel="slack",
        source_message_id="msg-1",
        parsed_decision={"decision": "approve", "note": ""},
        person_id=5,
    )
    result = asyncio.run(apply_resolution("run-1", resolution))
    assert result is True

    run = wf_persistence.get_run("run-1")
    assert run is not None
    assert run["status"] == "resolved"
    loaded_res = json.loads(run["resolution_json"])
    assert loaded_res["parsed_decision"]["decision"] == "approve"


def test_apply_resolution_is_idempotent() -> None:
    _seed_awaiting("run-2", person_id=3)
    resolution = WaitForHumanResolution(
        run_id="run-2",
        reply_text="ok",
        source_channel="telegram",
        parsed_decision={"decision": "approve", "note": ""},
        person_id=3,
    )
    first = asyncio.run(apply_resolution("run-2", resolution))
    second = asyncio.run(apply_resolution("run-2", resolution))
    assert first is True
    assert second is False  # run is no longer awaiting_human


def test_apply_resolution_writes_audit_log() -> None:
    from openexecutive.audit.logger import AuditLogger, set_audit_logger
    db = episodic.DB_PATH  # already monkeypatched to tmp_path in autouse fixture
    audit_logger = AuditLogger(db)
    set_audit_logger(audit_logger)

    _seed_awaiting("run-3", person_id=7)
    resolution = WaitForHumanResolution(
        run_id="run-3",
        reply_text="yes",
        source_channel="slack",
        parsed_decision={"decision": "approve", "note": ""},
        person_id=7,
    )
    asyncio.run(apply_resolution("run-3", resolution))

    events = audit_logger.query(event_type="human_resolution", limit=5)
    assert any("run-3" in str(e.summary) for e in events)


# ---------------------------------------------------------------------------
# Timeout handling
# ---------------------------------------------------------------------------

def test_timeout_escalate_creates_alert() -> None:
    dept_store.seed_default_departments()
    principal_id = people_store.upsert_person(full_name="Founder", is_principal=True)
    from openexecutive.people.models import AuthorityScope
    people_store.set_authority_scope(principal_id, [AuthorityScope.WILDCARD])

    _seed_awaiting("run-esc", person_id=principal_id, on_timeout="escalate", department="finance")
    run = wf_persistence.list_awaiting_runs()[0]
    asyncio.run(_handle_timeout(run, datetime.now(UTC)))

    run_after = wf_persistence.get_run("run-esc")
    assert run_after is not None
    assert run_after["status"] == "timed_out"


def test_timeout_fail_sets_error_status() -> None:
    _seed_awaiting("run-fail", person_id=1, on_timeout="fail")
    run = wf_persistence.list_awaiting_runs()[0]
    asyncio.run(_handle_timeout(run, datetime.now(UTC)))

    run_after = wf_persistence.get_run("run-fail")
    assert run_after is not None
    assert run_after["status"] == "error"


def test_timeout_auto_proceed_marks_resolved() -> None:
    _seed_awaiting("run-auto", person_id=2, on_timeout="auto_proceed")
    run = wf_persistence.list_awaiting_runs()[0]
    asyncio.run(_handle_timeout(run, datetime.now(UTC)))

    run_after = wf_persistence.get_run("run-auto")
    assert run_after is not None
    assert run_after["status"] == "resolved"
    loaded_res = json.loads(run_after["resolution_json"])
    assert loaded_res["parsed_decision"]["decision"] == "auto_proceed"


# ---------------------------------------------------------------------------
# _tick — poll cycle
# ---------------------------------------------------------------------------

def test_tick_processes_expired_runs() -> None:
    past = datetime.now(UTC) - timedelta(hours=1)
    future = datetime.now(UTC) + timedelta(hours=24)

    _seed_awaiting("expired", person_id=1, on_timeout="fail", awaiting_until=past)
    _seed_awaiting("not-yet", person_id=1, on_timeout="fail", awaiting_until=future)

    asyncio.run(_tick(datetime.now(UTC)))

    assert wf_persistence.get_run("expired")["status"] == "error"
    assert wf_persistence.get_run("not-yet")["status"] == "awaiting_human"


def test_tick_no_runs_is_no_op() -> None:
    # No awaiting runs — must not raise.
    asyncio.run(_tick(datetime.now(UTC)))


# ---------------------------------------------------------------------------
# Acknowledgement wording (#136)
# ---------------------------------------------------------------------------


def test_acknowledgement_names_the_decision_and_the_limit(tmp_path: Path) -> None:
    """"Got it — your response has been recorded." implied the workflow was
    now proceeding. It is not: apply_resolution stores the decision and
    releases the run, and nothing downstream runs (generator resume is
    unimplemented). Saying otherwise is the same class of overclaim #136 was
    filed for."""
    from openexecutive.workflows.resumer import resolution_acknowledgement

    db = tmp_path / "runs.db"
    wf_persistence.create_run("run-1", "vendor_review", "Vendor Review", {}, db_path=db)

    text = resolution_acknowledgement(
        "run-1",
        WaitForHumanResolution(
            run_id="run-1",
            reply_text="yes",
            source_channel="slack",
            parsed_decision={"decision": "approve", "note": ""},
            person_id=7,
        ),
        db_path=db,
    )

    assert text.startswith("Approved")
    assert "Vendor Review" in text
    assert "doesn't pick up from here on its own" in text


def test_acknowledgement_reflects_a_rejection(tmp_path: Path) -> None:
    from openexecutive.workflows.resumer import resolution_acknowledgement

    db = tmp_path / "runs.db"
    wf_persistence.create_run("run-1", "vendor_review", "Vendor Review", {}, db_path=db)

    text = resolution_acknowledgement(
        "run-1",
        WaitForHumanResolution(
            run_id="run-1",
            reply_text="no",
            source_channel="slack",
            parsed_decision={"decision": "reject", "note": "too expensive"},
            person_id=7,
        ),
        db_path=db,
    )

    assert text.startswith("Declined")


def test_acknowledgement_survives_a_missing_run(tmp_path: Path) -> None:
    from openexecutive.workflows.resumer import resolution_acknowledgement

    text = resolution_acknowledgement(
        "nope",
        WaitForHumanResolution(
            run_id="nope",
            reply_text="yes",
            source_channel="slack",
            parsed_decision={"decision": "approve"},
            person_id=7,
        ),
        db_path=tmp_path / "missing.db",
    )

    assert text.startswith("Approved")


# ---------------------------------------------------------------------------
# Resume — the background executor
# ---------------------------------------------------------------------------

def _resume_state_json(fingerprint: str = "") -> str:
    """A payload matching the definition `_real_workflow()` builds.

    Most of these tests replace `resume()` outright, so the fingerprint is
    never checked — but the one that doesn't would fail on a hardcoded value,
    and a literal here would silently rot the moment that definition changes.
    """
    return json.dumps({
        "version": 1,
        "engine": "dynamic",
        "workflow_name": "weekly_watch",
        "gate_step_id": "gate",
        "gate_step_index": 1,
        "steps_fingerprint": fingerprint,
        "outputs": {"research": ["Research", "prior output"]},
    })


_RESUME_STATE = _resume_state_json()


def _resolved(
    run_id: str = "res-1",
    decision: str = "approve",
    *,
    resume_state: str | None = _RESUME_STATE,
    workflow_name: str = "weekly_watch",
    inputs: dict | None = None,
) -> None:
    """A run sitting at `resolved` with (by default) a resume payload."""
    _seed_awaiting(
        run_id, 7, awaiting_until=datetime.now(UTC) + timedelta(hours=24),
        resume_state=resume_state, workflow_name=workflow_name,
        inputs=inputs if inputs is not None else {"topic": "pricing"},
    )
    wf_persistence.store_resolution(
        run_id,
        json.dumps({
            "run_id": run_id, "reply_text": "yes", "source_channel": "slack",
            "parsed_decision": {"decision": decision, "note": "fine"},
            "person_id": 7,
        }),
    )


def _real_workflow(events: list | None = None):  # noqa: ANN201
    """A genuine DynamicWorkflow whose `resume` is replaced.

    Deliberately not a duck-typed stub: `_execute_resume` guards on
    `isinstance(workflow, DynamicWorkflow)` to catch a dynamic name later
    shadowed by a built-in, and a fake would have to neuter that guard to be
    accepted — testing a path production never takes. This keeps the real
    type, the real input model, and records what resume() was handed.
    """
    from openexecutive.workflows.base import WorkflowEvent
    from openexecutive.workflows.dynamic import DynamicWorkflow
    from openexecutive.workflows.dynamic_models import DynamicWorkflowDef

    defn = DynamicWorkflowDef.model_validate({
        "name": "weekly_watch",
        "title": "Weekly Watch",
        "input_fields": [{"name": "topic", "label": "Topic", "required": False}],
        "steps": [
            {"kind": "specialist", "id": "research", "title": "Research",
             "specialist": "cso", "goal": "Analyze."},
            {"kind": "approval_gate", "id": "gate", "title": "Approve",
             "person_id": 7, "question": "OK?"},
            {"kind": "synthesis", "id": "assemble", "title": "Assemble"},
        ],
    })
    wf = DynamicWorkflow(defn)
    calls: list[dict] = []

    async def _resume(*, inputs, state, resolution, store):  # noqa: ANN001, ANN202
        calls.append({"state": state, "resolution": resolution, "inputs": inputs})
        for ev in (events if events is not None else [
            WorkflowEvent(type="artifact", content="# Final artifact")
        ]):
            yield ev

    wf.resume = _resume  # type: ignore[method-assign]
    wf.calls = calls  # type: ignore[attr-defined]
    return wf


def _install_stub(monkeypatch: pytest.MonkeyPatch, stub) -> None:  # noqa: ANN001
    """Point the executor at this workflow, with a no-op store and audit."""
    import openexecutive.audit as audit
    import openexecutive.knowledge.store as knowledge_store
    import openexecutive.workflows as wf_pkg

    monkeypatch.setattr(wf_pkg, "get_workflow", lambda name: stub)
    monkeypatch.setattr(knowledge_store, "ChromaDBStore", lambda *a, **k: object())
    monkeypatch.setattr(audit, "log_event", lambda *a, **k: None)


def test_resume_executes_remaining_steps_and_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The headline behaviour: a resolved run's remaining steps run and the
    artifact lands. Before this, the run simply stopped at `resolved`."""
    from openexecutive.workflows.resumer import _process_resumable

    _resolved()
    stub = _real_workflow()
    _install_stub(monkeypatch, stub)

    executed = asyncio.run(_process_resumable(datetime.now(UTC)))

    assert executed == 1
    run = wf_persistence.get_run("res-1")
    assert run["status"] == "done"
    assert run["artifact"] == "# Final artifact"
    # The payload is dropped so a finished run can never be re-claimed.
    assert run["resume_state_json"] is None
    # The engine was handed the recorded gate and the human's decision.
    assert stub.calls[0]["state"].gate_step_id == "gate"
    assert stub.calls[0]["state"].gate_step_index == 1
    assert stub.calls[0]["resolution"].parsed_decision["decision"] == "approve"


def test_resume_failure_is_recorded_as_a_failed_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A declined gate surfaces as an error event; the run must end at `error`
    rather than hanging in `running` with the claim still on it."""
    from openexecutive.workflows.base import WorkflowEvent
    from openexecutive.workflows.resumer import _process_resumable

    _resolved(decision="reject")
    _install_stub(monkeypatch, _real_workflow(
        [WorkflowEvent(type="error", message="Declined at the 'Approve' gate: fine")]
    ))

    asyncio.run(_process_resumable(datetime.now(UTC)))

    run = wf_persistence.get_run("res-1")
    assert run["status"] == "error"
    assert "Declined" in run["error"]
    assert run["resume_state_json"] is None


def test_pause_only_runs_are_never_claimed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A gate with no payload is finished at `resolved`. Claiming it would
    flip a completed run back to `running` with nothing to execute."""
    from openexecutive.workflows.resumer import _process_resumable

    _resolved(resume_state=None)
    _install_stub(monkeypatch, _real_workflow())

    assert asyncio.run(_process_resumable(datetime.now(UTC))) == 0
    assert wf_persistence.get_run("res-1")["status"] == "resolved"


def test_a_claimed_run_is_not_executed_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    """The immediate kick and the poll loop both reach the executor; the
    atomic claim is what stops a run being paid for twice."""
    from openexecutive.workflows.resumer import _process_resumable

    _resolved()
    stub = _real_workflow()
    _install_stub(monkeypatch, stub)

    asyncio.run(_process_resumable(datetime.now(UTC)))
    asyncio.run(_process_resumable(datetime.now(UTC)))

    assert len(stub.calls) == 1


def test_second_gate_recheckpoints_instead_of_completing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A workflow with two gates pauses again mid-resume. The run must go back
    to `awaiting_human` with the NEW payload and the OLD resolution cleared —
    otherwise the executor would re-claim it on gate 1's answer."""
    from openexecutive.workflows.resumer import _process_resumable
    from openexecutive.workflows.wait_for_human import (
        WaitForHumanEvent,
        WorkflowResumeState,
    )

    _resolved()
    second = WaitForHumanEvent(
        person_id=7, question="Ship it?",
        resume_state=WorkflowResumeState(
            workflow_name="weekly_watch", gate_step_id="gate2",
            gate_step_index=3, next_step_index=4, outputs={},
        ),
    )
    _install_stub(monkeypatch, _real_workflow([second]))

    async def _deliver(event, **_):  # noqa: ANN001, ANN202
        return event.model_copy(update={"delivery": "sent"}), "sent"

    monkeypatch.setattr(
        "openexecutive.workflows.gate_delivery.deliver_gate_question", _deliver
    )

    asyncio.run(_process_resumable(datetime.now(UTC)))

    run = wf_persistence.get_run("res-1")
    assert run["status"] == "awaiting_human"
    assert json.loads(run["resume_state_json"])["gate_step_id"] == "gate2"
    assert run["resolution_json"] is None
    assert run["artifact"] is None


def test_resume_fails_when_the_workflow_was_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A custom workflow can be deleted while a run sits at its gate."""
    import openexecutive.workflows as wf_pkg
    from openexecutive.workflows.resumer import _process_resumable

    _resolved()

    def _missing(name: str):  # noqa: ANN202
        raise KeyError(name)

    monkeypatch.setattr(wf_pkg, "get_workflow", _missing)
    monkeypatch.setattr(
        "openexecutive.workflows.dynamic_store.get_definition", lambda name: None
    )

    asyncio.run(_process_resumable(datetime.now(UTC)))

    run = wf_persistence.get_run("res-1")
    assert run["status"] == "error"
    assert "deleted" in run["error"]
    assert run["resume_state_json"] is None


def test_resume_says_deactivated_when_the_definition_still_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deleted and switched-off need different things from the reader."""
    import openexecutive.workflows as wf_pkg
    from openexecutive.workflows.resumer import _process_resumable

    _resolved()

    def _missing(name: str):  # noqa: ANN202
        raise KeyError(name)

    monkeypatch.setattr(wf_pkg, "get_workflow", _missing)
    monkeypatch.setattr(
        "openexecutive.workflows.dynamic_store.get_definition",
        lambda name: object(),
    )

    asyncio.run(_process_resumable(datetime.now(UTC)))

    assert "deactivated" in wf_persistence.get_run("res-1")["error"]


def test_unreadable_payload_fails_the_run_rather_than_the_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.workflows.resumer import _process_resumable

    _resolved(resume_state="{not json")
    _install_stub(monkeypatch, _real_workflow())

    asyncio.run(_process_resumable(datetime.now(UTC)))

    run = wf_persistence.get_run("res-1")
    assert run["status"] == "error"
    assert "could not be read" in run["error"]


def test_auto_proceed_timeout_executes_in_the_same_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`on_timeout='auto_proceed'` was inert: it synthesized a resolution and
    nothing ran. `_tick` calls the executor AFTER the timeout loop, which is
    what finally makes it proceed — with no special-casing anywhere."""
    _seed_awaiting(
        "auto-1", 7, on_timeout="auto_proceed",
        resume_state=_RESUME_STATE, workflow_name="weekly_watch",
        inputs={"topic": "pricing"},
    )
    stub = _real_workflow()
    _install_stub(monkeypatch, stub)

    asyncio.run(_tick(datetime.now(UTC)))

    run = wf_persistence.get_run("auto-1")
    assert run["status"] == "done"
    assert run["artifact"] == "# Final artifact"
    assert stub.calls[0]["resolution"].parsed_decision["decision"] == "auto_proceed"


def test_escalate_timeout_drops_the_resume_payload() -> None:
    """`timed_out` is terminal — nobody answered, so the payload will never be
    replayed and must not keep the run on the resumable queue."""
    _seed_awaiting("esc-1", 7, on_timeout="escalate", resume_state=_RESUME_STATE)

    asyncio.run(_tick(datetime.now(UTC)))

    run = wf_persistence.get_run("esc-1")
    assert run["status"] == "timed_out"
    assert run["resume_state_json"] is None


def test_a_stranded_resume_is_requeued_and_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Crash recovery: a worker that died after claiming leaves the run in
    `running`, which no other sweep looks at."""
    from openexecutive.workflows.resumer import _RESUME_STALE_AFTER, _process_resumable

    _resolved()
    wf_persistence.claim_run_for_resume("res-1")  # simulate the dead worker
    assert wf_persistence.get_run("res-1")["status"] == "running"

    stub = _real_workflow()
    _install_stub(monkeypatch, stub)
    later = datetime.now(UTC) + _RESUME_STALE_AFTER + timedelta(minutes=1)

    assert asyncio.run(_process_resumable(later)) == 1
    assert wf_persistence.get_run("res-1")["status"] == "done"


def test_acknowledgement_says_the_run_is_resuming() -> None:
    """#143 shipped wording promising the opposite; a resumable gate must not
    tell the approver to come back and ask for the next step themselves."""
    from openexecutive.workflows.resumer import resolution_acknowledgement

    _resolved()
    ack = resolution_acknowledgement(
        "res-1",
        WaitForHumanResolution(
            run_id="res-1", reply_text="yes", source_channel="slack",
            parsed_decision={"decision": "approve"}, person_id=7,
        ),
    )
    assert "Approved" in ack
    assert "picking up" in ack
    assert "doesn't pick up from here on its own" not in ack


def test_acknowledgement_says_the_run_stops_on_a_decline() -> None:
    from openexecutive.workflows.resumer import resolution_acknowledgement

    _resolved(decision="reject")
    ack = resolution_acknowledgement(
        "res-1",
        WaitForHumanResolution(
            run_id="res-1", reply_text="no", source_channel="slack",
            parsed_decision={"decision": "reject"}, person_id=7,
        ),
    )
    assert "Declined" in ack
    assert "stops there" in ack


def test_acknowledgement_keeps_the_old_wording_for_a_pause_only_gate() -> None:
    """Nothing continues a gate with no payload, and saying otherwise would be
    the same lie in the other direction."""
    from openexecutive.workflows.resumer import resolution_acknowledgement

    _resolved(resume_state=None)
    ack = resolution_acknowledgement(
        "res-1",
        WaitForHumanResolution(
            run_id="res-1", reply_text="yes", source_channel="slack",
            parsed_decision={"decision": "approve"}, person_id=7,
        ),
    )
    assert "doesn't pick up from here on its own" in ack


def test_resume_refuses_a_workflow_that_is_no_longer_dynamic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`get_workflow` checks the built-in registry first, so a custom
    workflow's name can be shadowed by a built-in shipped later. Running the
    built-in's steps against this run's payload would be nonsense."""
    import openexecutive.audit as audit
    import openexecutive.knowledge.store as knowledge_store
    import openexecutive.workflows as wf_pkg
    from openexecutive.workflows import WORKFLOW_REGISTRY
    from openexecutive.workflows.resumer import _process_resumable

    _resolved()
    builtin = WORKFLOW_REGISTRY["board_prep"]
    monkeypatch.setattr(wf_pkg, "get_workflow", lambda name: builtin)
    monkeypatch.setattr(knowledge_store, "ChromaDBStore", lambda *a, **k: object())
    monkeypatch.setattr(audit, "log_event", lambda *a, **k: None)

    asyncio.run(_process_resumable(datetime.now(UTC)))

    run = wf_persistence.get_run("res-1")
    assert run["status"] == "error"
    assert "no longer a custom workflow" in run["error"]


def test_a_crash_mid_resume_fails_the_run_on_both_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The kick and the poll must agree. An earlier version had the poll fail
    the run while the kick only logged, which left a crashed run sitting in
    `running` waiting on a 30-minute sweep — a different outcome for the same
    failure depending on which entry point happened to win the claim."""
    from openexecutive.workflows.resumer import _process_resumable

    _resolved()
    wf = _real_workflow()

    async def _boom(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("the model fell over")
        yield  # pragma: no cover - makes this an async generator

    wf.resume = _boom  # type: ignore[method-assign]
    _install_stub(monkeypatch, wf)

    asyncio.run(_process_resumable(datetime.now(UTC)))

    run = wf_persistence.get_run("res-1")
    assert run["status"] == "error"
    # Off the resumable queue, so it is not retried forever.
    assert run["resume_state_json"] is None


def test_a_gate_as_the_final_step_fails_rather_than_hanging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`validate_definition` requires synthesis last, but a definition stored
    before that rule can still reach the engine. Resuming past a trailing gate
    runs no steps and produces no artifact; the run must end at `error` with a
    legible reason instead of silently reporting success."""
    from openexecutive.workflows.resumer import _process_resumable

    _resolved()
    # next_step_index == len(steps): the loop body never executes.
    _install_stub(monkeypatch, _real_workflow(events=[]))

    asyncio.run(_process_resumable(datetime.now(UTC)))

    run = wf_persistence.get_run("res-1")
    assert run["status"] == "error"
    assert "without producing an artifact" in run["error"]


def test_a_run_past_its_retry_budget_is_ended(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exceeding the cap must be terminal, not a disappearance. The row is
    `running` with a payload, so it is in neither queue — without this it shows
    as in-progress on /jobs forever, with nothing coming back for it."""
    from openexecutive.workflows.resumer import (
        _MAX_RESUME_ATTEMPTS,
        _RESUME_STALE_AFTER,
        _process_resumable,
    )

    _resolved()
    for _ in range(_MAX_RESUME_ATTEMPTS):
        wf_persistence.claim_run_for_resume("res-1")
        wf_persistence.requeue_run_for_resume("res-1")
    wf_persistence.claim_run_for_resume("res-1")  # the claim that never returns
    assert wf_persistence.get_run("res-1")["status"] == "running"

    _install_stub(monkeypatch, _real_workflow())
    later = datetime.now(UTC) + _RESUME_STALE_AFTER + timedelta(minutes=1)
    asyncio.run(_process_resumable(later))

    run = wf_persistence.get_run("res-1")
    assert run["status"] == "error"
    assert "attempts" in run["error"]
    assert run["resume_state_json"] is None


def test_a_reply_arriving_mid_timeout_is_not_discarded() -> None:
    """`_tick` lists expired rows, then acts on them. A reply landing in that
    gap already moved the run to `resolved`; every write in the escalate branch
    is unguarded, so continuing would strip the payload off a run that is
    queued to resume — approval recorded, never executed."""
    from openexecutive.workflows.resumer import _handle_timeout

    _seed_awaiting("race-1", 7, on_timeout="escalate", resume_state=_RESUME_STATE)
    run = dict(wf_persistence.list_awaiting_runs()[0])  # as _tick would see it

    # The human answers before _handle_timeout gets there.
    wf_persistence.store_resolution("race-1", json.dumps({"decision": "approve"}))

    asyncio.run(_handle_timeout(run, datetime.now(UTC)))

    row = wf_persistence.get_run("race-1")
    assert row["status"] == "resolved", "the human's answer must stand"
    assert row["resume_state_json"] is not None, "and it must still be resumable"


def test_a_reply_arriving_mid_fail_timeout_is_not_discarded() -> None:
    """Same race, the `on_timeout='fail'` branch — `fail_run` has no status
    guard, so it would overwrite the resolution with `error`."""
    from openexecutive.workflows.resumer import _handle_timeout

    _seed_awaiting("race-2", 7, on_timeout="fail", resume_state=_RESUME_STATE)
    run = dict(wf_persistence.list_awaiting_runs()[0])
    wf_persistence.store_resolution("race-2", json.dumps({"decision": "approve"}))

    asyncio.run(_handle_timeout(run, datetime.now(UTC)))

    assert wf_persistence.get_run("race-2")["status"] == "resolved"


def test_a_second_gate_restores_the_retry_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end: reaching gate 2 resets `resume_attempts`, so a multi-gate
    workflow does not arrive at its last gate with no crash-recovery budget
    left having never failed at anything."""
    from openexecutive.workflows.resumer import _process_resumable
    from openexecutive.workflows.wait_for_human import (
        WaitForHumanEvent,
        WorkflowResumeState,
    )

    _resolved()
    second = WaitForHumanEvent(
        person_id=7, question="Ship it?",
        resume_state=WorkflowResumeState(
            workflow_name="weekly_watch", gate_step_id="gate2",
            gate_step_index=3, steps_fingerprint="abc", outputs={},
        ),
    )
    _install_stub(monkeypatch, _real_workflow([second]))

    async def _deliver(event, **_):  # noqa: ANN001, ANN202
        return event.model_copy(update={"delivery": "sent"}), "sent"

    monkeypatch.setattr(
        "openexecutive.workflows.gate_delivery.deliver_gate_question", _deliver
    )

    asyncio.run(_process_resumable(datetime.now(UTC)))

    import sqlite3
    conn = sqlite3.connect(str(wf_persistence.DB_PATH))
    attempts = conn.execute(
        "SELECT resume_attempts FROM workflow_runs WHERE run_id = 'res-1'"
    ).fetchone()[0]
    conn.close()
    assert attempts == 0


def test_a_second_gate_pause_is_not_counted_as_a_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`executed` is logged as "executed N resumed run(s)"; a run that merely
    parked again has not been executed."""
    from openexecutive.workflows.resumer import _process_resumable
    from openexecutive.workflows.wait_for_human import (
        WaitForHumanEvent,
        WorkflowResumeState,
    )

    _resolved()
    second = WaitForHumanEvent(
        person_id=7, question="Ship it?",
        resume_state=WorkflowResumeState(
            workflow_name="weekly_watch", gate_step_id="gate2",
            gate_step_index=3, steps_fingerprint="abc", outputs={},
        ),
    )
    _install_stub(monkeypatch, _real_workflow([second]))

    async def _deliver(event, **_):  # noqa: ANN001, ANN202
        return event.model_copy(update={"delivery": "sent"}), "sent"

    monkeypatch.setattr(
        "openexecutive.workflows.gate_delivery.deliver_gate_question", _deliver
    )

    assert asyncio.run(_process_resumable(datetime.now(UTC))) == 0


# ---------------------------------------------------------------------------
# Heartbeat — a long resume (workflow action steps) must never look dead
# ---------------------------------------------------------------------------


def test_touch_resume_claim_only_for_the_live_claim() -> None:
    _resolved()
    token = wf_persistence.claim_run_for_resume("res-1")
    assert token is not None
    before = wf_persistence.get_run("res-1")["resumed_at"]
    assert wf_persistence.touch_resume_claim("res-1", token) is True
    assert wf_persistence.get_run("res-1")["resumed_at"] >= before
    assert wf_persistence.touch_resume_claim("res-1", "not-the-claim") is False


def test_heartbeat_keeps_a_long_resume_off_the_stale_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Events refresh `resumed_at`, so the stale sweep never requeues (and
    replays the side effects of) a resume that is still working."""
    from openexecutive.workflows import resumer
    from openexecutive.workflows.base import WorkflowEvent

    monkeypatch.setattr(resumer, "_RESUME_HEARTBEAT_EVERY", timedelta(0))
    _resolved()
    wf = _real_workflow()
    seen_stale: list[list[str]] = []

    async def _resume(*, inputs, state, resolution, store):  # noqa: ANN001, ANN202
        # Pretend this worker has been busy for longer than the stale window.
        with wf_persistence._get_conn(wf_persistence._resolve(None)) as conn:
            conn.execute(
                "UPDATE workflow_runs SET resumed_at = ? WHERE run_id = 'res-1'",
                ((datetime.now(UTC) - timedelta(days=1)).isoformat(),),
            )
        yield WorkflowEvent(type="progress", step_id="act", summary="Using a tool…")
        seen_stale.append(
            wf_persistence.list_stale_resuming_runs(
                datetime.now(UTC) - resumer._RESUME_STALE_AFTER, 3
            )
        )
        yield WorkflowEvent(type="artifact", content="# Done")

    wf.resume = _resume  # type: ignore[method-assign]
    _install_stub(monkeypatch, wf)
    asyncio.run(resumer._process_resumable(datetime.now(UTC)))
    assert seen_stale == [[]]
    assert wf_persistence.get_run("res-1")["status"] == "done"


def test_a_superseded_resume_stops_before_acting_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the sweep handed the run to another worker, this one must stop at
    its next event — not carry on sending/writing a second time."""
    from openexecutive.workflows import resumer
    from openexecutive.workflows.base import WorkflowEvent

    monkeypatch.setattr(resumer, "_RESUME_HEARTBEAT_EVERY", timedelta(0))
    _resolved()
    wf = _real_workflow()
    reached: list[str] = []

    async def _resume(*, inputs, state, resolution, store):  # noqa: ANN001, ANN202
        yield WorkflowEvent(type="progress", step_id="act", summary="Using x…")
        wf_persistence.requeue_run_for_resume("res-1")  # the sweep gave up on us…
        wf_persistence.claim_run_for_resume("res-1")  # …and another worker took it
        yield WorkflowEvent(type="progress", step_id="act", summary="Using y…")
        reached.append("kept going")
        yield WorkflowEvent(type="artifact", content="# must not land")

    wf.resume = _resume  # type: ignore[method-assign]
    _install_stub(monkeypatch, wf)
    assert asyncio.run(resumer._process_resumable(datetime.now(UTC))) == 0
    assert reached == []
    run = wf_persistence.get_run("res-1")
    assert run["status"] == "running" and run["artifact"] is None


def test_a_busy_database_on_heartbeat_does_not_kill_the_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlite3

    from openexecutive.workflows import resumer
    from openexecutive.workflows.base import WorkflowEvent

    monkeypatch.setattr(resumer, "_RESUME_HEARTBEAT_EVERY", timedelta(0))

    def _locked(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(wf_persistence, "touch_resume_claim", _locked)
    _resolved()
    wf = _real_workflow(
        [
            WorkflowEvent(type="progress", step_id="act", summary="Using x…"),
            WorkflowEvent(type="artifact", content="# Done"),
        ]
    )
    _install_stub(monkeypatch, wf)
    assert asyncio.run(resumer._process_resumable(datetime.now(UTC))) == 1
    assert wf_persistence.get_run("res-1")["status"] == "done"


def test_requeue_with_cutoff_spares_a_freshly_heartbeated_run() -> None:
    """The sweep reads stale ids, then requeues; a heartbeat landing in
    between must win, or a live worker's run is handed to a second one."""
    _resolved()
    token = wf_persistence.claim_run_for_resume("res-1")
    assert token is not None
    cutoff = datetime.now(UTC) - timedelta(minutes=90)
    wf_persistence.touch_resume_claim("res-1", token)  # fresh heartbeat
    assert wf_persistence.requeue_run_for_resume("res-1", stale_before=cutoff) is False
    assert wf_persistence.get_run("res-1")["resume_claim"] == token
    # A genuinely stale claim is still requeued.
    assert wf_persistence.requeue_run_for_resume(
        "res-1", stale_before=datetime.now(UTC) + timedelta(minutes=1)
    ) is True
