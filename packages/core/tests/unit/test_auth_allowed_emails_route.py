"""GET /auth/allowed-emails returns non-archived people with non-null email;
GET /auth/me the email the request is made as."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import auth as auth_route
from openexecutive.people import store as people_store


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "people.db"
    monkeypatch.setattr(people_store, "DB_PATH", path)
    people_store.initialize_db()
    return path


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(auth_route.router)
    return TestClient(app)


def test_empty_roster_returns_empty_list(db: Path) -> None:
    """Fresh install — no people, no allowlist via this endpoint."""
    resp = _client().get("/auth/allowed-emails")
    assert resp.status_code == 200
    assert resp.json() == []


def test_returns_only_people_with_email(db: Path) -> None:
    pid_with = people_store.upsert_person(full_name="Alex", email="alex@example.com")
    people_store.upsert_person(full_name="No Email")  # email is None
    resp = _client().get("/auth/allowed-emails")
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0] == {"email": "alex@example.com", "person_id": pid_with}


def test_excludes_archived_people(db: Path) -> None:
    pid_active = people_store.upsert_person(full_name="Active", email="active@example.com")
    pid_archived = people_store.upsert_person(full_name="Ex Hire", email="ex@example.com")
    people_store.archive_person(pid_archived)
    rows = _client().get("/auth/allowed-emails").json()
    assert {r["email"] for r in rows} == {"active@example.com"}
    assert rows[0]["person_id"] == pid_active


def test_emails_normalized_to_lowercase(db: Path) -> None:
    """Mixed-case emails on Person rows are returned lowercased so
    the UI's lowercase comparison succeeds."""
    people_store.upsert_person(full_name="Alex", email="Alex@Example.COM")
    rows = _client().get("/auth/allowed-emails").json()
    assert rows[0]["email"] == "alex@example.com"


def test_me_names_the_callers_own_email_and_no_one_else() -> None:
    client = _client()
    assert client.get("/auth/me", headers={"x-caller-email": " Alex@Example.COM "}).json() == {
        "email": "alex@example.com"
    }
    assert client.get("/auth/me").json() == {"email": ""}
