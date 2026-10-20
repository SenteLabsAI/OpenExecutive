"""POST /chat/background: a new chat answered on the server to the end."""
from __future__ import annotations

import asyncio
import time
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
    chat_route._background_counts.clear()
    chat_route._background_tasks.clear()


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
    from openexecutive.utils import session_title

    monkeypatch.setattr(profile_builder, "load_or_create_profile", lambda: CompanyProfile())
    monkeypatch.setattr(retriever, "retrieve", lambda **_k: "")

    async def _no_title(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(session_title, "generate_session_title", _no_title)


def _executive(monkeypatch: pytest.MonkeyPatch, release: asyncio.Event | None = None) -> dict[str, Any]:
    """An Executive that answers in two pieces, optionally waiting on
    `release` between them, and records what it was asked."""
    from openexecutive.orchestrator import executive as exec_mod

    seen: dict[str, Any] = {}

    class _Exec:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def stream_chat(self, *, user_message: str, **kwargs: Any) -> AsyncIterator[str]:
            seen["message"] = user_message
            seen["memory_text"] = kwargs.get("memory_text")
            yield "Done: "
            if release is not None:
                await release.wait()
            yield "sent the plan to Finance."

        async def stream_chat_with_committee(self, **_kwargs: Any) -> AsyncIterator[str]:
            yield "unused"

    monkeypatch.setattr(exec_mod, "Executive", _Exec)
    return seen


def _wait_for_answer(session_id: str, db_path: Path) -> list[dict[str, Any]]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        history = session_store.load_messages(session_id, db_path=db_path)
        if any(m["role"] == "assistant" for m in history):
            return history
        time.sleep(0.05)
    raise AssertionError("the background turn never saved its answer")


def test_background_turn_answers_and_saves_without_a_reader(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _executive(monkeypatch)
    app = FastAPI()
    app.include_router(chat_route.router)
    with TestClient(app) as client:
        resp = client.post(
            "/chat/background",
            json={"message": "I've approved this proposal. Do the work.", "memory_text": "Approved: Q1 plan"},
        )
        assert resp.status_code == 202
        session_id = resp.json()["session_id"]
        history = _wait_for_answer(session_id, temp_db)

    assert [m["role"] for m in history] == ["user", "assistant"]
    assert history[0]["content"] == "I've approved this proposal. Do the work."
    assert history[1]["content"] == "Done: sent the plan to Finance."
    assert seen["memory_text"] == "Approved: Q1 plan"


def test_background_turn_keeps_going_when_the_request_reports_a_disconnect(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A /chat turn stops when its client goes; a background one must not,
    whatever the original request would say."""

    async def _gone(self: Any) -> bool:
        return True

    # The caller's own request reports the client gone; the route must not ask it.
    monkeypatch.setattr("starlette.requests.Request.is_disconnected", _gone)
    _executive(monkeypatch)
    app = FastAPI()
    app.include_router(chat_route.router)
    with TestClient(app) as client:
        resp = client.post("/chat/background", json={"message": "Go ahead."})
        assert resp.status_code == 202
        history = _wait_for_answer(resp.json()["session_id"], temp_db)
    assert history[-1]["content"] == "Done: sent the plan to Finance."


def test_background_turns_are_capped_per_caller(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = FastAPI()
    app.include_router(chat_route.router)
    with TestClient(app) as client:
        release = client.portal.call(asyncio.Event)  # bound to the app's loop
        _executive(monkeypatch, release)
        ids = []
        for _ in range(chat_route._BACKGROUND_TURNS_PER_CALLER):
            resp = client.post("/chat/background", json={"message": "Go ahead."})
            assert resp.status_code == 202
            ids.append(resp.json()["session_id"])
        assert len(set(ids)) == len(ids)
        refused = client.post("/chat/background", json={"message": "One more."})
        assert refused.status_code == 429
        client.portal.call(release.set)
        for session_id in ids:
            _wait_for_answer(session_id, temp_db)
        # Once they finish, the caller can start another.
        deadline = time.monotonic() + 5
        while chat_route._background_counts and time.monotonic() < deadline:
            time.sleep(0.05)
        assert client.post("/chat/background", json={"message": "Next."}).status_code == 202


def test_a_burst_cannot_get_past_the_cap(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The slot is taken before the slow pre-stream work, so requests that
    arrive together are counted together."""
    app = FastAPI()
    app.include_router(chat_route.router)
    with TestClient(app) as client:
        release = client.portal.call(asyncio.Event)
        _executive(monkeypatch, release)
        real = chat_route._run_chat_turn

        async def _slow(**kwargs: Any) -> Any:
            await asyncio.sleep(0.3)  # the context gather before the stream
            return await real(**kwargs)

        monkeypatch.setattr(chat_route, "_run_chat_turn", _slow)

        async def _burst() -> list[int]:
            import httpx

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                replies = await asyncio.gather(
                    *(c.post("/chat/background", json={"message": "Go."}) for _ in range(8))
                )
            return sorted(r.status_code for r in replies)

        codes = client.portal.call(_burst)
        assert codes.count(202) == chat_route._BACKGROUND_TURNS_PER_CALLER
        assert codes.count(429) == 8 - chat_route._BACKGROUND_TURNS_PER_CALLER
        client.portal.call(release.set)


def test_a_refused_turn_gives_its_slot_back(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _refuse(**_kwargs: Any) -> Any:
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail="not linked")

    monkeypatch.setattr(chat_route, "_run_chat_turn", _refuse)
    app = FastAPI()
    app.include_router(chat_route.router)
    with TestClient(app) as client:
        for _ in range(chat_route._BACKGROUND_TURNS_PER_CALLER + 1):
            assert client.post("/chat/background", json={"message": "Go."}).status_code == 403
    assert chat_route._background_counts == {}
