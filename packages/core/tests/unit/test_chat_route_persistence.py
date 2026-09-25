"""Tests for the chat route session handling.

Covers:
- `_get_or_create_session` is idempotent (same id → same Session).
- The chat endpoint persists the session row to SQLite before streaming any
  response chunks, so a mid-turn failure can't leave a ghost in-memory session
  without a DB row.
"""
from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import chat as chat_route
from openexecutive.memory import episodic, session_store
from openexecutive.memory.company_profile import CompanyProfile


def _all_sessions(db_path: Path) -> list[dict[str, Any]]:
    """Direct query, unscoped. These tests verify the persistence layer
    writes the row at all; ownership filtering is covered in
    `test_session_store` and `test_chat_route_principal`.
    """
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT s.session_id, s.title, s.caller_person_id,
                   COUNT(m.id) AS message_count
            FROM sessions s
            LEFT JOIN chat_messages m ON m.session_id = s.session_id
            GROUP BY s.session_id
            ORDER BY s.updated_at DESC
            """,
        ).fetchall()
    return [dict(r) for r in rows]


@pytest.fixture(autouse=True)
def _reset_route_state() -> None:
    chat_route._sessions.clear()
    chat_route._active_stops.clear()


@pytest.fixture()
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # session_store/episodic functions bind DB_PATH as default arg at import
    # time. The default is `Path("./episodic_memory.db")`, i.e. relative to
    # cwd. Chdir so all writes land in tmp_path.
    monkeypatch.chdir(tmp_path)
    db_path = Path("./episodic_memory.db").resolve()
    monkeypatch.setattr(episodic, "DB_PATH", db_path)
    monkeypatch.setattr(session_store, "DB_PATH", db_path)
    episodic.initialize_db(db_path)
    return db_path


class _FakeProfile(CompanyProfile):
    pass


@pytest.fixture()
def patched_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    from openexecutive.onboarding import profile_builder

    def _load_profile() -> CompanyProfile:
        return _FakeProfile()

    monkeypatch.setattr(profile_builder, "load_or_create_profile", _load_profile)

    from openexecutive.knowledge import retriever

    def _retrieve(query: str, specialist_name: Any = None, store: Any = None, **_k: Any) -> str:
        return ""

    monkeypatch.setattr(retriever, "retrieve", _retrieve)

    # Default to a no-op title generator so tests don't reach the live
    # Anthropic API. Individual tests can override this.
    from openexecutive.utils import session_title as _title_mod

    async def _no_title(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(_title_mod, "generate_session_title", _no_title)


def test_get_or_create_session_idempotent(temp_db: Path, patched_deps: None) -> None:
    """Two calls with same session_id return the same Session object."""

    class _Req:
        class _App:
            class _State:
                pass
            state = _State()

        app = _App()

    req: Any = _Req()
    s1 = chat_route._get_or_create_session("abc-123", req)
    s2 = chat_route._get_or_create_session("abc-123", req)
    assert s1 is s2
    assert s1.session_id == "abc-123"


def test_chat_endpoint_persists_session_before_streaming(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even if the executive yields nothing, the session row is in SQLite."""
    from openexecutive.orchestrator import executive as exec_mod

    class _EmptyExecutive:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def stream_chat(self, **_kwargs: Any) -> AsyncIterator[str]:
            # Yield nothing — simulates an immediate failure with no chunks.
            if False:
                yield ""
            return

    monkeypatch.setattr(exec_mod, "Executive", _EmptyExecutive)

    app = FastAPI()
    app.include_router(chat_route.router)
    client = TestClient(app)

    resp = client.post("/chat", json={"message": "hello world"})
    assert resp.status_code == 200
    # Drain the SSE body.
    _ = resp.text

    rows = _all_sessions(temp_db)
    assert len(rows) == 1
    assert rows[0]["title"] == "hello world"
    # No assistant response was produced → no chat_messages rows.
    assert rows[0]["message_count"] == 0


