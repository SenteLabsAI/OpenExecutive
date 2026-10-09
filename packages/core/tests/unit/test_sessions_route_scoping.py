"""Tests for GET /sessions per-caller scoping.

The Recent-chats sidebar must show each signed-in user only their own
chats. The route resolves `x-caller-email` to a Person and filters the
list; an unknown header MUST NOT fall through to the principal.
"""
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


def test_sessions_list_filters_by_caller_email(client: TestClient) -> None:
    """Two users with their own sessions each see only their own."""
    alex_id = people_store.upsert_person(
        full_name="Alex", is_principal=True, email="alex@example.com"
    )
    sabin_id = people_store.upsert_person(
        full_name="Sabin", email="sabin@example.com"
    )

    session_store.create_session("alex-s1", "Alex chat", "2024-01-01T00:00:00", caller_person_id=alex_id)
    session_store.create_session("sabin-s1", "Sabin chat", "2024-01-02T00:00:00", caller_person_id=sabin_id)

    alex_resp = client.get("/sessions", headers={"x-caller-email": "alex@example.com"})
    sabin_resp = client.get("/sessions", headers={"x-caller-email": "sabin@example.com"})

    assert alex_resp.status_code == 200
    assert sabin_resp.status_code == 200
    assert [s["session_id"] for s in alex_resp.json()] == ["alex-s1"]
    assert [s["session_id"] for s in sabin_resp.json()] == ["sabin-s1"]


def test_sessions_list_unknown_email_returns_empty_not_principal(client: TestClient) -> None:
    """A signed-in user whose email isn't in the roster gets an empty list,
    not the principal's chats. Mirrors the chat-route precedence rule."""
    alex_id = people_store.upsert_person(
        full_name="Alex", is_principal=True, email="alex@example.com"
    )
    session_store.create_session("alex-only", "Alex chat", "2024-01-01T00:00:00", caller_person_id=alex_id)

    resp = client.get("/sessions", headers={"x-caller-email": "stranger@example.com"})
    assert resp.status_code == 200
    assert resp.json() == []


def test_sessions_list_no_header_falls_back_to_principal(client: TestClient) -> None:
    """CLI / direct-curl callers (no x-caller-email header) see the principal's chats."""
    alex_id = people_store.upsert_person(
        full_name="Alex", is_principal=True, email="alex@example.com"
    )
    people_store.upsert_person(full_name="Sabin", email="sabin@example.com")

    session_store.create_session("alex-s1", "Alex chat", "2024-01-01T00:00:00", caller_person_id=alex_id)

    resp = client.get("/sessions")
    assert resp.status_code == 200
    assert [s["session_id"] for s in resp.json()] == ["alex-s1"]


def test_sessions_list_hides_legacy_null_owner_rows(client: TestClient) -> None:
    """Sessions created before the migration have NULL owner and stay hidden."""
    alex_id = people_store.upsert_person(
        full_name="Alex", is_principal=True, email="alex@example.com"
    )
    # Simulate a pre-migration row.
    session_store.create_session("legacy", "Pre-migration", "2024-01-01T00:00:00", caller_person_id=None)
    session_store.create_session("modern", "Post-migration", "2024-01-02T00:00:00", caller_person_id=alex_id)

    resp = client.get("/sessions", headers={"x-caller-email": "alex@example.com"})
    assert resp.status_code == 200
    assert [s["session_id"] for s in resp.json()] == ["modern"]


def test_sessions_list_email_case_insensitive(client: TestClient) -> None:
    """Header is lowercased before lookup; mixed-case still resolves."""
    sabin_id = people_store.upsert_person(
        full_name="Sabin", email="sabin@example.com"
    )
    session_store.create_session("sabin-s1", "Sabin chat", "2024-01-01T00:00:00", caller_person_id=sabin_id)

    resp = client.get("/sessions", headers={"x-caller-email": "Sabin@Example.COM"})
    assert resp.status_code == 200
    assert [s["session_id"] for s in resp.json()] == ["sabin-s1"]


def test_search_finds_words_inside_the_callers_own_chats(client: TestClient) -> None:
    """`?q=` searches titles AND messages, only in the caller's own chats, and
    shows the newest matching message around the match."""
    alex_id = people_store.upsert_person(
        full_name="Alex", is_principal=True, email="alex@example.com"
    )
    sabin_id = people_store.upsert_person(full_name="Sabin", email="sabin@example.com")

    session_store.create_session("cash", "Cash plan", "2024-01-01T00:00:00", caller_person_id=alex_id)
    session_store.save_message("cash", "user", "How much runway do we have if the loan slips?")
    session_store.save_message("cash", "assistant", "About 11 months of RUNWAY at today's burn.")
    session_store.create_session("deck", "Runway scenarios", "2024-01-02T00:00:00", caller_person_id=alex_id)
    session_store.create_session("other", "Lunch", "2024-01-03T00:00:00", caller_person_id=alex_id)
    session_store.save_message("other", "user", "Book lunch with Dana")
    session_store.create_session("sabin", "Sabin chat", "2024-01-04T00:00:00", caller_person_id=sabin_id)
    session_store.save_message("sabin", "user", "Our runway worries me")

    rows = client.get("/sessions", params={"q": "runway"}, headers={"x-caller-email": "alex@example.com"}).json()
    by_id = {r["session_id"]: r for r in rows}
    assert set(by_id) == {"cash", "deck"}
    assert by_id["cash"]["match_count"] == 2
    assert by_id["cash"]["snippet"] == "Executive: About 11 months of RUNWAY at today's burn."
    assert by_id["deck"]["match_count"] == 0
    assert "snippet" not in by_id["deck"]

    sabin = client.get("/sessions", params={"q": "runway"}, headers={"x-caller-email": "sabin@example.com"}).json()
    assert [r["session_id"] for r in sabin] == ["sabin"]

    # Without a query the list keeps its old shape.
    plain = client.get("/sessions", headers={"x-caller-email": "alex@example.com"}).json()
    assert all("match_count" not in r and "snippet" not in r for r in plain)


def test_search_treats_wildcards_as_text(client: TestClient) -> None:
    alex_id = people_store.upsert_person(
        full_name="Alex", is_principal=True, email="alex@example.com"
    )
    session_store.create_session("a", "One", "2024-01-01T00:00:00", caller_person_id=alex_id)
    session_store.save_message("a", "user", "growth was 9% last quarter")
    session_store.create_session("b", "Two", "2024-01-01T00:00:00", caller_person_id=alex_id)
    session_store.save_message("b", "user", "nothing to see")

    assert [r["session_id"] for r in client.get("/sessions", params={"q": "9%"}).json()] == ["a"]
    assert client.get("/sessions", params={"q": "%"}).json()[0]["session_id"] == "a"
    assert client.get("/sessions", params={"q": "_"}).json() == []


def test_snippet_is_cut_around_the_match() -> None:
    text = "word " * 100 + "runway is the point " + "tail " * 100
    snip = session_store._snippet(text, "runway")
    assert "runway" in snip and snip.startswith("…") and snip.endswith("…")
    assert len(snip) <= 170
