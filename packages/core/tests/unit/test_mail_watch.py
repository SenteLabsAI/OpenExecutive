"""Act as me's watch_for_email: tell the person when an awaited email lands
in their own mailbox (orchestrator/reminder_tools.py, delegation/mail_watch.py)."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from openexecutive.delegation import lockdown, mail_watch
from openexecutive.delegation.gmail import MailMessage
from openexecutive.orchestrator import reminder_tools as rt
from openexecutive.orchestrator.delegation_tools import (
    DELEGATION_TOOL_HANDLERS,
    DELEGATION_TOOL_NAMES,
    MAILBOX_TOOL_NAMES,
)
from openexecutive.orchestrator.schedule_tools import set_session
from openexecutive.orchestrator.session import Session
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store

# ghostwrite_email's fakes, and its autouse fixtures (a temp DB, a fresh turn pin).
from .test_delegation_tools import OWNER, FakeMailbox, _session, db, fresh_turn_state  # noqa: F401

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
DANA = "dana@northpeak.example"


@pytest.fixture
def principal_id() -> int:
    person_id = people_store.upsert_person(full_name="Olivia Owner", is_principal=True, email=OWNER)
    people_registry.invalidate()
    return person_id


@pytest.fixture(autouse=True)
def reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openexecutive.scheduler.runner.delivery_order", lambda _p, **_k: ["email"])
    monkeypatch.setattr("openexecutive.scheduler.runner.email_ready", lambda: True)
    monkeypatch.setattr("openexecutive.memory.workspace_settings.get_user_timezone", lambda *_a: UTC)


@pytest.fixture
def delivered(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, str]]:
    sent: list[tuple[int, str]] = []

    async def deliver(person: Any, text: str, *, label: str = "") -> Any:
        sent.append((person.id, text))
        return SimpleNamespace(ok=True, channel="email", reason="")

    monkeypatch.setattr("openexecutive.scheduler.runner.deliver_to_person", deliver)
    return sent


class _Inbox:
    """The person's mailbox as the watch sees it."""

    def __init__(self) -> None:
        self.mail: list[MailMessage] = []
        self.asked: list[str] = []

    async def recent_from(self, address: str, *, days: int = 90, limit: int = 5) -> list[MailMessage]:
        self.asked.append(address)
        return [m for m in self.mail if m.from_addr == address][:limit]


@pytest.fixture
def inbox(monkeypatch: pytest.MonkeyPatch) -> _Inbox:
    box = _Inbox()

    async def status(_email: str, *, gmail: Any = None) -> str:
        return "connected"

    monkeypatch.setattr("openexecutive.delegation.gmail.gmail_status", status)
    monkeypatch.setattr("openexecutive.delegation.gmail.gmail_for", lambda _email: box)
    return box


def _mail(received: datetime, subject: str = "Final offers", sender: str = DANA) -> MailMessage:
    return MailMessage(
        id=f"m{int(received.timestamp())}", thread_id="t", from_addr=sender,
        from_name="Dana Reyes", subject=subject, received_at=received.isoformat(),
        text="The body is never sent on.",
    )


def _watch(session: Session | None, tool_input: dict[str, Any]) -> dict[str, Any]:
    async def go() -> str:
        with set_session(session):
            return await rt.handle_watch_for_email(tool_input)

    return json.loads(asyncio.run(go()))


# --------------------------------------------------------------------------- #
# Setting one
# --------------------------------------------------------------------------- #


def test_a_watch_is_stored_for_the_speaker_and_reads_no_mail(principal_id: int) -> None:
    session = _session(FakeMailbox())
    result = _watch(session, {"from": "Dana@NorthPeak.example", "about": "offers", "days": 3})
    assert result["status"] == "watching" and result["from"] == DANA and result["days"] == 3
    assert mail_watch.pending_count(principal_id) == 1
    assert session.turn_delegation.touched_mail is False and session.turn_delegation.read_mail is False


def test_bad_input_is_refused(principal_id: int) -> None:
    session = _session(FakeMailbox())
    assert "address" in _watch(session, {"from": "Dana"})["error"]
    assert "own address" in _watch(session, {"from": OWNER})["error"]
    assert "not available" in _watch(None, {"from": DANA})["error"]


def test_watches_are_capped(principal_id: int, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mail_watch, "MAX_PENDING", 1)
    assert _watch(_session(FakeMailbox()), {"from": DANA})["status"] == "watching"
    assert "already have" in _watch(_session(FakeMailbox()), {"from": DANA})["error"]


# --------------------------------------------------------------------------- #
# Checking
# --------------------------------------------------------------------------- #


