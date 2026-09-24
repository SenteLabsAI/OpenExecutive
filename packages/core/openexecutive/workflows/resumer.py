"""Background resumer loop — timeouts, and continuing runs past their gate.

Mirrors ``scheduler/runner.py``'s polling pattern.

What it does
------------
* **Timeouts**: when ``awaiting_until <= now``, apply the ``on_timeout``
  policy (escalate / auto_proceed / fail).
* ``apply_resolution``: called by the inbound resolver (Slack / Discord /
  Telegram) when a human replies. Stores the resolution, marks the run
  ``resolved``, writes an audit entry.  Idempotent — a second call on the
  same run_id is a no-op (returns False).
* **Resume**: a ``resolved`` run carrying a ``resume_state_json`` payload is
  claimed (``resolved`` -> ``running``) and driven to completion — the steps
  after the gate actually execute and produce the artifact.

How resume works
----------------
Nothing serialises a Python generator frame; that assumption is what deferred
this for so long.  The engine hands out a small JSON payload at the gate (see
``WorkflowResumeState``) and ``DynamicWorkflow.resume`` replays its step loop
from the recorded index.  This module supplies the missing half: the human
replies hours later in a different process, with the original SSE connection
long gone, so *something* has to pick the run back up.  That something is
``_process_resumable``, reached two ways:

1. Immediately, via a task ``apply_resolution`` kicks off — so a Slack
   approval starts work in seconds rather than at the next poll.
2. From ``_tick``, 60s at a time — the durable backstop for a kick that never
   ran (process restart) or crashed.

Both go through ``claim_run_for_resume``, one guarded UPDATE, so whichever
gets there first wins and the other does nothing.

Concurrency
-----------
Single-worker *by intent*, not by safety: ``api/main.py`` starts this loop
unconditionally, so two API replicas means two resumers.  The atomic claim
makes that safe — a run executes once — but the loop now does real, billable
specialist work rather than flipping statuses, so running one per database is
still the deployment contract.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import openexecutive.workflows.persistence as _wf_persistence
from openexecutive.workflows.wait_for_human import (
    CONTINUE_DECISIONS,
    DECISION_VERBS,
    WaitForHumanResolution,
    WorkflowResumeState,
)

# How long a claimed resume may sit unfinished before we assume its worker
# died and requeue it.
#
# This is a wall-clock guess, not a lease, so it must clear the worst case
# comfortably or it forks live work: a definition may hold up to _MAX_STEPS
# (12) specialist consults, each able to run to the specialist timeout, which
# is well over half an hour before any retry/backoff. 30 minutes sat inside
# that range. The fencing token makes a wrong guess survivable — the
# superseded worker's writes are refused — but duplicated specialist calls are
# still billed, so the window is set beyond any plausible real run.
_RESUME_STALE_AFTER = timedelta(minutes=90)
# Bound on those requeues, so a run that reliably kills its worker stops
# rather than looping forever.
_MAX_RESUME_ATTEMPTS = 3
# A live resume refreshes its claim (``touch_resume_claim``) at most this
# often while events flow — each action-step tool call emits one — so a long
# but healthy run never ages past _RESUME_STALE_AFTER and is never replayed.
_RESUME_HEARTBEAT_EVERY = timedelta(minutes=1)

logger = logging.getLogger(__name__)


async def apply_resolution(
    run_id: str,
    resolution: WaitForHumanResolution,
    db_path: Path | None = None,
) -> bool:
    """Persist a human resolution and mark the run resolved.

    Idempotent: if the run is already ``resolved`` (or not ``awaiting_human``),
    returns ``False`` without modifying any row.
    """
    from openexecutive.audit import log_event as audit_log

    resolution_dict = resolution.model_dump()
    resolution_dict["resolved_at"] = resolution.resolved_at or datetime.now(UTC).isoformat()
    resolution_json = json.dumps(resolution_dict)

    updated = _wf_persistence.store_resolution(run_id, resolution_json, db_path=db_path)
    if not updated:
        logger.info("resumer.apply_resolution: run %s not awaiting_human (no-op)", run_id)
        return False

    # The approver decided — however they did it (web, Slack, a reply) — so
    # the nudges chasing this approval landed.
    from openexecutive.attunement.outcomes import OUTCOME_ACTED, resolve_by_ref

    # The ledger lives in the episodic store, not necessarily at `db_path`.
    # Credited to whoever resolved it, like alerts — not to everyone nudged.
    resolve_by_ref(
        f"nudge:stalled:{run_id}", OUTCOME_ACTED,
        person_ids={resolution.person_id} if resolution.person_id is not None else None,
    )

    audit_log(
        "human_resolution",
        f"WaitForHuman resolved: run_id={run_id} person={resolution.person_id} "
        f"channel={resolution.source_channel} "
        f"decision={resolution.parsed_decision.get('decision', '?')}",
        actor=f"person:{resolution.person_id}",
        details={
            "run_id": run_id,
            "person_id": resolution.person_id,
            "source_channel": resolution.source_channel,
            "source_message_id": resolution.source_message_id,
            "reply_text": resolution.reply_text[:300],
            "parsed_decision": resolution.parsed_decision,
        },
    )
    logger.info(
        "resumer.apply_resolution: run %s resolved by person %d",
        run_id, resolution.person_id,
    )

    # A resumable gate should not wait up to a poll interval to start moving —
    # someone is sitting in Slack having just approved it. The kick shares the
    # atomic claim with the poll loop, so this is a latency optimisation, not
    # a second execution path: if it never runs, `_tick` picks the run up.
    run = _wf_persistence.get_run(run_id, db_path=db_path)
    if run and run.get("resume_state_json"):
        _kick_resume(run_id, db_path=db_path)

    return True


# How a recorded decision reads back to the person. What follows the verb
# depends on whether the run can actually continue, because saying the wrong
# one is the same class of lie #136 was reported for — in either direction.
# A pause-only gate that claims the workflow is resuming is as wrong as a
# resumable one that tells the approver to come back and ask for the next
# step themselves.
#
# Vocabulary lives in wait_for_human so the acknowledgement and the artifact
# section a resumed run writes cannot disagree about what the human said.
_ACK_RESUMING = (
    "That's recorded, and the workflow is picking up from there now — "
    "I'll bring you the finished result."
)
_ACK_STOPPED = (
    "That's recorded, and the run stops there — nothing after the sign-off "
    "will be produced. Say the word and I'll start a fresh run when you want "
    "it."
    # Deliberately NOT "tell me if you'd rather I take it forward anyway":
    # there is no path out of `error`, and the resume payload is cleared on
    # the way there. Offering a follow-up nothing can deliver is the same
    # class of lie as the wording this replaced.
)
_ACK_PAUSE_ONLY = (
    "That's recorded against the sign-off and closes it out. The workflow "
    "doesn't pick up from here on its own, so tell me if you want me to take "
    "the next step."
)


def resolution_acknowledgement(
    run_id: str,
    resolution: WaitForHumanResolution,
    db_path: Path | None = None,
) -> str:
    """The reply a person gets after their answer resolves a gate."""
    decision = str(resolution.parsed_decision.get("decision") or "")
    verb = DECISION_VERBS.get(decision, "Recorded")

    title = ""
    resumable = False
    try:
        run = _wf_persistence.get_run(run_id, db_path=db_path)
        if run:
            title = str(run.get("title") or run.get("workflow_name") or "")
            resumable = bool(run.get("resume_state_json"))
    except Exception:
        logger.exception("resumer: could not read run %s for acknowledgement", run_id)

    if not resumable:
        tail = _ACK_PAUSE_ONLY
    elif decision not in CONTINUE_DECISIONS:
        # Mirrors the engine: anything that is not a recognised approval stops
        # the run, so the acknowledgement must say so rather than promising
        # work that will not happen.
        tail = _ACK_STOPPED
    else:
        tail = _ACK_RESUMING

    subject = f" — {title}" if title else ""
    return f"{verb}{subject}. {tail}"


# Strong refs to in-flight kick tasks. asyncio only holds a weak reference to
# a task, so without this the garbage collector can cancel a resume mid-run.
_KICK_TASKS: set[asyncio.Task[None]] = set()


def _abandon_resume(run_id: str, reason: str, db_path: Path | None = None) -> None:
    """End a run whose resume crashed, and take it off the resumable queue.

    Both entry points use this so they cannot diverge. Failing fast rather
    than retrying is deliberate: the run has already paid for its pre-gate
    specialist calls, a retry re-pays for every step after the gate with no
    guarantee of a different outcome, and a silent 30-minute retry loop hides
    a broken workflow from the person who approved it. The stale-resume sweep
    is left for the one case it was built for — the worker DIED, so no handler
    ran at all.
    """
    import contextlib

    # `db_path` on BOTH: passing it to one and not the other would write the
    # terminal row to the default DB while clearing the payload in the real
    # one, leaving the run `running` with no payload — in neither queue, which
    # is precisely the stranding this function exists to prevent.
    with contextlib.suppress(Exception):
        _wf_persistence.fail_run(run_id, reason, db_path=db_path)
    with contextlib.suppress(Exception):
        _wf_persistence.clear_resume_state(run_id, db_path=db_path)


def _kick_resume(run_id: str, db_path: Path | None = None) -> None:
    """Start executing a just-resolved run now, instead of at the next poll.

    Fire-and-forget by design: the caller is an inbound chat handler that must
    answer the human promptly, not wait on specialist calls. If the task never
    runs or dies, `_process_resumable` picks the run up within a tick — the
    claim is shared, so the two can never both execute it.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - no loop in a sync caller
        logger.debug("resumer: no running loop, leaving run %s to the poll", run_id)
        return

    async def _run() -> None:
        try:
            claim = _wf_persistence.claim_run_for_resume(run_id, db_path=db_path)
            if claim is None:
                return
            row = _load_resumable_row(run_id, db_path=db_path)
            if row is not None:
                await _execute_resume(row, claim, db_path=db_path)
        except Exception:
            logger.exception("resumer: kicked resume failed for run_id=%s", run_id)
            _abandon_resume(run_id, "resume failed unexpectedly", db_path=db_path)

    task = loop.create_task(_run())
    _KICK_TASKS.add(task)
    task.add_done_callback(_KICK_TASKS.discard)


