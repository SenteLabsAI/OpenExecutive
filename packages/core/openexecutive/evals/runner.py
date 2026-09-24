"""Async generator that runs eval scenarios in parallel and yields progress events.

Scenarios run with bounded concurrency (default 5, override via ``EVAL_CONCURRENCY``).
Events arrive in the order tasks finish — not in scenario order. The UI keys
state by ``scenario_id`` so this is fine.

Yielded event shapes:
  {"type": "suite_start",     "kind": str, "total": int}
  {"type": "scenario_start",  "index": int, "total": int, "scenario_id": str, "description": str}
  {"type": "scenario_done",   "index": int, "total": int, "scenario_id": str, "passed": bool, "scores": dict,
                              # plus kind-specific payload:
                              #   chat:     "query", "response"
                              #   workflow: "workflow_name", "workflow_inputs", "artifact"
                              #   triage:   "event", "decision"
                              }
  {"type": "scenario_error",  "index": int, "total": int, "scenario_id": str, "error": str}
  {"type": "suite_done",      "kind": str, "passed": int, "total": int}
  {"type": "suite_canceled",  "kind": str, "passed": int, "total": int}
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncGenerator, Callable, Coroutine
from typing import Any

from openexecutive.evals.judges import judge_chat, judge_triage, judge_workflow
from openexecutive.evals.scenarios import load_scenarios
from openexecutive.workflows.gate import ensure_workflow_event

logger = logging.getLogger(__name__)

_PASS_THRESHOLD = 3.5
_CONCURRENCY = max(1, int(os.environ.get("EVAL_CONCURRENCY", "5")))

# A `RunOne` runs a single scenario at a given index and puts events into the
# provided queue. Each kind builds one of these closures with its own context.
RunOne = Callable[[int, dict[str, Any]], Coroutine[Any, Any, None]]


async def run_scenarios(
    *,
    kind: str,
    scenario_id: str | None = None,
    store: Any = None,
    cancel_event: asyncio.Event | None = None,
) -> AsyncGenerator[dict[str, Any], None]:
    """Async generator that streams progress events as scenarios run in parallel.

    If ``cancel_event`` is provided and set during the run, all in-flight
    scenario tasks are cancelled and the generator yields ``suite_canceled``
    instead of ``suite_done``.
    """
    scenarios = load_scenarios(kind=kind, scenario_id=scenario_id)
    total = len(scenarios)
    passed = [0]  # single-item list — safe under asyncio without locks

    yield {"type": "suite_start", "kind": kind, "total": total}

    if total == 0:
        yield {"type": "suite_done", "kind": kind, "passed": 0, "total": 0}
        return

    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    sem = asyncio.Semaphore(_CONCURRENCY)

    run_one = _make_run_one(
        kind=kind,
        store=store,
        sem=sem,
        queue=queue,
        passed=passed,
        total=total,
        cancel_event=cancel_event,
    )

    tasks: list[asyncio.Task[None]] = [
        asyncio.create_task(run_one(i, s)) for i, s in enumerate(scenarios)
    ]

    async def watcher() -> None:
        # Optional companion task that cancels every in-flight scenario the
        # moment the external cancel event fires. Without this the suite
        # would have to wait for the slowest in-flight scenario (~60s) to
        # finish naturally before the user's Stop click takes effect.
        cancel_watcher_task: asyncio.Task[None] | None = None
        if cancel_event is not None:
            async def cancel_watcher() -> None:
                await cancel_event.wait()
                for t in tasks:
                    if not t.done():
                        t.cancel()
            cancel_watcher_task = asyncio.create_task(cancel_watcher())

        # `return_exceptions=True` so a per-task bug (or CancelledError)
        # can't deadlock the generator — each run_one already wraps its
        # work in try/except.
        await asyncio.gather(*tasks, return_exceptions=True)

        if cancel_watcher_task is not None and not cancel_watcher_task.done():
            cancel_watcher_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await cancel_watcher_task

        await queue.put(None)

    watcher_task = asyncio.create_task(watcher())

    try:
        while True:
            event = await queue.get()
            if event is None:
                break
            yield event
    finally:
        await watcher_task

    if cancel_event is not None and cancel_event.is_set():
        yield {"type": "suite_canceled", "kind": kind, "passed": passed[0], "total": total}
    else:
        yield {"type": "suite_done", "kind": kind, "passed": passed[0], "total": total}


def _make_run_one(
    *,
    kind: str,
    store: Any,
    sem: asyncio.Semaphore,
    queue: asyncio.Queue[dict[str, Any] | None],
    passed: list[int],
    total: int,
    cancel_event: asyncio.Event | None,
) -> RunOne:
    """Returns a `run_one(i, scenario)` coroutine for the requested kind."""
    if kind == "triage":
        return _make_triage_runner(sem, queue, passed, total, cancel_event)
    if kind == "workflow":
        return _make_workflow_runner(store, sem, queue, passed, total, cancel_event)
    # chat (default) and mcp both go through Executive.chat
    return _make_chat_runner(sem, queue, passed, total, cancel_event)


def _is_canceled(cancel_event: asyncio.Event | None) -> bool:
    return cancel_event is not None and cancel_event.is_set()


def _make_triage_runner(
    sem: asyncio.Semaphore,
    queue: asyncio.Queue[dict[str, Any] | None],
    passed: list[int],
    total: int,
    cancel_event: asyncio.Event | None,
) -> RunOne:
    from openexecutive.agents.triage import TriageAgent
    from openexecutive.alerts.models import AlertEvent

    async def run_one(i: int, scenario: dict[str, Any]) -> None:
        if _is_canceled(cancel_event):
            return
        try:
            async with sem:
                if _is_canceled(cancel_event):
                    return
                await queue.put(
                    {
                        "type": "scenario_start",
                        "index": i,
                        "total": total,
                        "scenario_id": scenario["id"],
                        "description": scenario.get("description", ""),
                    }
                )
                try:
                    event = AlertEvent(**scenario["event"])
                    ctx = scenario.get("context", {}) or {}
                    agent = TriageAgent()
                    decision = await agent.triage(
                        event,
                        recent_alerts=ctx.get("recent_alerts", []) or [],
                        mute_patterns=ctx.get("muted_topics", []) or [],
                        active_initiatives=ctx.get("active_initiatives", []) or [],
                    )
                    decision_dict = decision.model_dump(mode="json")
                    scores = await judge_triage(scenario, decision_dict)
                    ok = float(scores.get("overall", 0)) >= _PASS_THRESHOLD
                    if ok:
                        passed[0] += 1
                    await queue.put(
                        {
                            "type": "scenario_done",
                            "index": i,
                            "total": total,
                            "scenario_id": scenario["id"],
                            "passed": ok,
                            "scores": scores,
                            "event": scenario.get("event"),
                            "decision": decision_dict,
                        }
                    )
                except Exception as exc:
                    logger.exception("eval scenario %s failed", scenario["id"])
                    await queue.put(
                        {
                            "type": "scenario_error",
                            "index": i,
                            "total": total,
                            "scenario_id": scenario["id"],
                            "error": str(exc),
                        }
                    )
        except asyncio.CancelledError:
            # User clicked Stop. Exit silently — `suite_canceled` will
            # be emitted by the outer generator once all tasks settle.
            return

    return run_one


def _make_workflow_runner(
    store: Any,
    sem: asyncio.Semaphore,
    queue: asyncio.Queue[dict[str, Any] | None],
    passed: list[int],
    total: int,
    cancel_event: asyncio.Event | None,
) -> RunOne:
    from openexecutive.workflows import WORKFLOW_REGISTRY

    async def run_one(i: int, scenario: dict[str, Any]) -> None:
        if _is_canceled(cancel_event):
            return
        try:
            async with sem:
                if _is_canceled(cancel_event):
                    return
                await queue.put(
                    {
                        "type": "scenario_start",
                        "index": i,
                        "total": total,
                        "scenario_id": scenario["id"],
                        "description": scenario.get("description", ""),
                    }
                )
                try:
                    workflow_name = scenario.get("workflow")
                    workflow = WORKFLOW_REGISTRY.get(workflow_name) if workflow_name else None
                    if workflow is None:
                        raise KeyError(f"unknown workflow: {workflow_name!r}")
                    workflow_inputs = scenario.get("workflow_inputs") or {}
                    inputs = workflow.input_model()(**workflow_inputs)
                    artifact = ""
                    async for ev in workflow.run(inputs, store):
                        ev = ensure_workflow_event(ev, site='evals.runner')
                        if ev.type == "artifact":
                            artifact = ev.content or ""
                        elif ev.type == "error":
                            raise RuntimeError(f"workflow errored: {ev.message}")
                    scores = await judge_workflow(scenario, artifact)
                    ok = float(scores.get("overall", 0)) >= _PASS_THRESHOLD
                    if ok:
                        passed[0] += 1
                    await queue.put(
                        {
                            "type": "scenario_done",
                            "index": i,
                            "total": total,
                            "scenario_id": scenario["id"],
                            "passed": ok,
                            "scores": scores,
                            "workflow_name": workflow_name,
                            "workflow_inputs": workflow_inputs,
                            "artifact": artifact,
                        }
                    )
                except Exception as exc:
                    logger.exception("eval scenario %s failed", scenario["id"])
                    await queue.put(
                        {
                            "type": "scenario_error",
                            "index": i,
                            "total": total,
                            "scenario_id": scenario["id"],
                            "error": str(exc),
                        }
                    )
        except asyncio.CancelledError:
            return

    return run_one


def _make_chat_runner(
    sem: asyncio.Semaphore,
    queue: asyncio.Queue[dict[str, Any] | None],
    passed: list[int],
    total: int,
    cancel_event: asyncio.Event | None,
) -> RunOne:
    from openexecutive.memory.company_profile import CompanyProfile
    from openexecutive.orchestrator.executive import Executive
    from openexecutive.orchestrator.session import Session

    # Shared Executive — confirmed safe: no mutable instance state on the
    # hot path; chat()/stream_chat() route all per-call state through Session.
    executive = Executive()

    async def run_one(i: int, scenario: dict[str, Any]) -> None:
        if _is_canceled(cancel_event):
            return
        try:
            async with sem:
                if _is_canceled(cancel_event):
                    return
                await queue.put(
                    {
                        "type": "scenario_start",
                        "index": i,
                        "total": total,
                        "scenario_id": scenario["id"],
                        "description": scenario.get("description", ""),
                    }
                )
                try:
                    ctx = scenario.get("company_context", {})
                    profile = CompanyProfile(
                        name=ctx.get("name", "Eval Company"),
                        industry=ctx.get("industry", ""),
                        stage=ctx.get("stage", ""),
                        headcount=ctx.get("headcount"),
                        annual_revenue_arr=ctx.get("arr"),
                    )
                    if ctx.get("monthly_burn"):
                        profile.financials.burn_rate_monthly = ctx["monthly_burn"]
                    if ctx.get("runway_months"):
                        profile.financials.runway_months = ctx["runway_months"]
                    session = Session(company_profile=profile)
                    query = scenario["query"]
                    response = await executive.chat(
                        user_message=query,
                        session=session,
                        # A scenario may supply the <peer_memory> body itself.
                        # Evals run with no person_id, so the Honcho prefetch
                        # never fires; this is the only way to exercise how the
                        # Executive USES peer memory. None keeps chat()'s default.
                        peer_memory_context=scenario.get("peer_memory_context"),
                    )
                    scores = await judge_chat(scenario, response)
                    ok = float(scores.get("overall", 0)) >= _PASS_THRESHOLD
                    if ok:
                        passed[0] += 1
                    await queue.put(
                        {
                            "type": "scenario_done",
                            "index": i,
                            "total": total,
                            "scenario_id": scenario["id"],
                            "passed": ok,
                            "scores": scores,
                            "query": query,
                            "response": response,
                        }
                    )
                except Exception as exc:
                    logger.exception("eval scenario %s failed", scenario["id"])
                    await queue.put(
                        {
                            "type": "scenario_error",
                            "index": i,
                            "total": total,
                            "scenario_id": scenario["id"],
                            "error": str(exc),
                        }
                    )
        except asyncio.CancelledError:
            return

    return run_one
