"""Chat topics: the store (memory/chat_topics.py), the model side
(utils/chat_topics.py: naming, tidy, backfill) and the routes
(api/routes/sessions.py)."""
from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import chat as chat_route
from openexecutive.api.routes import sessions as sessions_route
from openexecutive.memory import chat_topics as store
from openexecutive.memory import episodic, session_store
from openexecutive.people import store as people_store
from openexecutive.utils import chat_topics as model

ALEX = {"x-caller-email": "alex@example.com"}  # principal
SABIN = {"x-caller-email": "sabin@example.com"}


@pytest.fixture(autouse=True)
def _quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    chat_route._sessions.clear()
    chat_route._session_starters.clear()
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *_a, **_k: None)
    model._backfilling.clear()
    model._backfill_failed_at.clear()


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
    }


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.delenv("BACKEND_SHARED_SECRET", raising=False)
    app = FastAPI()
    app.include_router(sessions_route.router)
    # One event loop for the whole test, as a server has, so the backfill a
    # request starts keeps running after it answers.
    with TestClient(app) as c:
        yield c


class _Model:
    """Stands in for the utility-fast call: replies in order, records prompts."""

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, system: str, content: str, *, max_tokens: int, actor: str) -> str:
        self.calls.append({"system": system, "content": content, "actor": actor})
        if not self.replies:
            raise RuntimeError("no reply scripted")
        return self.replies.pop(0)


@pytest.fixture()
def fake(monkeypatch: pytest.MonkeyPatch) -> _Model:
    """The real naming call (conftest routes it through the title stub),
    over a scripted model."""
    m = _Model()
    monkeypatch.setattr(model, "_ask", m)
    monkeypatch.setattr(model, "_title_and_topic", _REAL_TITLE_AND_TOPIC)
    return m


_REAL_TITLE_AND_TOPIC = model._title_and_topic


def _chat(owner: int, sid: str, title: str = "Placeholder", at: str = "2026-10-01T09:00:00+00:00") -> str:
    session_store.create_session(sid, title, at, caller_person_id=owner)
    session_store.save_message(sid, "user", "question", sender_person_id=owner)
    session_store.save_message(sid, "assistant", "answer")
    with episodic._get_conn() as conn:
        conn.execute("UPDATE sessions SET updated_at = ? WHERE session_id = ?", (at, sid))
    return sid


def _topic_of(sid: str) -> tuple[int | None, str | None]:
    with episodic._get_conn() as conn:
        row = conn.execute("SELECT topic_id, topic_source FROM sessions WHERE session_id = ?", (sid,)).fetchone()
    return row["topic_id"], row["topic_source"]


# ── parsing ──────────────────────────────────────────────────────────────


def test_title_and_topic_lines_are_read() -> None:
    assert model.parse_title_and_topic('Title: "Acme renewal terms."\nTopic: Acme renewal') == (
        "Acme renewal terms", "Acme renewal",
    )
    assert model.parse_title_and_topic("Title: Quick hello\nTopic: none") == ("Quick hello", None)
    # A model that ignores the format still names the chat.
    assert model.parse_title_and_topic("Board deck numbers") == ("Board deck numbers", None)


def test_backfill_lines_are_read() -> None:
    picks = model.parse_backfill("1: Hiring a sales lead\n2) none\n3. Acme renewal\n9: Out of range\njunk", 3)
    assert picks == {1: "Hiring a sales lead", 2: None, 3: "Acme renewal"}


def test_topic_names_are_cleaned() -> None:
    assert store.clean_topic_name("  'Cash  and runway.' ") == "Cash and runway"
    assert store.clean_topic_name("None") is None
    assert store.clean_topic_name("x" * 80) == "x" * store.MAX_TOPIC_CHARS


# ── naming a new chat ────────────────────────────────────────────────────


def test_a_new_chat_gets_a_title_and_reuses_a_topic(people: dict[str, int], fake: _Model) -> None:
    alex = people["alex"]
    _chat(alex, "a")
    fake.replies = ["Title: Shortlist review\nTopic: Hiring a sales lead"]
    assert asyncio.run(model.name_chat("a", "who's on the shortlist?", "Three people.")) == "Shortlist review"
    first, source = _topic_of("a")
    assert first is not None and source == "auto"
    # The second chat sees the first's topic and files under it (any case).
    _chat(alex, "b")
    fake.replies = ["Title: Offer terms\nTopic: hiring a sales lead"]
    asyncio.run(model.name_chat("b", "offer terms?", "Here."))
    assert _topic_of("b")[0] == first
    assert "- Hiring a sales lead" in fake.calls[1]["content"]
    assert fake.calls[1]["actor"] == "session_title"
    # Reusing a topic needs no tidy call.
    assert len(fake.calls) == 2


