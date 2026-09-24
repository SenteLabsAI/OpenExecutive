"""Per-session routes must check who owns the session.

Session ids are guessable (`slack:dm:<user id>`, `telegram:<chat id>`), so
reading, deleting or continuing a chat by id must be limited to its owner or
the principal — the same rule the feedback and followup routes already use.
"""
from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import chat as chat_route
from openexecutive.api.routes import sessions as sessions_route
from openexecutive.memory import episodic, session_store
from openexecutive.memory.company_profile import CompanyProfile
from openexecutive.people import store as people_store

ALEX = {"x-caller-email": "alex@example.com"}  # principal
SABIN = {"x-caller-email": "sabin@example.com"}
RIYA = {"x-caller-email": "riya@example.com"}
STRANGER = {"x-caller-email": "stranger@example.com"}  # signed in, not rostered


@pytest.fixture(autouse=True)
def _reset_route_state() -> None:
    chat_route._sessions.clear()
    chat_route._session_starters.clear()


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    db_path = Path("./episodic_memory.db").resolve()
    monkeypatch.setattr(episodic, "DB_PATH", db_path)
    monkeypatch.setattr(session_store, "DB_PATH", db_path)
    monkeypatch.setattr(people_store, "DB_PATH", tmp_path / "people.db")
    episodic.initialize_db(db_path)
    people_store.initialize_db()
    return db_path


@pytest.fixture()
def people(db: Path) -> dict[str, int]:
    return {
        "alex": people_store.upsert_person(
            full_name="Alex", is_principal=True, email="alex@example.com"
        ),
        "sabin": people_store.upsert_person(full_name="Sabin", email="sabin@example.com"),
        "riya": people_store.upsert_person(full_name="Riya", email="riya@example.com"),
    }


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Both routers, with the Executive and its context fetches stubbed out."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    from openexecutive.knowledge import retriever
    from openexecutive.onboarding import profile_builder
    from openexecutive.orchestrator import executive as exec_mod

    monkeypatch.setattr(profile_builder, "load_or_create_profile", lambda: CompanyProfile())
    monkeypatch.setattr(retriever, "retrieve", lambda *_a, **_k: "")

    class _StubExecutive:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def stream_chat(self, **_kwargs: Any) -> AsyncIterator[str]:
            yield "ok"

        async def stream_chat_with_committee(self, **_kwargs: Any) -> AsyncIterator[str]:
            yield "ok"

    monkeypatch.setattr(exec_mod, "Executive", _StubExecutive)
    app = FastAPI()
    app.include_router(chat_route.router)
    app.include_router(sessions_route.router)
    return TestClient(app)


def _chat(client: TestClient, headers: dict[str, str], session_id: str | None = None) -> int:
    body: dict[str, Any] = {"message": "hi"}
    if session_id is not None:
        body["session_id"] = session_id
    resp = client.post("/chat", json=body, headers=headers)
    _ = resp.text  # drain the SSE body so the turn finishes
    return resp.status_code


def _session_ids(db_path: Path) -> list[str]:
    with sqlite3.connect(db_path) as conn:
        return [r[0] for r in conn.execute("SELECT session_id FROM sessions ORDER BY rowid")]


def _seed(session_id: str, owner: int | None) -> None:
    session_store.create_session(session_id, "t", "2026-01-01T00:00:00", caller_person_id=owner)


# --- read / delete ---------------------------------------------------------


@pytest.mark.parametrize("path", ["/sessions/{id}", "/sessions/{id}/messages"])
def test_reads_are_limited_to_owner_and_principal(
    client: TestClient, people: dict[str, int], path: str
) -> None:
    _seed("slack:dm:USABIN", people["sabin"])
    url = path.format(id="slack:dm:USABIN")

    assert client.get(url, headers=SABIN).status_code == 200
    assert client.get(url, headers=ALEX).status_code == 200
    assert client.get(url, headers=RIYA).status_code == 403
    assert client.get(url, headers=STRANGER).status_code == 403


def test_unknown_session_is_404_not_403(client: TestClient, people: dict[str, int]) -> None:
    assert client.get("/sessions/nope/messages", headers=RIYA).status_code == 404
    assert client.delete("/sessions/nope", headers=RIYA).status_code == 404


