"""Tests for the chat route timeout / hang recovery.

If `Executive.stream_chat` hangs, the route must:
- Detect the timeout via `asyncio.wait_for`.
- Emit an `{type:"error", ...}` SSE event with a timeout message.
- Emit a final `{type:"done", ...}` so the client's stream consumer terminates.
"""
from __future__ import annotations

import asyncio
import json
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
    """Direct query, unscoped — this test asserts row existence, not
    per-user filtering (covered separately in test_session_store)."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT session_id, title FROM sessions ORDER BY updated_at DESC",
        ).fetchall()
    return [dict(r) for r in rows]


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

    from openexecutive.knowledge import retriever
    from openexecutive.onboarding import profile_builder

    monkeypatch.setattr(profile_builder, "load_or_create_profile", lambda: CompanyProfile())
    monkeypatch.setattr(retriever, "retrieve", lambda **_k: "")


def test_chat_endpoint_handles_hanging_executive(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.config import Settings
    from openexecutive.orchestrator import executive as exec_mod

    class _HangingExecutive:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def stream_chat(self, **_kwargs: Any) -> AsyncIterator[str]:
            await asyncio.sleep(60)  # would hang forever in real time
            yield "never"

    monkeypatch.setattr(exec_mod, "Executive", _HangingExecutive)

    # Force a very small timeout so the test completes in <1s.
    original_init = Settings.__init__

    def _short_timeout_init(self: Settings, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        object.__setattr__(self, "chat_stream_timeout_s", 0.2)

    monkeypatch.setattr(Settings, "__init__", _short_timeout_init)

    app = FastAPI()
    app.include_router(chat_route.router)
    client = TestClient(app)

    resp = client.post("/chat", json={"message": "trigger hang"})
    assert resp.status_code == 200
    body = resp.text

    assert '"type": "error"' in body
    assert "timed out" in body.lower()
    assert '"type": "done"' in body

    # Session row was still persisted up-front despite the timeout.
    rows = _all_sessions(temp_db)
    assert len(rows) == 1
    assert rows[0]["title"] == "trigger hang"


def test_debug_events_reach_only_the_callers_own_stream(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn's debug events go to the caller over their own SSE stream. There
    is no process-wide "last turn" snapshot any other caller could read (#198)."""
    from openexecutive.orchestrator import executive as exec_mod

    class _MiniExecutive:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def stream_chat(self, **_kwargs: Any) -> AsyncIterator[str]:
            yield "hi"

    monkeypatch.setattr(exec_mod, "Executive", _MiniExecutive)

    app = FastAPI()
    app.include_router(chat_route.router)
    client = TestClient(app)

    chat_resp = client.post("/chat", json={"message": "hello"})
    assert chat_resp.status_code == 200
    events = [
        json.loads(line[len("data: "):])
        for line in chat_resp.text.splitlines()
        if line.startswith("data: ")
    ]
    debug = [e for e in events if e.get("type") == "debug_event"]
    kinds = [e["kind"] for e in debug]
    assert "knowledge_retrieved" in kinds
    assert "turn_complete" in kinds
    # All events carry the same turn_id.
    assert len({e.get("turn_id") for e in debug}) == 1

    assert client.get("/debug/last-turn").status_code == 404
