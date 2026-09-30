"""Act as me's drafts table (delegation/drafts.py) and the daily limit it
feeds (delegation/caps.py), shared by chat and the inbox watcher."""
from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openexecutive.delegation import caps, drafts
from openexecutive.delegation import voice as dvoice
from openexecutive.memory import episodic

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", path)
    episodic.initialize_db(path)
    caps._SAVED_TODAY.clear()
    caps._IN_FLIGHT.clear()
    yield path
    caps._SAVED_TODAY.clear()
    caps._IN_FLIGHT.clear()


def _record(person: int, source: str, thread: str | None, draft: str, when: datetime = NOW) -> int:
    return drafts.record(person, source=source, thread_id=thread, draft_id=draft, message_id=f"m-{draft}", now=when)


def test_each_draft_is_one_row_of_ids(db: Path) -> None:
    import sqlite3

    _record(1, drafts.SOURCE_CHAT, "t1", "d1")
    with sqlite3.connect(db) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(delegation_drafts)")]
        row = conn.execute("SELECT * FROM delegation_drafts").fetchone()
    # Ids and times only: never an address, a subject or text.
    assert cols == ["id", "person_id", "source", "thread_id", "draft_id", "message_id",
                    "sent_message_id", "created_at"]
    assert row[1:7] == (1, "chat", "t1", "d1", "m-d1", None)
    with pytest.raises(ValueError):
        _record(1, "email", "t1", "d2")


def test_the_count_covers_both_sources_and_only_the_window() -> None:
    _record(1, drafts.SOURCE_CHAT, "t1", "d1")
    _record(1, drafts.SOURCE_INBOX, "t2", "d2")
    _record(1, drafts.SOURCE_INBOX, "t3", "d3", when=NOW - timedelta(days=1))
    _record(2, drafts.SOURCE_CHAT, "t4", "d4")
    day = NOW.replace(hour=0)
    assert drafts.count_since(1, day) == 2
    assert drafts.count_since(2, day) == 1


def test_what_the_voice_learner_leaves_out() -> None:
    _record(1, drafts.SOURCE_CHAT, "chat-thread", "d1")
    _record(1, drafts.SOURCE_INBOX, "inbox-thread", "d2")
    _record(1, drafts.SOURCE_CHAT, None, "d3")  # a new email: no thread
    assert drafts.mark_sent(1, "d2", "sent-2") is True
    assert drafts.mark_sent(2, "d2", "sent-x") is False  # someone else's draft id
    # A chat draft's thread; an inbox draft only as the message it became.
    assert drafts.chat_thread_ids(1) == {"chat-thread"}
    assert drafts.sent_message_ids(1) == {"sent-2"}
    assert dvoice.drafted_thread_ids(1) >= {"chat-thread"}
    assert dvoice.sent_draft_ids(1) == {"sent-2"}


def test_collect_samples_skips_a_sent_draft_but_keeps_its_thread() -> None:
    words = "Thanks for the note, Thursday works for me and I'll bring the numbers."

    def sent(mid: str, thread: str) -> Any:
        return SimpleNamespace(
            id=mid, thread_id=thread, text=words, to=["a@x.example"], cc=[],
            auto_generated=False, ghostwritten=False,
        )

    samples = dvoice.collect_samples(
        [sent("sent-2", "inbox-thread"), sent("own-1", "inbox-thread"), sent("own-2", "chat-thread")],
        skip_threads={"chat-thread"},
        skip_messages={"sent-2"},
    )
    # Their own reply in the watcher's thread is still theirs to learn from.
    assert len(samples) == 1


def test_the_daily_limit_is_shared_and_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    today = datetime.now(UTC)
    _record(1, drafts.SOURCE_INBOX, "t1", "d1", when=today)
    assert caps.drafts_today(1) == 1
    assert caps.reserve(1, daily_cap=2) is None
    # One saved from the inbox and one in flight: the limit of two is used.
    assert caps.reserve(1, daily_cap=2) == caps.REACHED
    caps.release(1, saved=False)
    assert caps.reserve(1, daily_cap=2) is None
    caps.release(1, saved=True)
    # A saved draft counts even if its row never landed.
    assert caps.drafts_today(1) == 1 and caps._SAVED_TODAY[(1, caps._utc_day())] == 1

    def broken(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("db locked")

    monkeypatch.setattr(drafts, "count_since", broken)
    assert caps.drafts_today(1) is None
    assert caps.reserve(1, daily_cap=50) == caps.UNCOUNTABLE
    assert caps._IN_FLIGHT == {}


def test_the_table_is_per_company() -> None:
    from openexecutive.clients.slots import _BLANK_WIPE_TABLES
    from openexecutive.delegation.schema import DRAFTS_TABLE, TABLES

    assert DRAFTS_TABLE in TABLES and DRAFTS_TABLE in _BLANK_WIPE_TABLES