def _load_resumable_row(run_id: str, db_path: Path | None = None) -> dict | None:
    """The row shape `_execute_resume` expects, for a single claimed run."""
    run = _wf_persistence.get_run(run_id, db_path=db_path)
    if run is None or not run.get("resume_state_json"):
        return None
    return {
        "run_id": run_id,
        "workflow_name": run.get("workflow_name") or "",
        "title": run.get("title") or "",
        "inputs": run.get("inputs") or {},
        "resume_state_json": run["resume_state_json"],
        "resolution_json": run.get("resolution_json"),
    }


async def _process_resumable(now: datetime, db_path: Path | None = None) -> int:
    """Claim and execute every resolved run that carries a resume payload.

    Returns how many runs were executed. Per-run exception isolation, matching
    `_handle_timeout`: one bad run must never stop the sweep.
    """
    import sqlite3

    # Crash recovery first, so a run stranded by a dead worker is back in the
    # queue before we read it.
    cutoff = now - _RESUME_STALE_AFTER
    for stale_id in _wf_persistence.list_stale_resuming_runs(
        cutoff, _MAX_RESUME_ATTEMPTS, db_path=db_path
    ):
        if _wf_persistence.requeue_run_for_resume(
            stale_id, db_path=db_path, stale_before=cutoff
        ):
            logger.warning(
                "resumer: run %s was claimed for resume but never finished — requeued",
                stale_id,
            )

    # A run past its retry budget leaves the stale query but is still
    # `running` with a payload, so nothing else would ever look at it again.
    # End it, loudly, rather than letting it show as in-progress forever.
    for dead_id in _wf_persistence.list_exhausted_resuming_runs(
        cutoff, _MAX_RESUME_ATTEMPTS, db_path=db_path
    ):
        logger.error(
            "resumer: run %s failed to resume after %d attempts — giving up",
            dead_id, _MAX_RESUME_ATTEMPTS,
        )
        _abandon_resume(
            dead_id,
            f"resume did not complete after {_MAX_RESUME_ATTEMPTS} attempts",
            db_path=db_path,
        )

    executed = 0
    for row in _wf_persistence.list_resumable_runs(db_path=db_path):
        run_id = row["run_id"]
        try:
            claim = _wf_persistence.claim_run_for_resume(run_id, db_path=db_path)
        except sqlite3.OperationalError:
            # The DB is shared with the scheduler, the API and the chat bots.
            # A lock here means "someone else is writing", not "this run is
            # broken" — leave it for the next tick rather than failing it.
            logger.warning("resumer: database busy claiming run %s — next tick", run_id)
            continue
        if claim is None:
            continue  # a kick, or another worker, got there first
        try:
            if await _execute_resume(row, claim, db_path=db_path):
                executed += 1
        except Exception:
            logger.exception("resumer: resume failed for run_id=%s", run_id)
            _abandon_resume(run_id, "resume failed unexpectedly", db_path=db_path)
    if executed:
        logger.info("resumer: executed %d resumed run(s)", executed)
    return executed


