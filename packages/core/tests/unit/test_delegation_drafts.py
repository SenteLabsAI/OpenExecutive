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
    drafts._seen.clear()
    yield path
    drafts._seen.clear()
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
                    "sent_message_id", "created_at", "gone_at", "session_id"]
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


class _Mailbox:
    """``get_draft`` the way Gmail answers it: the draft, or None once it is
    sent or deleted."""

    def __init__(self, present: set[str], fail: Exception | None = None) -> None:
        self.present = present
        self.fail = fail
        self.asked: list[str] = []

    async def get_draft(self, draft_id: str) -> Any:
        self.asked.append(draft_id)
        if self.fail is not None:
            raise self.fail
        return SimpleNamespace(draft_id=draft_id) if draft_id in self.present else None


PERSON = SimpleNamespace(id=1, email="owner@example.com")


async def test_a_chat_draft_sent_in_gmail_stops_waiting() -> None:
    _record(1, drafts.SOURCE_CHAT, "t1", "d1")
    _record(1, drafts.SOURCE_CHAT, None, "d2")
    _record(1, drafts.SOURCE_INBOX, "t3", "d3")  # the watcher's cards settle these
    day = NOW.replace(hour=0)
    assert drafts.count_waiting(1, day) == 2

    box = _Mailbox(present={"d2"})
    assert await drafts.settle_waiting(PERSON, now=NOW, gmail=box) == 1

    assert sorted(box.asked) == ["d1", "d2"]
    assert drafts.count_waiting(1, day) == 1
    # Once gone, never asked about again.
    box.asked.clear()
    assert await drafts.settle_waiting(PERSON, now=NOW, gmail=box) == 0
    assert box.asked == ["d2"]


async def test_a_waiting_draft_is_named_by_who_it_goes_to_and_its_subject() -> None:
    _record(1, drafts.SOURCE_CHAT, "t1", "d1")
    _record(1, drafts.SOURCE_CHAT, None, "d2")
    day = NOW.replace(hour=0)
    # Before any settle pass: listed, but nothing read yet.
    assert [(d.to, d.subject) for d in drafts.waiting_drafts(1, day)] == [((), ""), ((), "")]

    class Box:
        async def get_draft(self, draft_id: str) -> Any:
            if draft_id == "d1":
                return None  # sent
            message = SimpleNamespace(to=["dana@example.com"], cc=[], subject="Wednesday meeting")
            return SimpleNamespace(draft_id=draft_id, message=message)

    assert await drafts.settle_waiting(PERSON, now=NOW, gmail=Box()) == 1
    waiting = drafts.waiting_drafts(1, day)
    assert [(d.to, d.subject) for d in waiting] == [(("dana@example.com",), "Wednesday meeting")]


async def test_a_reply_draft_stops_waiting_once_they_answered_in_the_thread() -> None:
    """They replied in Gmail themselves instead of sending the draft: the
    draft is still in Drafts, but it is no longer theirs to review."""
    answered = _record(1, drafts.SOURCE_CHAT, "t1", "d1", when=NOW - timedelta(hours=3))
    _record(1, drafts.SOURCE_CHAT, "t2", "d2", when=NOW - timedelta(hours=3))
    after = (NOW - timedelta(hours=1)).isoformat()
    before = (NOW - timedelta(hours=5)).isoformat()

    def msg(sender: str, labels: list[str], at: str) -> SimpleNamespace:
        return SimpleNamespace(from_addr=sender, labels=labels, received_at=at)

    threads = {
        "t1": [msg("dana@example.com", ["INBOX"], before), msg("owner@example.com", ["SENT"], after)],
        # Only an older send of theirs, a draft and someone else's mail since.
        "t2": [
            msg("owner@example.com", ["SENT"], before),
            msg("owner@example.com", ["DRAFT"], after),
            msg("dana@example.com", ["INBOX"], after),
        ],
    }

    class Box:
        async def get_draft(self, draft_id: str) -> Any:
            return SimpleNamespace(draft_id=draft_id, message=None)

        async def get_thread(self, thread_id: str) -> Any:
            return SimpleNamespace(messages=threads[thread_id])

    assert await drafts.settle_waiting(PERSON, now=NOW, gmail=Box()) == 1
    left = drafts._waiting(1, NOW - timedelta(days=1), None)
    assert [draft_id for _, draft_id in left] == ["d2"]
    assert (1, answered) not in drafts._seen


async def test_one_persons_pass_keeps_anothers_details() -> None:
    _record(1, drafts.SOURCE_CHAT, None, "d1")
    _record(2, drafts.SOURCE_CHAT, None, "d2")

    class Box:
        async def get_draft(self, draft_id: str) -> Any:
            message = SimpleNamespace(to=["dana@example.com"], cc=[], subject=f"About {draft_id}")
            return SimpleNamespace(draft_id=draft_id, message=message)

    await drafts.settle_waiting(PERSON, now=NOW, gmail=Box())
    await drafts.settle_waiting(SimpleNamespace(id=2, email="ben@example.com"), now=NOW, gmail=Box())
    day = NOW.replace(hour=0)
    assert [d.subject for d in drafts.waiting_drafts(1, day)] == ["About d1"]
    assert [d.subject for d in drafts.waiting_drafts(2, day)] == ["About d2"]


