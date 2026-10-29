"""Team chat messages that ask something of the principal, kept until they
answer and shown in Right now (integrations/chat_asks.py, slack_bot.note_chat_ask,
briefing.live_signals, briefing.header_context)."""
from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from openexecutive.briefing.header_context import needs_you_tags, render_header_context
from openexecutive.briefing.live_signals import LiveSignals
from openexecutive.integrations import chat_asks, slack_bot
from openexecutive.memory import episodic
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store

NOW = datetime(2026, 10, 29, 15, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", path)
    monkeypatch.setattr(people_store, "DB_PATH", path)
    episodic.initialize_db(path)
    people_store.initialize_db(path)
    people_registry.invalidate()
    yield path
    people_registry.invalidate()


@pytest.fixture
def owner() -> int:
    person_id = people_store.upsert_person(
        full_name="Olivia Owner", is_principal=True, email="olivia@acme.example", slack_user_id="UOWNER",
    )
    people_store.upsert_person(full_name="Ben Teammate", email="ben@acme.example", slack_user_id="UBEN")
    people_registry.invalidate()
    return person_id


def _ask(owner: int, ref: str, *, thread: str = "t1", at: datetime = NOW - timedelta(hours=1)) -> bool:
    return chat_asks.note_ask(
        "slack", place="C1", thread=thread, message_ref=ref, person_id=owner, from_name="Ben Teammate",
        text="@Olivia can you sign off on the budget?", link=f"https://acme.slack.com/archives/C1/p{ref}",
        place_name="#launch", at=at,
    )


def test_an_ask_waits_half_an_hour_then_shows_until_answered(owner: int) -> None:
    assert _ask(owner, "1", at=NOW - timedelta(minutes=10))
    assert chat_asks.waiting(owner, NOW) == []
    later = NOW + timedelta(minutes=25)
    [ask] = chat_asks.waiting(owner, later)
    assert (ask.from_name, chat_asks.where(ask)) == ("Ben Teammate", "Slack #launch")
    # A reply in another thread answers nothing; one in its thread does.
    chat_asks.note_spoke("slack", place="C1", thread="t9", top_level=False, person_id=owner, at=later)
    assert len(chat_asks.waiting(owner, later)) == 1
    chat_asks.note_spoke("slack", place="C1", thread="t1", top_level=False, person_id=owner, at=later)
    assert chat_asks.waiting(owner, later) == []


def test_a_new_message_in_the_channel_answers_the_asks_before_it(owner: int) -> None:
    _ask(owner, "1", thread="t1")
    _ask(owner, "2", thread="t2")
    chat_asks.note_spoke("slack", place="C1", thread="t3", top_level=True, person_id=owner, at=NOW)
    assert chat_asks.waiting(owner, NOW) == []


def test_only_the_principals_asks_are_kept_once_and_old_ones_drop(owner: int) -> None:
    ben = people_store.find_person_by_slack_id("UBEN")
    assert ben is not None and ben.id is not None
    assert not chat_asks.note_ask(
        "slack", place="C1", thread="t", message_ref="9", person_id=ben.id, from_name="x", text="y",
    )
    assert _ask(owner, "1")
    assert not _ask(owner, "1")
    _ask(owner, "2", at=NOW - timedelta(hours=30))
    assert [a.link for a in chat_asks.waiting(owner, NOW)] == ["https://acme.slack.com/archives/C1/p1"]


class FakeSlack:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def users_info(self, user: str) -> dict[str, Any]:
        self.calls.append("users_info")
        return {"user": {"real_name": "Someone Else"}}

    async def conversations_info(self, channel: str) -> dict[str, Any]:
        return {"channel": {"name": "launch"}}

    async def chat_getPermalink(self, channel: str, message_ts: str) -> dict[str, Any]:
        return {"permalink": f"https://acme.slack.com/archives/{channel}/p{message_ts.replace('.', '')}"}


def test_slack_keeps_a_mention_of_the_principal_and_their_reply_answers_it(owner: int) -> None:
    slack = FakeSlack()
    event = {"user": "UBEN", "channel": "C1", "ts": "100.1", "text": "<@UOWNER> can you sign off? cc <@UX>"}
    asyncio.run(slack_bot.note_chat_ask(event, slack))
    [ask] = chat_asks.waiting(owner, datetime.now(UTC) + timedelta(hours=1))
    assert ask.text == "@Olivia can you sign off? cc @someone"
    assert ask.from_name == "Ben Teammate" and slack.calls == []  # on the roster: no lookup
    assert ask.link == "https://acme.slack.com/archives/C1/p1001"
    assert chat_asks.where(ask) == "Slack #launch"
    # Her answer in its thread clears it.
    asyncio.run(slack_bot.note_chat_ask({"user": "UOWNER", "channel": "C1", "ts": "200.1", "thread_ts": "100.1",
                                         "text": "Yes, done."}, slack))
    assert chat_asks.waiting(owner, datetime.now(UTC) + timedelta(hours=1)) == []


def test_a_labelled_mention_counts_and_a_new_channel_message_answers(owner: int) -> None:
    slack = FakeSlack()
    event = {"user": "UBEN", "channel": "C1", "ts": "1.1", "thread_ts": "0.5",
             "text": "<@UOWNER|olivia> and <@U2> please sign off"}
    asyncio.run(slack_bot.note_chat_ask(event, slack))
    later = datetime.now(UTC) + timedelta(hours=1)
    [ask] = chat_asks.waiting(owner, later)
    assert ask.text == "@Olivia and @someone please sign off"
    # A reply of hers in another thread answers nothing; a new message in the channel does.
    asyncio.run(slack_bot.note_chat_ask({"user": "UOWNER", "channel": "C1", "ts": "2.1", "thread_ts": "9.9",
                                         "text": "other"}, slack))
    assert len(chat_asks.waiting(owner, later)) == 1
    asyncio.run(slack_bot.note_chat_ask({"user": "UOWNER", "channel": "C1", "ts": "3.1", "text": "On it."}, slack))
    assert chat_asks.waiting(owner, later) == []


def test_slack_ignores_messages_that_dont_mention_the_principal(owner: int) -> None:
    asyncio.run(slack_bot.note_chat_ask({"user": "UBEN", "channel": "C1", "ts": "1.1", "text": "lunch?"}, FakeSlack()))
    asyncio.run(slack_bot.note_chat_ask({"user": "UBEN", "channel": "C1", "ts": "1.2", "text": "<@UOTHER> lunch?"},
                                        FakeSlack()))
    assert chat_asks.waiting(owner, datetime.now(UTC) + timedelta(hours=1)) == []


def test_someone_off_the_roster_is_named_from_slack(owner: int) -> None:
    slack = FakeSlack()
    asyncio.run(slack_bot.note_chat_ask({"user": "UGUEST", "channel": "C1", "ts": "1.1", "text": "<@UOWNER> hi"}, slack))
    [ask] = chat_asks.waiting(owner, datetime.now(UTC) + timedelta(hours=1))
    assert ask.from_name == "Someone Else" and slack.calls == ["users_info"]


def test_right_now_shows_the_ask_with_its_link() -> None:
    ref = {"kind": "link", "url": "https://acme.slack.com/archives/C1/p1", "app": "Slack"}
    ctx = render_header_context(
        period_label="2026-10-29", today_data={}, activity=[], now=NOW,
        live=LiveSignals(chat_asks=("Slack #launch, Ben Teammate at 10:00: @Olivia can you sign off?",),
                         chat_ask_refs=(ref,)),
    )
    assert "[M1] Chat message asking the reader, not answered yet: Slack #launch" in ctx.text
    assert ctx.sources == {"M1": ref}
    assert ctx.moved == frozenset({"M1"})
    assert needs_you_tags(ctx.text) == frozenset({"M1"})
    assert "https://" not in ctx.text  # the link never reaches the model


def test_live_signals_read_the_principals_asks(owner: int, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.briefing import live_signals

    _ask(owner, "1")
    signals = live_signals.gather_live_signals(NOW - timedelta(hours=12), now=NOW, include_private=True)
    assert len(signals.chat_asks) == 1 and "Slack #launch, Ben Teammate" in signals.chat_asks[0]
    assert signals.chat_ask_refs[0]["url"] == "https://acme.slack.com/archives/C1/p1"
    assert signals.keys["chat_asks"] == [1]
    shared = live_signals.gather_live_signals(NOW - timedelta(hours=12), now=NOW, include_private=False)
    assert shared.chat_asks == ()
