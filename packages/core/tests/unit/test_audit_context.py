"""Audit context manager — must survive async generators whose iteration
crosses asyncio Task boundaries.

Regression: when ``set_turn`` used ``ContextVar.set()`` + ``reset(token)``,
the Token validation raised ``ValueError: <Token …> was created in a
different Context`` whenever the wrapped ``async for`` over a streaming
generator resumed inside a different Task than the one that called
``__enter__``. This shows up on the FastAPI SSE path and on the httpx-
backed OpenRouter provider's streaming dispatch.

The fix saves the prior value and restores it on exit, which is
Context-independent.
"""
from __future__ import annotations

import asyncio
from unittest import mock

import pytest

from openexecutive.audit.context import (
    get_active_ids,
    set_turn,
)


def test_set_turn_binds_and_restores_prior_values() -> None:
    """Outside the block both vars are None; inside they reflect the kwargs;
    after the block they restore to the prior values (None here)."""
    assert get_active_ids() == (None, None)
    with set_turn(session_id="s1", turn_id="t1"):
        assert get_active_ids() == ("s1", "t1")
    assert get_active_ids() == (None, None)


def test_set_turn_nests_and_restores_outer_values() -> None:
    """Nested ``set_turn`` blocks must restore the outer values on exit —
    a workflow that opens a sub-turn for a tool call must not stomp the
    caller's turn id."""
    with set_turn(session_id="outer-s", turn_id="outer-t"):
        with set_turn(session_id="inner-s", turn_id="inner-t"):
            assert get_active_ids() == ("inner-s", "inner-t")
        # Inner block exits → outer ids restored.
        assert get_active_ids() == ("outer-s", "outer-t")
    assert get_active_ids() == (None, None)


def test_set_turn_survives_async_generator_resumed_across_tasks() -> None:
    """The exact failure mode the user hit: a ``with set_turn(...)`` block
    has its ``__enter__`` and ``__exit__`` run in different asyncio
    ``Context`` instances. Pre-fix, ``ContextVar.reset(token)`` raised
    ``ValueError: <Token …> was created in a different Context``.

    We provoke this directly with ``contextvars.copy_context()`` rather
    than indirectly through asyncio task scheduling — copy_context is
    the underlying primitive every async runtime uses to give a new
    task its own Context, and it gives us deterministic reproduction
    of the boundary without depending on FastAPI / httpx / anyio
    internals.
    """
    import contextvars

    # Drive __enter__ in Context A, __exit__ in Context B. Pre-fix, the
    # finally-branch ``reset(token)`` blows up because the Token was
    # created in A but reset is being called from B.
    cm = set_turn(session_id="cross-ctx-s", turn_id="cross-ctx-t")
    ctx_a = contextvars.copy_context()
    ctx_b = contextvars.copy_context()

    ctx_a.run(cm.__enter__)
    # Generator's ``__exit__`` accepts the (exc_type, exc, tb) triple.
    ctx_b.run(cm.__exit__, None, None, None)

    # Outside both contexts, the live default Context is untouched.
    assert get_active_ids() == (None, None)


def test_set_turn_partial_kwargs_pass_none_for_unset_field() -> None:
    """Either kwarg can be set independently — workflows that have a
    session but no logical turn pass just ``session_id``."""
    with set_turn(session_id="only-session"):
        assert get_active_ids() == ("only-session", None)
    with set_turn(turn_id="only-turn"):
        assert get_active_ids() == (None, "only-turn")


def test_set_turn_exit_runs_even_on_exception() -> None:
    """A streaming consumer that raises mid-iteration must not leak
    the bound ids — the ``finally`` branch restores prior values."""
    with pytest.raises(RuntimeError, match="boom"):
        with set_turn(session_id="leaky-s", turn_id="leaky-t"):
            assert get_active_ids() == ("leaky-s", "leaky-t")
            raise RuntimeError("boom")
    assert get_active_ids() == (None, None)


# --------------------------------------------------------------------- #
# Fire-and-forget actors must carry the scheduling turn with them
# --------------------------------------------------------------------- #


def test_schedule_extraction_carries_the_scheduling_turn() -> None:
    """The memory_extractor's model call runs in a task spawned after the
    caller's `with set_turn(...)` has exited, so the ids must be
    snapshotted at scheduling time and re-bound inside the task. Without
    that, every extraction row records with session_id=NULL."""
    import asyncio

    from openexecutive.audit.context import get_active_ids, set_turn
    from openexecutive.memory import episodic

    seen: list[tuple[str | None, str | None]] = []

    async def _fake_extract(*_a: object, **_k: object) -> None:
        seen.append(get_active_ids())

    async def _go() -> None:
        # Deliberately nested, not merged: the set_turn block must EXIT
        # while the patch stays active, so the background task runs in
        # exactly the unbound context it sees in production. Merging the two
        # would keep the turn bound across the awaits and the test would
        # pass without the fix.
        with mock.patch.object(episodic, "_extract_and_store", _fake_extract):
            with set_turn(session_id="s-1", turn_id="t-1"):
                episodic.schedule_extraction("u", "a", session_id="s-1")
            # The caller's block has exited — exactly the window in which
            # the background task actually runs.
            await asyncio.sleep(0)
            await asyncio.sleep(0)

    asyncio.run(_go())
    assert seen == [("s-1", "t-1")]


