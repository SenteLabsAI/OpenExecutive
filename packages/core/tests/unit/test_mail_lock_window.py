"""Act as me: a later turn of a conversation that read the owner's mail stays
private to them but is not locked: only the reading turn is
(delegation/settings.py ``_carry_kept_private``). The conversation still
records when it read mail (memory/session_store.py ``mail_read_at``)."""
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


def test_history_len_reads_unknown_as_none() -> None:
    assert dsettings.history_len(object()) is None
    assert dsettings.history_len(Session()) == 0


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
    # Read "now": 42 messages in.
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
    # Unknown stays unknown.
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


def test_a_later_turn_is_private_but_not_locked(store_on_db: Path) -> None:
    session_store.mark_mail_private("tg:1", None, db_path=store_on_db, history_len=4)
    pinned = TurnDelegation(session_id="tg:1")
    dsettings._carry_kept_private(pinned)
    assert pinned.touched_mail and not pinned.read_mail
    # Nothing carries the reading turn's lockdown into this one.
    assert not hasattr(pinned, "mail_in_view")


def test_a_conversation_that_never_read_mail_is_left_alone(store_on_db: Path) -> None:
    pinned = TurnDelegation(session_id="fresh")
    dsettings._carry_kept_private(pinned)
    assert not pinned.touched_mail
