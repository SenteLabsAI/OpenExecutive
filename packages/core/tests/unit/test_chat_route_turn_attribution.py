"""Every step of an SSE turn must record under the same session and turn.

`_sse_body` drives the executive with `asyncio.wait_for`, which wraps each
`__anext__()` in a fresh Task that copies the context at that moment. A
`set_turn(...)` entered *inside* the executive's own async generator
therefore lands in a throwaway per-step context and is gone by the next
resume — so only the work done during step one carries a session and turn.
Everything after it, which is the entire tool-call loop and the specialist
fan-out, records with `(None, None)` and is invisible in the per-session
audit view.

The fix binds the turn in `event_generator`, the generator Starlette itself
drives, so every resumed step inherits it.

The contract pinned here:
  1. every resume step of the stream sees the same non-None (session, turn);
  2. the id the route logs is the id the executive uses — binding without
     unifying them would split one turn across two ids, which is worse
     than the all-NULL state it replaces;
  3. work scheduled from inside the stream (the fan-out's own tasks) still
     inherits the binding, since a child task copies the live context;
  4. entry points that pass no turn_id keep generating their own.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import chat as chat_route
from openexecutive.memory import episodic, session_store
from openexecutive.memory.company_profile import CompanyProfile


@pytest.fixture(autouse=True)
def _reset_route_state() -> None:
    chat_route._sessions.clear()


@pytest.fixture()
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    db_path = Path("./episodic_memory.db").resolve()
    monkeypatch.setattr(episodic, "DB_PATH", db_path)
    monkeypatch.setattr(session_store, "DB_PATH", db_path)
    episodic.initialize_db(db_path)
    return db_path


@pytest.fixture()
def patched_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    from openexecutive.knowledge import retriever
    from openexecutive.onboarding import profile_builder

    monkeypatch.setattr(
        profile_builder, "load_or_create_profile", lambda: CompanyProfile()
    )
    monkeypatch.setattr(retriever, "retrieve", lambda **_k: "")


def _run_turn(
    monkeypatch: pytest.MonkeyPatch, steps: int = 4
) -> tuple[list[tuple[str | None, str | None]], list[str | None]]:
    """Drive one real SSE turn; return what each resume step observed.

    The fake executive records `get_active_ids()` immediately before each
    yield, which is exactly where the real tool loop and specialist
    fan-out do their recording.
    """
    from openexecutive.audit.context import get_active_ids
    from openexecutive.orchestrator import executive as exec_mod

    seen: list[tuple[str | None, str | None]] = []
    received_turn_ids: list[str | None] = []

    class _RecordingExecutive:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def stream_chat(
            self, *, turn_id: str | None = None, **_kwargs: Any
        ) -> AsyncIterator[str]:
            received_turn_ids.append(turn_id)
            for i in range(steps):
                seen.append(get_active_ids())
                yield f"chunk-{i} "

    monkeypatch.setattr(exec_mod, "Executive", _RecordingExecutive)

    app = FastAPI()
    app.include_router(chat_route.router)
    client = TestClient(app)
    resp = client.post("/chat", json={"message": "attribute this turn"})
    assert resp.status_code == 200
    return seen, received_turn_ids


def test_every_resume_step_sees_the_same_session_and_turn(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression itself.

    Before the route-level bind, step one saw real ids and every later
    step saw (None, None) — so the tool loop and the fan-out, which is
    where nearly all of a turn's work happens, recorded unattributed.
    """
    seen, _ = _run_turn(monkeypatch, steps=4)

    assert len(seen) == 4
    assert all(sid is not None for sid, _ in seen), (
        f"a resume step lost its session id: {seen}"
    )
    assert all(tid is not None for _, tid in seen), (
        f"a resume step lost its turn id: {seen}"
    )
    assert len(set(seen)) == 1, (
        f"steps disagreed about which turn they belong to: {set(seen)}"
    )


