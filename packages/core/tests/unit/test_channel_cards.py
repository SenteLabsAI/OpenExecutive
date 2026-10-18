# ruff: noqa: F811 — pytest fixtures imported from test_action_cards share their names with the test parameters.
"""Approval cards in Telegram, with Approve and Skip buttons
(integrations/channel_cards.py, integrations/telegram_bot.py,
api/routes/decisions.resolve_in_chat)."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from openexecutive.api.routes import decisions as decisions_route
from openexecutive.delegation.settings import set_enabled
from openexecutive.delegation.verified import NOT_YOURS, ChatTap, caller_refusal
from openexecutive.integrations import channel_cards, telegram_bot
from openexecutive.integrations.channel_context import build_channel_context_block
from openexecutive.memory.decision_ledger import STATUS_APPROVED_UNCHANGED, get_decision_instance
from openexecutive.orchestrator.executive import Executive
from openexecutive.orchestrator.session import Session
from openexecutive.people.store import get_person

# Fakes and autouse fixtures (a temp DB, a fresh turn pin, a roster, sends).
from .test_action_cards import _card, _message, _teammate_session, roster, sends  # noqa: F401
from .test_delegation_tools import (  # noqa: F401
    OWNER,
    TEAM,
    FakeMailbox,
    _session,
    db,
    fresh_turn_state,
)

TOKEN = "123456789:AAH" + "x" * 32


@pytest.fixture
def telegram(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("OE_LOCAL_LOGIN", "OE_PUBLIC_DEPLOYMENT", "BACKEND_SHARED_SECRET"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "hook-secret")


def _tg_session(chat: str = "4242", person_id: int | None = 1) -> Session:
    session = Session(origin_channel="telegram", origin_channel_ref=chat)
    session.caller_person_id = person_id
    return session


def test_buttons_round_trip_and_junk_is_ignored() -> None:
    assert channel_cards.parse_callback(channel_cards.callback_data(7, channel_cards.APPROVE)) == (7, True)
    assert channel_cards.parse_callback(channel_cards.callback_data(7, channel_cards.SKIP)) == (7, False)
    for junk in (None, "", "card:7", "card:x:a", "card:7:z", "other:7:a", "card:-7:a", "card:" + "9" * 20 + ":a"):
        assert channel_cards.parse_callback(junk) is None


def test_cards_show_only_in_a_private_telegram_chat_only_telegram_can_reach(
    telegram: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert channel_cards.shows_cards(_tg_session())
    assert not channel_cards.shows_cards(_tg_session(chat="-100123"))  # a group
    assert not channel_cards.shows_cards(_tg_session(person_id=None))
    assert not channel_cards.shows_cards(Session(origin_channel="slack", origin_channel_ref="U1"))
    assert not channel_cards.shows_cards(None)
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "")
    assert not channel_cards.shows_cards(_tg_session())
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "# not one Telegram can send")
    assert not channel_cards.shows_cards(_tg_session())


def test_a_chat_tap_is_only_the_person_it_names() -> None:
    assert caller_refusal(ChatTap("olivia@acme.example"), "olivia@acme.example") is None
    assert caller_refusal(ChatTap("ben@acme.example"), "olivia@acme.example") == NOT_YOURS
    assert caller_refusal(ChatTap(""), "") == NOT_YOURS


def test_only_the_chats_own_person_can_tap() -> None:
    def tap(chat: dict[str, Any], sender: int) -> dict[str, Any]:
        return {"id": "q1", "from": {"id": sender}, "data": "card:3:a",
                "message": {"message_id": 9, "chat": chat, "text": "Meeting to book"}}

    found = telegram_bot._card_tap(tap({"id": 4242, "type": "private"}, 4242))
    assert found == {"callback_id": "q1", "chat_id": 4242, "message_id": 9,
                     "card_text": "Meeting to book", "data": "card:3:a"}
    assert telegram_bot._card_tap(tap({"id": 4242, "type": "private"}, 999)) is None
    assert telegram_bot._card_tap(tap({"id": -100, "type": "group"}, -100)) is None
    assert telegram_bot._card_tap(tap({"id": 4242, "type": "supergroup"}, 4242)) is None
    assert telegram_bot._card_tap("nope") is None


def test_a_tap_is_taken_only_on_a_webhook_only_telegram_can_call(
    telegram: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.api.main import create_app

    taken: list[dict[str, Any]] = []

    async def handle(**kwargs: Any) -> None:
        taken.append(kwargs)

    monkeypatch.setattr(telegram_bot, "_handle_card_tap", handle)
    update = {"callback_query": {"id": "q1", "from": {"id": 4242}, "data": "card:3:a",
                                 "message": {"message_id": 9, "chat": {"id": 4242, "type": "private"}}}}
    client = TestClient(create_app())
    headers = {"X-Telegram-Bot-Api-Secret-Token": "hook-secret"}
    assert client.post("/webhook/telegram", json=update, headers=headers).status_code == 200
    assert [t["chat_id"] for t in taken] == [4242]
    # Someone else's tap in that chat is ignored.
    forged = json.loads(json.dumps(update))
    forged["callback_query"]["from"]["id"] = 7
    assert client.post("/webhook/telegram", json=forged, headers=headers).status_code == 200
    assert len(taken) == 1
    # With no secret, anyone could post a tap: none is taken.
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "")
    assert TestClient(create_app()).post("/webhook/telegram", json=update).status_code == 200
    assert len(taken) == 1


def test_a_card_is_approved_by_its_person_and_skipped_cards_do_nothing(
    roster: SimpleNamespace, sends: list[Any],
) -> None:
    set_enabled(roster.principal, True, updated_by="t")
    card = _card(_session(FakeMailbox()), [_message(roster.teammate)])
    text = channel_cards.card_text(card.id)
    assert text is not None and text.startswith("Waiting for your approval") and "Message Ben Teammate" in text
    # Someone else on the team: not theirs, so it isn't there.
    assert asyncio.run(channel_cards.resolve(card.id, get_person(roster.teammate), approve=True)) == (
        "Not done: This card is no longer there."
    )
    assert sends == []
    assert asyncio.run(channel_cards.resolve(card.id, get_person(roster.principal), approve=True)) == "Approved."
    assert [i.get("text") for _, i, _ in sends] == ["Can you check the pilot dates?"]
    assert get_decision_instance(card.id).status == STATUS_APPROVED_UNCHANGED
    assert channel_cards.card_text(card.id) is None
    assert asyncio.run(channel_cards.resolve(card.id, get_person(roster.principal), approve=True)).startswith("Not done")
    assert len(sends) == 1

    skipped = _card(_session(FakeMailbox()), [_message(roster.teammate, "another")])
    assert asyncio.run(channel_cards.resolve(skipped.id, get_person(roster.principal), approve=False)) == "Skipped."
    assert get_decision_instance(skipped.id).status == "rejected"
    assert len(sends) == 1


def test_a_team_members_card_is_theirs_alone(
    roster: SimpleNamespace, sends: list[Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("openexecutive.delegation.settings.team_members_enabled", lambda **_: True)
    set_enabled(roster.teammate, True, updated_by="t")
    card = _card(_teammate_session(), [_message(roster.principal)])
    with pytest.raises(HTTPException) as refused:
        asyncio.run(decisions_route.resolve_in_chat(card.id, get_person(roster.principal), approve=False))
    assert refused.value.status_code == 404
    assert get_decision_instance(card.id).status == "proposed"
    asyncio.run(decisions_route.resolve_in_chat(card.id, get_person(roster.teammate), approve=True))
    assert [i.get("text") for _, i, _ in sends] == ["Can you check the pilot dates?"]


def test_the_executive_is_told_where_the_card_is(roster: SimpleNamespace, telegram: None) -> None:
    from .test_action_cards import _propose

    set_enabled(roster.principal, True, updated_by="t")
    web = _session(FakeMailbox())
    assert "in this chat" in _propose(web, {"why": "x", "actions": [_message(roster.teammate)]})["note"]
    slack = _session(FakeMailbox())
    slack.origin_channel = "slack"
    note = _propose(slack, {"why": "x", "actions": [_message(roster.teammate, "two")]})["note"]
    assert "can't show it" in note and "in this chat and" not in note


def test_the_channel_block_says_cards_have_buttons_only_where_they_do() -> None:
    plain = build_channel_context_block("telegram", ui_base_url="")
    assert "can ONLY be approved" in plain and "Approve and Skip" not in plain
    buttons = build_channel_context_block("telegram", ui_base_url="", cards_here=True)
    assert "Approve and Skip buttons" in buttons and "can ONLY be approved" not in buttons


def test_chat_collects_the_cards_a_reply_left() -> None:
    async def stream(**_: Any) -> Any:
        yield "Left you a card."
        yield {"type": "action_taken", "tool": "propose_actions", "decision_id": 5}
        yield {"type": "action_taken", "tool": "propose_actions", "decision_id": 5}
        yield {"type": "action_taken", "tool": "message_person", "decision_id": True}
        yield {"type": "activity", "decision_id": 6}

    executive = Executive.__new__(Executive)
    executive.stream_chat = stream  # type: ignore[method-assign]
    cards: list[int] = []
    reply = asyncio.run(executive.chat(user_message="hi", session=Session(), cards=cards))
    assert reply == "Left you a card." and cards == [5]


def test_a_meeting_is_booked_by_the_owner_or_its_approver(
    roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.memory.decision_ledger import create_decision_instance

    booked: list[bool] = []

    async def book(instance: Any, payload: dict[str, Any], *, by_principal: bool) -> dict[str, Any]:
        booked.append(by_principal)
        return {"event_id": f"e{len(booked)}"}

    monkeypatch.setattr(decisions_route, "_execute_booking", book)

    def seed(approver: int | None, key: str, private: bool = False) -> int:
        return create_decision_instance(
            decision_class="meeting_scheduling", department="operations", originating_session_id=None,
            proposed_payload={"title": "Pilot review", "start": "2026-06-15T10:00:00", "end": "2026-06-15T11:00:00",
                              "attendee_emails": ["dana@northpeak.example"], "description": "",
                              **({"private": True} if private else {})},
            idempotency_key=key, gate_mode="propose", approver_person_id=approver, confidence=0.8,
        )

    owners = seed(roster.principal, "k1")
    assert channel_cards.card_text(owners) == (
        "Meeting to book\nPilot review\nMon 15 Jun, 10:00-11:00\nWith dana@northpeak.example"
    )
    assert asyncio.run(channel_cards.resolve(owners, get_person(roster.teammate), approve=True)).startswith("Not done")
    assert asyncio.run(channel_cards.resolve(owners, get_person(roster.principal), approve=True)) == "Approved."
    assert booked == [True] and get_decision_instance(owners).external_event_id == "e1"

    theirs = seed(roster.teammate, "k2")
    assert asyncio.run(channel_cards.resolve(theirs, get_person(roster.teammate), approve=True)) == "Approved."
    assert booked == [True, False]
    # The owner's private booking is theirs alone, even when it went to someone.
    private = seed(roster.teammate, "k3", private=True)
    assert asyncio.run(channel_cards.resolve(private, get_person(roster.teammate), approve=False)).startswith("Not done")
    assert get_decision_instance(private).status == "proposed"


class _FakeTelegram:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []

    async def post(self, url: str, json: dict[str, Any]) -> Any:  # noqa: A002 - httpx's keyword
        self.posts.append((url.rsplit("/", 1)[-1], json))
        return SimpleNamespace(is_error=False, status_code=200, text="")


@pytest.fixture
def fake_telegram(monkeypatch: pytest.MonkeyPatch) -> _FakeTelegram:
    fake = _FakeTelegram()
    monkeypatch.setattr(telegram_bot, "_get_http_client", lambda: fake)
    return fake


def test_a_card_goes_out_with_approve_and_skip(fake_telegram: _FakeTelegram) -> None:
    asyncio.run(telegram_bot.send_card(TOKEN, 4242, 7, "Meeting to book\nPilot review"))
    [(method, body)] = fake_telegram.posts
    assert method == "sendMessage" and body["chat_id"] == 4242
    assert body["text"] == "Meeting to book\nPilot review" and "parse_mode" not in body
    assert body["reply_markup"] == {"inline_keyboard": [[
        {"text": "Approve", "callback_data": "card:7:a"},
        {"text": "Skip", "callback_data": "card:7:s"},
    ]]}


def test_a_tap_answers_audits_and_leaves_the_card_without_buttons(
    fake_telegram: _FakeTelegram, monkeypatch: pytest.MonkeyPatch,
) -> None:
    person = SimpleNamespace(id=3)
    monkeypatch.setattr("openexecutive.people.store.find_person_by_telegram_chat_id",
                        lambda chat: person if chat == "4242" else None)
    resolved: list[tuple[int, Any, bool]] = []

    async def resolve(decision_id: int, who: Any, *, approve: bool) -> str:
        resolved.append((decision_id, who, approve))
        return "Approved."

    monkeypatch.setattr(channel_cards, "resolve", resolve)
    audited: list[dict[str, Any]] = []
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **k: audited.append(k["details"]))

    def tap(chat_id: int, data: str) -> None:
        asyncio.run(telegram_bot._handle_card_tap(
            callback_id="q1", chat_id=chat_id, message_id=9, card_text="Meeting to book", data=data, token=TOKEN,
        ))

    tap(4242, "card:7:a")
    assert resolved == [(7, person, True)]
    assert audited == [{"channel": "telegram", "chat_id": 4242, "decision_id": 7, "choice": "approve", "outcome": "done", "person_id": 3}]
    assert [m for m, _ in fake_telegram.posts] == ["answerCallbackQuery", "editMessageText"]
    edited = fake_telegram.posts[1][1]
    assert edited["text"] == "Meeting to book\n\nApproved." and "reply_markup" not in edited

    # Off the roster, or data that isn't a card's: answered, never resolved.
    fake_telegram.posts.clear()
    tap(999, "card:7:a")
    tap(4242, "something:else")
    assert len(resolved) == 1 and len(audited) == 1
    assert [(m, b["text"]) for m, b in fake_telegram.posts] == [
        ("answerCallbackQuery", "This card is no longer there."),
        ("answerCallbackQuery", "This card is no longer there."),
    ]


def test_a_second_tap_while_the_first_is_booking_is_turned_away(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    async def slow(decision_id: int, person: Any, *, approve: bool) -> None:
        calls.append(decision_id)
        await asyncio.sleep(0.05)

    monkeypatch.setattr(decisions_route, "resolve_in_chat", slow)

    async def both() -> list[str]:
        return list(await asyncio.gather(
            channel_cards.resolve(7, SimpleNamespace(id=1), approve=True),
            channel_cards.resolve(7, SimpleNamespace(id=1), approve=True),
        ))

    assert sorted(asyncio.run(both())) == ["Already on it.", "Approved."]
    assert calls == [7]
    assert asyncio.run(channel_cards.resolve(7, SimpleNamespace(id=1), approve=False)) == "Skipped."


def test_a_busy_tap_only_answers(fake_telegram: _FakeTelegram, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openexecutive.people.store.find_person_by_telegram_chat_id", lambda chat: SimpleNamespace(id=3))

    async def busy(decision_id: int, who: Any, *, approve: bool) -> str:
        return channel_cards.BUSY

    monkeypatch.setattr(channel_cards, "resolve", busy)
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **k: None)
    asyncio.run(telegram_bot._handle_card_tap(
        callback_id="q1", chat_id=4242, message_id=9, card_text="Meeting to book", data="card:7:a", token=TOKEN,
    ))
    assert [(m, b["text"]) for m, b in fake_telegram.posts] == [("answerCallbackQuery", "Already on it.")]


def test_a_taps_outcome_is_recorded_apart_from_its_choice() -> None:
    assert channel_cards.outcome_of("Approved.") == "done"
    assert channel_cards.outcome_of("Skipped.") == "done"
    assert channel_cards.outcome_of("Not done: This card was already handled.") == "refused"
    assert channel_cards.outcome_of(channel_cards.BUSY) == "busy"