async def _execute_resume(
    row: dict, claim: str, db_path: Path | None = None
) -> bool:
    """Drive one claimed run's remaining steps to a terminal state.

    `claim` is the fencing token from `claim_run_for_resume`; every write here
    carries it, so if the stale sweep declared this worker dead and handed the
    run to another, our writes are refused rather than landing on theirs.

    Returns True when the run reached a terminal state, False when it paused
    again at a later gate or our claim was superseded — so the caller's
    "executed N" count means completions, not merely claims.
    """
    import contextlib
    import sqlite3

    from openexecutive.audit import log_event as audit_log
    from openexecutive.config import get_settings
    from openexecutive.knowledge.store import ChromaDBStore
    from openexecutive.workflows import get_workflow
    from openexecutive.workflows.dynamic import DynamicWorkflow
    from openexecutive.workflows.dynamic_store import get_definition
    from openexecutive.workflows.gate import (
        ClaimSupersededError,
        checkpoint_gate,
    )
    from openexecutive.workflows.wait_for_human import WaitForHumanEvent

    run_id = row["run_id"]
    name = row["workflow_name"]

    def _abort(reason: str) -> None:
        logger.warning("resumer: cannot resume run %s — %s", run_id, reason)
        _abandon_resume(run_id, reason, db_path=db_path)

    try:
        state = WorkflowResumeState.model_validate_json(row["resume_state_json"])
    except Exception:
        logger.exception("resumer: unreadable resume payload for run %s", run_id)
        _abort("the stored resume payload could not be read")
        return True

    resolution = _resolution_from_row(row, run_id)

    try:
        workflow = get_workflow(name)
    except KeyError:
        # Tell the two cases apart — "you deleted it" and "you switched it
        # off" need different things from the reader.
        definition = None
        with contextlib.suppress(Exception):
            definition = get_definition(name)
        _abort(
            f"the custom workflow {name!r} was "
            + ("deactivated" if definition is not None else "deleted")
            + " while this run was awaiting approval"
        )
        return True

    if not isinstance(workflow, DynamicWorkflow):
        # `get_workflow` checks the built-in registry first, so a dynamic
        # workflow's name can be shadowed by a built-in added later.
        _abort(
            f"{name!r} is no longer a custom workflow, so this run cannot be resumed"
        )
        return True

    try:
        inputs = workflow.input_model().model_validate(row["inputs"])
    except Exception as exc:
        _abort(f"the workflow's inputs no longer validate: {exc}")
        return True

    store = ChromaDBStore(persist_directory=get_settings().vector_store_path)
    artifact = ""
    last_error = ""
    last_heartbeat = datetime.now(UTC)
    # No handler here: a crash propagates to the caller, which routes it
    # through `_abandon_resume` — the one failure path both entry points share.
    async for event in workflow.resume(
        inputs=inputs, state=state, resolution=resolution, store=store
    ):
        if isinstance(event, WaitForHumanEvent):
            # A SECOND gate. Re-checkpoint through the same helper the
            # other runners use: that flips running -> awaiting_human,
            # stores the fresh payload, and clears gate 1's resolution so
            # this run is not immediately re-claimed on its old answer.
            # A scheduled run still owes its artifact to its recipient.
            if event.resume_state is not None and state.deliver_to_person_id is not None:
                event.resume_state.deliver_to_person_id = state.deliver_to_person_id
            try:
                await checkpoint_gate(
                    run_id=run_id,
                    event=event,
                    workflow_title=workflow.title,
                    db_path=db_path,
                    expect_claim=claim,
                )
            except ClaimSupersededError:
                # Another worker has already taken this run somewhere. Parking
                # it now would resurrect whatever they finished.
                logger.warning(
                    "resumer: run %s lost its claim before a later gate — "
                    "discarding this worker's result",
                    run_id,
                )
                return False
            audit_log(
                "workflow_resume",
                f"run {run_id} resumed and paused again at a later gate",
                actor="resumer",
                details={
                    "run_id": run_id,
                    "workflow": name,
                    "gate_step_id": state.gate_step_id,
                    "outcome": "awaiting_human",
                },
            )
            return False
        now = datetime.now(UTC)
        if now - last_heartbeat >= _RESUME_HEARTBEAT_EVERY:
            last_heartbeat = now
            try:
                still_ours = _wf_persistence.touch_resume_claim(run_id, claim, db_path=db_path)
            except sqlite3.OperationalError:
                # A busy shared DB is not a lost claim — keep working; the next
                # heartbeat (one interval later) retries. Only a definite
                # False means superseded.
                logger.warning("resumer: database busy on heartbeat for run %s", run_id)
                still_ours = True
            if not still_ours:
                # Superseded: another worker owns this run now. Stop before the
                # next step acts again (action steps have external effects).
                logger.warning(
                    "resumer: run %s lost its claim mid-resume — stopping this worker",
                    run_id,
                )
                return False
        if event.type == "artifact" and event.content:
            artifact = event.content
        elif event.type == "error" and event.message:
            last_error = event.message

    decision = str(resolution.parsed_decision.get("decision") or "")
    message = "" if artifact else (
        last_error or "resume finished without producing an artifact"
    )
    # One fenced write for both outcomes: it sets the status, clears the
    # payload, and refuses outright if this worker has been superseded.
    won = _wf_persistence.finish_resumed_run(
        run_id,
        claim,
        artifact=artifact or None,
        error=message or None,
        db_path=db_path,
    )
    if not won:
        logger.warning(
            "resumer: run %s lost its claim before finishing — result discarded",
            run_id,
        )
        return False
    outcome = "done" if artifact else "error"

    audit_log(
        "workflow_resume",
        f"run {run_id} resumed after {decision or 'a'} decision -> {outcome}",
        actor="resumer",
        details={
            "run_id": run_id,
            "workflow": name,
            "gate_step_id": state.gate_step_id,
            "decision": decision,
            "outcome": outcome,
            "artifact_chars": len(artifact),
        },
    )
    logger.info("resumer: run %s resumed -> %s", run_id, outcome)
    if artifact and state.deliver_to_person_id is not None:
        await _deliver_artifact(run_id, state.deliver_to_person_id, artifact)
    return True


