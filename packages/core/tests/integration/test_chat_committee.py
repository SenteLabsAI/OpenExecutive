"""Chat route integration tests for the Committee opt-in path."""
from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

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
    monkeypatch.setattr(profile_builder, "load_or_create_profile", lambda: CompanyProfile())
    monkeypatch.setattr(retriever, "retrieve", lambda **_k: "")


def test_chat_with_committee_streams_phases_and_revised_text(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When committee_review=true, the route must:
    - call stream_chat_with_committee (not stream_chat)
    - pass through phase events as SSE
    - persist only the revised text (not the draft) as the assistant message
    """
    from openexecutive.orchestrator import executive as exec_mod

    class _CommitteeExecutive:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_kwargs: Any) -> None:
            self.committee_called = False
            self.plain_called = False

        async def stream_chat(self, **_kwargs: Any) -> AsyncIterator[str]:
            self.plain_called = True
            yield "draft text"

        async def stream_chat_with_committee(
            self, *, session: Any, **_kwargs: Any
        ) -> AsyncIterator[Any]:
            self.committee_called = True
            yield {"type": "phase", "phase": "drafting", "session_id": session.session_id}
            yield {"type": "phase", "phase": "reviewing", "session_id": session.session_id}
            yield {
                "type": "committee_critique",
                "reviewer": "quality_judge",
                "severity": "medium",
                "session_id": session.session_id,
            }
            yield {"type": "phase", "phase": "finalizing", "session_id": session.session_id}
            yield "Revised "
            yield "executive response."

    monkeypatch.setattr(exec_mod, "Executive", _CommitteeExecutive)

    app = FastAPI()
    app.include_router(chat_route.router)
    client = TestClient(app)

    resp = client.post(
        "/chat",
        json={"message": "How should we price?", "committee_review": True},
    )
    assert resp.status_code == 200
    body = resp.text

    # Phase events made it to the client.
    assert '"phase": "drafting"' in body
    assert '"phase": "reviewing"' in body
    assert '"phase": "finalizing"' in body
    # Critique severity preview also surfaced.
    assert '"reviewer": "quality_judge"' in body
    # Final text chunks streamed through (split across two SSE events).
    assert '"content": "Revised "' in body
    assert '"content": "executive response."' in body
    # Draft text was NOT yielded as a text chunk to the route.
    assert "draft text" not in body
    # done sentinel terminates the stream.
    assert '"type": "done"' in body

    # Persisted assistant message is the revised text, not the draft.
    msgs = session_store.list_sessions(db_path=temp_db)
    assert len(msgs) == 1
    history = session_store.load_messages(msgs[0]["session_id"], db_path=temp_db)
    assistant = [m for m in history if m["role"] == "assistant"]
    assert len(assistant) == 1
    assert assistant[0]["content"] == "Revised executive response."


def test_chat_without_committee_uses_plain_stream_chat(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default path (committee_review omitted/false) must route through
    stream_chat, not stream_chat_with_committee."""
    from openexecutive.orchestrator import executive as exec_mod

    captured: dict[str, bool] = {"committee": False, "plain": False}

    class _Plain:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def stream_chat(self, **_kwargs: Any) -> AsyncIterator[str]:
            captured["plain"] = True
            yield "hello"

        async def stream_chat_with_committee(self, **_kwargs: Any) -> AsyncIterator[str]:
            captured["committee"] = True
            yield "should not be called"

    monkeypatch.setattr(exec_mod, "Executive", _Plain)

    app = FastAPI()
    app.include_router(chat_route.router)
    client = TestClient(app)

    resp = client.post("/chat", json={"message": "hi"})
    assert resp.status_code == 200
    assert captured["plain"] is True
    assert captured["committee"] is False
    assert "hello" in resp.text


def test_committee_timeout_extends_chat_deadline(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """committee_review=true must add committee_extra_timeout_s to the
    whole-turn deadline. Verified by patching Settings to a tiny base
    timeout and a large extra — the request should complete (not time out)
    because the extra carries the slow stream."""
    import asyncio

    from openexecutive.config import Settings
    from openexecutive.orchestrator import executive as exec_mod

    class _SlowCommittee:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def stream_chat_with_committee(
            self, *, session: Any, **_kwargs: Any
        ) -> AsyncIterator[Any]:
            await asyncio.sleep(0.3)  # > base, < base+extra
            yield "done after sleep"

        async def stream_chat(self, **_kwargs: Any) -> AsyncIterator[str]:
            yield "unused"

    monkeypatch.setattr(exec_mod, "Executive", _SlowCommittee)

    original_init = Settings.__init__

    def _short_base_init(self: Settings, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        object.__setattr__(self, "chat_stream_timeout_s", 0.1)
        object.__setattr__(self, "committee_extra_timeout_s", 5.0)

    monkeypatch.setattr(Settings, "__init__", _short_base_init)

    app = FastAPI()
    app.include_router(chat_route.router)
    client = TestClient(app)

    resp = client.post(
        "/chat",
        json={"message": "slow", "committee_review": True},
    )
    assert resp.status_code == 200
    body = resp.text
    # Should NOT have timed out — extra timeout covered the 0.3s sleep.
    assert '"type": "error"' not in body
    assert "done after sleep" in body