def test_route_and_executive_agree_on_the_turn_id(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Binding without unifying would be worse than the bug it fixes.

    The route logs one id and the executive used to mint another, so a
    turn would land half under each — fragmenting the audit view instead
    of merely leaving it empty.
    """
    seen, received_turn_ids = _run_turn(monkeypatch, steps=2)

    assert received_turn_ids and received_turn_ids[0] is not None, (
        "the route must pass its own turn_id into the executive"
    )
    bound_turn_ids = {tid for _, tid in seen}
    assert bound_turn_ids == {received_turn_ids[0]}


def test_work_scheduled_mid_stream_inherits_the_binding(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A task spawned from inside the stream copies the live context.

    This is how the specialist fan-out records — if the binding were only
    present on the driving task, `asyncio.gather` children would still
    come out unattributed.
    """
    import asyncio

    from openexecutive.audit.context import get_active_ids
    from openexecutive.orchestrator import executive as exec_mod

    from_child: list[tuple[str | None, str | None]] = []

    class _FanOutExecutive:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def stream_chat(self, **_kwargs: Any) -> AsyncIterator[str]:
            yield "first "

            async def _child() -> None:
                from_child.append(get_active_ids())

            await asyncio.gather(_child(), _child())
            yield "second "

    monkeypatch.setattr(exec_mod, "Executive", _FanOutExecutive)

    app = FastAPI()
    app.include_router(chat_route.router)
    client = TestClient(app)
    assert client.post("/chat", json={"message": "fan out"}).status_code == 200

    assert len(from_child) == 2
    for session_id, turn_id in from_child:
        assert session_id is not None
        assert turn_id is not None


def test_turn_id_is_generated_when_the_caller_supplies_none() -> None:
    """Slack, Discord, Telegram, email and the CLI pass nothing and must
    keep minting their own — the parameter is additive, not required."""
    import inspect

    from openexecutive.orchestrator.executive import Executive

    for fn in (Executive.stream_chat, Executive.stream_chat_with_committee):
        param = inspect.signature(fn).parameters["turn_id"]
        assert param.default is None, f"{fn.__name__} must not require turn_id"


# --------------------------------------------------------------------- #
# Isolation: the binding is held across an async generator
# --------------------------------------------------------------------- #


def test_concurrent_turns_do_not_cross_contaminate(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two turns in flight at once must not see each other's identity.

    The binding is now held across an async generator for the whole
    response body, so this pins the property that makes that safe: each
    request's ASGI task owns its own context. A leak here would mean audit
    rows attributed to the wrong session.
    """
    from openexecutive.audit.context import get_active_ids
    from openexecutive.orchestrator import executive as exec_mod

    # message -> list of (session_id, turn_id) observed on each step
    observed: dict[str, list[tuple[str | None, str | None]]] = {}

    class _Interleaving:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_k: Any) -> None:
            pass

        async def stream_chat(self, *, user_message: str = "", **_k: Any) -> AsyncIterator[str]:
            for _ in range(5):
                observed.setdefault(user_message, []).append(get_active_ids())
                # Yield control so the two requests genuinely interleave.
                await asyncio.sleep(0)
                yield "x "

    monkeypatch.setattr(exec_mod, "Executive", _Interleaving)

    app = FastAPI()
    app.include_router(chat_route.router)

    async def _go() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            await asyncio.gather(
                c.post("/chat", json={"message": "alpha"}),
                c.post("/chat", json={"message": "bravo"}),
            )

    asyncio.run(_go())

    assert set(observed) == {"alpha", "bravo"}, observed
    for msg, ids in observed.items():
        assert len(set(ids)) == 1, f"{msg} saw shifting ids: {ids}"
        assert ids[0][0] is not None and ids[0][1] is not None, f"{msg} unbound: {ids}"
    a = observed["alpha"][0]
    b = observed["bravo"][0]
    assert a != b, f"two concurrent turns shared identity: {a} == {b}"


def test_binding_is_restored_after_the_response(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No leak to unrelated work on the same task after the turn ends."""
    from openexecutive.audit.context import get_active_ids
    from openexecutive.orchestrator import executive as exec_mod

    class _Tiny:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_k: Any) -> None:
            pass

        async def stream_chat(self, **_k: Any) -> AsyncIterator[str]:
            yield "done "

    monkeypatch.setattr(exec_mod, "Executive", _Tiny)
    app = FastAPI()
    app.include_router(chat_route.router)

    async def _go() -> tuple[str | None, str | None]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            await c.post("/chat", json={"message": "leak check"})
        return get_active_ids()

    assert asyncio.run(_go()) == (None, None)