async def _deliver_artifact(run_id: str, person_id: int, artifact: str) -> None:
    """DM a resumed scheduled run's artifact — the delivery the scheduler
    would have made had the run not paused. Best effort: the run is already
    stored as done, and a failed send must not change that."""
    try:
        from openexecutive.orchestrator.schedule_tools import handle_message_person

        await handle_message_person({"person_id": person_id, "text": artifact})
    except Exception:
        logger.exception("resumer: run %s artifact delivery failed (run still done)", run_id)


def _resolution_from_row(row: dict, run_id: str) -> WaitForHumanResolution:
    """The stored resolution, or a neutral stand-in if it cannot be read.

    A missing or corrupt `resolution_json` must not strand a run that has
    already paid for its pre-gate steps: continue with an explicit "unknown"
    decision, which is neither an approval nor a rejection, so the run
    completes and the ambiguity is visible in the artifact.
    """
    raw = row.get("resolution_json")
    if raw:
        try:
            return WaitForHumanResolution.model_validate_json(raw)
        except Exception:
            logger.exception("resumer: unreadable resolution for run %s", run_id)
    return WaitForHumanResolution(
        run_id=run_id,
        reply_text="",
        source_channel="system",
        parsed_decision={"decision": "unknown", "note": "resolution unreadable"},
        person_id=0,
    )


