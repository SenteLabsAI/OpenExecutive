"""Replies kept on their card alone, and changing a reply's words on its card
(delegation/card_drafts.py)."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from openexecutive.api.caller import Caller
from openexecutive.delegation import card_drafts, drafts, inbox, replies
from openexecutive.delegation.reply_send import SendRefused, send_approved_reply
from openexecutive.memory import decision_ledger as ledger

from . import test_delegation_inbox as _inbox_tests
from .test_delegation_inbox import DANA, NOW, OWNER, FakeInbox, _msg, _scan

db = _inbox_tests.db
models = _inbox_tests.models

LOCAL = Caller("open", "")


@pytest.fixture(autouse=True)
def local_login(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OE_LOCAL_LOGIN", "1")
    monkeypatch.delenv("OE_PUBLIC_DEPLOYMENT", raising=False)
    monkeypatch.delenv("CALLER_ASSERTION_PUBLIC_KEYS", raising=False)


@pytest.fixture
def owner() -> Any:
    """The owner with both switches on and the default: replies on their card alone."""
    from openexecutive.delegation.settings import set_enabled
    from openexecutive.people import registry as people_registry
    from openexecutive.people import store as people_store

    pid = people_store.upsert_person(full_name="Olivia Owner", is_principal=True, email=OWNER)
    people_store.upsert_person(full_name="Dana Park", email=DANA, kind="contact")
    people_registry.invalidate()
    set_enabled(pid, True, updated_by="test")
    inbox.set_watch(pid, True, updated_by="test", now=NOW - timedelta(days=1))
    return people_store.get_person(pid)


def _card(owner: Any) -> tuple[FakeInbox, Any]:
    mailbox = FakeInbox()
    mailbox.add(_msg("m1", "t1"))
    _scan(owner, mailbox)
    return mailbox, inbox.open_cards(owner.id)[0]


def _fresh(card: Any) -> Any:
    row = ledger.get_decision_instance(card.id)
    assert row is not None
    return row


def _send(card: Any, mailbox: FakeInbox, owner: Any, **confirm: Any) -> str:
    return asyncio.run(send_approved_reply(
        _fresh(card), caller=LOCAL, resolver=owner.id, confirm=confirm or None, gmail=mailbox,
        now=NOW + timedelta(minutes=30),
    ))


def _edit(card: Any, mailbox: FakeInbox, owner: Any, text: str, *, resolver: int | None = -1,
          caller: Caller = LOCAL) -> dict[str, Any]:
    return asyncio.run(card_drafts.edit(
        _fresh(card), caller=caller, resolver=owner.id if resolver == -1 else resolver, text=text, gmail=mailbox,
    ))


def test_off_by_default_the_reply_waits_on_its_card_alone(db: Path, owner: Any, models: dict[str, Any]) -> None:
    assert inbox.get_watch(owner.id).mailbox_drafts is False
    mailbox, card = _card(owner)
    # Nothing saved in their mailbox.
    assert mailbox.specs == [] and mailbox.drafts == {}
    payload = inbox.card_payload(card)
    assert payload["draft_id"] == "" and payload["card_only"] is True
    assert payload["draft_spec"]["to"] == [DANA] and payload["draft_spec"]["in_reply_to"] == "<m1@x.example>"
    # Still counted against the day's limit.
    assert drafts.count_since(owner.id, NOW - timedelta(hours=1)) == 1
    shown = replies.cards(owner)[0]
    assert shown.in_mailbox is False and shown.gmail_link == ""
    # The reconciler has no draft to miss: the card stays.
    assert asyncio.run(inbox.reconcile(owner, mailbox, now=NOW + timedelta(minutes=5), own={OWNER})) == 0
    assert _fresh(card).status == "proposed"


def test_send_makes_the_draft_from_the_card_and_sends_it(db: Path, owner: Any, models: dict[str, Any]) -> None:
    mailbox, card = _card(owner)
    assert _send(card, mailbox, owner) == "sent-d1"
    spec = mailbox.specs[0]
    assert spec.to == [DANA] and spec.thread_id == "t1" and spec.in_reply_to == "<m1@x.example>"
    assert "get back to you shortly" in spec.body
    assert mailbox.sent == ["d1"] and _fresh(card).status == "approved_unchanged"
    # The day's row became that draft, and knows what it was sent as.
    import sqlite3

    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT draft_id, sent_message_id FROM delegation_drafts").fetchall()
    assert rows == [("d1", "sent-d1")]


def test_an_edit_on_the_card_is_what_goes(db: Path, owner: Any, models: dict[str, Any]) -> None:
    mailbox, card = _card(owner)
    before = list(mailbox.calls)
    changed = _edit(card, mailbox, owner, "Hi Dana,\n\nFriday at 10 works. I'll send a new invite.\n\nOlivia\n\n")
    assert changed["draft_body"].endswith("Olivia") and changed["edited_on_card"] is True
    assert replies.cards(owner)[0].draft_body.startswith("Hi Dana,\n\nFriday at 10 works.")
    assert mailbox.calls == before  # on the card alone: no mailbox call
    assert _send(card, mailbox, owner) == "sent-d1"
    assert "Friday at 10 works" in mailbox.specs[0].body
    assert _fresh(card).status == "approved_with_edit"


def test_a_refused_send_takes_the_draft_back(db: Path, owner: Any, models: dict[str, Any]) -> None:
    mailbox, card = _card(owner)
    mailbox.add(_msg("m2", "t1", minutes_ago=10, text="Actually, Monday is better."))
    with pytest.raises(SendRefused) as err:
        _send(card, mailbox, owner)
    assert err.value.code == "confirm"
    # The draft it made for that tap is gone again; the card still holds the reply.
    assert mailbox.deleted == ["d1"] and mailbox.drafts == {}
    assert card_drafts.is_card_only(inbox.card_payload(_fresh(card)))
    # The second yes makes it again and sends it.
    assert _send(card, mailbox, owner, thread_moved_on=True) == "sent-d2"
    assert _fresh(card).status == "approved_unchanged"


def test_dismiss_has_nothing_to_delete(db: Path, owner: Any, models: dict[str, Any]) -> None:
    mailbox, card = _card(owner)
    assert asyncio.run(replies.dismiss(_fresh(card), gmail=mailbox)) == "card_only"
    assert mailbox.deleted == []


def test_their_own_reply_closes_a_card_only_reply(db: Path, owner: Any, models: dict[str, Any]) -> None:
    mailbox, card = _card(owner)
    mailbox.add(_msg("s1", "t1", sender=OWNER, to=[DANA], labels=("SENT",), minutes_ago=5))
    assert asyncio.run(inbox.reconcile(owner, mailbox, now=NOW + timedelta(minutes=5), own={OWNER})) == 1
    assert _fresh(card).status == "closed_externally"


def test_only_its_person_and_only_while_it_waits(db: Path, owner: Any, models: dict[str, Any]) -> None:
    mailbox, card = _card(owner)
    with pytest.raises(SendRefused) as err:
        _edit(card, mailbox, owner, "Hi", resolver=owner.id + 1)
    assert err.value.code == "not_yours"
    with pytest.raises(SendRefused) as err:
        _edit(card, mailbox, owner, "Hi", caller=Caller("user", "ben@co.example"))
    assert err.value.code == "not_yours"
    with pytest.raises(SendRefused) as err:
        _edit(card, mailbox, owner, "   \n")
    assert err.value.code == "empty"
    _send(card, mailbox, owner)
    with pytest.raises(SendRefused) as err:
        _edit(card, mailbox, owner, "Too late")
    assert err.value.code == "already_handled"


def test_in_the_mailbox_an_edit_rewrites_the_draft_there(db: Path, owner: Any, models: dict[str, Any]) -> None:
    inbox.set_mailbox_drafts(owner.id, True, updated_by="test")
    mailbox, card = _card(owner)
    assert list(mailbox.drafts) == ["d1"] and replies.cards(owner)[0].in_mailbox is True
    _edit(card, mailbox, owner, "Friday works.")
    assert mailbox.drafts["d1"].message.text == "Friday works."
    # A second edit starts from the version the first one left.
    _edit(card, mailbox, owner, "Friday at 10 works.")
    assert mailbox.drafts["d1"].message.text == "Friday at 10 works."
    # Changed in the mailbox since: never overwritten from the card.
    mailbox.edit("d1")
    with pytest.raises(SendRefused) as err:
        _edit(card, mailbox, owner, "Monday works.")
    assert err.value.code == "draft_changed"
    assert mailbox.drafts["d1"].message.text == "Friday at 10 works."


def test_an_edited_mailbox_draft_dismissed_on_its_card_is_deleted(
    db: Path, owner: Any, models: dict[str, Any]
) -> None:
    inbox.set_mailbox_drafts(owner.id, True, updated_by="test")
    mailbox, card = _card(owner)
    _edit(card, mailbox, owner, "Friday works.")
    assert asyncio.run(replies.dismiss(_fresh(card), gmail=mailbox)) == "deleted"


def test_the_setting_is_kept_while_the_switch_is_off(db: Path, owner: Any) -> None:
    inbox.set_mailbox_drafts(owner.id, True, updated_by="test")
    inbox.set_watch(owner.id, False, updated_by="test")
    inbox.set_watch(owner.id, True, updated_by="test")
    assert inbox.get_watch(owner.id).mailbox_drafts is True


def test_one_change_at_a_time(db: Path, owner: Any, models: dict[str, Any]) -> None:
    """A second save while the first is still being written is refused before
    it touches the mailbox, so the card and the draft never disagree."""
    inbox.set_mailbox_drafts(owner.id, True, updated_by="test")
    mailbox, card = _card(owner)
    card_drafts._EDITING.add(card.id)
    try:
        with pytest.raises(SendRefused) as err:
            _edit(card, mailbox, owner, "Friday works.")
        assert err.value.code == "busy" and "update:d1" not in mailbox.calls
    finally:
        card_drafts._EDITING.discard(card.id)
    _edit(card, mailbox, owner, "Friday works.")
    assert card.id not in card_drafts._EDITING
