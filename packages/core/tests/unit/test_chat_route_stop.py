"""Tests for stopping an in-flight chat turn (the Stop button).

Covers the SSE side of `POST /chat/stop`:
- A stop mid-stream terminates the turn with `stopped` then `done` (never
  `error` — a stop is a user decision, not a failure).
- The partial reply is persisted AND flagged, and lands in the live in-memory
  Session so the next turn's prompt matches what was saved.
- A stop that lands before the Executive produces anything persists nothing.
- The registry entry is always released.
- The whole-turn deadline still fires when a stop switch exists but is never
  flipped (i.e. racing the stop waiter did not break `wait_for`'s semantics).

Note on the harness: FastAPI's `TestClient` buffers the whole ASGI response
before handing it back, so a *concurrent* stop request cannot be delivered
mid-stream from the test thread. These tests instead flip the switch from
inside the fake Executive's own generator, which exercises the same race and
cancellation path deterministically. The endpoint's own contract (404s, caller
matching) is covered in `test_chat_stop_endpoint.py`.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import chat as chat_route
from openexecutive.memory import episodic, session_store
from openexecutive.memory.company_profile import CompanyProfile

CLIENT_ID = "stop-me-0000-0001"


def _all_sessions(db_path: Path) -> list[dict[str, Any]]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT s.session_id, COUNT(m.id) AS message_count
            FROM sessions s
            LEFT JOIN chat_messages m ON m.session_id = s.session_id
            GROUP BY s.session_id
            """,
        ).fetchall()
    return [dict(r) for r in rows]


def _events(body: str) -> list[dict[str, Any]]:
    """Parse the SSE body into the list of JSON payloads it carried."""
    out: list[dict[str, Any]] = []
    for line in body.splitlines():
        if line.startswith("data: "):
            out.append(json.loads(line[6:]))
    return out


