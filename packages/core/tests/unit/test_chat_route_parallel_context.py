"""Verify the chat route runs retrieve, format_for_prompt, and the Honcho
prefetch concurrently — and that the resolved peer_memory_context is passed
through to the Executive so the inner stream_chat skips its own prefetch.

Background: production data showed Honcho's `low` prefetch averaging ~2.9s.
Running it serially before the model call paid the full latency every turn.
"""
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
from openexecutive.people import store as people_store


@pytest.fixture(autouse=True)
def _reset_route_state() -> None:
    chat_route._sessions.clear()


@pytest.fixture()
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    db_path = Path("./episodic_memory.db").resolve()
    monkeypatch.setattr(episodic, "DB_PATH", db_path)
    monkeypatch.setattr(session_store, "DB_PATH", db_path)
    monkeypatch.setattr(people_store, "DB_PATH", tmp_path / "people.db")
    episodic.initialize_db(db_path)
    people_store.initialize_db()
    return db_path


class _FakeProfile(CompanyProfile):
    pass


@pytest.fixture()
def patched_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    from openexecutive.onboarding import profile_builder

    monkeypatch.setattr(profile_builder, "load_or_create_profile", lambda: _FakeProfile())


def _install_capturing_executive(monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]) -> None:
    from openexecutive.orchestrator import executive as exec_mod

    class _CapturingExecutive:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def stream_chat(self, **kwargs: Any) -> AsyncIterator[str]:
            captured["stream_chat"] = kwargs
            yield "ok"

        async def stream_chat_with_committee(self, **kwargs: Any) -> AsyncIterator[str]:
            captured["stream_chat_with_committee"] = kwargs
            yield "ok"

    monkeypatch.setattr(exec_mod, "Executive", _CapturingExecutive)


def test_chat_route_passes_peer_memory_context_to_executive(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route must pass the pre-resolved peer_memory_context to stream_chat
    so the Executive skips its own prefetch (the whole point of the refactor).
    """
    people_store.upsert_person(full_name="Alex", is_principal=True)

    captured: dict[str, Any] = {}
    _install_capturing_executive(monkeypatch, captured)

    from openexecutive.knowledge import retriever as retriever_mod
    from openexecutive.memory import honcho_client as honcho_mod

    monkeypatch.setattr(retriever_mod, "retrieve", lambda **_: "RAG-RESULT")

    async def fake_prefetch(*_args: Any, **_kw: Any) -> str:
        return "PREFETCHED-PEER-MEMORY"

    monkeypatch.setattr(honcho_mod, "prefetch", fake_prefetch)

    app = FastAPI()
    app.include_router(chat_route.router)
    client = TestClient(app)

    resp = client.post("/chat", json={"message": "hi there team"})
    assert resp.status_code == 200
    _ = resp.text

    assert "stream_chat" in captured
    assert captured["stream_chat"]["peer_memory_context"] == "PREFETCHED-PEER-MEMORY"
    assert captured["stream_chat"]["retrieved_context"] == "RAG-RESULT"


def test_chat_route_runs_context_fetches_in_parallel(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """retrieve, format_for_prompt, and honcho.prefetch must run concurrently.
    Each is given a controlled 2.0s delay; total wall-clock should be ~2s
    (parallel) not ~6s (serial). Bound at 5s — gives FastAPI/SSE/TestClient
    overhead room on loaded CI runners while still catching any serial
    regression by a clear margin (a serial run would blow past 5s by ~1s+)."""
    people_store.upsert_person(full_name="Alex", is_principal=True)

    _install_capturing_executive(monkeypatch, {})

    from openexecutive.knowledge import retriever as retriever_mod
    from openexecutive.memory import episodic as episodic_mod
    from openexecutive.memory import honcho_client as honcho_mod

    SLEEP_S = 2.0

    def slow_retrieve(**_: Any) -> str:
        time.sleep(SLEEP_S)
        return "rag"

    def slow_format() -> str:
        time.sleep(SLEEP_S)
        return "episodic"

    async def slow_prefetch(*_a: Any, **_kw: Any) -> str:
        await asyncio.sleep(SLEEP_S)
        return "peer"

    monkeypatch.setattr(retriever_mod, "retrieve", slow_retrieve)
    monkeypatch.setattr(episodic_mod, "format_for_prompt", slow_format)
    monkeypatch.setattr(honcho_mod, "prefetch", slow_prefetch)

    app = FastAPI()
    app.include_router(chat_route.router)
    client = TestClient(app)

    t0 = time.monotonic()
    resp = client.post("/chat", json={"message": "hi there team"})
    assert resp.status_code == 200
    _ = resp.text
    elapsed = time.monotonic() - t0

    # Serial would be ~6.0s; parallel ~2.0s. 5.0s ceiling absorbs CI-runner
    # overhead variance while still catching a serial regression (~6s) by ~1s.
    assert elapsed < 5.0, f"context fetches ran serially: {elapsed:.2f}s"


def test_chat_route_passes_briefing_context_to_executive(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route resolves the open-alert digest and passes it to stream_chat so
    the Executive can discuss an item the principal clicked/named on /today."""
    people_store.upsert_person(full_name="Alex", is_principal=True)

    captured: dict[str, Any] = {}
    _install_capturing_executive(monkeypatch, captured)

    from openexecutive.alerts import store as alerts_store
    from openexecutive.knowledge import retriever as retriever_mod
    from openexecutive.memory import honcho_client as honcho_mod

    # Point the alerts store at its own temp DB and seed one open alert.
    alerts_db = temp_db.parent / "alerts.db"
    monkeypatch.setattr(alerts_store, "DB_PATH", alerts_db)
    alerts_store.initialize_db(alerts_db)
    alerts_store.insert_alert(
        source="email",
        external_id="gulf-1",
        severity="high",
        headline="Gulf Coast Port Cyberattack",
        body="Refined product movement blocked.",
        db_path=alerts_db,
    )

    monkeypatch.setattr(retriever_mod, "retrieve", lambda **_: "")

    async def fake_prefetch(*_a: Any, **_kw: Any) -> str:
        return ""

    monkeypatch.setattr(honcho_mod, "prefetch", fake_prefetch)

    app = FastAPI()
    app.include_router(chat_route.router)
    client = TestClient(app)

    resp = client.post("/chat", json={"message": "what's going on with the port?"})
    assert resp.status_code == 200
    _ = resp.text

    assert "stream_chat" in captured
    assert "briefing_context" in captured["stream_chat"]
    assert "Gulf Coast Port Cyberattack" in captured["stream_chat"]["briefing_context"]


def test_chat_committee_route_passes_peer_memory_context(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same propagation contract for the committee-review path."""
    people_store.upsert_person(full_name="Alex", is_principal=True)

    captured: dict[str, Any] = {}
    _install_capturing_executive(monkeypatch, captured)

    from openexecutive.knowledge import retriever as retriever_mod
    from openexecutive.memory import honcho_client as honcho_mod

    monkeypatch.setattr(retriever_mod, "retrieve", lambda **_: "")

    async def fake_prefetch(*_a: Any, **_kw: Any) -> str:
        return "COMMITTEE-PEER-MEMORY"

    monkeypatch.setattr(honcho_mod, "prefetch", fake_prefetch)

    app = FastAPI()
    app.include_router(chat_route.router)
    client = TestClient(app)

    resp = client.post("/chat", json={"message": "weigh in committee", "committee_review": True})
    assert resp.status_code == 200
    _ = resp.text

    assert "stream_chat_with_committee" in captured
    assert (
        captured["stream_chat_with_committee"]["peer_memory_context"]
        == "COMMITTEE-PEER-MEMORY"
    )