def test_chat_endpoint_persists_messages_on_success(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.orchestrator import executive as exec_mod

    class _RealExecutive:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def stream_chat(self, **_kwargs: Any) -> AsyncIterator[str]:
            for piece in ("Hello", " ", "world"):
                yield piece
                await asyncio.sleep(0)

    monkeypatch.setattr(exec_mod, "Executive", _RealExecutive)

    app = FastAPI()
    app.include_router(chat_route.router)
    client = TestClient(app)

    resp = client.post("/chat", json={"message": "ping"})
    assert resp.status_code == 200
    body = resp.text
    assert '"type": "done"' in body
    assert '"type": "chunk"' in body

    msgs = _all_sessions(temp_db)
    assert len(msgs) == 1
    assert msgs[0]["message_count"] == 2  # user + assistant


def test_chat_endpoint_persists_partial_turn_on_disconnect(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the client disconnects mid-response, the user message and the
    partial assistant reply produced so far must still be persisted — leaving
    a chat mid-stream should not lose the turn."""
    import starlette.requests

    from openexecutive.orchestrator import executive as exec_mod

    class _SlowExecutive:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def stream_chat(self, **_kwargs: Any) -> AsyncIterator[str]:
            # Stream forever; the route should break out when the client
            # "disconnects" (simulated below) after the first chunk.
            yield "partial answer"
            while True:
                await asyncio.sleep(0)
                yield "..."

    monkeypatch.setattr(exec_mod, "Executive", _SlowExecutive)

    # `is_disconnected()` is polled at the top of every loop iteration. Report
    # connected for the first poll (so one chunk streams), then disconnected.
    poll_count = {"n": 0}

    async def _fake_is_disconnected(self: Any) -> bool:
        poll_count["n"] += 1
        return poll_count["n"] > 1

    monkeypatch.setattr(
        starlette.requests.Request, "is_disconnected", _fake_is_disconnected
    )

    app = FastAPI()
    app.include_router(chat_route.router)
    client = TestClient(app)

    resp = client.post("/chat", json={"message": "explain our burn rate"})
    assert resp.status_code == 200
    _ = resp.text  # drain SSE

    rows = _all_sessions(temp_db)
    assert len(rows) == 1
    # User + partial assistant both persisted despite the disconnect.
    assert rows[0]["message_count"] == 2

    saved = session_store.load_messages(rows[0]["session_id"])
    assert [m["role"] for m in saved] == ["user", "assistant"]
    assert saved[0]["content"] == "explain our burn rate"
    assert saved[1]["content"] == "partial answer"

    # The live in-memory Session must agree with what was persisted. The
    # Executive's own post-turn block is skipped when its generator is closed
    # early, and `_get_or_create_session` never re-reads history for a session
    # it already holds — so without the route mirroring the turn here, the NEXT
    # turn in this process would prompt as though this one never happened while
    # a page reload showed it.
    session = chat_route._sessions[rows[0]["session_id"]]
    assert session.conversation_history == [
        {"role": "user", "content": "explain our burn rate"},
        {"role": "assistant", "content": "partial answer"},
    ]


def test_chat_endpoint_renames_session_after_first_turn(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a successful first turn, the session title is replaced by the
    Haiku-generated topic title — not the truncated user message."""
    from openexecutive.orchestrator import executive as exec_mod
    from openexecutive.utils import session_title as _title_mod

    class _RealExecutive:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def stream_chat(self, **_kwargs: Any) -> AsyncIterator[str]:
            for piece in ("Pricing tiers", " ", "depend on"):
                yield piece
                await asyncio.sleep(0)

    monkeypatch.setattr(exec_mod, "Executive", _RealExecutive)

    captured: dict[str, tuple[str, str]] = {}

    async def _fake_title(user_msg: str, assistant_msg: str, **_kwargs: Any) -> str:
        captured["call"] = (user_msg, assistant_msg)
        return "Enterprise pricing strategy"

    monkeypatch.setattr(_title_mod, "generate_session_title", _fake_title)

    app = FastAPI()
    app.include_router(chat_route.router)
    client = TestClient(app)

    resp = client.post(
        "/chat",
        json={"message": "How should we price the enterprise tier?"},
    )
    assert resp.status_code == 200
    _ = resp.text  # drain SSE

    rows = _all_sessions(temp_db)
    assert len(rows) == 1
    assert rows[0]["title"] == "Enterprise pricing strategy"
    assert captured["call"][0] == "How should we price the enterprise tier?"
    assert captured["call"][1] == "Pricing tiers depend on"


def test_chat_endpoint_keeps_fallback_title_when_llm_fails(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If generate_session_title returns None (e.g. Haiku failure), the
    truncated-message fallback title set by create_session() is preserved."""
    from openexecutive.orchestrator import executive as exec_mod
    from openexecutive.utils import session_title as _title_mod

    class _RealExecutive:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def stream_chat(self, **_kwargs: Any) -> AsyncIterator[str]:
            yield "ok"

    monkeypatch.setattr(exec_mod, "Executive", _RealExecutive)

    async def _fail(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(_title_mod, "generate_session_title", _fail)

    app = FastAPI()
    app.include_router(chat_route.router)
    client = TestClient(app)

    resp = client.post("/chat", json={"message": "what's our runway?"})
    assert resp.status_code == 200
    _ = resp.text

    rows = _all_sessions(temp_db)
    assert len(rows) == 1
    assert rows[0]["title"] == "what's our runway?"  # fallback preserved


def test_chat_endpoint_survives_title_gen_exception(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raise from generate_session_title must not break the turn — the
    response is still persisted, `done` still fires, and the session keeps
    its fallback title."""
    from openexecutive.orchestrator import executive as exec_mod
    from openexecutive.utils import session_title as _title_mod

    class _RealExecutive:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def stream_chat(self, **_kwargs: Any) -> AsyncIterator[str]:
            yield "answer"

    monkeypatch.setattr(exec_mod, "Executive", _RealExecutive)

    async def _boom(*_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError("simulated Anthropic outage")

    monkeypatch.setattr(_title_mod, "generate_session_title", _boom)

    app = FastAPI()
    app.include_router(chat_route.router)
    client = TestClient(app)

    resp = client.post("/chat", json={"message": "what is the meaning of life?"})
    assert resp.status_code == 200
    body = resp.text
    assert '"type": "done"' in body  # turn still completes

    rows = _all_sessions(temp_db)
    assert len(rows) == 1
    assert rows[0]["title"] == "what is the meaning of life?"  # fallback held
    assert rows[0]["message_count"] == 2  # user + assistant both persisted
