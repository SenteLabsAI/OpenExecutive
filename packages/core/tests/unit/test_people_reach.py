"""One rule for where the Executive reaches a person (people/reach.py)."""
from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store
from openexecutive.people.models import Person
from openexecutive.people.reach import note_talked, reach_order
from openexecutive.scheduler.runner import delivery_order


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(people_store, "DB_PATH", tmp_path / "people.db")
    people_registry.invalidate()
    people_store.initialize_db()
    yield
    people_registry.invalidate()


def _person(**kw: Any) -> Person:
    fields: dict[str, Any] = {
        "full_name": "Ada Example", "email": "ada@example.com", "slack_user_id": "U01",
        "discord_user_id": "100000000000000001", "telegram_chat_id": "4242",
    }
    fields.update(kw)
    return Person(**fields)


def test_chat_apps_come_first_and_email_is_the_backup() -> None:
    assert reach_order(_person()) == ["slack", "discord", "telegram", "email"]
    assert reach_order(_person(), email=False) == ["slack", "discord", "telegram"]
    assert reach_order(_person(slack_user_id=None, discord_user_id=None, telegram_chat_id=None)) == ["email"]


def test_where_they_last_talked_goes_first_unless_they_picked_one() -> None:
    assert reach_order(_person(last_channel="telegram")) == ["telegram", "slack", "discord", "email"]
    assert reach_order(_person(last_channel="telegram", preferred_channel="discord")) == [
        "discord", "slack", "telegram", "email",
    ]
    assert reach_order(_person(last_channel="telegram", preferred_channel="email"))[0] == "email"
    # A last app they have no id for any more is passed over.
    assert reach_order(_person(last_channel="telegram", telegram_chat_id=None))[0] == "slack"


def test_briefs_follow_the_same_rule() -> None:
    person = _person(last_channel="telegram")
    assert delivery_order(person, email_ready=True) == ["telegram", "slack_dm", "discord_dm", "email"]
    # A group chat id is never a brief's destination.
    assert delivery_order(_person(telegram_chat_id="-100", last_channel="telegram"), email_ready=False) == [
        "slack_dm", "discord_dm",
    ]


def _session(channel: str, ref: str, person_id: int | None) -> SimpleNamespace:
    return SimpleNamespace(origin_channel=channel, origin_channel_ref=ref, caller_person_id=person_id)


@pytest.fixture(autouse=True)
def _telegram_secured(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.integrations import channel_cards

    monkeypatch.setattr(channel_cards, "telegram_proves_taps", lambda: True)


def test_a_turn_from_their_own_account_is_remembered() -> None:
    pid = people_store.upsert_person(full_name="Ada", slack_user_id="U01", telegram_chat_id="4242")

    assert note_talked(_session("telegram", "4242", pid))
    stored = people_store.get_person(pid)
    assert stored is not None and stored.last_channel == "telegram" and stored.last_channel_at

    assert note_talked(_session("slack", "U01", pid))
    stored = people_store.get_person(pid)
    assert stored is not None and stored.last_channel == "slack"


def test_a_turn_not_from_their_own_account_is_not() -> None:
    pid = people_store.upsert_person(full_name="Ada", slack_user_id="U01", telegram_chat_id="-1001")

    assert not note_talked(_session("slack", "U02", pid))  # someone else's Slack id
    assert not note_talked(_session("telegram", "-1001", pid))  # a group chat
    assert not note_talked(_session("email", "ada@example.com", pid))  # not a chat app
    assert not note_talked(_session("slack", "U01", None))
    assert not note_talked(_session("slack", "U01", pid + 99))
    stored = people_store.get_person(pid)
    assert stored is not None and stored.last_channel is None


def test_a_telegram_turn_without_a_webhook_secret_is_not(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.integrations import channel_cards

    monkeypatch.setattr(channel_cards, "telegram_proves_taps", lambda: False)
    pid = people_store.upsert_person(full_name="Ada", telegram_chat_id="4242")
    assert not note_talked(_session("telegram", "4242", pid))


def test_a_telegram_group_is_never_where_they_are_reached() -> None:
    group = _person(slack_user_id=None, discord_user_id=None, telegram_chat_id="-100", last_channel="telegram")
    assert reach_order(group) == ["email"]
    assert reach_order(group, telegram_groups=True) == ["telegram", "email"]


def test_when_they_last_talked_is_not_serialised() -> None:
    dumped = _person(last_channel="slack", last_channel_at="2026-10-01T09:00:00+00:00").model_dump()
    assert dumped["last_channel"] == "slack" and "last_channel_at" not in dumped


def test_the_same_app_is_written_at_most_hourly() -> None:
    pid = people_store.upsert_person(full_name="Ada", slack_user_id="U01")
    start = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)

    assert people_store.note_last_channel(pid, "slack", now=start)
    assert not people_store.note_last_channel(pid, "slack", now=start + timedelta(minutes=30))
    assert people_store.note_last_channel(pid, "slack", now=start + timedelta(minutes=61))
    assert people_store.note_last_channel(pid, "discord", now=start + timedelta(minutes=62))


async def test_message_person_tries_where_they_last_talked_first(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.orchestrator import schedule_tools

    pid = people_store.upsert_person(full_name="Ada", slack_user_id="U01", telegram_chat_id="4242")
    people_store.note_last_channel(pid, "telegram")
    people_registry.invalidate()
    sent: list[str] = []

    async def telegram(args: dict[str, Any]) -> str:
        sent.append("telegram")
        return json.dumps({"status": "sent"})

    async def slack(args: dict[str, Any]) -> str:
        sent.append("slack")
        return json.dumps({"status": "sent"})

    monkeypatch.setattr(schedule_tools, "handle_send_telegram_message", telegram)
    monkeypatch.setattr(schedule_tools, "handle_send_slack_dm", slack)
    monkeypatch.setattr(schedule_tools, "configured_integrations", lambda settings: {"slack", "telegram"})

    result = json.loads(await schedule_tools.message_person({"person_id": pid, "text": "hi"}))
    assert result["status"] == "sent" and sent == ["telegram"]