async def _handle_timeout(run: dict, now: datetime) -> None:
    """Apply the timeout policy for an expired awaiting_human run."""

    import contextlib
    run_id = run["run_id"]
    state: dict = {}
    with contextlib.suppress(json.JSONDecodeError, TypeError):
        state = json.loads(run.get("state_json") or "{}")

    on_timeout = state.get("on_timeout", "escalate")
    person_id: int | None = run.get("awaiting_person_id")
    department = state.get("department", "")
    question = state.get("question", "")

    logger.info(
        "resumer: timeout for run_id=%s on_timeout=%s person=%s",
        run_id, on_timeout, person_id,
    )

    if on_timeout == "escalate":
        if department and person_id is not None:
            try:
                from openexecutive.departments.authority import propose_via_alert
                propose_via_alert(
                    department_slug=department,
                    person_id=person_id,
                    summary=f"[TIMEOUT] WaitForHuman expired: {question[:100]}",
                    body=(
                        f"Workflow run {run_id} timed out waiting for person {person_id}.\n\n"
                        f"Question: {question}\n\n"
                        f"Workflow: {run.get('workflow_name', '')} — {run.get('title', '')}"
                    ),
                    suggested_action=(
                        f"Resume the '{run.get('workflow_name', '')}' workflow — proceed "
                        "with a sensible default, or close it out if it's no longer needed."
                    ),
                )
            except Exception:
                logger.exception("resumer: escalation alert failed for run_id=%s", run_id)
        else:
            logger.warning(
                "resumer: escalate timeout for run_id=%s has no department/person — "
                "alert skipped, run marked timed_out",
                run_id,
            )
        if not _wf_persistence.mark_timed_out(run_id):
            # The row moved between `_tick` listing it and us acting: almost
            # always a reply that arrived in the gap and already resolved it.
            # Every write below is unguarded, so continuing would discard a
            # decision a human actually made — clear_resume_state would strip
            # the payload from a `resolved` run, leaving it queued to resume
            # with nothing to resume from.
            logger.info(
                "resumer: run %s was answered before its timeout was applied "
                "— leaving the resolution alone",
                run_id,
            )
            return
        # `timed_out` is terminal: nobody answered, so the payload will never
        # be replayed. Dropping it keeps the completed-step text out of a row
        # nothing will read, and keeps the run off the resumable queue.
        _wf_persistence.clear_resume_state(run_id)

    elif on_timeout == "auto_proceed":
        auto_resolution = WaitForHumanResolution(
            run_id=run_id,
            reply_text="[auto-proceed on timeout]",
            source_channel="system",
            parsed_decision={"decision": "auto_proceed", "note": "timeout"},
            person_id=person_id or 0,
            resolved_at=now.isoformat(),
        )
        await apply_resolution(run_id, auto_resolution)

    else:  # "fail"
        # Same race as the escalate branch: `fail_run` has no status guard, so
        # check the transition first rather than overwriting a resolution that
        # landed in the gap.
        if not _wf_persistence.mark_timed_out(run_id):
            logger.info(
                "resumer: run %s was answered before its timeout was applied "
                "— not failing it",
                run_id,
            )
            return
        _wf_persistence.fail_run(
            run_id,
            f"WaitForHuman timed out (on_timeout=fail) at {now.isoformat()}",
        )
        _wf_persistence.clear_resume_state(run_id)


