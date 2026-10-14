"""Pin, rename, share, export and take back the last turn of a chat
(api/routes/sessions.py, memory/session_store.py, memory/chat_export.py)."""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import chat as chat_route
from openexecutive.api.routes import sessions as sessions_route
from openexecutive.memory import episodic, session_store
from openexecutive.memory.chat_export import file_name
from openexecutive.memory.company_profile import CompanyProfile
from openexecutive.people import store as people_store

ALEX = {"x-caller-email": "alex@example.com"}  # principal
SABIN = {"x-caller-email": "sabin@example.com"}
RIYA = {"x-caller-email": "riya@example.com"}


@pytest.fixture(autouse=True)
def _reset_route_state(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    chat_route._sessions.clear()
    chat_route._session_starters.clear()
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "openexecutive.audit.log_event",
        lambda event_type, *_a, details=None, **_k: events.append((event_type, details or {})),
    )
    return events


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
        "alex": people_store.upsert_person(full_name="Alex", is_principal=True, email="alex@example.com"),
        "sabin": people_store.upsert_person(full_name="Sabin", email="sabin@example.com"),
        "riya": people_store.upsert_person(full_name="Riya", email="riya@example.com"),
        "vendor": people_store.upsert_person(full_name="Vendor", email="v@example.com", kind="contact"),
    }


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.delenv("BACKEND_SHARED_SECRET", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    from openexecutive.knowledge import retriever
    from openexecutive.onboarding import profile_builder
    from openexecutive.orchestrator import executive as exec_mod
    from openexecutive.utils import session_title

    monkeypatch.setattr(profile_builder, "load_or_create_profile", lambda: CompanyProfile())
    monkeypatch.setattr(retriever, "retrieve", lambda *_a, **_k: "")

    async def _title(*_a: Any, **_k: Any) -> str:
        return "Generated title"

    monkeypatch.setattr(session_title, "generate_session_title", _title)

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


def _chat_of(owner: int, sid: str = "web-1", turns: int = 2) -> str:
    session_store.create_session(sid, "Placeholder", "2026-10-01T09:00:00+00:00", caller_person_id=owner)
    for i in range(turns):
        session_store.save_message(sid, "user", f"question {i}", sender_person_id=owner)
        session_store.save_message(
            sid, "assistant", f"answer {i}",
            sources=json.dumps({"sources": [{"kind": "web", "title": "Q3 report", "url": "https://x.test/q3"}],
                                "unavailable": []}),
        )
    return sid


def _last_id(sid: str) -> int:
    return int(session_store.load_messages(sid)[-1]["id"])


def test_pin_puts_a_chat_first_and_rename_sticks(client: TestClient, people: dict[str, int]) -> None:
    _chat_of(people["alex"], "old")
    _chat_of(people["alex"], "new")
    session_store.update_session_timestamp("new")
    assert [s["session_id"] for s in client.get("/sessions", headers=ALEX).json()] == ["new", "old"]

    resp = client.patch("/sessions/old", json={"pinned": True, "title": "  Board  prep "}, headers=ALEX)
    assert resp.status_code == 200 and resp.json()["title"] == "Board prep"
    listed = client.get("/sessions", headers=ALEX).json()
    assert [s["session_id"] for s in listed] == ["old", "new"]
    assert listed[0]["pinned_at"] and "pinned_at" not in listed[1]

    # The automatic title never replaces the owner's name.
    session_store.update_session_title("old", "Auto title")
    assert client.get("/sessions/old", headers=ALEX).json()["title"] == "Board prep"
    session_store.update_session_title("new", "Auto title")
    assert client.get("/sessions/new", headers=ALEX).json()["title"] == "Auto title"

    client.patch("/sessions/old", json={"pinned": False}, headers=ALEX)
    assert "pinned_at" not in client.get("/sessions/old", headers=ALEX).json()
    assert client.patch("/sessions/old", json={"title": "   "}, headers=ALEX).status_code == 422


def test_only_the_owner_pins_renames_or_shares(client: TestClient, people: dict[str, int]) -> None:
    _chat_of(people["sabin"], "slack:channel:C1")  # a channel chat the principal may open
    assert client.get("/sessions/slack:channel:C1", headers=ALEX).status_code == 200
    assert client.patch("/sessions/slack:channel:C1", json={"pinned": True}, headers=ALEX).status_code == 404
    assert client.put("/sessions/slack:channel:C1/shares", json={"person_ids": []}, headers=ALEX).status_code == 404
    assert client.patch("/sessions/slack:channel:C1", json={"pinned": True}, headers=RIYA).status_code == 404


def test_a_shared_chat_is_read_only_for_who_it_is_shared_with(
    client: TestClient, people: dict[str, int], _reset_route_state: list[tuple[str, dict[str, Any]]]
) -> None:
    sid = _chat_of(people["sabin"])
    # A teammate's own chat is theirs alone until they share it.
    assert client.get(f"/sessions/{sid}/messages", headers=ALEX).status_code == 404

    resp = client.put(f"/sessions/{sid}/shares", json={"person_ids": [people["alex"]]}, headers=SABIN)
    assert resp.status_code == 200
    assert [s["full_name"] for s in resp.json()] == ["Alex"]
    assert _reset_route_state[-1][0] == "session_shared"
    assert _reset_route_state[-1][1]["added"] == [people["alex"]]

    shared = client.get("/sessions/shared-with-me", headers=ALEX).json()
    assert [(s["session_id"], s["shared_by"], s["read_only"]) for s in shared] == [(sid, "Sabin", True)]
    assert client.get("/sessions/shared-with-me", headers=RIYA).json() == []
    # It is not in Alex's own list.
    assert sid not in [s["session_id"] for s in client.get("/sessions", headers=ALEX).json()]

    meta = client.get(f"/sessions/{sid}", headers=ALEX).json()
    assert meta["read_only"] is True and meta["shared_by"] == "Sabin"
    assert len(client.get(f"/sessions/{sid}/messages", headers=ALEX).json()) == 4
    assert client.get(f"/sessions/{sid}/export", headers=ALEX).status_code == 200

    # Reading is all: no deleting, rating, renaming, redoing or writing in it.
    assert client.delete(f"/sessions/{sid}", headers=ALEX).status_code == 404
    assert client.patch(f"/sessions/{sid}", json={"title": "Mine"}, headers=ALEX).status_code == 404
    assert client.post(
        f"/sessions/{sid}/rewind", json={"last_message_id": _last_id(sid)}, headers=ALEX
    ).status_code == 404
    client.post("/chat", json={"message": "hi", "session_id": sid}, headers=ALEX).read()
    assert len(session_store.load_messages(sid)) == 4

    # Unsharing takes it away.
    client.put(f"/sessions/{sid}/shares", json={"person_ids": []}, headers=SABIN)
    assert client.get(f"/sessions/{sid}/messages", headers=ALEX).status_code == 404
    assert client.get("/sessions/shared-with-me", headers=ALEX).json() == []


def test_sharing_refuses_contacts_the_owner_and_a_chat_that_read_mail(
    client: TestClient, people: dict[str, int]
) -> None:
    sid = _chat_of(people["sabin"])
    for pid in (people["vendor"], people["sabin"], 9999):
        assert client.put(f"/sessions/{sid}/shares", json={"person_ids": [pid]}, headers=SABIN).status_code == 422
    client.put(f"/sessions/{sid}/shares", json={"person_ids": [people["riya"]]}, headers=SABIN)

    session_store.mark_mail_private(sid, people["sabin"])
    assert client.put(
        f"/sessions/{sid}/shares", json={"person_ids": [people["riya"]]}, headers=SABIN
    ).status_code == 409
    # Shared before it read mail: no longer readable or listed.
    assert client.get(f"/sessions/{sid}/messages", headers=RIYA).status_code == 404
    assert client.get("/sessions/shared-with-me", headers=RIYA).json() == []


def test_deleting_a_chat_drops_its_shares(client: TestClient, people: dict[str, int]) -> None:
    sid = _chat_of(people["alex"])
    client.put(f"/sessions/{sid}/shares", json={"person_ids": [people["riya"]]}, headers=ALEX)
    assert client.delete(f"/sessions/{sid}", headers=ALEX).status_code == 204
    assert session_store.shared_with(sid) == []


def test_export_is_markdown_with_names_and_sources(client: TestClient, people: dict[str, int]) -> None:
    sid = _chat_of(people["alex"], turns=1)
    client.patch(f"/sessions/{sid}", json={"title": "Q3 / numbers?"}, headers=ALEX)
    resp = client.get(f"/sessions/{sid}/export", headers=ALEX)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert 'filename="q3-numbers.md"' in resp.headers["content-disposition"]
    text = resp.text
    assert text.startswith("# Q3 / numbers?\n\nStarted 1 October 2026\n")
    assert "## Alex\n\nquestion 0" in text
    assert "## Executive\n\nanswer 0\n\nSources:\n- [Q3 report](https://x.test/q3)" in text
    assert file_name("!!!") == "chat.md"


def test_rewind_takes_back_the_last_turn_and_the_next_turn_replaces_it(
    client: TestClient, people: dict[str, int]
) -> None:
    sid = _chat_of(people["alex"])
    session_store.save_message(sid, "user", "question 2", sender_person_id=people["alex"])
    session_store.save_message(sid, "user", "and one more thing", sender_person_id=people["alex"])
    session_store.save_message(sid, "assistant", "answer 2")
    last = _last_id(sid)

    # A stale page (it names an earlier reply) takes nothing back.
    first_reply = int(session_store.load_messages(sid)[1]["id"])
    assert client.post(
        f"/sessions/{sid}/rewind", json={"last_message_id": first_reply}, headers=ALEX
    ).status_code == 409

    resp = client.post(f"/sessions/{sid}/rewind", json={"last_message_id": last}, headers=ALEX)
    assert resp.status_code == 200
    assert resp.json() == {"message": "question 2\n\nand one more thing"}
    assert [m["content"] for m in session_store.load_messages(sid)] == [
        "question 0", "answer 0", "question 1", "answer 1",
    ]

    client.post("/chat", json={"message": "question 2, edited", "session_id": sid}, headers=ALEX).read()
    assert [m["content"] for m in session_store.load_messages(sid)][-2:] == ["question 2, edited", "ok"]


def test_rewind_is_for_web_chats_only(client: TestClient, people: dict[str, int]) -> None:
    sid = _chat_of(people["alex"], "telegram:42")
    assert client.post(
        f"/sessions/{sid}/rewind", json={"last_message_id": _last_id(sid)}, headers=ALEX
    ).status_code == 409
    assert len(session_store.load_messages(sid)) == 4


def test_a_teammates_share_and_rewind_rows_are_private_to_them(
    client: TestClient, people: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.audit.context import rows_owner

    owners: list[Any] = []
    monkeypatch.setattr(
        "openexecutive.audit.log_event", lambda *_a, **_k: owners.append(rows_owner())
    )
    sid = _chat_of(people["sabin"])
    client.put(f"/sessions/{sid}/shares", json={"person_ids": [people["riya"]]}, headers=SABIN)
    client.post(f"/sessions/{sid}/rewind", json={"last_message_id": _last_id(sid)}, headers=SABIN)
    assert owners == [people["sabin"], people["sabin"]]


def test_the_principal_cant_take_over_an_ownerless_chat_someone_is_in(
    client: TestClient, people: dict[str, int]
) -> None:
    sid = _chat_of(people["alex"], "legacy")
    with session_store._get_conn(session_store.DB_PATH) as conn:
        conn.execute("UPDATE sessions SET caller_person_id = NULL WHERE session_id = 'legacy'")
    assert client.patch("/sessions/legacy", json={"pinned": True}, headers=ALEX).status_code == 200
    chat_route._session_starters[sid] = frozenset({"email:someone@example.com"})
    assert client.patch("/sessions/legacy", json={"pinned": True}, headers=ALEX).status_code == 404