def test_legacy_ownerless_session_is_the_principals_alone(
    client: TestClient, people: dict[str, int]
) -> None:
    _seed("legacy", None)

    assert client.get("/sessions/legacy/messages", headers=ALEX).status_code == 200
    assert client.get("/sessions/legacy/messages", headers=SABIN).status_code == 403
    assert client.get("/sessions/legacy/messages", headers=STRANGER).status_code == 403


def test_delete_refuses_someone_elses_session_and_keeps_it(
    client: TestClient, db: Path, people: dict[str, int]
) -> None:
    _seed("sabin-chat", people["sabin"])

    assert client.delete("/sessions/sabin-chat", headers=RIYA).status_code == 403
    assert _session_ids(db) == ["sabin-chat"]

    assert client.delete("/sessions/sabin-chat", headers=SABIN).status_code == 204
    assert _session_ids(db) == []


def test_delete_evicts_the_in_memory_session(
    client: TestClient, db: Path, people: dict[str, int]
) -> None:
    assert _chat(client, SABIN) == 200
    (sid,) = _session_ids(db)
    assert sid in chat_route._sessions

    assert client.delete(f"/sessions/{sid}", headers=SABIN).status_code == 204
    assert sid not in chat_route._sessions
    assert sid not in chat_route._session_starters


# --- continuing a chat -----------------------------------------------------


def test_chat_refuses_to_continue_someone_elses_session(
    client: TestClient, db: Path, people: dict[str, int]
) -> None:
    _seed("sabin-chat", people["sabin"])

    assert _chat(client, RIYA, "sabin-chat") == 403
    assert _chat(client, STRANGER, "sabin-chat") == 403
    assert _chat(client, SABIN, "sabin-chat") == 200
    assert _chat(client, ALEX, "sabin-chat") == 200


def test_forbidden_chat_does_not_strand_a_stop_entry(
    client: TestClient, people: dict[str, int]
) -> None:
    _seed("sabin-chat", people["sabin"])
    before = dict(chat_route._active_stops)

    resp = client.post(
        "/chat",
        json={"message": "hi", "session_id": "sabin-chat", "client_turn_id": "c-123"},
        headers=RIYA,
    )

    assert resp.status_code == 403
    assert chat_route._active_stops == before


def test_unresolved_caller_can_continue_only_their_own_new_chat(
    client: TestClient, db: Path, people: dict[str, int]
) -> None:
    """An allowlisted user who isn't on the roster has no Person id, so their
    chat's row has no owner. They started it in this process, so they may keep
    talking in it; another unresolved user may not."""
    assert _chat(client, STRANGER) == 200
    (sid,) = _session_ids(db)

    assert _chat(client, STRANGER, sid) == 200
    assert client.get(f"/sessions/{sid}/messages", headers=STRANGER).status_code == 200
    assert _chat(client, {"x-caller-email": "other@example.com"}, sid) == 403
    assert _chat(client, SABIN, sid) == 403


def test_fresh_install_without_a_principal_keeps_working(
    client: TestClient, db: Path
) -> None:
    """No roster at all: every caller is unresolved, and a multi-turn chat
    must still work for the person who started it."""
    headers = {"x-caller-email": "founder@example.com"}
    assert _chat(client, headers) == 200
    (sid,) = _session_ids(db)
    assert _chat(client, headers, sid) == 200


def test_channel_namespaced_id_cannot_be_squatted_from_the_web(
    client: TestClient, db: Path, people: dict[str, int]
) -> None:
    """Claiming `slack:dm:<someone>` before their first DM would make the
    caller the owner of that DM's history. The id is ignored instead."""
    assert _chat(client, RIYA, "slack:dm:UVICTIM") == 200

    ids = _session_ids(db)
    assert "slack:dm:UVICTIM" not in ids
    assert len(ids) == 1


def test_web_client_may_still_name_a_new_plain_id(
    client: TestClient, db: Path, people: dict[str, int]
) -> None:
    assert _chat(client, SABIN, "my-own-id") == 200
    assert _session_ids(db) == ["my-own-id"]