async def sweep_stale_awaiting(db_path: Path | None = None) -> int:
    """Apply the full on_timeout policy for all overdue ``awaiting_human`` rows.

    Called once at resumer startup to handle runs whose ``awaiting_until``
    fired while the server was down.  Mirrors ``_tick`` but runs eagerly before
    the first poll-sleep so the DB is consistent from the moment the resumer
    is live.

    The full ``_handle_timeout`` policy is applied (escalation alerts,
    auto-resolution, fail) — not merely a status flip — so no run silently
    loses its policy just because the server was restarted.

    Returns the count of runs processed.
    """
    now = datetime.now(UTC)
    runs = _wf_persistence.list_awaiting_runs(db_path=db_path)
    swept = 0
    for run in runs:
        raw_until = run.get("awaiting_until")
        if not raw_until:
            continue
        try:
            until_dt = datetime.fromisoformat(raw_until)
        except ValueError:
            logger.warning(
                "resumer.sweep: malformed awaiting_until %r for run_id=%s",
                raw_until, run.get("run_id"),
            )
            continue
        if until_dt.tzinfo is None:
            until_dt = until_dt.replace(tzinfo=UTC)
        if until_dt <= now:
            try:
                await _handle_timeout(run, now)
                swept += 1
                logger.info(
                    "resumer.sweep: applied timeout policy for run_id=%s (awaiting_until=%s)",
                    run["run_id"], raw_until,
                )
            except Exception:
                logger.exception(
                    "resumer.sweep: _handle_timeout failed for run_id=%s", run.get("run_id")
                )
    if swept:
        logger.info("resumer.sweep: %d stale awaiting_human run(s) processed at startup", swept)
    return swept