def test_a_new_topic_is_merged_into_one_that_means_the_same(people: dict[str, int], fake: _Model) -> None:
    alex = people["alex"]
    _chat(alex, "a")
    _chat(alex, "b")
    fake.replies = ["Title: One\nTopic: Hiring a sales lead"]
    asyncio.run(model.name_chat("a", "q", "r"))
    fake.replies = ["Title: Two\nTopic: Sales lead hire", "Hiring a sales lead"]

    async def _run() -> None:
        await model.name_chat("b", "q", "r")
        await asyncio.gather(*model._tasks)

    asyncio.run(_run())
    assert _topic_of("b")[0] == _topic_of("a")[0]
    assert [t["name"] for t in store.list_topics(alex)] == ["Hiring a sales lead"]
    tidy = fake.calls[-1]
    assert tidy["actor"] == "chat_topics" and "NEW TOPIC: Sales lead hire" in tidy["content"]


def test_the_tidy_leaves_a_distinct_topic_and_a_kept_one(people: dict[str, int], fake: _Model) -> None:
    alex = people["alex"]
    _chat(alex, "a")
    _chat(alex, "b")
    store.set_auto_topic("a", alex, "Acme renewal")
    new_id, made = store.set_auto_topic("b", alex, "Office move")
    assert made and new_id is not None
    fake.replies = ["none"]
    assert asyncio.run(model.tidy_new_topic(alex, new_id)) is None
    store.keep_topic(alex, new_id)
    assert asyncio.run(model.tidy_new_topic(alex, new_id)) is None
    assert len(fake.calls) == 1  # a kept topic isn't even asked about
    assert {t["name"] for t in store.list_topics(alex)} == {"Acme renewal", "Office move"}


def test_a_chat_with_no_owner_only_gets_a_title(db: Path, monkeypatch: pytest.MonkeyPatch, fake: _Model) -> None:
    from openexecutive.utils import session_title

    session_store.create_session("legacy", "Placeholder", "2026-10-01T09:00:00+00:00")

    async def _title(*_a: Any, **_k: Any) -> str:
        return "Old chat"

    monkeypatch.setattr(session_title, "generate_session_title", _title)
    assert asyncio.run(model.name_chat("legacy", "q", "r")) == "Old chat"
    assert _topic_of("legacy") == (None, None)
    assert fake.calls == []


def test_a_failed_call_keeps_the_placeholder(people: dict[str, int], fake: _Model) -> None:
    _chat(people["alex"], "a")
    assert asyncio.run(model.name_chat("a", "q", "r")) is None
    assert _topic_of("a") == (None, None)


# ── owner actions stick ──────────────────────────────────────────────────


def test_a_chat_the_owner_filed_is_never_retagged(people: dict[str, int]) -> None:
    alex = people["alex"]
    _chat(alex, "a")
    assert store.file_session("a", alex, name="Board prep")
    assert store.set_auto_topic("a", alex, "Something else") == (None, False)
    topic_id, source = _topic_of("a")
    assert source == "owner"
    assert store.list_topics(alex)[0]["name"] == "Board prep" and store.list_topics(alex)[0]["kept"]
    # Someone else's topic id is refused.
    _chat(people["sabin"], "s")
    store.file_session("s", people["sabin"], name="Theirs")
    theirs = store.topic_id_by_name(people["sabin"], "Theirs")
    assert theirs is not None and not store.file_session("a", alex, topic_id=theirs)
    assert _topic_of("a")[0] == topic_id


# ── backfill ─────────────────────────────────────────────────────────────


def test_backfill_files_older_chats_from_their_titles(
    people: dict[str, int], fake: _Model, monkeypatch: pytest.MonkeyPatch
) -> None:
    alex = people["alex"]
    _chat(alex, "c1", "Comp bands for sales roles", "2026-10-01T09:00:00+00:00")
    _chat(alex, "c2", "Acme asks for a discount", "2026-10-02T09:00:00+00:00")
    _chat(alex, "c3", "Hello", "2026-10-03T09:00:00+00:00")
    _chat(alex, "c4", "Offer terms for Dana", "2026-10-04T09:00:00+00:00")
    _chat(people["sabin"], "s1", "Not Alex's")
    monkeypatch.setattr(model, "BACKFILL_BATCH", 2)
    # Newest first, two per call; the second call sees the first's topics.
    fake.replies = ["1: Hiring a sales lead\n2: none", "1: Acme renewal\n2: Hiring a sales lead"]
    assert asyncio.run(model.backfill(alex)) == 4
    assert "- Hiring a sales lead" in fake.calls[1]["content"]
    assert "Not Alex's" not in fake.calls[0]["content"] + fake.calls[1]["content"]
    assert _topic_of("c4")[0] == _topic_of("c1")[0] is not None
    assert _topic_of("c3") == (None, "auto")
    assert store.untagged_sessions(alex) == []
    assert [s["session_id"] for s in store.untagged_sessions(people["sabin"])] == ["s1"]