def test_the_awaited_email_is_told_once_without_its_text(
    principal_id: int, inbox: _Inbox, delivered: list[tuple[int, str]],
) -> None:
    mail_watch.add(principal_id, DANA, "", NOW + timedelta(days=7), now=NOW)
    inbox.mail = [_mail(NOW - timedelta(hours=1))]  # came before the watch: not it
    assert asyncio.run(mail_watch.check_due(NOW + timedelta(minutes=5))) == 0
    assert delivered == []

    inbox.mail.insert(0, _mail(NOW + timedelta(minutes=30), subject="Final offers https://evil.example"))
    assert asyncio.run(mail_watch.check_due(NOW + timedelta(hours=1))) == 1
    assert delivered == [(principal_id, 'The email you were waiting for came in: Dana Reyes, "Final offers [link]".')]
    assert "body" not in delivered[0][1]
    assert asyncio.run(mail_watch.check_due(NOW + timedelta(hours=2))) == 0
    assert len(delivered) == 1 and mail_watch.pending_count(principal_id) == 0


def test_the_subject_word_must_match(
    principal_id: int, inbox: _Inbox, delivered: list[tuple[int, str]],
) -> None:
    mail_watch.add(principal_id, DANA, "contract", NOW + timedelta(days=7), now=NOW)
    inbox.mail = [_mail(NOW + timedelta(minutes=10), subject="Lunch?")]
    assert asyncio.run(mail_watch.check_due(NOW + timedelta(hours=1))) == 0
    inbox.mail.insert(0, _mail(NOW + timedelta(minutes=20), subject="Signed Contract attached"))
    assert asyncio.run(mail_watch.check_due(NOW + timedelta(hours=1))) == 1


def test_a_watch_nothing_answered_ends_with_a_note(
    principal_id: int, inbox: _Inbox, delivered: list[tuple[int, str]],
) -> None:
    mail_watch.add(principal_id, DANA, "", NOW + timedelta(days=1), now=NOW)
    assert asyncio.run(mail_watch.check_due(NOW + timedelta(days=2))) == 1
    assert delivered == [(principal_id, "No email from the sender you were waiting on came in since Oct 7. I've stopped watching for it.")]


def test_a_mailbox_failure_keeps_the_watch(
    principal_id: int, inbox: _Inbox, delivered: list[tuple[int, str]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken(*_a: Any, **_k: Any) -> list[MailMessage]:
        raise RuntimeError("mail service down")

    monkeypatch.setattr(inbox, "recent_from", broken)
    mail_watch.add(principal_id, DANA, "", NOW + timedelta(days=7), now=NOW)
    assert asyncio.run(mail_watch.check_due(NOW + timedelta(hours=1))) == 0
    assert mail_watch.pending_count(principal_id) == 1 and delivered == []


def test_a_broken_mailbox_still_ends_the_watch_on_time(
    principal_id: int, inbox: _Inbox, delivered: list[tuple[int, str]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken(*_a: Any, **_k: Any) -> list[MailMessage]:
        raise RuntimeError("credential revoked")

    monkeypatch.setattr(inbox, "recent_from", broken)
    mail_watch.add(principal_id, DANA, "", NOW + timedelta(days=1), now=NOW)
    assert asyncio.run(mail_watch.check_due(NOW + timedelta(days=2))) == 1
    assert len(delivered) == 1 and "couldn't read your mailbox" in delivered[0][1]
    assert mail_watch.pending_count(principal_id) == 0


def test_a_subject_watch_reads_past_the_newest_five(
    principal_id: int, inbox: _Inbox, delivered: list[tuple[int, str]],
) -> None:
    mail_watch.add(principal_id, DANA, "contract", NOW + timedelta(days=7), now=NOW)
    inbox.mail = [_mail(NOW + timedelta(minutes=50 - i), subject=f"Note {i}") for i in range(6)]
    inbox.mail.append(_mail(NOW + timedelta(minutes=10), subject="The contract"))
    assert asyncio.run(mail_watch.check_due(NOW + timedelta(hours=1))) == 1


def test_an_undelivered_message_is_tried_again(
    principal_id: int, inbox: _Inbox, monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = [False, True]
    sent: list[str] = []

    async def deliver(person: Any, text: str, *, label: str = "") -> Any:
        sent.append(text)
        return SimpleNamespace(ok=results.pop(0), channel="email", reason="down")

    monkeypatch.setattr("openexecutive.scheduler.runner.deliver_to_person", deliver)
    mail_watch.add(principal_id, DANA, "", NOW + timedelta(days=7), now=NOW)
    inbox.mail = [_mail(NOW + timedelta(minutes=30))]
    assert asyncio.run(mail_watch.check_due(NOW + timedelta(hours=1))) == 0
    assert mail_watch.pending_count(principal_id) == 1
    assert asyncio.run(mail_watch.check_due(NOW + timedelta(hours=2))) == 1
    assert len(sent) == 2 and mail_watch.pending_count(principal_id) == 0


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #


def test_it_rides_with_act_as_me_but_opens_no_mailbox() -> None:
    assert rt.WATCH_FOR_EMAIL in DELEGATION_TOOL_NAMES and rt.WATCH_FOR_EMAIL in DELEGATION_TOOL_HANDLERS
    assert rt.WATCH_FOR_EMAIL not in MAILBOX_TOOL_NAMES
    assert not lockdown.mail_touched_withholds(rt.WATCH_FOR_EMAIL, {})
