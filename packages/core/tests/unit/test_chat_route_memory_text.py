"""The web chat routes hand ``memory_text`` to the Executive.

``/chat`` forwards the client's ``memory_text`` — a briefing handoff seeds
the turn with the Executive's own card body, and sends a short line of what
the user actually asked for peer memory to record instead. ``/chat/upload``
records the typed text plus the filenames, never the extracted document text,
which is the document's words and not the caller's.
"""
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
from openexecutive.people import store as people_store


@pytest.fixture(autouse=True)
def _reset_route_state() -> None:
    chat_route._sessions.clear()
    chat_route._last_turn_events.clear()
    chat_route._last_turn_meta.clear()


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.chdir(tmp_path)
    db_path = Path("./episodic_memory.db").resolve()
    monkeypatch.setattr(episodic, "DB_PATH", db_path)
    monkeypatch.setattr(session_store, "DB_PATH", db_path)
    monkeypatch.setattr(people_store, "DB_PATH", tmp_path / "people.db")
    episodic.initialize_db(db_path)
    people_store.initialize_db()
    people_store.upsert_person(full_name="Alex Rivera", role="CEO", is_principal=True)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    from openexecutive.knowledge import retriever
    from openexecutive.onboarding import profile_builder

    monkeypatch.setattr(profile_builder, "load_or_create_profile", lambda: CompanyProfile())
    monkeypatch.setattr(retriever, "retrieve", lambda *a, **k: "")

    app = FastAPI()
    app.include_router(chat_route.router)
    return TestClient(app)


@pytest.fixture()
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from openexecutive.orchestrator import executive as exec_mod

    calls: dict[str, Any] = {}

    class _CapturingExecutive:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def stream_chat(self, **kwargs: Any) -> AsyncIterator[str]:
            calls["stream_chat"] = kwargs
            yield "ok"

        async def stream_chat_with_committee(self, **kwargs: Any) -> AsyncIterator[str]:
            calls["stream_chat_with_committee"] = kwargs
            yield "ok"

    monkeypatch.setattr(exec_mod, "Executive", _CapturingExecutive)
    return calls


SEED = "Let's discuss this artifact you flagged for my review:\n\n# Plan\n\nAssign Sam to sign."
LINE = 'Asked to discuss the flagged artifact "Plan".'


@pytest.mark.parametrize(
    ("committee", "entry"),
    [(False, "stream_chat"), (True, "stream_chat_with_committee")],
)
def test_chat_forwards_memory_text(
    client: TestClient, captured: dict[str, Any], committee: bool, entry: str
) -> None:
    resp = client.post(
        "/chat", json={"message": SEED, "memory_text": LINE, "committee_review": committee}
    )
    assert resp.status_code == 200
    _ = resp.text
    assert captured[entry]["memory_text"] == LINE
    assert captured[entry]["user_message"] == SEED


def test_chat_without_memory_text_forwards_none(
    client: TestClient, captured: dict[str, Any]
) -> None:
    resp = client.post("/chat", json={"message": "hi"})
    assert resp.status_code == 200
    _ = resp.text
    assert captured["stream_chat"]["memory_text"] is None


@pytest.mark.parametrize("bad", ["", "x" * 2001])
def test_chat_rejects_empty_or_oversized_memory_text(
    client: TestClient, captured: dict[str, Any], bad: str
) -> None:
    resp = client.post("/chat", json={"message": "hi", "memory_text": bad})
    assert resp.status_code == 422
    assert captured == {}


def test_upload_records_typed_text_and_filenames_not_document_text(
    client: TestClient, captured: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        chat_route,
        "build_attachment_output",
        lambda filename, data, content_type: (f"[{filename}] EXTRACTED DOCUMENT TEXT", []),
    )
    resp = client.post(
        "/chat/upload",
        data={"message": "Summarise these"},
        files=[
            ("files", ("appraisal (1).pdf", b"%PDF-1.4", "application/pdf")),
            ("files", ("notes.txt", b"hello", "text/plain")),
        ],
    )
    assert resp.status_code == 200
    _ = resp.text
    kwargs = captured["stream_chat"]
    assert kwargs["memory_text"] == "Summarise these\n\n[Attached: appraisal (1).pdf, notes.txt]"
    # The Executive still sees the extracted text.
    assert "EXTRACTED DOCUMENT TEXT" in kwargs["user_message"]


def test_memory_text_is_kept_in_the_chat_turn_audit_row(
    client: TestClient, captured: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """memory_text reaches peer and department memory but not the transcript,
    so the audit row is its only record."""
    rows: list[dict[str, Any]] = []

    def _audit(event_type: str, summary: str, **kwargs: Any) -> None:
        rows.append({"event_type": event_type, **kwargs})

    monkeypatch.setattr(chat_route, "audit_log", _audit)
    resp = client.post("/chat", json={"message": SEED, "memory_text": LINE})
    assert resp.status_code == 200
    _ = resp.text
    turn = next(r for r in rows if r["event_type"] == "chat_turn")
    assert turn["full"] == {"message": SEED, "memory_text": LINE}
