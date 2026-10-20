"""Tests for the DELETE /sessions/{session_id} route."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import sessions as sessions_route
from openexecutive.memory import episodic, session_store
from openexecutive.people import store as people_store


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.chdir(tmp_path)
    db_path = Path("./episodic_memory.db").resolve()
    monkeypatch.setattr(episodic, "DB_PATH", db_path)
    monkeypatch.setattr(session_store, "DB_PATH", db_path)
    monkeypatch.setattr(people_store, "DB_PATH", tmp_path / "people.db")
    episodic.initialize_db(db_path)
    people_store.initialize_db()

    app = FastAPI()
    app.include_router(sessions_route.router)
    return TestClient(app)


def test_delete_session_success(client: TestClient) -> None:
    # No caller header resolves to the principal, who owns the session.
    principal_id = people_store.upsert_person(full_name="Alex", is_principal=True)
    session_store.create_session(
        "s1", "title", "2024-01-01T00:00:00", caller_person_id=principal_id
    )
    session_store.save_message("s1", "user", "hi")

    resp = client.delete("/sessions/s1")
    assert resp.status_code == 204
    assert resp.content == b""

    assert session_store.get_session_metadata("s1") is None
    assert client.get("/sessions").json() == []


def test_delete_session_not_found(client: TestClient) -> None:
    resp = client.delete("/sessions/does-not-exist")
    assert resp.status_code == 404


def test_a_chat_whose_id_holds_a_slash_opens_and_deletes(client: TestClient) -> None:
    # A Google Chat chat's id carries the space's name. The web proxy sends it
    # as `~` + base64url (api/path_ids.py); a `%2F` never routes.
    from openexecutive.api.path_ids import encode_path_id

    principal_id = people_store.upsert_person(full_name="Alex", is_principal=True)
    sid = "google_chat:spaces/AAQA~6"
    session_store.create_session(sid, "Offers", "2024-01-01T00:00:00", caller_person_id=principal_id)
    session_store.save_message(sid, "user", "hi")
    segment = encode_path_id(sid)
    assert "/" not in segment

    assert client.get(f"/sessions/{segment}").json()["session_id"] == sid
    assert [m["content"] for m in client.get(f"/sessions/{segment}/messages").json()] == ["hi"]
    assert client.delete(f"/sessions/{segment}").status_code == 204
    assert session_store.get_session_metadata(sid) is None
