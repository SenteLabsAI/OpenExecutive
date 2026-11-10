"""Follow-ups as you (delegation/follow_ups.py): the inbox watcher chases the
person's own unanswered email under Handle it for me, sending it on its own
only where Approvals (Careful / Balanced / Hands-off) lets it."""
from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest

from openexecutive.delegation import follow_ups, handle_it, inbox
from openexecutive.memory import decision_ledger as ledger

from . import test_delegation_inbox as _inbox_tests
from .test_delegation_inbox import DANA, NOW, OWNER, FakeInbox, _msg, _scan

db = _inbox_tests.db
owner = _inbox_tests.owner
models = _inbox_tests.models

DAY = 24 * 60
LEE = "lee@elsewhere.example"


@pytest.fixture(autouse=True)
def local_login(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("OE_LOCAL_LOGIN", "1")
    monkeypatch.delenv("OE_PUBLIC_DEPLOYMENT", raising=False)
    monkeypatch.delenv("CALLER_ASSERTION_PUBLIC_KEYS", raising=False)
    follow_ups._LAST_LOOK.clear()
    yield
    follow_ups._LAST_LOOK.clear()


@pytest.fixture(autouse=True)
def settled(models: dict[str, Any]) -> None:
    """The follow-up leaves nothing to answer: one that does waits for them."""
    models["questions"] = []


def test_a_follow_up_that_leaves_them_something_to_answer_waits(owner: Any, models: dict[str, Any]) -> None:
    models["questions"] = ["Which deck did they mean?"]
    _on(owner, "hands_off")
    mailbox = FakeInbox()
    mailbox.add(_asked())
    _scan(owner, mailbox)
    assert mailbox.sent == []
    (card,) = ledger.list_instances("delegation_reply", limit=50)
    assert card.status == "proposed"
    assert inbox.card_payload(card)["handle_it_reason"] == "questions"


# The Approvals level the test picked with _on, by its outside_mode token.
_LEVEL = {"careful": "ask", "balanced": "outsiders", "hands_off": "trust"}
_PICKED: list[str] = ["ask"]


@pytest.fixture(autouse=True)
def approvals(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from openexecutive.orchestrator import outside_mode

    _PICKED[0] = "ask"
    monkeypatch.setattr(outside_mode, "get_mode", lambda **_: _PICKED[0])
    yield


def _asked(mid: str = "s1", thread: str = "t9", *, to: str = DANA, days: float = 4,
           text: str = "Hi Dana, could you send me the slides from Tuesday?") -> Any:
    return _msg(mid, thread, sender=OWNER, name="Olivia Owner", to=[to], labels=("SENT",),
                minutes_ago=int(days * DAY), text=text)


def _on(person: Any, level: str) -> None:
    """Handle it for me on, with Approvals at ``level``."""
    _PICKED[0] = _LEVEL[level]
    inbox.set_watch(person.id, True, updated_by="test")


def _cards() -> list[Any]:
    return ledger.list_instances("delegation_reply", limit=50)


def test_on_balanced_it_follows_up_with_a_contact(owner: Any, models: dict[str, Any]) -> None:
    _on(owner, "balanced")
    mailbox = FakeInbox()
    mailbox.add(_asked())
    result = _scan(owner, mailbox)
    assert result.drafted == 1 and result.handled == 1
    assert mailbox.sent == ["d1"]
    spec = mailbox.specs[0]
    assert spec.to == [DANA] and spec.cc == [] and spec.thread_id == "t9"
    assert spec.in_reply_to == "<s1@x.example>"
    [card] = _cards()
    payload = inbox.card_payload(card)
    assert payload["source"] == "follow_up" and payload["from_email"] == DANA
    assert card.status == ledger.STATUS_EXECUTED and card.gate_mode == "auto_execute"
    # The composer was asked for a nudge, from the thread.
    assert "follow-up" in models["composed"][0]


def test_on_careful_it_waits_on_a_card(owner: Any, models: dict[str, Any]) -> None:
    _on(owner, "careful")
    mailbox = FakeInbox()
    mailbox.add(_asked())
    result = _scan(owner, mailbox)
    assert result.drafted == 1 and result.handled == 0 and mailbox.sent == []
    [card] = _cards()
    assert card.status == ledger.STATUS_PROPOSED and card.gate_mode == "propose"
    assert inbox.card_payload(card)["handle_it_reason"] == "level"


@pytest.mark.parametrize("level", ["balanced", "hands_off"])
def test_someone_they_only_wrote_to_gets_one_above_careful(owner: Any, models: dict[str, Any], level: str) -> None:
    _on(owner, level)
    mailbox = FakeInbox()
    mailbox.written_to.add(LEE)
    mailbox.add(_asked(to=LEE, text="Hi Lee, could you send me the slides from Tuesday?"))
    result = _scan(owner, mailbox)
    assert result.drafted == 1 and mailbox.sent == ["d1"]


def test_someone_on_cc_gets_it_too(owner: Any, models: dict[str, Any]) -> None:
    _on(owner, "balanced")
    mailbox = FakeInbox()
    mailbox.written_to.add(LEE)
    mailbox.add(_msg("s1", "t9", sender=OWNER, name="Olivia Owner", to=[DANA], cc=(LEE,), labels=("SENT",),
                     minutes_ago=4 * DAY, text="Hi Dana, could you send me the slides from Tuesday?"))
    result = _scan(owner, mailbox)
    assert result.drafted == 1 and mailbox.sent == ["d1"]


def test_off_it_never_looks(owner: Any, models: dict[str, Any]) -> None:
    inbox.set_watch(owner.id, False, updated_by="test")
    mailbox = FakeInbox()
    mailbox.add(_asked())
    _scan(owner, mailbox)
    assert "list:sent" not in mailbox.calls and _cards() == []


@pytest.mark.parametrize("message", [
    _asked(days=2),  # too soon
    _asked(days=12),  # too long ago
    _asked(text="Hi Dana, here are the slides from Tuesday."),  # asked nothing
])
def test_only_a_question_waiting_a_few_days(owner: Any, models: dict[str, Any], message: Any) -> None:
    _on(owner, "hands_off")
    mailbox = FakeInbox()
    mailbox.add(message)
    assert _scan(owner, mailbox).drafted == 0 and _cards() == []


def test_not_when_someone_already_answered(owner: Any, models: dict[str, Any]) -> None:
    _on(owner, "hands_off")
    mailbox = FakeInbox()
    mailbox.add(_asked(), _msg("m2", "t9", minutes_ago=2 * DAY, text="Sure, here they are."))
    assert _scan(owner, mailbox).drafted == 0 and _cards() == []


def test_one_follow_up_per_email_ever(owner: Any, models: dict[str, Any]) -> None:
    _on(owner, "careful")
    mailbox = FakeInbox()
    mailbox.add(_asked())
    _scan(owner, mailbox)
    [card] = _cards()
    assert ledger.close_externally(card.id, reason="dismissed")  # settled, so the thread is free again
    follow_ups._LAST_LOOK.clear()
    _scan(owner, mailbox, NOW + timedelta(hours=2))
    assert len(mailbox.specs) == 1


def test_it_looks_at_most_once_an_hour(owner: Any, models: dict[str, Any]) -> None:
    _on(owner, "careful")
    mailbox = FakeInbox()
    _scan(owner, mailbox)
    _scan(owner, mailbox, NOW + timedelta(minutes=10))
    assert mailbox.calls.count("list:sent") == 1
    _scan(owner, mailbox, NOW + timedelta(hours=2))
    assert mailbox.calls.count("list:sent") == 2


def test_an_answer_closes_the_waiting_follow_up(owner: Any, models: dict[str, Any]) -> None:
    _on(owner, "careful")
    mailbox = FakeInbox()
    mailbox.add(_asked())
    _scan(owner, mailbox)
    [card] = _cards()
    mailbox.add(_msg("m2", "t9", minutes_ago=5, text="Sorry for the wait, here they are."))
    _scan(owner, mailbox, NOW + timedelta(minutes=1))
    closed = ledger.get_decision_instance(card.id)
    assert closed is not None and closed.status != ledger.STATUS_PROPOSED
    assert "d1" in mailbox.deleted  # nobody had touched the draft


def _check(owner: Any, reply: Any, *, relation: str = "contact", sent: Any = None) -> str | None:
    return handle_it.follow_up_refusal(
        owner.id, sent or _asked(), reply, relation=relation, own={OWNER}, exec_address="exec@co.example",
        now=NOW,
    )


def _nudge(**over: Any) -> inbox.Reply:
    fields: dict[str, Any] = {
        "to": [DANA], "cc": [], "subject": "Re: Thursday call",
        "body": "Hi Dana, just checking whether you had a chance to look at this?\n\nOlivia",
        "open_questions": [], "flags": [], "in_reply_to": "<s1@x.example>", "references": None,
    }
    fields.update(over)
    return inbox.Reply(**fields)


@pytest.mark.parametrize(("reply", "code"), [
    (_nudge(cc=[LEE]), "recipients"),
    (_nudge(to=[LEE]), "recipients"),
    (_nudge(body="Hi Dana, " + "just checking in. " * 300), "long"),
    (_nudge(flags=["asks_if_ai"]), "flagged"),
])
def test_what_a_follow_up_never_sends(owner: Any, reply: Any, code: str) -> None:
    _on(owner, "hands_off")
    assert _check(owner, reply) == code


def test_below_hands_off_a_follow_up_to_a_stranger_waits(owner: Any) -> None:
    _on(owner, "balanced")
    assert _check(owner, _nudge(), relation="stranger") == "stranger"
    _on(owner, "hands_off")
    assert _check(owner, _nudge(), relation="stranger") is None


def test_a_follow_up_counts_toward_the_day(owner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _on(owner, "hands_off")
    monkeypatch.setenv("DELEGATION_HANDLE_IT_MAX_SENDS_PER_DAY", "1")
    handle_it.record_handled(owner.id, "other", 1, "x", now=NOW - timedelta(hours=1))
    assert _check(owner, _nudge()) == "daily_limit"


def test_the_send_path_rechecks_the_setting(owner: Any, models: dict[str, Any]) -> None:
    from openexecutive.delegation.reply_send import SendRefused, send_on_its_own

    _on(owner, "careful")
    mailbox = FakeInbox()
    mailbox.add(_asked())
    _scan(owner, mailbox)
    [card] = _cards()
    # Even a card somehow marked to go on its own refuses on Careful.
    with pytest.raises(SendRefused) as refused:
        asyncio.run(send_on_its_own(type("C", (), {**card.__dict__, "gate_mode": "auto_execute"})(),
                                    gmail=mailbox, now=NOW))
    assert refused.value.code == "level" and mailbox.sent == []


def test_the_send_path_rechecks_a_stranger(owner: Any, models: dict[str, Any]) -> None:
    import json

    from openexecutive.delegation.reply_send import SendRefused, send_on_its_own

    _on(owner, "careful")
    mailbox = FakeInbox()
    mailbox.add(_asked())
    _scan(owner, mailbox)
    [card] = _cards()
    _on(owner, "balanced")
    payload = {**inbox.card_payload(card), "handled_as": "stranger"}
    fake = type("C", (), {**card.__dict__, "gate_mode": "auto_execute", "proposed_payload_json": json.dumps(payload)})()
    with pytest.raises(SendRefused) as refused:
        asyncio.run(send_on_its_own(fake, gmail=mailbox, now=NOW))
    assert refused.value.code == "stranger" and mailbox.sent == []


@pytest.mark.parametrize(("body", "code"), [
    ("Hi Dana, the slides are at https://files.example.com/x", "link"),
    ("Hi Dana, any news on the 20% figure?", "amount"),
    ("Hi Dana, any news on the contract?", "sensitive"),
])
def test_on_balanced_a_link_amount_or_topic_waits(owner: Any, body: str, code: str) -> None:
    _on(owner, "balanced")
    assert _check(owner, _nudge(body=body)) == code
    _on(owner, "hands_off")
    assert _check(owner, _nudge(body=body)) is None


def test_a_nudge_stays_short(owner: Any) -> None:
    _on(owner, "hands_off")
    assert _check(owner, _nudge(body="Hi Dana, " + "x" * 700)) == "long"