async def run_resumer(poll_interval_seconds: int = 60) -> None:
    """Poll for timed-out awaiting_human runs and apply timeout policies.

    Mirrors ``scheduler/runner.run_scheduler`` — same single-worker assumption,
    same asyncio-sleep polling pattern.

    An async startup sweep runs first to apply the full on_timeout policy for
    any runs that expired while the server was down, before the first tick.
    """
    logger.info("resumer started (poll_interval=%ds)", poll_interval_seconds)
    swept = await sweep_stale_awaiting()
    if swept:
        logger.info("resumer: startup sweep processed %d stale run(s)", swept)
    # Replies that landed while the server was down are already `resolved`;
    # execute them now rather than after a first full poll interval.
    try:
        resumed = await _process_resumable(datetime.now(UTC))
        if resumed:
            logger.info("resumer: startup resumed %d run(s)", resumed)
    except Exception:
        logger.exception("resumer: startup resume sweep failed")
    while True:
        try:
            now = datetime.now(UTC)
            await _tick(now)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("resumer tick failed")
        try:
            await asyncio.sleep(poll_interval_seconds)
        except asyncio.CancelledError:
            logger.info("resumer cancelled — exiting")
            raise


async def _tick(now: datetime) -> None:
    """Process one poll cycle — handle all timed-out runs."""
    runs = _wf_persistence.list_awaiting_runs()
    timed_out = []
    for r in runs:
        raw_until = r.get("awaiting_until")
        if not raw_until:
            continue
        try:
            until_dt = datetime.fromisoformat(raw_until)
            # Always compare timezone-aware datetimes. Stored timestamps
            # may lack tz info (naive) if saved with a naive datetime; treat
            # those as UTC to avoid silent early-expiry on naive inputs.
            if until_dt.tzinfo is None:
                until_dt = until_dt.replace(tzinfo=UTC)
            if until_dt <= now:
                timed_out.append(r)
        except ValueError:
            logger.warning("resumer: malformed awaiting_until %r for run_id=%s", raw_until, r.get("run_id"))
    if timed_out:
        logger.info("resumer: %d timed-out run(s)", len(timed_out))
    for run in timed_out:
        try:
            await _handle_timeout(run, now)
        except Exception:
            logger.exception("resumer: _handle_timeout failed for run_id=%s", run.get("run_id"))

    # AFTER the timeout loop, deliberately: `_handle_timeout`'s auto_proceed
    # branch resolves a run, and running the executor second means that run
    # executes in the SAME tick. That ordering is the whole reason
    # `on_timeout='auto_proceed'` now does what its name says, with no
    # special-casing anywhere.
    await _process_resumable(now)
