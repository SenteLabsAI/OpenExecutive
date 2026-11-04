"""briefing.right_now_list: Home's Right now as a plain list of what is
waiting on the principal that Needs you doesn't show."""
from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openexecutive.audit.logger import AuditLogger, set_audit_logger
from openexecutive.briefing import live_signals, narrative_cache, right_now_list, waiting_on

NOW = datetime(2026, 11, 4, 16, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    from openexecutive.integrations import chat_asks
    from openexecutive.memory import episodic

    logger = AuditLogger(db_path=tmp_path / "audit.db")
    set_audit_logger(logger)
    monkeypatch.setattr(episodic, "DB_PATH", tmp_path / "episodic.db")
    monkeypatch.setattr(narrative_cache, "DB_PATH", tmp_path / "episodic.db")
    episodic.initialize_db()
    me = SimpleNamespace(id=7, email="me@co.test", email_aliases=[], full_name="Pat Lee")
    monkeypatch.setattr(live_signals, "_principal", lambda: me)
    monkeypatch.setattr(waiting_on, "replied", lambda: {})
    monkeypatch.setattr(waiting_on, "wrote_to", lambda: {})
    monkeypatch.setattr(chat_asks, "waiting", lambda pid, now=None: [])
    yield me
    set_audit_logger(None)


def _mail(mid: str, sender: str, name: str, subject: str, at: datetime, thread: str = "") -> Any:
    from openexecutive.delegation.gmail import MailMessage

    return MailMessage(
        id=mid, thread_id=thread or f"t-{mid}", from_addr=sender, from_name=name, subject=subject,
        received_at=at.isoformat(),
    )


def _note(mid: str, at: datetime, *, thread: str = "", relation: str = "contact",
          card_id: int | None = None, name: str = "Sam Ortiz", subject: str = "Loan figures") -> None:
    from openexecutive.delegation import inbox

    inbox._note_mail(
        7, _mail(mid, "sam@vendor.test", name, subject, at, thread), relation=relation, card_id=card_id,
    )


def test_only_mail_that_needs_them_and_no_card_shows(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.delegation import inbox

    at = NOW - timedelta(hours=2)
    _note("m1", at, thread="t1", subject="Thanks!")  # needs nothing
    _note("m2", at, thread="t2", subject="Lease terms")  # on a reply card (Needs you)
    _note("m3", at, thread="t3", subject="Budget")  # answered for them
    _note("m4", at, thread="t4", subject="Rent roll")  # the watcher failed
    _note("m5", NOW - timedelta(minutes=5), thread="t5", subject="Quick q")  # failed just now
    _note("m6", at, thread="t6", subject="Not looked at yet")
    _note("m7", at + timedelta(minutes=5), thread="t2", subject="Lease terms", card_id=12)  # came after the card
    _note("m8", at, thread="t8", relation="stranger", subject="Webinar")
    monkeypatch.setattr(inbox, "outcomes", lambda pid, ids: {
        "m1": inbox.NOT_NEEDED, "m2": inbox.DRAFTED, "m3": inbox.SENT,
        "m4": inbox.FAILED, "m5": inbox.RETRY, "m7": inbox.DEFERRED, "m8": inbox.FAILED,
    })

    rows = right_now_list.rows(NOW)

    # Mail on a reply card's thread is Needs you's, even a newer email on it.
    assert [r.key for r in rows] == ["mail:t4"]
    assert rows[0].note == "I couldn't draft a reply to this one"
    assert rows[0].who == "Sam Ortiz"
    assert rows[0].source == {"kind": "mail", "message_id": "m4", "thread_id": "t4"}


def test_mail_they_wrote_back_to_is_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.delegation import inbox

    at = NOW - timedelta(hours=2)
    _note("m1", at, thread="t1")
    monkeypatch.setattr(inbox, "outcomes", lambda pid, ids: {"m1": inbox.FAILED})
    assert [r.key for r in right_now_list.rows(NOW)] == ["mail:t1"]

    monkeypatch.setattr(waiting_on, "replied", lambda: {"t1": at + timedelta(minutes=10)})
    assert right_now_list.rows(NOW) == []


def test_one_row_per_thread_and_a_dismissal_holds_until_new_mail(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.delegation import inbox

    first = NOW - timedelta(hours=3)
    _note("m1", first, thread="t1")
    _note("m2", first + timedelta(minutes=30), thread="t1")
    monkeypatch.setattr(inbox, "outcomes", lambda pid, ids: {m: inbox.FAILED for m in ids})

    rows = right_now_list.rows(NOW)
    assert [r.key for r in rows] == ["mail:t1"]
    assert rows[0].source["message_id"] == "m2"

    assert right_now_list.dismiss("mail:t1", NOW) is True
    # Gone on every later load, the next day too.
    assert right_now_list.rows(NOW) == []
    assert right_now_list.rows(NOW + timedelta(days=1)) == []

    # A newer email in the thread brings it back.
    _note("m3", NOW + timedelta(hours=1), thread="t1")
    assert [r.source["message_id"] for r in right_now_list.rows(NOW + timedelta(hours=2))] == ["m3"]


def test_only_a_current_row_can_be_dismissed() -> None:
    assert right_now_list.dismiss("mail:nope", NOW) is False
    assert right_now_list.dismiss("draft:1", NOW) is False


def test_drafts_and_chat_asks_are_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.delegation import drafts
    from openexecutive.integrations import chat_asks

    monkeypatch.setattr(drafts, "waiting_drafts", lambda pid, since: [
        drafts.WaitingDraft(
            created_at=(NOW - timedelta(hours=20)).isoformat(), to=("morgan@co.test",),
            subject="Q3 financials", row_id=55, thread_id="t9",
        ),
        # Kept for themselves: not waiting on anyone.
        drafts.WaitingDraft(
            created_at=NOW.isoformat(), to=("me@co.test",), subject="Signed LOI", row_id=56,
        ),
        # Not read by a settle pass yet: nothing to name.
        drafts.WaitingDraft(created_at=NOW.isoformat(), row_id=57),
    ])
    monkeypatch.setattr(chat_asks, "waiting", lambda pid, now=None: [
        chat_asks.ChatAsk(
            id=3, channel="slack", place_name="ops", from_name="Kim Ono", text="Can you sign off on the budget?",
            link="https://chat.example.com/m/3", asked_at=(NOW - timedelta(hours=1)).isoformat(),
        ),
    ])

    rows = right_now_list.rows(NOW)

    assert [r.key for r in rows] == ["chat:3", "draft:55"]
    chat, draft = rows
    assert chat.kind == "chat" and chat.who == "Kim Ono" and chat.source["kind"] == "link"
    assert draft.kind == "draft" and draft.subject == "Q3 financials"
    assert draft.note == "In your Drafts, not sent"
    assert right_now_list.dismiss("draft:55", NOW) is True
    assert [r.key for r in right_now_list.rows(NOW)] == ["chat:3"]


def test_no_principal_no_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(live_signals, "_principal", lambda: None)
    assert right_now_list.rows(NOW) == []
