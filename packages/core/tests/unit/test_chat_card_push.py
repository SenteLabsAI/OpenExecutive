"""Every card that waits for someone, with buttons in their chat app: the
cards a chat app can now decide (a reply to send, an invite, an action Take
the lead or a department held) and the pass that sends the ones made outside
any chat (integrations/channel_cards.py, api/routes/decisions.resolve_in_chat)."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException

from openexecutive.audit import logger as audit_logger
from openexecutive.audit.logger import AuditLogger
from openexecutive.delegation import invites, reply_send
from openexecutive.delegation.verified import ChatTap
from openexecutive.integrations import channel_cards
from openexecutive.memory import decision_ledger, episodic
from openexecutive.memory.decision_ledger import create_decision_instance, get_decision_instance
from openexecutive.orchestrator import take_the_lead
from openexecutive.orchestrator.schedule_tools import current_session
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store
from openexecutive.people.store import get_person

NOW = datetime(2026, 10, 29, 15, 0, tzinfo=UTC)
OWNER = "olivia@acme.example"


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", path)
    monkeypatch.setattr(people_store, "DB_PATH", path)
    episodic.initialize_db(path)
    people_store.initialize_db(path)
    people_registry.invalidate()
    monkeypatch.setattr(audit_logger, "_default_logger", AuditLogger(db_path=path))
    for var in ("OE_LOCAL_LOGIN", "OE_PUBLIC_DEPLOYMENT", "BACKEND_SHARED_SECRET"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456789:AAH" + "x" * 32)
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "hook-secret")
    token = current_session.set(None)
    yield path
    current_session.reset(token)
    people_registry.invalidate()


@pytest.fixture
def owner() -> int:
    person_id = people_store.upsert_person(
        full_name="Olivia Owner", is_principal=True, email=OWNER, telegram_chat_id="4242",
    )
    people_registry.invalidate()
    return person_id


def _card(
    decision_class: str, payload: dict[str, Any], approver: int | None, *, session: str | None = None,
    made: datetime = NOW - timedelta(minutes=5), gate_mode: str = "propose",
) -> int:
    card_id = create_decision_instance(
        decision_class=decision_class, department="executive", originating_session_id=session,
        proposed_payload=payload, idempotency_key=None, gate_mode=gate_mode,
        approver_person_id=approver, confidence=None,
    )
    with decision_ledger._get_conn(episodic.DB_PATH) as conn:
        conn.execute("UPDATE decision_instances SET created_at = ? WHERE id = ?", (made.isoformat(), card_id))
    return card_id


def _reply(owner: int, **kw: Any) -> int:
    payload = {
        "private": True, "person_id": owner, "from_name": "Dana", "from_email": "dana@northpeak.example",
        "draft_to": ["dana@northpeak.example"], "draft_subject": "Re: Pilot dates",
        "draft_body": "Tuesday works for us.",
    }
    return _card("delegation_reply", payload, owner, **kw)


@pytest.fixture
def pushed(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int, str, tuple[str, str]]]:
    sent: list[tuple[str, int, str, tuple[str, str]]] = []

    async def fake(channel: str, person: Any, decision_id: int, text: str, labels: tuple[str, str]) -> None:
        sent.append((channel, decision_id, text, labels))

    monkeypatch.setattr(channel_cards, "_send", fake)
    return sent


def test_a_reply_card_reads_as_the_draft_with_send_and_skip(owner: int) -> None:
    card = _reply(owner)
    text = channel_cards.card_text(card)
    assert text is not None
    assert text.splitlines()[:4] == [
        "Reply ready to send", "To Dana", "Goes to dana@northpeak.example", "Re: Pilot dates",
    ]
    assert text.endswith("Tuesday works for us.")
    assert channel_cards.card_buttons(card) == ("Send", "Skip")


def test_an_invite_card_has_accept() -> None:
    owner_id = people_store.upsert_person(full_name="Olivia Owner", is_principal=True, email=OWNER)
    card = _card("delegation_invite", {
        "private": True, "person_id": owner_id, "title": "Pilot review", "from_name": "Dana",
        "start": "2026-10-30T10:00:00", "end": "2026-10-30T10:30:00",
    }, owner_id)
    text = channel_cards.card_text(card)
    assert text == "Meeting invite\nPilot review\nFri 30 Oct, 10:00-10:30\nFrom Dana"
    assert channel_cards.card_buttons(card) == ("Accept", "Skip")


def test_send_in_a_chat_app_sends_the_draft_as_its_person(owner: int, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    async def send(instance: Any, **kw: Any) -> str:
        calls.append(kw)
        return "m1"

    monkeypatch.setattr(reply_send, "send_approved_reply", send)
    card = _reply(owner)
    line = asyncio.run(channel_cards.resolve(card, get_person(owner), approve=True))
    assert line == "Sent."
    assert calls == [{"caller": ChatTap(OWNER), "resolver": owner, "confirm": {}}]


def test_a_send_that_needs_a_second_yes_says_where_to_give_it(owner: int, monkeypatch: pytest.MonkeyPatch) -> None:
    async def send(instance: Any, **kw: Any) -> str:
        raise reply_send.SendRefused(409, "confirm", "The draft's recipients changed since it was written.")

    monkeypatch.setattr(reply_send, "send_approved_reply", send)
    line = asyncio.run(channel_cards.resolve(_reply(owner), get_person(owner), approve=True))
    assert line == "Not done: The draft's recipients changed since it was written. Open it in the app to check and send it."


def test_a_reply_card_is_its_persons_alone(owner: int, monkeypatch: pytest.MonkeyPatch) -> None:
    teammate = people_store.upsert_person(full_name="Ben Teammate", email="ben@acme.example")
    people_registry.invalidate()

    async def send(instance: Any, **kw: Any) -> str:
        raise AssertionError("must not send")

    monkeypatch.setattr(reply_send, "send_approved_reply", send)
    line = asyncio.run(channel_cards.resolve(_reply(owner), get_person(teammate), approve=True))
    assert line == "Not done: This card is no longer there."


def test_accept_answers_the_invite(owner: int, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    async def answer(instance: Any, **kw: Any) -> str:
        calls.append(kw)
        return "accepted"

    monkeypatch.setattr(invites, "answer", answer)
    card = _card("delegation_invite", {"private": True, "person_id": owner, "title": "Pilot review"}, owner)
    assert asyncio.run(channel_cards.resolve(card, get_person(owner), approve=True)) == "Accepted."
    assert calls == [{"caller": ChatTap(OWNER), "resolver": owner, "answer": "accept"}]


def test_a_take_the_lead_card_is_done_as_it_stands(owner: int, monkeypatch: pytest.MonkeyPatch) -> None:
    done: list[tuple[dict[str, Any], bool]] = []

    async def carry_out(payload: dict[str, Any], *, by_principal: bool) -> str:
        done.append((payload, by_principal))
        return json.dumps({"status": "sent"})

    monkeypatch.setattr(take_the_lead, "carry_out", carry_out)
    payload = {"tool": "send_slack_dm", "input": {"text": "hi"}, "summary": "Message Ben: hi", "reason": "it's new"}
    card = _card("take_the_lead_action", payload, owner)
    assert channel_cards.card_text(card) == "Waiting for your OK\nMessage Ben: hi\nIt waited because it's new.\n\ntext: hi"
    assert asyncio.run(channel_cards.resolve(card, get_person(owner), approve=True)) == "Approved."
    assert done == [(payload, True)]
    assert get_decision_instance(card).status == decision_ledger.STATUS_APPROVED_UNCHANGED


def test_a_skipped_card_does_nothing(owner: int) -> None:
    card = _card("take_the_lead_action", {"tool": "x", "summary": "Do x", "input": {}}, owner)
    assert asyncio.run(channel_cards.resolve(card, get_person(owner), approve=False)) == "Skipped."
    assert get_decision_instance(card).status == decision_ledger.STATUS_REJECTED


def test_a_card_made_outside_any_chat_goes_to_their_chat_app_once(owner: int, pushed: list[Any]) -> None:
    card = _reply(owner)
    assert asyncio.run(channel_cards.push_waiting(NOW)) == 1
    assert pushed == [("telegram", card, channel_cards.card_text(card), ("Send", "Skip"))]
    assert asyncio.run(channel_cards.push_waiting(NOW + timedelta(minutes=1))) == 0
    assert len(pushed) == 1


def test_what_is_not_sent(owner: int, pushed: list[Any]) -> None:
    _reply(owner, made=NOW - timedelta(seconds=30))  # just made: may be handled where it was made
    _reply(owner, made=NOW - timedelta(hours=30))  # too old to bother them with
    _reply(owner, session="web-1")  # shown in the conversation it was made in
    _reply(owner, gate_mode="auto_execute")  # Handle it for me sends it
    _card("delegation_actions", {"private": True, "person_id": owner, "actions": []}, owner)  # made by a background run: sent
    sent = asyncio.run(channel_cards.push_waiting(NOW))
    assert sent == 1 and [p[1] for p in pushed] == [5]


def test_a_department_card_reaches_its_approver_though_made_on_the_askers_turn(
    owner: int, pushed: list[Any],
) -> None:
    card = _card("department_action", {
        "tool": "send_email", "summary": "Email the vendor", "requested_by": "Ben Teammate", "reason": "it's money",
        "input": {"to": "vendor@example.com", "body": "Please send the invoice."},
    }, owner, session="ben-chat")
    assert asyncio.run(channel_cards.push_waiting(NOW)) == 1
    assert pushed[0][1] == card
    assert pushed[0][2].startswith("Waiting for your approval\nBen Teammate asked for this.\nEmail the vendor")


def test_nothing_goes_without_a_chat_app_that_proves_taps(owner: int, pushed: list[Any], monkeypatch: pytest.MonkeyPatch) -> None:
    _reply(owner)
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "")
    assert asyncio.run(channel_cards.push_waiting(NOW)) == 0
    # It goes once the chat app can take the tap.
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "hook-secret")
    assert asyncio.run(channel_cards.push_waiting(NOW)) == 1


def test_quiet_hours_hold_it_until_they_end(owner: int, pushed: list[Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.people import channel

    quiet = {"on": True}
    monkeypatch.setattr(channel, "is_within_availability", lambda person, **kw: not quiet["on"])
    _reply(owner)
    assert asyncio.run(channel_cards.push_waiting(NOW)) == 0
    quiet["on"] = False
    assert asyncio.run(channel_cards.push_waiting(NOW)) == 1


def test_a_failed_send_is_tried_again_a_few_times(owner: int, monkeypatch: pytest.MonkeyPatch) -> None:
    tries: list[int] = []

    async def down(channel: str, person: Any, decision_id: int, text: str, labels: tuple[str, str]) -> None:
        tries.append(decision_id)
        if len(tries) < 2:
            raise RuntimeError("rate limited")

    monkeypatch.setattr(channel_cards, "_send", down)
    _reply(owner)
    assert asyncio.run(channel_cards.push_waiting(NOW)) == 0
    assert asyncio.run(channel_cards.push_waiting(NOW)) == 1
    assert asyncio.run(channel_cards.push_waiting(NOW)) == 0
    assert len(tries) == 2


def test_a_send_that_keeps_failing_stops_after_its_tries(owner: int, monkeypatch: pytest.MonkeyPatch) -> None:
    tries: list[int] = []

    async def down(channel: str, person: Any, decision_id: int, text: str, labels: tuple[str, str]) -> None:
        tries.append(decision_id)
        raise RuntimeError("down")

    monkeypatch.setattr(channel_cards, "_send", down)
    _reply(owner)
    for _ in range(channel_cards.SEND_TRIES + 2):
        assert asyncio.run(channel_cards.push_waiting(NOW)) == 0
    assert len(tries) == channel_cards.SEND_TRIES


def test_a_send_cut_off_midway_is_tried_again_later(owner: int, pushed: list[Any]) -> None:
    card = _reply(owner)
    # A restart between the claim and the send leaves it marked in progress.
    assert channel_cards._mark_pushed(card, owner, "telegram", NOW, None)
    assert asyncio.run(channel_cards.push_waiting(NOW + timedelta(minutes=1))) == 0
    assert asyncio.run(channel_cards.push_waiting(NOW + channel_cards.SEND_STALE + timedelta(minutes=1))) == 1
    assert [d for _, d, _, _ in pushed] == [card]


def test_the_reason_a_card_waited_is_shown_as_written(owner: int) -> None:
    card = _card(
        "take_the_lead_action",
        {"summary": "Share the file", "tool": "share", "reason": "the path is ../shared.", "input": {"path": "x"}},
        owner,
    )
    text = channel_cards.card_text(card)
    assert text is not None and "It waited because the path is ../shared." in text


def test_the_chat_app_is_the_first_one_that_proves_taps(owner: int, monkeypatch: pytest.MonkeyPatch) -> None:
    person = get_person(owner)
    assert channel_cards.chat_for(person) == "telegram"
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "")
    assert channel_cards.chat_for(person) is None


def test_a_slack_dm_card_has_the_cards_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.integrations import slack_bot

    blocks = slack_bot._card_blocks("Reply ready to send", 7, ("Send", "Skip"))
    assert [e["text"]["text"] for e in blocks[1]["elements"]] == ["Send", "Skip"]


def test_resolve_refuses_a_card_class_a_chat_cant_show(owner: int) -> None:
    from openexecutive.api.routes.decisions import resolve_in_chat

    card = _card("something_else", {}, owner)
    with pytest.raises(HTTPException):
        asyncio.run(resolve_in_chat(card, get_person(owner), approve=True))


def test_a_card_too_long_to_show_whole_is_neither_sent_nor_approved_from_chat(
    owner: int, pushed: list[Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def send(instance: Any, **kw: Any) -> str:
        raise AssertionError("must not send what the card never showed")

    monkeypatch.setattr(reply_send, "send_approved_reply", send)
    payload = {
        "private": True, "person_id": owner, "from_name": "Dana", "draft_to": ["dana@northpeak.example"],
        "draft_subject": "Re: Pilot dates", "draft_body": "Fine. " + "x" * channel_cards.CARD_MAX,
    }
    card = _card("delegation_reply", payload, owner)
    assert channel_cards.card_text(card) is None
    assert asyncio.run(channel_cards.push_waiting(NOW)) == 0
    line = asyncio.run(channel_cards.resolve(card, get_person(owner), approve=True))
    assert line == "Not done: This one is too long to approve here. Open it in the app."
    # Skip still works.
    assert asyncio.run(channel_cards.resolve(card, get_person(owner), approve=False)) == "Skipped."


def test_a_held_action_card_shows_every_field_it_would_send(owner: int) -> None:
    card = _card("take_the_lead_action", {
        "tool": "send_email", "summary": "Email Dana: Quick note...",
        "input": {"to": "dana@northpeak.example", "bcc": ["x@evil.example"], "body": "Quick note. And more."},
    }, owner)
    text = channel_cards.card_text(card)
    assert text is not None
    assert 'bcc: ["x@evil.example"]' in text and "body: Quick note. And more." in text
    # No call stored: not shown.
    assert channel_cards.card_text(_card("take_the_lead_action", {"tool": "x", "summary": "Do x"}, owner)) is None


def test_a_card_that_cant_be_read_is_not_left_busy(owner: int, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.memory import decision_ledger as ledger

    card = _reply(owner)

    def locked(decision_id: int, **kw: Any) -> Any:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(ledger, "get_decision_instance", locked)
    assert asyncio.run(channel_cards.resolve(card, get_person(owner), approve=False)).startswith("Not done")
    assert card not in channel_cards._resolving
