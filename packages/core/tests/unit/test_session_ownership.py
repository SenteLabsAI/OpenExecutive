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
    from openexecutive.utils import session_title

    monkeypatch.setattr(profile_builder, "load_or_create_profile", lambda: CompanyProfile())
    monkeypatch.setattr(retriever, "retrieve", lambda *_a, **_k: "")

    async def _no_title(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(session_title, "generate_session_title", _no_title)

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


def _owner(db_path: Path, session_id: str) -> int | None:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT caller_person_id FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
    return row[0]


def _message_count(db_path: Path, session_id: str) -> int:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM chat_messages WHERE session_id = ?", (session_id,)
        ).fetchone()
    return int(row[0])


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
    assert client.get(url, headers=RIYA).status_code == 404
    assert client.get(url, headers=STRANGER).status_code == 404


def test_someone_elses_session_looks_exactly_like_an_unknown_one(
    client: TestClient, people: dict[str, int]
) -> None:
    """Ids are guessable, so "exists but not yours" must not be told apart
    from "no such session" — or the routes would reveal who has chatted."""
    _seed("slack:dm:USABIN", people["sabin"])

    for sid in ("slack:dm:USABIN", "slack:dm:UNOBODY"):
        for resp in (
            client.get(f"/sessions/{sid}", headers=RIYA),
            client.get(f"/sessions/{sid}/messages", headers=RIYA),
            client.get(f"/sessions/{sid}/followup", headers=RIYA),
            client.delete(f"/sessions/{sid}", headers=RIYA),
        ):
            assert (resp.status_code, resp.json()) == (404, {"detail": "Session not found"})


def test_legacy_ownerless_session_is_the_principals_alone(
    client: TestClient, people: dict[str, int]
) -> None:
    _seed("legacy", None)

    assert client.get("/sessions/legacy/messages", headers=ALEX).status_code == 200
    assert client.get("/sessions/legacy/messages", headers=SABIN).status_code == 404
    assert client.get("/sessions/legacy/messages", headers=STRANGER).status_code == 404


def test_delete_refuses_someone_elses_session_and_keeps_it(
    client: TestClient, db: Path, people: dict[str, int]
) -> None:
    _seed("sabin-chat", people["sabin"])

    assert client.delete("/sessions/sabin-chat", headers=RIYA).status_code == 404
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


def test_chat_never_writes_into_someone_elses_session(
    client: TestClient, db: Path, people: dict[str, int]
) -> None:
    """Naming another person's session starts a fresh chat instead: nothing
    lands in theirs, and the response is the same as for an unknown id."""
    _seed("sabin-chat", people["sabin"])

    assert _chat(client, RIYA, "sabin-chat") == 200
    assert _chat(client, STRANGER, "sabin-chat") == 200
    assert _message_count(db, "sabin-chat") == 0
    assert len(_session_ids(db)) == 3  # sabin-chat + one fresh chat each

    assert _chat(client, SABIN, "sabin-chat") == 200
    assert _chat(client, ALEX, "sabin-chat") == 200
    assert _message_count(db, "sabin-chat") == 4


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
    assert _chat(client, {"x-caller-email": "other@example.com"}, sid) == 200
    assert _chat(client, SABIN, sid) == 200
    assert _message_count(db, sid) == 4  # only the starter's two turns
    assert client.get(f"/sessions/{sid}/messages", headers=SABIN).status_code == 404


def test_stale_cached_session_is_not_handed_to_the_next_claimant(
    client: TestClient, db: Path, people: dict[str, int]
) -> None:
    """A fixture reset or client-slot switch wipes the rows but not the
    in-memory cache. Whoever names the id next must get an empty chat, not the
    cached transcript."""
    assert _chat(client, SABIN) == 200
    (sid,) = _session_ids(db)
    stale = chat_route._sessions[sid]
    stale.conversation_history = [{"role": "user", "content": "SABIN'S SECRET"}]
    chat_route._session_starters.clear()  # e.g. a resumed chat: no starter here
    session_store.delete_session(sid)  # what reset_all_state does to the row

    assert _chat(client, RIYA, sid) == 200
    assert chat_route._sessions[sid] is not stale
    assert "SABIN'S SECRET" not in repr(chat_route._sessions[sid].conversation_history)
    assert _message_count(db, sid) == 2  # Riya's turn only


def test_turn_finishing_after_its_session_was_deleted_is_not_saved(
    client: TestClient, db: Path, people: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting a chat while a reply streams must not leave that reply's
    messages behind under the deleted id."""
    from openexecutive.orchestrator import executive as exec_mod

    class _DeletesMidStream:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def stream_chat(self, **_kwargs: Any) -> AsyncIterator[str]:
            (sid,) = _session_ids(db)
            session_store.delete_session(sid)
            chat_route.forget_session(sid)
            yield "ok"

    monkeypatch.setattr(exec_mod, "Executive", _DeletesMidStream)

    assert _chat(client, SABIN) == 200
    assert _session_ids(db) == []
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0] == 0


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


def test_starter_keeps_their_chat_after_joining_the_roster(
    client: TestClient, db: Path, people: dict[str, int]
) -> None:
    """Started while unrostered, then added to the roster: the same verified
    email still matches, and the row is bound to their new Person."""
    newhire = {"x-caller-email": "newhire@example.com"}
    assert _chat(client, newhire) == 200
    (sid,) = _session_ids(db)
    assert _owner(db, sid) is None

    newhire_id = people_store.upsert_person(full_name="New", email="newhire@example.com")

    assert _chat(client, newhire, sid) == 200
    assert _owner(db, sid) == newhire_id


def test_principal_continuing_a_chat_does_not_take_it_over(
    client: TestClient, db: Path, people: dict[str, int]
) -> None:
    assert _chat(client, STRANGER) == 200
    (sid,) = _session_ids(db)

    assert _chat(client, ALEX, sid) == 200
    assert _owner(db, sid) is None
    assert _chat(client, STRANGER, sid) == 200


def test_orphaned_chat_after_restart_continues_in_a_fresh_one(
    client: TestClient, db: Path, people: dict[str, int]
) -> None:
    """The starter record is in-memory. After a restart an unresolved caller's
    ownerless chat can't be vouched for: reads are refused, and /chat starts a
    new chat instead of failing every send."""
    assert _chat(client, STRANGER) == 200
    (sid,) = _session_ids(db)
    chat_route._sessions.clear()
    chat_route._session_starters.clear()  # simulate a restart

    assert client.get(f"/sessions/{sid}/messages", headers=STRANGER).status_code == 404
    assert _chat(client, STRANGER, sid) == 200
    ids = _session_ids(db)
    assert len(ids) == 2 and ids[0] == sid


def test_delete_of_a_never_persisted_chat_reports_success(
    client: TestClient, people: dict[str, int]
) -> None:
    """A chat whose row failed to persist lives only in memory; deleting it
    drops that state and is a success, not a 404."""
    chat_route._session_starters["live-only"] = frozenset(
        {f"person:{people['sabin']}", "email:sabin@example.com"}
    )
    chat_route._sessions["live-only"] = object()

    assert client.delete("/sessions/live-only", headers=SABIN).status_code == 204
    assert "live-only" not in chat_route._sessions

