"""A chat app's endless stream is cut into conversations.

Telegram chats, Slack and Discord DMs, and a person's @mentions in one channel
each used to be one session forever, so the Chats page showed them as one
chat that never ended. Now a quiet gap or a "new chat" starts the next
conversation (memory.conversations), and what has to see the whole stream
(approvals, decisions) compares base ids.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.integrations import conversations as chat_conversations
from openexecutive.integrations import telegram_bot
from openexecutive.memory import conversations, episodic, session_store
from openexecutive.memory.conversation_ids import (
    base_session_id,
    conversation_id,
    conversation_number,
    is_rolling,
)
from openexecutive.memory.episodic import _get_conn, initialize_db

OWNER = 7
BASE = "telegram:4242"


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    path = tmp_path / "test.db"
    initialize_db(path)
    return path


def _turn(db: Path, sid: str, at: datetime, user: str = "Q", reply: str = "A") -> None:
    session_store.create_session(sid, "Telegram Dana", at.isoformat(), caller_person_id=OWNER, db_path=db)
    with _get_conn(db) as conn:
        for role, text in (("user", user), ("assistant", reply)):
            conn.execute(
                "INSERT INTO chat_messages (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (sid, role, text, at.isoformat()),
            )
        conn.execute("UPDATE sessions SET updated_at = ? WHERE session_id = ?", (at.isoformat(), sid))


NOW = datetime(2026, 3, 4, 15, 0, tzinfo=UTC)


# ── ids ──────────────────────────────────────────────────────────────────


def test_ids_round_trip() -> None:
    assert conversation_id(BASE, 1) == BASE
    assert conversation_id(BASE, 3) == "telegram:4242~3"
    assert base_session_id("telegram:4242~3") == BASE
    assert base_session_id("slack:dm:U1~12") == "slack:dm:U1"
    assert conversation_number("discord:dm:9~2") == 2
    assert conversation_number(BASE) == 1


def test_only_chat_app_streams_roll() -> None:
    assert is_rolling("telegram:-100") and is_rolling("slack:channel:C1:U1")
    assert is_rolling("discord:dm:9") and is_rolling("discord:channel:5:9")
    for sid in ("4f1c2a", "slack:thread:C1:1.2", "discord:thread:5", "email:t1", "google_chat:spaces/x"):
        assert not is_rolling(sid)
        assert base_session_id(sid) == sid


# ── picking the conversation ─────────────────────────────────────────────


def test_a_new_stream_starts_on_its_base_id(db: Path) -> None:
    assert conversations.current_conversation(BASE, now=NOW, db_path=db) == BASE


def test_within_the_gap_the_conversation_continues(db: Path) -> None:
    _turn(db, BASE, NOW - timedelta(minutes=90))
    assert conversations.current_conversation(BASE, now=NOW, db_path=db) == BASE


def test_after_two_quiet_hours_the_next_one_starts(db: Path) -> None:
    _turn(db, BASE, NOW - timedelta(hours=2, minutes=1))
    assert conversations.current_conversation(BASE, now=NOW, db_path=db) == f"{BASE}~2"
    _turn(db, f"{BASE}~2", NOW)
    assert conversations.current_conversation(BASE, now=NOW, db_path=db) == f"{BASE}~2"
    later = NOW + timedelta(hours=3)
    assert conversations.current_conversation(BASE, now=later, db_path=db) == f"{BASE}~3"


def test_ids_that_are_not_streams_pass_through(db: Path) -> None:
    assert conversations.current_conversation("slack:thread:C1:1.2", now=NOW, db_path=db) == "slack:thread:C1:1.2"


def test_new_starts_a_fresh_conversation_once(db: Path) -> None:
    _turn(db, BASE, NOW - timedelta(minutes=5))
    session_store.update_session_title(BASE, "Board deck numbers", db_path=db)
    new_id, ended = conversations.start_new_conversation(BASE, owner_person_id=OWNER, db_path=db)
    assert (new_id, ended) == (f"{BASE}~2", "Board deck numbers")
    # A second /new before anything is said reuses the empty one.
    assert conversations.start_new_conversation(BASE, owner_person_id=OWNER, db_path=db) == (new_id, None)
    # The next message lands there even after the gap.
    later = NOW + timedelta(hours=5)
    assert conversations.current_conversation(BASE, now=later, db_path=db) == new_id


def test_an_empty_conversation_stays_off_the_list(db: Path) -> None:
    _turn(db, BASE, NOW - timedelta(minutes=5))
    conversations.start_new_conversation(BASE, owner_person_id=OWNER, db_path=db)
    ids = [s["session_id"] for s in session_store.list_sessions(OWNER, db_path=db)]
    assert ids == [BASE]


def test_new_command_words() -> None:
    for text in ("/new", "/new@ExecBot", "new chat", "New conversation.", "  NEW CHAT! "):
        assert chat_conversations.is_new_conversation_command(text), text
    for text in ("new", "start a new chat with Dana", "/news", "new chat please"):
        assert not chat_conversations.is_new_conversation_command(text), text


def test_start_fresh_names_the_conversation_it_ends(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conversations, "DB_PATH", db)
    _turn(db, BASE, NOW)
    session_store.update_session_title(BASE, "Hiring plan", db_path=db)
    real = conversations.start_new_conversation
    monkeypatch.setattr(
        conversations, "start_new_conversation",
        lambda base, owner_person_id: real(base, owner_person_id=owner_person_id, db_path=db),
    )
    assert "“Hiring plan”" in chat_conversations.start_fresh(BASE, OWNER)
    assert chat_conversations.start_fresh(BASE, OWNER) == "Started a fresh conversation."


# ── titles ───────────────────────────────────────────────────────────────


_real_meta = session_store.get_session_metadata


def _run_title(db: Path, sid: str, user: str, generated: str | None) -> tuple[list[str], AsyncMock]:
    titled: list[str] = []
    gen = AsyncMock(return_value=generated)
    with (
        patch("openexecutive.memory.session_store.get_session_metadata",
              side_effect=lambda s: _real_meta(s, db_path=db)),
        patch("openexecutive.memory.session_store.update_session_title",
              side_effect=lambda s, t: titled.append(t)),
        patch("openexecutive.utils.session_title.generate_session_title", new=gen),
    ):
        asyncio.run(chat_conversations._title(sid, user, "reply"))
    return titled, gen


def test_first_exchange_titles_the_conversation(db: Path) -> None:
    _turn(db, f"{BASE}~2", NOW)
    titled, _ = _run_title(db, f"{BASE}~2", "What do I need for the board deck?", "Board deck numbers")
    assert titled == ["Board deck numbers"]


def test_title_falls_back_to_the_first_words(db: Path) -> None:
    _turn(db, f"{BASE}~2", NOW)
    titled, _ = _run_title(db, f"{BASE}~2", "Remind me to call Dana tomorrow at 9", None)
    assert titled == ["Remind me to call Dana tomorrow at 9"]


def test_later_turns_keep_the_title(db: Path) -> None:
    _turn(db, BASE, NOW)
    _turn(db, BASE, NOW)
    titled, gen = _run_title(db, BASE, "hi", "x")
    assert titled == []
    gen.assert_not_awaited()


# ── one-shot split of old history ────────────────────────────────────────


def _old_stream(db: Path, mail_private: bool = False) -> None:
    t0 = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
    session_store.create_session(BASE, "Telegram Dana", t0.isoformat(), caller_person_id=OWNER, db_path=db)
    plan = [
        (t0, "user", "Draft a reply to the landlord"),
        (t0 + timedelta(minutes=1), "assistant", "Here's a draft."),
        (t0 + timedelta(minutes=30), "user", "Shorter please"),
        (t0 + timedelta(minutes=31), "assistant", "Shorter draft."),
        # 5 hours quiet
        (t0 + timedelta(hours=5, minutes=31), "user", "Hiring plan for two sales roles"),
        (t0 + timedelta(hours=5, minutes=32), "assistant", "Here's a plan."),
        # next day
        (t0 + timedelta(days=1), "user", "[Dana]: " + "Board deck numbers for Thursday " * 4),
        (t0 + timedelta(days=1, minutes=1), "assistant", "Q3 revenue…"),
    ]
    with _get_conn(db) as conn:
        for at, role, text in plan:
            conn.execute(
                "INSERT INTO chat_messages (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (BASE, role, text, at.isoformat()),
            )
        if mail_private:
            conn.execute("UPDATE sessions SET mail_private = 1, mail_read_at = 3 WHERE session_id = ?", (BASE,))


def test_old_history_is_cut_by_the_same_rule(db: Path) -> None:
    _old_stream(db)
    assert conversations.split_existing_streams(db_path=db) == 2
    rows = {s["session_id"]: s for s in session_store.list_sessions(OWNER, db_path=db)}
    assert set(rows) == {BASE, f"{BASE}~2", f"{BASE}~3"}
    assert rows[BASE]["message_count"] == 4
    assert rows[BASE]["title"] == "Draft a reply to the landlord"
    assert rows[f"{BASE}~2"]["title"] == "Hiring plan for two sales roles"
    assert rows[f"{BASE}~3"]["title"].startswith("Board deck numbers for Thursday")
    assert rows[f"{BASE}~3"]["title"].endswith("…")
    assert [m["content"] for m in session_store.load_messages(f"{BASE}~2", db_path=db)] == [
        "Hiring plan for two sales roles", "Here's a plan.",
    ]
    # Runs once: a second boot changes nothing.
    assert conversations.split_existing_streams(db_path=db) == 0


def test_a_mail_read_lock_carries_to_every_part(db: Path) -> None:
    _old_stream(db, mail_private=True)
    conversations.split_existing_streams(db_path=db)
    for sid in (BASE, f"{BASE}~2", f"{BASE}~3"):
        assert session_store.session_mail_private(sid, db_path=db)
        assert session_store.mail_read_at(sid, db_path=db) is None


def test_web_chats_and_threads_are_not_cut(db: Path) -> None:
    for sid in ("4f1c2a", "slack:thread:C1:1.2"):
        _turn(db, sid, NOW - timedelta(days=2))
        _turn(db, sid, NOW)
    conversations.split_existing_streams(db_path=db)
    assert {s["session_id"] for s in session_store.list_sessions(OWNER, db_path=db)} == {
        "4f1c2a", "slack:thread:C1:1.2",
    }


# ── what still sees the whole stream ─────────────────────────────────────


def test_decisions_follow_the_stream(db: Path) -> None:
    for sid in (BASE, f"{BASE}~2", "telegram:99"):
        episodic.store_decision("finance", f"from {sid}", session_id=sid, db_path=db)
    got = {d.summary for d in episodic.get_recent_decisions(db_path=db, session_id=f"{BASE}~3")}
    assert got == {f"from {BASE}", f"from {BASE}~2"}


def test_an_approval_asked_before_the_cut_is_answered_after_it() -> None:
    from openexecutive.workflows.inbound_resolver import _scoped_to_session

    run = {"state_json": '{"origin_session_id": "telegram:4242"}'}
    other = {"state_json": '{"origin_session_id": "telegram:99"}'}
    assert _scoped_to_session([run, other], [f"{BASE}~4"]) == [run]
    asked_later = {"state_json": '{"origin_session_id": "telegram:4242~2"}'}
    assert _scoped_to_session([asked_later], [f"{BASE}~3"]) == [asked_later]


# ── Telegram ─────────────────────────────────────────────────────────────


def test_telegram_new_starts_fresh_instead_of_a_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456789:AAH" + "x" * 32)
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "")
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **kw: None)
    fresh, turn = AsyncMock(), AsyncMock()
    app = FastAPI()
    app.include_router(telegram_bot.router)
    update: dict[str, Any] = {
        "message": {"message_id": 5, "chat": {"id": 4242, "type": "private"},
                    "from": {"first_name": "Dana"}, "text": "/new"},
    }
    with (
        patch("openexecutive.people.store.find_person_by_telegram_chat_id", return_value=object()),
        patch.object(telegram_bot, "_start_fresh_and_reply", new=fresh),
        patch.object(telegram_bot, "_process_and_reply", new=turn),
    ):
        client = TestClient(app)
        assert client.post("/webhook/telegram", json=update).status_code == 200
        fresh.assert_awaited_once()
        assert fresh.await_args.kwargs["chat_id"] == 4242
        turn.assert_not_awaited()
        update["message"]["text"] = "new chat with Dana about rent"
        client.post("/webhook/telegram", json=update)
        turn.assert_awaited_once()


def test_an_adapter_can_add_its_own_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.memory import conversation_ids

    monkeypatch.setattr(conversation_ids, "_extra_streams", [])
    assert not is_rolling("pigeon:dm:7")
    conversation_ids.register_rolling(lambda sid: sid.startswith("pigeon:dm:"))
    assert is_rolling("pigeon:dm:7") and is_rolling("pigeon:dm:7~3")
    assert base_session_id("pigeon:dm:7~3") == "pigeon:dm:7"
    assert not is_rolling("pigeon:thread:7")


def test_a_long_message_is_never_a_command_and_stays_fast() -> None:
    import time

    started = time.monotonic()
    assert not chat_conversations.is_new_conversation_command("/new" + " " * 40_000 + "x")
    assert time.monotonic() - started < 0.1


def test_later_conversations_pass_the_api_id_checks() -> None:
    from openexecutive.api.routes import audit, chat

    for pattern in (chat._SESSION_ID_RE, audit._SESSION_ID_RE):
        assert pattern.match("telegram:4242~2") and pattern.match("slack:dm:U1~11")
