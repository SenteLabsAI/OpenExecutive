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