async def test_settling_reads_nothing_without_recent_chat_drafts() -> None:
    _record(1, drafts.SOURCE_CHAT, "t1", "old", when=NOW - timedelta(days=3))
    _record(2, drafts.SOURCE_CHAT, "t2", "theirs")
    box = _Mailbox(present=set())
    assert await drafts.settle_waiting(PERSON, now=NOW, gmail=box) == 0
    assert box.asked == []


async def test_a_mailbox_error_leaves_drafts_waiting() -> None:
    from openexecutive.delegation.gmail import GmailAuthError

    _record(1, drafts.SOURCE_CHAT, "t1", "d1")
    box = _Mailbox(present=set(), fail=GmailAuthError("revoked"))
    assert await drafts.settle_waiting(PERSON, now=NOW, gmail=box) == 0
    assert drafts.count_waiting(1, NOW.replace(hour=0)) == 1


def _sent(subject: str, to: list[str], at: datetime) -> SimpleNamespace:
    return SimpleNamespace(subject=subject, to=to, cc=[], bcc=[], received_at=at.isoformat())


class _Versions:
    """Two versions of one email still in Drafts, and their sent mail."""

    def __init__(self, sent: list[SimpleNamespace], subjects: dict[str, str] | None = None) -> None:
        self.sent = sent
        self.subjects = subjects or {}
        self.sent_reads = 0

    async def get_draft(self, draft_id: str) -> Any:
        subject = self.subjects.get(draft_id, "2027 budget")
        message = SimpleNamespace(to=["Dana@example.com"], cc=[], subject=subject)
        return SimpleNamespace(draft_id=draft_id, message=message)

    async def list_sent(self, limit: int = 40) -> list[SimpleNamespace]:
        self.sent_reads += 1
        return self.sent


async def test_a_draft_sent_as_another_version_stops_waiting() -> None:
    """They redrafted it, sent one version (or wrote it fresh) and left the
    other in Drafts: both stop waiting once the sent copy is there."""
    _record(1, drafts.SOURCE_CHAT, None, "d1", when=NOW - timedelta(hours=3))
    _record(1, drafts.SOURCE_CHAT, None, "d2", when=NOW - timedelta(hours=2, minutes=50))
    box = _Versions([_sent("RE: 2027  Budget", ["dana@example.com"], NOW - timedelta(hours=2))])

    assert await drafts.settle_waiting(PERSON, now=NOW, gmail=box) == 2
    assert drafts.count_waiting(1, NOW.replace(hour=0)) == 0
    assert box.sent_reads == 1


async def test_a_sent_email_that_is_not_the_draft_leaves_it_waiting() -> None:
    _record(1, drafts.SOURCE_CHAT, None, "d1", when=NOW - timedelta(hours=3))
    box = _Versions([
        _sent("2027 budget", ["dana@example.com"], NOW - timedelta(hours=4)),  # before the draft
        _sent("2027 budget", ["ben@example.com"], NOW - timedelta(hours=1)),  # someone else
        _sent("Lunch", ["dana@example.com"], NOW - timedelta(hours=1)),  # another subject
        SimpleNamespace(subject="2027 budget", to=None, received_at=NOW.isoformat()),  # no fields
    ])

    assert await drafts.settle_waiting(PERSON, now=NOW, gmail=box) == 0
    assert drafts.count_waiting(1, NOW.replace(hour=0)) == 1


async def test_sent_mail_is_not_read_when_every_draft_is_gone() -> None:
    _record(1, drafts.SOURCE_CHAT, None, "d1")

    class Box(_Versions):
        async def get_draft(self, draft_id: str) -> Any:
            return None

    box = Box([])
    assert await drafts.settle_waiting(PERSON, now=NOW, gmail=box) == 1
    assert box.sent_reads == 0


async def test_one_drafts_failure_does_not_stop_the_pass() -> None:
    _record(1, drafts.SOURCE_CHAT, None, "d1", when=NOW - timedelta(hours=1))
    _record(1, drafts.SOURCE_CHAT, None, "d2", when=NOW - timedelta(hours=2))

    class Box:
        async def get_draft(self, draft_id: str) -> Any:
            if draft_id == "d2":
                raise ValueError("unexpected reply")
            return None

    # Newest first: d2's failure comes first, and d1 is still checked.
    assert await drafts.settle_waiting(PERSON, now=NOW, gmail=Box()) == 1
    assert [d for _, d in drafts._waiting(1, NOW - timedelta(days=1), None)] == ["d2"]


def test_an_older_table_gets_the_gone_at_and_session_id_columns(tmp_path: Path) -> None:
    import sqlite3

    from openexecutive.delegation.schema import ensure_schema

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE delegation_drafts (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "person_id INTEGER NOT NULL, source TEXT NOT NULL, thread_id TEXT, draft_id TEXT, "
            "message_id TEXT, sent_message_id TEXT, created_at TEXT NOT NULL)"
        )
        ensure_schema(conn)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(delegation_drafts)")]
    assert cols[-2:] == ["gone_at", "session_id"]