def test_schedule_evaluation_carries_the_scheduling_turn() -> None:
    """Same contract for triage, which is a copy of the same pattern."""
    from openexecutive.alerts import pipeline
    from openexecutive.alerts.models import AlertEvent
    from openexecutive.audit.context import get_active_ids, set_turn

    seen: list[tuple[str | None, str | None]] = []

    async def _fake_eval(*_a: object, **_k: object) -> None:
        seen.append(get_active_ids())

    async def _go() -> None:
        with mock.patch.object(pipeline, "evaluate_and_dispatch", _fake_eval):
            with set_turn(session_id="s-2", turn_id="t-2"):
                pipeline.schedule_evaluation(
                    AlertEvent(source="test", external_id="e-1", title="t", body="b")
                )
            await asyncio.sleep(0)
            await asyncio.sleep(0)

    asyncio.run(_go())
    assert seen == [("s-2", "t-2")]


def test_out_of_turn_scheduling_stays_unattributed() -> None:
    """A scheduler or monitor sweep with no turn bound must keep recording
    (None, None) — those really are out-of-turn calls, and inventing an id
    for them would mask the next regression of this kind."""
    import asyncio

    from openexecutive.audit.context import get_active_ids
    from openexecutive.memory import episodic

    seen: list[tuple[str | None, str | None]] = []

    async def _fake_extract(*_a: object, **_k: object) -> None:
        seen.append(get_active_ids())

    async def _go() -> None:
        with mock.patch.object(episodic, "_extract_and_store", _fake_extract):
            episodic.schedule_extraction("u", "a", session_id="")
            await asyncio.sleep(0)
            await asyncio.sleep(0)

    asyncio.run(_go())
    assert seen == [(None, None)]


def test_direct_extraction_does_not_clobber_an_ambient_turn() -> None:
    """Awaiting the extractor directly from inside a live turn must keep
    that turn's ids. Binding the snapshot unconditionally would overwrite
    them with (None, None) — the very failure this plumbing exists to fix,
    reintroduced from the other direction."""
    from openexecutive.audit.context import get_active_ids, set_turn
    from openexecutive.memory import episodic

    seen: list[tuple[str | None, str | None]] = []

    async def _fake_extract(*_a: object, **_k: object) -> None:
        seen.append(get_active_ids())

    async def _go() -> None:
        with (
            mock.patch.object(episodic, "_extract_and_store", _fake_extract),
            set_turn(session_id="s-amb", turn_id="t-amb"),
        ):
            await episodic.extract_and_store("u", "a")

    asyncio.run(_go())
    assert seen == [("s-amb", "t-amb")]


def test_thread_fallback_carries_the_turn() -> None:
    """The branch the snapshot parameters actually exist for.

    In the event-loop branch `loop.create_task` copies the context at
    creation, so the child already inherits the ids and the re-bind is
    belt-and-braces. A thread started by the no-running-loop fallback gets
    a FRESH, EMPTY context and inherits nothing — so without the explicit
    snapshot its model call records unattributed. Asserting only on the
    task branch would pass with the whole mechanism removed.
    """
    import threading

    from openexecutive.audit.context import get_active_ids, set_turn
    from openexecutive.memory import episodic

    seen: list[tuple[str | None, str | None]] = []
    done = threading.Event()

    async def _fake_extract(*_a: object, **_k: object) -> None:
        seen.append(get_active_ids())
        done.set()

    # No running loop here, so schedule_extraction takes the thread branch.
    with (
        mock.patch.object(episodic, "_extract_and_store", _fake_extract),
        set_turn(session_id="s-thread", turn_id="t-thread"),
    ):
        episodic.schedule_extraction("u", "a", session_id="s-thread")
        assert done.wait(timeout=5), "extraction thread never ran"

    assert seen == [("s-thread", "t-thread")]


def test_partial_snapshot_does_not_erase_the_ambient_counterpart() -> None:
    """A half-empty snapshot must not null out the other field.

    Binding (session, None) over a live turn yields a row that shows up in
    the session view attached to no turn — worse than both binding both and
    leaving the ambient pair alone.
    """
    from openexecutive.audit.context import get_active_ids, set_turn
    from openexecutive.memory import episodic

    seen: list[tuple[str | None, str | None]] = []

    async def _fake_extract(*_a: object, **_k: object) -> None:
        seen.append(get_active_ids())

    async def _go() -> None:
        with (
            mock.patch.object(episodic, "_extract_and_store", _fake_extract),
            set_turn(session_id="s-live", turn_id="t-live"),
        ):
            # Session supplied, turn omitted — the ambient turn must survive.
            await episodic.extract_and_store(
                "u", "a", audit_session_id="s-other"
            )

    asyncio.run(_go())
    assert seen == [("s-other", "t-live")]
