"""The `/chat` SSE route forwards `activity` events without touching them.

`api/routes/chat.py` has no event-type allowlist — any non-str stream item is
serialized straight to the wire, and only `action_taken` is pulled aside for
persistence. These tests pin both halves of that:

  * an `activity` event reaches the client verbatim, ahead of `thinking`;
  * it is NOT persisted onto the assistant row as an action chip. The label is
    progress, not a record of something that happened.
"""
from __future__ import annotations

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

MCP_ACTIVITY: dict[str, Any] = {
    "type": "activity",
    "label": "Using google_workspace__send_gmail_message…",
    "tool": "google_workspace__send_gmail_message",
    "iteration": 1,
}
SPECIALIST_ACTIVITY: dict[str, Any] = {
    "type": "activity",
    "label": "Consulting specialists…",
    "tool": "consult_specialist",
    "iteration": 1,
}


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

    async def _no_title(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(_title_mod, "generate_session_title", _no_title)


def _client_streaming(
    monkeypatch: pytest.MonkeyPatch, activity: dict[str, Any]
) -> TestClient:
    """A fake Executive that yields one activity + sentinel + one text chunk."""
    from openexecutive.orchestrator import executive as exec_mod

    class _FakeExecutive:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def stream_chat(self, **_kwargs: Any) -> AsyncIterator[Any]:
            yield activity
            yield self._THINKING
            yield "all set"

    monkeypatch.setattr(exec_mod, "Executive", _FakeExecutive)

    app = FastAPI()
    app.include_router(chat_route.router)
    return TestClient(app)


def _sse_events(body: str) -> list[dict[str, Any]]:
    return [
        json.loads(line[len("data: ") :])
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


def _types(events: list[dict[str, Any]]) -> list[str]:
    return [e.get("type", "") for e in events]


@pytest.mark.parametrize(
    "activity", [MCP_ACTIVITY, SPECIALIST_ACTIVITY], ids=["mcp", "specialist"]
)
def test_activity_event_is_forwarded_verbatim_before_thinking(
    temp_db: Path,
    patched_deps: None,
    monkeypatch: pytest.MonkeyPatch,
    activity: dict[str, Any],
) -> None:
    client = _client_streaming(monkeypatch, activity)
    resp = client.post("/chat", json={"message": "hi"})
    assert resp.status_code == 200

    events = _sse_events(resp.text)
    matches = [e for e in events if e.get("type") == "activity"]
    assert matches == [activity]  # every field, untouched

    types = _types(events)
    assert types.index("activity") < types.index("thinking") < types.index("chunk")


def test_activity_event_is_not_persisted_as_an_action_chip(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guards chat.py's collector against widening to "any dict"."""
    client = _client_streaming(monkeypatch, MCP_ACTIVITY)
    resp = client.post("/chat", json={"message": "hi"})
    assert resp.status_code == 200
    _ = resp.text

    with sqlite3.connect(temp_db) as conn:
        conn.row_factory = sqlite3.Row
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT role, content, action_chips FROM chat_messages "
                "ORDER BY id"
            ).fetchall()
        ]

    assistant = [r for r in rows if r["role"] == "assistant"]
    assert assistant, "assistant turn was not persisted"
    assert assistant[-1]["content"] == "all set"
    assert assistant[-1]["action_chips"] is None