def test_a_failed_backfill_waits_before_trying_again(people: dict[str, int], fake: _Model) -> None:
    _chat(people["alex"], "a")
    assert asyncio.run(model.backfill(people["alex"])) == 0
    assert store.untagged_sessions(people["alex"])  # still waiting

    async def _start() -> bool:
        return model.start_backfill(people["alex"])

    assert asyncio.run(_start()) is False


# ── routes ───────────────────────────────────────────────────────────────


def test_topics_list_and_organize(client: TestClient, people: dict[str, int], fake: _Model) -> None:
    alex = people["alex"]
    _chat(alex, "a", "Comp bands")
    fake.replies = ["1: Hiring a sales lead"]
    first = client.get("/sessions/topics", headers=ALEX).json()
    assert first["topics"] == [] and first["organizing"] is True
    deadline = time.monotonic() + 5
    while model.backfill_running(alex) and time.monotonic() < deadline:
        time.sleep(0.02)
    second = client.get("/sessions/topics", headers=ALEX).json()
    assert second["organizing"] is False
    assert [(t["name"], t["chat_count"]) for t in second["topics"]] == [("Hiring a sales lead", 1)]
    listed = client.get("/sessions", headers=ALEX).json()
    assert listed[0]["topic_id"] == second["topics"][0]["id"]
    # Another person sees none of it.
    assert client.get("/sessions/topics", headers=SABIN).json()["topics"] == []


def test_rename_pin_merge_and_ungroup(client: TestClient, people: dict[str, int]) -> None:
    alex = people["alex"]
    for sid in ("a", "b", "c"):
        _chat(alex, sid)
    hire, _ = store.set_auto_topic("a", alex, "Sales hire")
    acme, _ = store.set_auto_topic("b", alex, "Acme")
    store.set_auto_topic("c", alex, "Acme")

    resp = client.patch(f"/sessions/topics/{hire}", json={"name": " Hiring a  sales lead ", "pinned": True}, headers=ALEX)
    assert resp.status_code == 200
    assert resp.json()["name"] == "Hiring a sales lead" and resp.json()["pinned_at"] and resp.json()["kept"]
    assert client.get("/sessions/topics", headers=ALEX).json()["topics"][0]["id"] == hire

    # Someone else can't touch it.
    assert client.patch(f"/sessions/topics/{hire}", json={"pinned": False}, headers=SABIN).status_code == 404
    assert client.delete(f"/sessions/topics/{hire}", headers=SABIN).status_code == 404

    assert client.post(f"/sessions/topics/{acme}/merge", json={"into_id": hire}, headers=ALEX).status_code == 204
    topics = client.get("/sessions/topics", headers=ALEX).json()["topics"]
    assert [(t["id"], t["chat_count"]) for t in topics] == [(hire, 3)]

    assert client.delete(f"/sessions/topics/{hire}", headers=ALEX).status_code == 204
    assert client.get("/sessions/topics", headers=ALEX).json()["topics"] == []
    assert all("topic_id" not in s for s in client.get("/sessions", headers=ALEX).json())
    # Ungrouped chats stay out of the backfill.
    assert store.untagged_sessions(alex) == []


def test_move_a_chat_to_a_topic(client: TestClient, people: dict[str, int]) -> None:
    alex = people["alex"]
    _chat(alex, "a")
    resp = client.patch("/sessions/a", json={"topic_name": "Board prep"}, headers=ALEX)
    assert resp.status_code == 200 and resp.json()["topic_id"]
    resp = client.patch("/sessions/a", json={"topic_id": None}, headers=ALEX)
    assert resp.status_code == 200 and "topic_id" not in resp.json()
    assert client.patch("/sessions/a", json={"topic_id": 999}, headers=ALEX).status_code == 404
    assert client.patch("/sessions/a", json={"topic_name": "x"}, headers=SABIN).status_code == 404