@pytest.fixture(autouse=True)
def _reset_route_state() -> None:
    chat_route._sessions.clear()
    chat_route._active_stops.clear()


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

    from openexecutive.onboarding import profile_builder

    monkeypatch.setattr(
        profile_builder, "load_or_create_profile", lambda: CompanyProfile()
    )

    from openexecutive.knowledge import retriever

    monkeypatch.setattr(
        retriever,
        "retrieve",
        lambda query, specialist_name=None, store=None, **_k: "",
    )

    from openexecutive.utils import session_title as _title_mod

    async def _no_title(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(_title_mod, "generate_session_title", _no_title)

    # The audit logger writes to the default ./episodic_memory.db unless the
    # test isolates it; a stopped turn now emits an outbound row, so without
    # this the rows leak into other modules' assertions on a full-suite run.
    from openexecutive import audit

    monkeypatch.setattr(audit, "log_event", lambda *a, **k: None)


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(chat_route.router)
    return TestClient(app)


def test_stop_mid_stream_ends_turn_and_keeps_partial(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop after the first chunk ends the turn cleanly and keeps the text."""
    from openexecutive.orchestrator import executive as exec_mod

    class _StoppableExecutive:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def stream_chat(self, **_kwargs: Any) -> Any:
            yield "partial answer"
            # Resumed by the route's anext task. Flip the switch, then block:
            # the route's stop waiter must win the race and cancel this sleep.
            # If cancellation is broken the test hangs here instead of
            # finishing, which the elapsed-time assertion below catches.
            chat_route._request_stop(CLIENT_ID, "local")
            await asyncio.sleep(30)
            yield "never reached"

    monkeypatch.setattr(exec_mod, "Executive", _StoppableExecutive)

    t0 = time.monotonic()
    resp = _client().post(
        "/chat", json={"message": "explain our burn rate", "client_turn_id": CLIENT_ID}
    )
    elapsed = time.monotonic() - t0

    assert resp.status_code == 200
    # The cancel must be immediate, not "eventually". 30s would mean the stop
    # waiter never won and we simply drained the fake stream.
    assert elapsed < 10, f"stop took {elapsed:.1f}s — cancellation is not working"

    types = [e.get("type") for e in _events(resp.text)]
    assert "stopped" in types
    assert "done" in types
    assert types.index("stopped") < types.index("done")
    # A stop is not a timeout and not a failure.
    assert "error" not in types

    rows = _all_sessions(temp_db)
    assert len(rows) == 1
    assert rows[0]["message_count"] == 2  # user + partial assistant

    saved = session_store.load_messages(rows[0]["session_id"])
    assert [m["role"] for m in saved] == ["user", "assistant"]
    assert saved[1]["content"] == "partial answer"
    assert saved[1]["stopped"] is True
    # The user turn is not a stopped message.
    assert "stopped" not in saved[0]

    # The live Session must agree with the DB, or the next turn in this process
    # would prompt as though the stopped turn never happened.
    session = chat_route._sessions[rows[0]["session_id"]]
    assert session.conversation_history == [
        {"role": "user", "content": "explain our burn rate"},
        {"role": "assistant", "content": "partial answer"},
    ]

    # Registry released.
    assert chat_route._active_stops == {}


def test_stop_before_first_chunk_persists_nothing(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop that lands before any output costs nothing and saves nothing.

    This is the pre-stream window (the context fan-out): the Executive is never
    asked for a first step, so no model call is ever made.
    """
    from openexecutive.orchestrator import executive as exec_mod

    started = {"n": 0}

    class _NeverRunExecutive:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def stream_chat(self, **_kwargs: Any) -> Any:
            started["n"] += 1
            yield "should never be produced"

    monkeypatch.setattr(exec_mod, "Executive", _NeverRunExecutive)

    # Hand back an already-set event, simulating a stop that arrived while the
    # route was still fetching context.
    real_register = chat_route._register_stop

    def _pre_set(client_turn_id: str, caller: Any, turn_id: str) -> asyncio.Event:
        event = real_register(client_turn_id, caller, turn_id)
        event.set()
        return event

    monkeypatch.setattr(chat_route, "_register_stop", _pre_set)

    resp = _client().post(
        "/chat", json={"message": "never mind", "client_turn_id": CLIENT_ID}
    )
    assert resp.status_code == 200

    types = [e.get("type") for e in _events(resp.text)]
    assert "stopped" in types
    assert "done" in types
    assert "chunk" not in types
    assert started["n"] == 0, "the executive stream was driven despite a pre-set stop"

    rows = _all_sessions(temp_db)
    assert len(rows) == 1
    # Nothing to save: no response text means no user/assistant pair, which
    # keeps the stored history a clean alternation for the next turn.
    assert rows[0]["message_count"] == 0
    assert chat_route._active_stops == {}


def test_normal_turn_is_unaffected_and_releases_registry(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn that is never stopped behaves exactly as before."""
    from openexecutive.orchestrator import executive as exec_mod

    class _NormalExecutive:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def stream_chat(self, **_kwargs: Any) -> Any:
            yield "complete "
            yield "answer"

    monkeypatch.setattr(exec_mod, "Executive", _NormalExecutive)

    resp = _client().post(
        "/chat", json={"message": "hello", "client_turn_id": CLIENT_ID}
    )
    assert resp.status_code == 200

    types = [e.get("type") for e in _events(resp.text)]
    assert "stopped" not in types
    assert "error" not in types
    assert types[-1] == "done"

    rows = _all_sessions(temp_db)
    saved = session_store.load_messages(rows[0]["session_id"])
    assert saved[1]["content"] == "complete answer"
    # Not flagged, and the key is absent rather than False.
    assert "stopped" not in saved[1]

    # The Executive's own post-turn block owns the in-memory mirror on this
    # path; the route must not double-append.
    session = chat_route._sessions[rows[0]["session_id"]]
    assert session.conversation_history == []

    assert chat_route._active_stops == {}


def test_turn_without_client_turn_id_still_streams(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An omitted or malformed id only means the turn isn't stoppable."""
    from openexecutive.orchestrator import executive as exec_mod

    class _NormalExecutive:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def stream_chat(self, **_kwargs: Any) -> Any:
            yield "fine"

    monkeypatch.setattr(exec_mod, "Executive", _NormalExecutive)

    resp = _client().post("/chat", json={"message": "hi"})
    assert resp.status_code == 200
    assert [e.get("type") for e in _events(resp.text)][-1] == "done"
    assert chat_route._active_stops == {}


def test_timeout_still_fires_with_an_unused_stop_waiter(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deadline must survive racing an unused stop waiter.

    Regression guard for the `wait_for` → `asyncio.wait` rewrite: a stop switch
    that is registered but never flipped must not swallow the whole-turn
    deadline.
    """
    from openexecutive.config import Settings
    from openexecutive.orchestrator import executive as exec_mod

    class _HangingExecutive:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def stream_chat(self, **_kwargs: Any) -> Any:
            yield "started"
            await asyncio.sleep(30)
            yield "never"

    monkeypatch.setattr(exec_mod, "Executive", _HangingExecutive)

    class _FastTimeout(Settings):
        def __init__(self, **kw: Any) -> None:
            super().__init__(**kw)
            object.__setattr__(self, "chat_stream_timeout_s", 0.2)

    from openexecutive import config as config_mod

    monkeypatch.setattr(config_mod, "get_settings", lambda: _FastTimeout())
    monkeypatch.setattr(chat_route, "get_settings", _FastTimeout, raising=False)

    t0 = time.monotonic()
    resp = _client().post(
        "/chat", json={"message": "hang please", "client_turn_id": CLIENT_ID}
    )
    elapsed = time.monotonic() - t0

    assert resp.status_code == 200
    assert elapsed < 10, "the deadline did not fire — the stop waiter swallowed it"

    events = _events(resp.text)
    types = [e.get("type") for e in events]
    assert "error" in types
    assert "stopped" not in types
    assert types[-1] == "done"
    assert chat_route._active_stops == {}


def test_item_produced_in_the_same_tick_as_the_stop_is_not_dropped(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tie between the stop waiter and a produced item must favour the item.

    Both futures can complete in the same tick. Branching on the stop first
    would silently discard work already paid for — and an `action_taken` dict
    dropped here never reaches the chips persisted with the message, so the
    user loses the record of a side effect that really happened.
    """
    from openexecutive.orchestrator import executive as exec_mod

    class _ChipThenStopExecutive:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def stream_chat(self, **_kwargs: Any) -> Any:
            yield "sending the note"
            # Stop lands BEFORE the chip is yielded, so by the time the route's
            # `asyncio.wait` returns, both the item and the stop waiter are
            # ready. The chip must still be delivered and persisted.
            chat_route._request_stop(CLIENT_ID, "local")
            yield {
                "type": "action_taken",
                "tool": "send_dm",
                "summary": "DM sent to Priya",
            }
            await asyncio.sleep(30)

    monkeypatch.setattr(exec_mod, "Executive", _ChipThenStopExecutive)

    t0 = time.monotonic()
    resp = _client().post(
        "/chat", json={"message": "tell priya", "client_turn_id": CLIENT_ID}
    )
    assert time.monotonic() - t0 < 10

    types = [e.get("type") for e in _events(resp.text)]
    assert "action_taken" in types, "the chip was dropped by the stop"
    assert "stopped" in types
    assert types.index("action_taken") < types.index("stopped")

    rows = _all_sessions(temp_db)
    saved = session_store.load_messages(rows[0]["session_id"])
    assert saved[1]["stopped"] is True
    # And it survived into the persisted chips, not just the live stream.
    assert saved[1]["actions"] == [
        {"type": "action_taken", "tool": "send_dm", "summary": "DM sent to Priya"}
    ]


def test_partial_is_persisted_before_the_stopped_frame_goes_out(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Persist must precede the terminal frames on the stop path.

    Yielding `stopped` first opens a gap in which closing the tab loses the
    partial reply — the one thing the feature exists to preserve. Asserted by
    recording the DB row count at the moment the `stopped` frame is written.
    """
    from openexecutive.memory import session_store as store_mod
    from openexecutive.orchestrator import executive as exec_mod

    class _StoppableExecutive:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def stream_chat(self, **_kwargs: Any) -> Any:
            yield "partial answer"
            chat_route._request_stop(CLIENT_ID, "local")
            await asyncio.sleep(30)

    monkeypatch.setattr(exec_mod, "Executive", _StoppableExecutive)

    saved_before_frame: list[int] = []
    real_save = store_mod.save_message
    order: list[str] = []

    def _tracking_save(*a: Any, **k: Any) -> Any:
        order.append("save")
        return real_save(*a, **k)

    monkeypatch.setattr(store_mod, "save_message", _tracking_save)

    resp = _client().post(
        "/chat", json={"message": "burn rate?", "client_turn_id": CLIENT_ID}
    )
    body = resp.text
    for line in body.splitlines():
        if '"stopped"' in line:
            saved_before_frame.append(len(order))
            break

    # Both rows (user + assistant) are written before the stopped frame.
    assert saved_before_frame == [2], (
        f"expected 2 saves before the stopped frame, saw {saved_before_frame}"
    )


def test_stopped_first_turn_renames_without_blocking_the_stream(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rename must not hold the stream open, but must still happen.

    It is a Haiku round-trip with a ~10s budget, so awaiting it on the stop
    path would make the button slower than the stop it reports. Skipping it
    outright is not an option either: the rename only ever runs on a first
    turn, and the stopped turn is now part of the history, so turn 2 would not
    be a first turn and the placeholder title would stick forever.
    """
    from openexecutive.orchestrator import executive as exec_mod
    from openexecutive.utils import session_title as title_mod

    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_title(*_a: Any, **_k: Any) -> str:
        started.set()
        await release.wait()
        return "Generated Title"

    monkeypatch.setattr(title_mod, "generate_session_title", _slow_title)

    class _StoppableExecutive:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def stream_chat(self, **_kwargs: Any) -> Any:
            yield "partial"
            chat_route._request_stop(CLIENT_ID, "local")
            await asyncio.sleep(30)

    monkeypatch.setattr(exec_mod, "Executive", _StoppableExecutive)

    t0 = time.monotonic()
    resp = _client().post(
        "/chat", json={"message": "hello there", "client_turn_id": CLIENT_ID}
    )
    elapsed = time.monotonic() - t0

    assert resp.status_code == 200
    # The stream closed without waiting on the (still-blocked) title call.
    assert elapsed < 10, f"the stream waited on the rename ({elapsed:.1f}s)"
    assert '"type": "done"' in resp.text
    # ...but the rename was dispatched rather than dropped.
    assert started.is_set(), "the stopped first turn never attempted a rename"
    release.set()


def test_registry_is_released_when_the_session_load_fails(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure between registration and the streaming body must not strand
    the entry.

    `_sse_body`'s `finally` only runs once Starlette consumes the generator, so
    anything raising before that has to release the switch itself. Stranded
    entries are effectively permanent — the registry refuses new turns at its
    cap rather than evicting live ones — so enough of them would take Stop away
    from everybody until a restart.
    """
    def _boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("profile.yaml no longer parses")

    monkeypatch.setattr(chat_route, "_get_or_create_session", _boom)

    with pytest.raises(RuntimeError):
        _client().post(
            "/chat", json={"message": "hi", "client_turn_id": CLIENT_ID}
        )

    assert chat_route._active_stops == {}, "the stop entry was stranded"
