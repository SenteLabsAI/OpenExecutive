"""Stop a workflow run while it runs.

Every place that drives a run (the Jobs page's stream, the chat tool, the
scheduler, the resumer) iterates it through ``watch(run_id, events)``. A
Stop (``POST /workflows/runs/{run_id}/stop``) marks the run ``stopped`` in
the database (``persistence.stop_run``) and calls ``stop(run_id)``:

* while the run is waiting inside its own work (a model call, a tool call,
  a script), the task driving it is cancelled there, and ``watch`` turns
  that one cancellation into ``RunStopped`` (``Task.uncancel``), so the
  driver itself is never cancelled — the scheduler loop that drives a
  scheduled run keeps going;
* otherwise (the driver is handling an event between steps), ``watch``
  raises ``RunStopped`` the next time it is asked for an event.

A sign-off that was already on its way when Stop landed is not sent:
``gate.checkpoint_gate`` raises ``RunStopped`` for a run marked ``stopped``.

A driver catches ``RunStopped`` and writes nothing: the row is already
``stopped``, and ``complete_run`` / ``fail_run`` / a checkpoint never write
over that. What the run already did stays done.

Runs live in this process only (one per company machine). A run this
process isn't driving — a stale ``running`` row after a restart, a run
parked for a sign-off — is just marked ``stopped``.
"""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TypeVar

T = TypeVar("T")


class RunStopped(Exception):
    """Someone pressed Stop on this run."""


@dataclass
class _Run:
    task: asyncio.Task[object] | None
    inside: bool = False
    stopping: bool = False


_runs: dict[str, _Run] = {}


async def watch(
    run_id: str, events: AsyncIterator[T], *, quiet: bool = False
) -> AsyncIterator[T]:
    """``events``, until the run is stopped: then ``RunStopped``, or with
    ``quiet`` the events just end (for a driver whose own ending already
    copes with a run that produced nothing)."""
    run = _Run(task=None)
    _runs[run_id] = run
    try:
        while True:
            if run.stopping:
                if quiet:
                    return
                raise RunStopped
            # The task asking for this event is the one a Stop cancels.
            run.task = asyncio.current_task()
            run.inside = True
            try:
                item = await anext(events)
            except StopAsyncIteration:
                return
            except asyncio.CancelledError:
                if run.stopping and run.task is not None:
                    # Our own cancel: undo it for the driver, end the run.
                    run.task.uncancel()
                    if quiet:
                        return
                    raise RunStopped from None
                raise
            finally:
                run.inside = False
            # Stopped while this event was being made: don't hand it on (a
            # sign-off question would go out for a run that is over).
            if run.stopping:
                if quiet:
                    return
                raise RunStopped
            yield item
    finally:
        if _runs.get(run_id) is run:
            del _runs[run_id]
        aclose = getattr(events, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(Exception):
                await aclose()


def stop(run_id: str) -> bool:
    """Stop ``run_id`` if this process is driving it. Returns whether it was."""
    run = _runs.get(run_id)
    if run is None:
        return False
    if not run.stopping:
        run.stopping = True
        if run.inside and run.task is not None and run.task is not asyncio.current_task():
            run.task.cancel()
    return True


def is_running_here(run_id: str) -> bool:
    return run_id in _runs
