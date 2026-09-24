"""Direct tests for `_next_stream_step`, the SSE driver's one-step race.

This is the most delicate code in the stop feature: it races a produced item
against the stop switch and the whole-turn deadline, and the ordering matters
in three independent ways (don't drop a produced item; await a cancellation
before touching the generator; keep the deadline working). Testing it on its
own — rather than only through a full ASGI turn — makes each rule fail loudly
and in isolation.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from openexecutive.api.routes.chat import _next_stream_step


def _waiter(event: asyncio.Event) -> asyncio.Task[bool]:
    return asyncio.ensure_future(event.wait())


async def _drain(task: asyncio.Task[Any] | None) -> None:
    if task is not None and not task.done():
        task.cancel()


@pytest.mark.asyncio
async def test_produced_item_is_returned() -> None:
    async def gen() -> Any:
        yield "hello"

    step = await _next_stream_step(gen().__aiter__(), None, 5.0, "t-1")
    assert step.produced is True
    assert step.item == "hello"
    assert not step.stopped and not step.timed_out and not step.exhausted


@pytest.mark.asyncio
async def test_exhausted_stream_is_reported() -> None:
    async def gen() -> Any:
        return
        yield  # pragma: no cover - unreachable; makes this an async generator

    step = await _next_stream_step(gen().__aiter__(), None, 5.0, "t-2")
    assert step.exhausted is True
    assert step.produced is False


@pytest.mark.asyncio
async def test_stop_cancels_a_hanging_step() -> None:
    """The whole point: a set switch must cut a step that would never finish."""
    cancelled = {"seen": False}

    async def gen() -> Any:
        try:
            await asyncio.sleep(30)
            yield "never"
        except asyncio.CancelledError:
            cancelled["seen"] = True
            raise

    event = asyncio.Event()
    waiter = _waiter(event)
    stream = gen().__aiter__()

    async def flip() -> None:
        await asyncio.sleep(0.05)
        event.set()

    asyncio.ensure_future(flip())
    t0 = time.monotonic()
    step = await _next_stream_step(stream, waiter, 30.0, "t-3")
    elapsed = time.monotonic() - t0

    assert step.stopped is True
    assert step.produced is False
    assert elapsed < 5, "the step was not cancelled promptly"
    # The cancellation really reached the generator — this is what stops the
    # Executive's in-flight Anthropic call / tool gather.
    assert cancelled["seen"] is True
    await _drain(waiter)


@pytest.mark.asyncio
async def test_an_already_set_stop_still_returns_a_ready_item() -> None:
    """A tie must favour the item: it is already paid for, and an
    `action_taken` dropped here never reaches the persisted chips."""
    async def gen() -> Any:
        yield {"type": "action_taken", "tool": "send_dm"}

    event = asyncio.Event()
    event.set()
    waiter = _waiter(event)

    step = await _next_stream_step(gen().__aiter__(), waiter, 5.0, "t-4")
    assert step.produced is True
    assert step.item == {"type": "action_taken", "tool": "send_dm"}
    assert step.stopped is False
    await _drain(waiter)


@pytest.mark.asyncio
async def test_deadline_still_fires_without_a_stop_switch() -> None:
    async def gen() -> Any:
        await asyncio.sleep(30)
        yield "never"

    t0 = time.monotonic()
    step = await _next_stream_step(gen().__aiter__(), None, 0.1, "t-5")
    assert step.timed_out is True
    assert time.monotonic() - t0 < 5


@pytest.mark.asyncio
async def test_deadline_still_fires_with_an_unset_stop_switch() -> None:
    """Regression guard for the wait_for -> asyncio.wait rewrite: a registered
    but never-flipped switch must not swallow the deadline."""
    async def gen() -> Any:
        await asyncio.sleep(30)
        yield "never"

    waiter = _waiter(asyncio.Event())
    step = await _next_stream_step(gen().__aiter__(), waiter, 0.1, "t-6")
    assert step.timed_out is True
    assert step.stopped is False
    await _drain(waiter)


@pytest.mark.asyncio
async def test_generator_is_closeable_after_a_stop() -> None:
    """`aclose()` must not raise "asynchronous generator is already running".

    That is what happens if the cancelled step is not awaited before the
    caller's best-effort `aclose()` runs.
    """
    async def gen() -> Any:
        await asyncio.sleep(30)
        yield "never"

    event = asyncio.Event()
    event.set()
    waiter = _waiter(event)
    stream = gen().__aiter__()

    step = await _next_stream_step(stream, waiter, 5.0, "t-7")
    assert step.stopped is True
    # The caller does exactly this next.
    await stream.aclose()
    await _drain(waiter)


@pytest.mark.asyncio
async def test_a_raising_step_does_not_escape() -> None:
    """A generator that fails during its cancellation unwind is logged, not
    propagated — the turn still has a partial reply to persist."""
    async def gen() -> Any:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            raise RuntimeError("cleanup blew up") from None
        yield "never"

    event = asyncio.Event()
    event.set()
    waiter = _waiter(event)

    step = await _next_stream_step(gen().__aiter__(), waiter, 5.0, "t-8")
    assert step.stopped is True
    await _drain(waiter)


@pytest.mark.asyncio
async def test_outer_cancellation_does_not_orphan_the_step() -> None:
    """A client disconnect must not leave the Executive running.

    Regression guard for the `wait_for` -> `asyncio.wait` rewrite.
    `asyncio.wait_for` cancels its inner task when the task awaiting it is
    cancelled; `asyncio.wait` does NOT. Starlette cancels the whole SSE body on
    `http.disconnect`, so without the `finally` in `_next_stream_step` a closed
    tab would leave `__anext__` running a full specialist or tool round with no
    deadline, no registry entry and no persistence — i.e. unbounded work per
    abandoned request.
    """
    state = {"phase": "init"}

    async def gen() -> Any:
        try:
            state["phase"] = "running"
            await asyncio.sleep(5)
            state["phase"] = "completed-anyway"
            yield "late"
        except asyncio.CancelledError:
            state["phase"] = "cancelled"
            raise

    stream = gen().__aiter__()
    task = asyncio.ensure_future(_next_stream_step(stream, None, 30.0, "t-9"))
    await asyncio.sleep(0.05)

    task.cancel()  # <- what Starlette does on http.disconnect
    with pytest.raises(asyncio.CancelledError):
        await task

    assert state["phase"] == "cancelled", (
        "the executive step was orphaned by the outer cancellation and kept "
        f"running (phase={state['phase']!r})"
    )

    # And it stays cancelled — it must not resume and finish its round later.
    await asyncio.sleep(0.2)
    assert state["phase"] == "cancelled"
