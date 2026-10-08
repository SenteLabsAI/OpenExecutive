"""Act as me: a later turn of a conversation that read the owner's mail holds
links, scripts, workflows and posts to everyone only while the reading turn
is in the history the model is shown (delegation/settings.py
``mail_still_in_view``, memory/session_store.py ``mail_read_at``)."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from openexecutive.delegation import settings as dsettings
from openexecutive.delegation.settings import TurnDelegation
from openexecutive.memory import episodic, session_store
from openexecutive.orchestrator.session import Session, history_window_start


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", path)
    episodic.initialize_db(path)
    yield path


def test_the_window_start_matches_what_the_model_is_shown() -> None:
    for total in (0, 10, 40, 41, 59, 60, 61, 79, 80, 100, 137):
        session = Session(conversation_history=[
            {"role": "user" if i % 2 == 0 else "assistant", "content": str(i)} for i in range(total)
        ])
        shown = session.get_recent_history()
        start = history_window_start(total)
        assert [m["content"] for m in shown] == [str(i) for i in range(start, total)]


def test_the_carried_lock_lifts_once_the_reading_turn_is_out_of_view() -> None:
    # Mail read with 4 messages before the turn: it spans messages 4 to 6.
    assert dsettings.mail_still_in_view(4, 10)
    assert history_window_start(59) == 0 and dsettings.mail_still_in_view(4, 59)
    assert history_window_start(60) == 20 and not dsettings.mail_still_in_view(4, 60)
    # A read late in a long chat stays in view for a while.
    assert dsettings.mail_still_in_view(100, 120)
    assert dsettings.mail_still_in_view(100, 159)
    assert not dsettings.mail_still_in_view(100, 180)
    # Unknown (a conversation marked before the column): always in view.
    assert dsettings.mail_still_in_view(None, 10_000)
    # A read mid-conversation (30 messages before it) holds until the window
    # starts past its last message.
    for total in (40, 50, 59, 60, 79):
        assert dsettings.mail_still_in_view(30, total), total
    assert history_window_start(80) == 40 and not dsettings.mail_still_in_view(30, 80)
    # A history that can't be measured stores and reads as unknown too.
    assert dsettings.history_len(object()) is None
    assert dsettings.history_len(Session()) == 0
    assert dsettings.mail_still_in_view(0, None)


def test_a_conversation_that_read_mail_before_the_column_counts_from_now(db: Path) -> None:
    import sqlite3

    session_store.mark_mail_private("old", 7, db_path=db)
    session_store.mark_mail_private("other", 7, db_path=db, history_len=3)
    with sqlite3.connect(db) as conn:
        conn.executemany(
            "INSERT INTO chat_messages (session_id, role, content, created_at) VALUES ('old', 'user', ?, '')",
            [(str(i),) for i in range(42)],
        )
        conn.execute("ALTER TABLE sessions DROP COLUMN mail_read_at")
    episodic.initialize_db(db)
    # Read "now": 42 messages in, so the hold lifts some 20 to 30 later.
    assert session_store.mail_read_at("old", db_path=db) == 42
    assert session_store.mail_read_at("other", db_path=db) == 0


def test_mark_mail_private_stores_when_the_mail_was_read(db: Path) -> None:
    assert session_store.mail_read_at("s1", db_path=db) is None
    session_store.mark_mail_private("s1", 7, db_path=db, history_len=12)
    assert session_store.session_mail_private("s1", db_path=db)
    assert session_store.mail_read_at("s1", db_path=db) == 12
    # A later read moves it on.
    session_store.mark_mail_private("s1", 7, db_path=db, history_len=50)
    assert session_store.mail_read_at("s1", db_path=db) == 50
    # Unknown stays unknown, which keeps the lock.
    session_store.mark_mail_private("s2", 7, db_path=db)
    assert session_store.mail_read_at("s2", db_path=db) is None


@pytest.fixture
def store_on_db(db: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the session store's readers at the test DB (their ``db_path``
    default is bound at import)."""
    for name in ("session_mail_private", "mail_read_at", "get_session_owner"):
        real = getattr(session_store, name)
        monkeypatch.setattr(session_store, name, lambda sid, _real=real: _real(sid, db_path=db))
    return db


def _later_turn(total: int, read_at: int | None, db: Path) -> TurnDelegation:
    session_store.mark_mail_private("tg:1", None, db_path=db, history_len=read_at)
    pinned = TurnDelegation(session_id="tg:1")
    dsettings._carry_kept_private(pinned, total)
    return pinned


def test_a_later_turn_carries_the_lock_only_while_the_mail_is_in_view(store_on_db: Path) -> None:
    near = _later_turn(20, 4, store_on_db)
    assert near.touched_mail and near.mail_in_view
    far = _later_turn(200, 4, store_on_db)
    # Still private to its owner, but no longer locked.
    assert far.touched_mail and not far.mail_in_view
    # Marked before the column existed: kept locked.
    old = _later_turn(200, None, store_on_db)
    assert old.touched_mail and old.mail_in_view
