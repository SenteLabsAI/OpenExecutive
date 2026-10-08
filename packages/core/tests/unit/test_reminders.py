"""Act as me's remind_me: plain text to the person who set it, when it is due
(orchestrator/reminder_tools.py, delegation/reminders.py)."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from openexecutive.delegation import lockdown, reminders
from openexecutive.delegation.settings import TurnDelegation
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


@pytest.fixture
def roster() -> SimpleNamespace:
    principal = people_store.upsert_person(full_name="Olivia Owner", is_principal=True, email=OWNER)
    people_registry.invalidate()
    return SimpleNamespace(principal=principal)


@pytest.fixture(autouse=True)
def reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openexecutive.scheduler.runner.delivery_order", lambda _p, **_k: ["email"])
    monkeypatch.setattr("openexecutive.scheduler.runner.email_ready", lambda: True)
    monkeypatch.setattr("openexecutive.memory.workspace_settings.get_user_timezone", lambda *_a: UTC)


def _remind(session: Session | None, tool_input: dict[str, Any]) -> dict[str, Any]:
    async def go() -> str:
        with set_session(session):
            return await rt.handle_remind_me(tool_input)

    return json.loads(asyncio.run(go()))


def _soon(days: int = 2) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M")


# --------------------------------------------------------------------------- #
# The text and the time
# --------------------------------------------------------------------------- #


def test_the_text_loses_links_and_addresses_and_stays_short() -> None:
    text = reminders.clean_text(
        "Reply to Dana\nsee https://evil.example/?d=secret, www.x.example or mail dana@northpeak.example "
        "or evil.example/path?q=1" + " x" * 400
    )
    assert "\n" not in text
    assert "evil.example" not in text and "dana@" not in text and "www." not in text
    assert text.count("[link]") == 4
    assert len(text) <= reminders.MAX_TEXT


def test_when_is_read_in_local_time_and_kept_ahead() -> None:
    assert rt.parse_when("2026-10-10T08:30", NOW) == datetime(2026, 10, 10, 8, 30, tzinfo=UTC)
    assert rt.parse_when("2026-10-10", NOW) == datetime(2026, 10, 10, 9, 0, tzinfo=UTC)
    assert "passed" in str(rt.parse_when("2026-10-01T09:00", NOW))
    assert "year" in str(rt.parse_when("2028-01-01T09:00", NOW))
    assert "date and time" in str(rt.parse_when("next friday", NOW))


# --------------------------------------------------------------------------- #
# Setting one
# --------------------------------------------------------------------------- #


def test_refused_on_a_turn_act_as_me_was_not_offered_to(roster: SimpleNamespace) -> None:
    assert "not available" in _remind(None, {"text": "x", "when": _soon()})["error"]
    stale = Session(turn_delegation=TurnDelegation(offered=True, person_id=roster.principal))
    assert "not available" in _remind(stale, {"text": "x", "when": _soon()})["error"]


def test_a_reminder_is_stored_for_the_speaker_and_opens_no_mailbox(roster: SimpleNamespace) -> None:
    session = _session(FakeMailbox())
    result = _remind(session, {"text": "Reply to Dana about the pilot", "when": _soon()})
    assert result["status"] == "set" and result["text"] == "Reply to Dana about the pilot"
    assert reminders.pending_count(roster.principal) == 1
    # Setting a reminder reads no mail: the turn stays unlocked and unmarked.
    assert session.turn_delegation.touched_mail is False and session.turn_delegation.read_mail is False


def test_bad_input_is_refused(roster: SimpleNamespace) -> None:
    session = _session(FakeMailbox())
    assert "text" in _remind(session, {"text": " ", "when": _soon()})["error"]
    assert "text" in _remind(session, {"text": "https://evil.example", "when": _soon()})["error"]
    assert "passed" in _remind(session, {"text": "x", "when": "2020-01-01T09:00"})["error"]


def test_no_channel_to_reach_them_is_refused(roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openexecutive.scheduler.runner.delivery_order", lambda _p, **_k: [])
    assert "no way to reach them" in _remind(_session(FakeMailbox()), {"text": "x", "when": _soon()})["error"]


def test_reminders_are_capped_per_turn_and_waiting(roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    session = _session(FakeMailbox())
    for _ in range(rt.REMINDERS_PER_TURN):
        assert _remind(session, {"text": "x", "when": _soon()})["status"] == "set"
    assert "this turn" in _remind(session, {"text": "x", "when": _soon()})["error"]
    monkeypatch.setattr(reminders, "MAX_PENDING", rt.REMINDERS_PER_TURN)
    assert "already have" in _remind(_session(FakeMailbox()), {"text": "x", "when": _soon()})["error"]


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #


@pytest.fixture
def delivered(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, str]]:
    sent: list[tuple[int, str]] = []

    async def deliver(person: Any, text: str, *, label: str = "") -> Any:
        sent.append((person.id, text))
        return SimpleNamespace(ok=True, channel="email", reason="")

    monkeypatch.setattr("openexecutive.scheduler.runner.deliver_to_person", deliver)
    return sent


def test_a_due_reminder_goes_once_to_its_person_as_stored(
    roster: SimpleNamespace, delivered: list[tuple[int, str]]
) -> None:
    reminders.add(roster.principal, "Reply to Dana", NOW - timedelta(minutes=1), now=NOW)
    reminders.add(roster.principal, "Later", NOW + timedelta(days=1), now=NOW)
    assert asyncio.run(reminders.send_due(NOW)) == 1
    assert delivered == [(roster.principal, "Reminder: Reply to Dana")]
    assert asyncio.run(reminders.send_due(NOW)) == 0  # claimed: never twice
    assert reminders.pending_count(roster.principal) == 1


def test_a_reminder_missed_by_days_is_dropped_not_sent_late(
    roster: SimpleNamespace, delivered: list[tuple[int, str]]
) -> None:
    reminders.add(roster.principal, "Old", NOW - reminders.STALE_AFTER - timedelta(hours=1), now=NOW)
    assert asyncio.run(reminders.send_due(NOW)) == 0
    assert delivered == []


def test_an_archived_person_gets_nothing(roster: SimpleNamespace, delivered: list[tuple[int, str]]) -> None:
    reminders.add(roster.principal, "x", NOW - timedelta(minutes=1), now=NOW)
    people_store.archive_person(roster.principal)
    people_registry.invalidate()
    asyncio.run(reminders.send_due(NOW))
    assert delivered == []


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #


def test_it_rides_with_act_as_me_but_opens_no_mailbox() -> None:
    assert rt.REMIND_ME in DELEGATION_TOOL_NAMES and rt.REMIND_ME in DELEGATION_TOOL_HANDLERS
    assert rt.REMIND_ME not in MAILBOX_TOOL_NAMES


def test_it_stays_on_after_mail_is_read() -> None:
    assert not lockdown.mail_touched_withholds(rt.REMIND_ME, {})
    assert not lockdown.carried_withholds(rt.REMIND_ME, {})
