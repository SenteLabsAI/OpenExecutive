"""A message added mid-turn (POST /chat/add) is saved with the turn it joined.

Same harness as test_chat_route_stop.py: TestClient buffers the whole SSE
response, so the fake Executive delivers the message from inside its own
generator, through the same registry the endpoint uses.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import chat as chat_route
from openexecutive.memory import episodic, session_store
from openexecutive.memory.company_profile import CompanyProfile

CLIENT_ID = "add-to-me-0000-01"


def _events(body: str) -> list[dict[str, Any]]:
    return [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]


@pytest.fixture(autouse=True)
def _reset_route_state() -> None:
    chat_route._sessions.clear()
    chat_route._active_stops.clear()
    chat_route._session_turn_locks.clear()


@pytest.fixture()
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    db_path = Path("./episodic_memory.db").resolve()
    monkeypatch.setattr(episodic, "DB_PATH", db_path)
    monkeypatch.setattr(session_store, "DB_PATH", db_path)
    episodic.initialize_db(db_path)
    return db_path


@pytest.fixture(autouse=True)
def patched_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    from openexecutive import audit
    from openexecutive.knowledge import retriever
    from openexecutive.onboarding import profile_builder
    from openexecutive.utils import session_title as _title_mod

    monkeypatch.setattr(profile_builder, "load_or_create_profile", lambda: CompanyProfile())
    monkeypatch.setattr(retriever, "retrieve", lambda query, specialist_name=None, store=None, **_k: "")

    async def _no_title(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(_title_mod, "generate_session_title", _no_title)
    monkeypatch.setattr(audit, "log_event", lambda *a, **k: None)


def _fake_executive(monkeypatch: pytest.MonkeyPatch, *, late: bool = False) -> None:
    """A turn that is sent "make it Friday" while it works. With ``late`` the
    message arrives after the turn's last step, so it is never taken."""
    from openexecutive.orchestrator import executive as exec_mod

    class _Exec:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def stream_chat(self, *, inbox: Any = None, session: Any, user_message: str, **_k: Any) -> Any:
            assert inbox is not None
            assert chat_route._add_to_turn(CLIENT_ID, "local", "msg-000001", "make it Friday")
            if not late:
                taken = inbox.take()
                yield {"type": "message_added", "ids": [m.id for m in taken]}
            yield "Booked for Friday."
            session.add_user_message(user_message)
            for t in inbox.taken_texts():
                session.add_user_message(t)
            session.add_assistant_message("Booked for Friday.")

    monkeypatch.setattr(exec_mod, "Executive", _Exec)


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(chat_route.router)
    return TestClient(app)


def test_added_message_is_saved_between_the_turn_and_its_reply(
    temp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_executive(monkeypatch)
    resp = _client().post("/chat", json={"message": "book the room", "client_turn_id": CLIENT_ID})

    events = _events(resp.text)
    assert {"type": "message_added", "ids": ["msg-000001"]} in events
    session_id = events[-1]["session_id"]
    saved = session_store.load_messages(session_id)
    assert [(m["role"], m["content"]) for m in saved] == [
        ("user", "book the room"),
        ("user", "make it Friday"),
        ("assistant", "Booked for Friday."),
    ]
    # Closed with the turn: nothing more can join it.
    assert chat_route._active_stops == {}
    assert chat_route._add_to_turn(CLIENT_ID, "local", "msg-000002", "x") is None


def test_message_the_turn_never_took_is_not_saved(
    temp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_executive(monkeypatch, late=True)
    resp = _client().post("/chat", json={"message": "book the room", "client_turn_id": CLIENT_ID})

    events = _events(resp.text)
    assert not any(e.get("type") == "message_added" for e in events)
    saved = session_store.load_messages(events[-1]["session_id"])
    # The client sends it as the next turn instead.
    assert [m["content"] for m in saved] == ["book the room", "Booked for Friday."]


def test_continuing_a_conversation_releases_its_turn_lock(
    temp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_executive(monkeypatch)
    first = _client().post("/chat", json={"message": "book the room", "client_turn_id": CLIENT_ID})
    session_id = _events(first.text)[-1]["session_id"]

    second = _client().post(
        "/chat",
        json={"message": "and lunch", "session_id": session_id, "client_turn_id": CLIENT_ID},
    )
    assert second.status_code == 200
    assert _events(second.text)[-1]["type"] == "done"
    assert chat_route._session_turn_locks == {}
