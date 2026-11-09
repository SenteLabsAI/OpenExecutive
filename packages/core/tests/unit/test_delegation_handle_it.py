"""Handle it for me (delegation/handle_it.py): the inbox watcher sends some
replies on its own, decided by plain code and the Approvals level, and
leaves everything else on a card. Includes the attack suite: email that tries to steer it never ends in a
send it shouldn't."""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import delegation as route
from openexecutive.delegation import handle_it, inbox
from openexecutive.delegation.gmail import GmailError
from openexecutive.delegation.inbox_classifier import Verdict
from openexecutive.delegation.reply_send import SendRefused, send_on_its_own
from openexecutive.memory import decision_ledger as ledger
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store

from . import test_delegation_inbox as _inbox_tests
from .test_delegation_inbox import DANA, NOW, OWNER, FakeInbox, _ledger, _msg, _scan

db = _inbox_tests.db
owner = _inbox_tests.owner
models = _inbox_tests.models


@pytest.fixture(autouse=True)
def local_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """``make dev``'s local login unless a test says otherwise: the API can
    tie the switch to the person."""
    monkeypatch.setenv("OE_LOCAL_LOGIN", "1")
    monkeypatch.delenv("OE_PUBLIC_DEPLOYMENT", raising=False)
    monkeypatch.delenv("CALLER_ASSERTION_PUBLIC_KEYS", raising=False)


@pytest.fixture(autouse=True)
def balanced(monkeypatch: pytest.MonkeyPatch) -> None:
    """Approvals on Balanced unless a test says otherwise: Careful holds
    everything it would send as them."""
    from openexecutive.orchestrator import outside_mode

    monkeypatch.setattr(outside_mode, "get_mode", lambda **_: outside_mode.OUTSIDERS)


def _careful(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.orchestrator import outside_mode

    monkeypatch.setattr(outside_mode, "get_mode", lambda **_: outside_mode.ASK)


def _on(person: Any) -> None:
    """Replies may go on their own: Act as me writes replies to the inbox
    (the owner fixture's default; there is no switch of its own)."""
    inbox.set_watch(person.id, True, updated_by="test")


def _scan_one(owner: Any, *messages: Any) -> tuple[FakeInbox, inbox.ScanResult]:
    mailbox = FakeInbox()
    mailbox.add(*(messages or (_msg("m1", "t1"),)))
    return mailbox, _scan(owner, mailbox)


def _only_card(owner: Any) -> Any:
    cards = ledger.list_instances("delegation_reply", limit=50)
    assert len(cards) == 1
    return cards[0]


# ── the switch ────────────────────────────────────────────────────────────────


def test_off_by_default() -> None:
    stored = handle_it.get(7)
    assert stored.enabled is False


def test_it_follows_the_inbox_watcher_and_approvals_decide(owner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    assert handle_it.get(owner.id).enabled is True and handle_it.approvals_hold() is False
    inbox.set_watch(owner.id, False, updated_by="t")
    assert handle_it.get(owner.id).enabled is False
    inbox.set_watch(owner.id, True, updated_by="t")
    _careful(monkeypatch)
    assert handle_it.approvals_hold() is True  # Careful holds it


def test_a_table_from_before_keeps_the_switch(tmp_path: Path) -> None:
    from openexecutive.delegation.schema import ensure_schema

    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE delegation_handle_it (person_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0, "
        "levels TEXT NOT NULL DEFAULT '{}', updated_at TEXT, updated_by TEXT)"
    )
    conn.execute("INSERT INTO delegation_handle_it VALUES (1, 1, ?, '', 't')",
                 ('{"reply_known": "handle", "reply_stranger": "handle"}',))
    conn.execute("INSERT INTO delegation_handle_it VALUES (4, 1, ?, '', 't')",
                 ('{"reply_known": "ask", "reply_stranger": "ask"}',))
    conn.commit()
    ensure_schema(conn)
    ensure_schema(conn)  # idempotent
    conn.close()
    # The old row is no switch any more: without Act as me and the inbox
    # watcher in that file, nothing goes on its own.
    assert not handle_it.get(1, db_path=db).enabled
    assert not handle_it.get(4, db_path=db).enabled


def test_someone_who_never_turned_it_on_starts_in_training(tmp_path: Path) -> None:
    """Handle it for me was off until the person turned it on. Now that
    Approvals decides, anyone whose switch was off is put in training, so
    nothing goes out as them until they allow it; turning training off
    afterwards sticks."""
    from openexecutive.delegation import training
    from openexecutive.delegation.schema import ensure_schema

    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE delegation_handle_it (person_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0, "
        "levels TEXT NOT NULL DEFAULT '{}', updated_at TEXT, updated_by TEXT)"
    )
    conn.execute("INSERT INTO delegation_handle_it VALUES (1, 1, '{}', '', 't')")  # had turned it on
    conn.execute("INSERT INTO delegation_handle_it VALUES (4, 0, '{}', '', 't')")  # never did
    conn.commit()
    conn.close()
    # Person 6 had the inbox watcher on and never touched Handle it for me.
    # Everything above is a file from before the upgrade, so the marker that
    # says it ran is taken away again.
    inbox.set_watch(6, True, updated_by="t", db_path=db)
    conn = sqlite3.connect(str(db))
    conn.execute("DELETE FROM delegation_upgrades")
    conn.execute("DELETE FROM delegation_training")
    conn.commit()
    ensure_schema(conn)
    conn.close()
    assert not training.get(1, db_path=db).training
    assert training.get(4, db_path=db).training
    assert training.get(6, db_path=db).training
    # Turning it off sticks, and someone who sets Act as me up after the
    # upgrade gets the new default: Approvals decides.
    training.set_(4, {"training": False}, updated_by="t", db_path=db)
    inbox.set_watch(9, True, updated_by="t", db_path=db)
    conn = sqlite3.connect(str(db))
    ensure_schema(conn)
    conn.close()
    assert not training.get(4, db_path=db).training
    assert not training.get(9, db_path=db).training


# ── sending on its own ────────────────────────────────────────────────────────


def test_a_reply_sent_on_its_own_says_an_ai_wrote_it(
    owner: Any, models: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.delegation import disclosure

    monkeypatch.delenv("AI_DISCLOSURE", raising=False)
    _on(owner)
    mailbox, result = _scan_one(owner)
    assert result.handled == 1
    (spec,) = mailbox.specs
    assert spec.body.endswith(disclosure.LINE)
    assert inbox.card_payload(_only_card(owner))["draft_body"] == spec.body


def test_a_reply_waiting_for_them_has_no_ai_line_unless_they_asked(
    owner: Any, models: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.delegation import disclosure

    monkeypatch.delenv("AI_DISCLOSURE", raising=False)
    _careful(monkeypatch)  # every reply waits for them
    mailbox, _ = _scan_one(owner)
    assert disclosure.LINE not in mailbox.specs[0].body
    disclosure.set_(owner.id, approved_sends=True, updated_by="test")
    mailbox, _ = _scan_one(owner, _msg("m2", "t2"))
    assert mailbox.specs[0].body.endswith(disclosure.LINE)



def test_on_it_sends_a_reply_to_someone_they_know(db: Path, owner: Any, models: dict[str, Any]) -> None:
    _on(owner)
    mailbox, result = _scan_one(owner)
    assert result.drafted == 1 and result.handled == 1
    assert mailbox.sent == ["d1"]
    card = _only_card(owner)
    assert card.status == "executed" and card.gate_mode == "auto_execute"
    assert card.resolver_person_id is None
    assert _ledger(db)["m1"] == ("sent", "handled")
    assert inbox.open_cards(owner.id) == []
    assert handle_it.sent_today(owner.id, NOW) == 1
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT private_to_principal FROM audit_log WHERE event_type = 'delegation_reply_handled'"
        ).fetchall()
    assert rows == [(1,)]


def test_a_send_is_counted_even_when_later_bookkeeping_fails(
    owner: Any, models: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.delegation import drafts

    def broken(*_: Any, **__: Any) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(drafts, "mark_sent", broken)
    _on(owner)
    mailbox, _ = _scan_one(owner)
    assert mailbox.sent == ["d1"]
    assert handle_it.sent_today(owner.id, NOW) == 1  # the limits still see it


def test_the_counted_rules_are_checked_again_just_before_sending(
    owner: Any, models: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    real = handle_it.count_refusal

    def limit_reached_by_send_time(person_id: int, thread_id: str, now: Any, **kw: Any) -> str | None:
        calls.append(thread_id)
        # The scan's check passes; by the time it sends, the limit is reached.
        return real(person_id, thread_id, now, **kw) if len(calls) == 1 else "daily_limit"

    monkeypatch.setattr(handle_it, "count_refusal", limit_reached_by_send_time)
    _on(owner)
    mailbox, result = _scan_one(owner)
    assert len(calls) == 2
    assert mailbox.sent == [] and result.handled == 0
    card = _only_card(owner)
    assert card.status == "proposed" and card.gate_mode == "propose"  # it waits for them
    assert inbox.card_payload(card)["handle_it_reason"] == "daily_limit"
    from openexecutive.delegation import replies

    assert replies.cards(owner)[0].waited_because == handle_it.REASONS["daily_limit"]


def test_counted_rules_allow_one_reply_a_thread_a_day() -> None:
    assert handle_it.count_refusal(7, "t1", NOW) is None
    handle_it.record_handled(7, "t1", 1, "s1", now=NOW)
    assert handle_it.count_refusal(7, "t1", NOW) == "thread_recent"
    assert handle_it.count_refusal(7, "t2", NOW) is None
    assert handle_it.count_refusal(7, "t1", NOW + timedelta(days=2)) is None


def test_handled_lists_it_with_the_questions_left_for_them(owner: Any, models: dict[str, Any]) -> None:
    _on(owner)
    _scan_one(owner)
    found = handle_it.handled(owner.id)
    assert len(found) == 1
    assert found[0].to_email == DANA
    assert found[0].open_questions == ["Can the call move to Friday at 10?"]
    assert handle_it.handled(owner.id + 1) == []  # nobody else's


def test_handled_is_not_crowded_out_by_other_peoples_sends(owner: Any, models: dict[str, Any]) -> None:
    from openexecutive.memory import decision_ledger

    _on(owner)
    _scan_one(owner)
    other = owner.id + 1
    for n in range(3):  # newer sends of someone else's
        row = decision_ledger.create_decision_instance(
            decision_class="delegation_reply", department="general", originating_session_id=None,
            proposed_payload={"person_id": other}, idempotency_key=f"other-{n}", gate_mode="auto_execute",
            approver_person_id=other, confidence=None,
        )
        assert decision_ledger.claim_for_execution(row, resolver_person_id=None)
        assert decision_ledger.finish_execution(row, status=decision_ledger.STATUS_EXECUTED)
    assert len(handle_it.handled(owner.id, limit=1)) == 1


def test_on_careful_everything_waits(owner: Any, models: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    _on(owner)
    _careful(monkeypatch)
    mailbox, result = _scan_one(owner)
    assert result.handled == 0 and mailbox.sent == []
    assert inbox.card_payload(_only_card(owner))["handle_it_reason"] == "level"


def test_below_hands_off_a_stranger_gets_a_card(owner: Any, models: dict[str, Any]) -> None:
    # Like a send to someone not in People anywhere (levels.NEW_PERSON).
    _on(owner)
    stranger = _msg("m1", "t1", sender="sam@unknown.example", name="Sam")
    mailbox, result = _scan_one(owner, stranger)
    assert result.handled == 0 and mailbox.sent == []
    assert inbox.card_payload(_only_card(owner))["handle_it_reason"] == "stranger"


def test_a_stranger_gets_the_reply_on_hands_off(
    owner: Any, models: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _on(owner)
    _hands_off(monkeypatch)
    stranger = _msg("m1", "t1", sender="sam@unknown.example", name="Sam")
    mailbox, result = _scan_one(owner, stranger)
    assert result.handled == 1 and mailbox.sent == ["d1"]


@pytest.mark.parametrize(("body", "code"), [
    ("The agenda is at https://docs.example.com/agenda.", "link"),
    ("Sure, we can do it for $4,000.", "amount"),
    ("Yes, I'll sign the contract tomorrow.", "sensitive"),
    ("Here's the new password for the portal.", "sensitive"),
])
def test_below_hands_off_a_reply_with_a_link_amount_or_topic_waits(
    switched_on: Any, monkeypatch: pytest.MonkeyPatch, body: str, code: str,
) -> None:
    assert _check(switched_on, reply=_reply(body=body)) == code
    _hands_off(monkeypatch)
    assert _check(switched_on, reply=_reply(body=body)) is None


def test_below_hands_off_the_email_s_own_topic_holds_the_reply(switched_on: Any) -> None:
    message = _msg("m1", "t1", text="Can you approve the invoice before Friday?")
    assert _check(switched_on, message=message) == "sensitive"


def test_without_signed_sign_ins_on_a_server_nothing_goes(
    owner: Any, models: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _on(owner)
    monkeypatch.delenv("OE_LOCAL_LOGIN", raising=False)
    mailbox, result = _scan_one(owner)
    assert result.handled == 0 and mailbox.sent == []
    assert inbox.card_payload(_only_card(owner))["handle_it_reason"] == "signing_off"


def test_one_reply_on_its_own_per_conversation_a_day(owner: Any, models: dict[str, Any]) -> None:
    _on(owner)
    mailbox = FakeInbox()
    mailbox.add(_msg("m1", "t1"))
    _scan(owner, mailbox)
    assert mailbox.sent == ["d1"]
    mailbox.add(_msg("m2", "t1", minutes_ago=-60, text="Thanks! Also, could you send the agenda over?"))
    result = _scan(owner, mailbox, now=NOW + timedelta(hours=2))
    assert result.drafted == 1 and result.handled == 0
    assert mailbox.sent == ["d1"]
    assert inbox.open_cards(owner.id)[0].gate_mode == "propose"


def test_the_daily_limit_holds(owner: Any, models: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    _on(owner)
    monkeypatch.setenv("DELEGATION_HANDLE_IT_MAX_SENDS_PER_DAY", "1")
    mailbox = FakeInbox()
    mailbox.add(_msg("m1", "t1"), _msg("m2", "t2", sender="lee@co.example", name="Lee", minutes_ago=40))
    people_store.upsert_person(full_name="Lee", email="lee@co.example", kind="contact")
    people_registry.invalidate()
    result = _scan(owner, mailbox)
    assert result.drafted == 2 and result.handled == 1
    assert len(mailbox.sent) == 1


def test_turning_it_off_before_the_send_leaves_the_card(
    owner: Any, models: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _careful(monkeypatch)
    mailbox, _ = _scan_one(owner)
    card = _only_card(owner)
    # A card made as auto_execute, then the inbox watcher goes off.
    with sqlite3.connect(_inbox_tests.episodic.DB_PATH) as conn:
        conn.execute("UPDATE decision_instances SET gate_mode = 'auto_execute' WHERE id = ?", (card.id,))
    inbox.set_watch(owner.id, False, updated_by="t")
    fresh = ledger.get_decision_instance(card.id)
    with pytest.raises(SendRefused) as err:
        asyncio.run(send_on_its_own(fresh, gmail=mailbox, now=NOW))
    assert err.value.code == "handle_it_off" and mailbox.sent == []


def test_a_card_made_to_ask_is_never_sent_on_its_own(
    owner: Any, models: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.orchestrator import outside_mode

    _careful(monkeypatch)
    mailbox, _ = _scan_one(owner)  # Careful: a propose card
    monkeypatch.setattr(outside_mode, "get_mode", lambda **_: outside_mode.OUTSIDERS)
    with pytest.raises(SendRefused) as err:
        asyncio.run(send_on_its_own(_only_card(owner), gmail=mailbox, now=NOW))
    assert err.value.code == "handle_it_off" and mailbox.sent == []


def test_an_edit_in_the_mailbox_stops_the_send(owner: Any, models: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    _on(owner)
    real = inbox._send_on_its_own

    async def edit_first(person: Any, client: Any, decision_id: int, result: Any, *, now: Any) -> None:
        client.edit("d1")
        await real(person, client, decision_id, result, now=now)

    monkeypatch.setattr(inbox, "_send_on_its_own", edit_first)
    mailbox, result = _scan_one(owner)
    assert result.handled == 0 and mailbox.sent == []
    card = inbox.open_cards(owner.id)[0]
    assert card.status == "proposed" and card.gate_mode == "propose"
    assert inbox.card_payload(card)["handle_it_reason"] == "draft_changed"


def test_an_unconfirmed_send_settles_as_sent_on_its_own(db: Path, owner: Any, models: dict[str, Any]) -> None:
    _on(owner)
    mailbox = FakeInbox()
    mailbox.add(_msg("m1", "t1"))
    mailbox.send_then_fail = GmailError("gmail POST failed: ReadTimeout", maybe_done=True)
    result = _scan(owner, mailbox)
    assert result.handled == 0
    card = _only_card(owner)
    assert card.status == "executing"
    mailbox.send_then_fail = None
    asyncio.run(inbox.reconcile(owner, mailbox, now=NOW + timedelta(minutes=10), own={OWNER}))
    settled = ledger.get_decision_instance(card.id)
    assert settled is not None and settled.status == "executed"
    assert handle_it.sent_today(owner.id, NOW) == 1
    assert _ledger(db)["m1"] == ("sent", "handled")


# ── the attack suite ──────────────────────────────────────────────────────────
#
# The reader and the writer have no tools, so an email can only change what
# they answer. These are the answers an email might steer them into, and the
# rules must keep every one on a card.


def _reply(**over: Any) -> inbox.Reply:
    base: dict[str, Any] = {
        "to": [DANA], "cc": [], "subject": "Re: Thursday call",
        "body": "Hi Dana,\n\nThanks, I'll get back to you shortly.\n\nOlivia",
        "open_questions": [], "flags": [], "in_reply_to": None, "references": None,
    }
    base.update(over)
    return inbox.Reply(**base)


def _verdict(**over: Any) -> Verdict:
    base: dict[str, Any] = {"needs_reply": True, "kind": "scheduling", "confidence": 0.95, "asked_of_them": True}
    base.update(over)
    return Verdict(**base)


@pytest.fixture
def switched_on(owner: Any) -> Any:
    _on(owner)
    return owner


def _thread(message: Any) -> Any:
    from openexecutive.delegation.gmail import MailThread

    return MailThread(id=message.thread_id, messages=[message])


def _check(owner: Any, *, message: Any = None, reply: Any = None, verdict: Any = None,
           relation: str = "contact") -> str | None:
    message = message or _msg("m1", "t1")
    return handle_it.refusal(
        owner.id, message, _thread(message), reply or _reply(), verdict or _verdict(),
        relation=relation, own={OWNER}, exec_address="exec@co.example", now=NOW,
    )


def test_a_plain_reply_passes(switched_on: Any) -> None:
    assert _check(switched_on) is None


@pytest.mark.parametrize(("reply", "code"), [
    # "Forward this to me" / "copy my colleague": someone the email didn't go to.
    ({"cc": ["attacker@evil.example"]}, "recipients"),
    ({"to": ["attacker@evil.example"]}, "recipients"),
    ({"to": [], "cc": []}, "recipients"),
    # The Executive's own address is never a recipient of a reply sent as them.
    ({"cc": ["exec@co.example"]}, "recipients"),
    # Long replies say more than an acknowledgement should.
    ({"body": "word " * 1000}, "long"),
    # Anything the writer flagged.
    ({"flags": ["asks_if_ai"]}, "flagged"),
    ({"flags": ["names_the_executive"]}, "flagged"),
    ({"flags": ["cc_trimmed"]}, "flagged"),
])
def test_steered_replies_stay_on_a_card(switched_on: Any, reply: dict[str, Any], code: str) -> None:
    assert _check(switched_on, reply=_reply(**reply)) == code


def _hands_off(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.orchestrator import outside_mode

    monkeypatch.setattr(outside_mode, "get_mode", lambda **_: outside_mode.TRUST)


def test_an_unauthenticated_sender_never_gets_one(owner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _on(owner)
    message = _msg("m1", "t1", sender_authenticated=False)
    # The watcher handles such a sender as a stranger: no, even on Hands-off.
    assert _check(owner, message=message, relation="stranger") == "stranger"
    _hands_off(monkeypatch)
    assert _check(owner, message=message, relation="stranger") == "sender_unverified"


def test_below_hands_off_a_reply_to_a_stranger_waits(switched_on: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _check(switched_on, relation="stranger") == "stranger"
    # Someone they have written to before counts as known.
    assert _check(switched_on, relation="correspondent") is None
    _hands_off(monkeypatch)
    assert _check(switched_on, relation="stranger") is None


@pytest.mark.parametrize(("verdict", "code"), [
    ({"kind": "phishing"}, "kind"),
    ({"kind": "pitch"}, "kind"),
    ({"kind": "other"}, "kind"),
    ({"confidence": 0.6}, "unsure"),
    ({"confidence": 0.79}, "unsure"),
])
def test_unsure_or_odd_verdicts_stay_on_a_card(switched_on: Any, verdict: dict[str, Any], code: str) -> None:
    assert _check(switched_on, verdict=_verdict(**verdict)) == code


def test_others_already_on_the_email_may_be_replied_to(switched_on: Any) -> None:
    message = _msg("m1", "t1", cc=("lee@co.example",))
    assert _check(switched_on, message=message, reply=_reply(cc=["lee@co.example"])) is None


def test_never_twice_in_a_row_in_one_conversation(switched_on: Any) -> None:
    from openexecutive.delegation.gmail import MailMessage, MailThread

    first = _msg("m1", "t1")
    handle_it.record_handled(switched_on.id, "t1", 1, "sent-1", now=NOW - timedelta(days=3))
    sent = MailMessage(id="sent-1", thread_id="t1", from_addr=OWNER, to=[DANA], labels=["SENT"],
                       received_at=(NOW - timedelta(days=3)).isoformat())
    later = _msg("m2", "t1", text="And Friday?")
    thread = MailThread(id="t1", messages=[first, sent, later])
    why = handle_it.refusal(
        switched_on.id, later, thread, _reply(), _verdict(), relation="contact", own={OWNER},
        exec_address="", now=NOW,
    )
    assert why == "in_a_row"


def test_the_rules_fail_closed(switched_on: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **kw: Any) -> Any:
        raise RuntimeError("db gone")

    monkeypatch.setattr(handle_it, "_handled_rows", boom)
    assert _check(switched_on) == "uncountable"


def test_every_refusal_has_words_for_the_person() -> None:
    import inspect
    import re

    source = inspect.getsource(handle_it._refusal) + inspect.getsource(handle_it.refusal)
    codes = set(re.findall(r'return "([a-z_]+)"', source))
    assert codes <= set(handle_it.REASONS)


# ── the routes ────────────────────────────────────────────────────────────────

HEADERS = {"x-caller-email": OWNER}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    async def connected(email: str | None, *, gmail: Any = None) -> str:
        return "connected"

    monkeypatch.setattr(route, "gmail_status", connected)
    app = FastAPI()
    app.include_router(route.router)
    return TestClient(app)


def test_get_shows_it_following_the_inbox_watcher(client: TestClient, owner: Any) -> None:
    body = client.get("/delegation", headers=HEADERS).json()
    assert body["handle_it"] == {"enabled": True, "available": True, "sent_today": 0}
    assert body["training"] == {"training": False, "drafts": False, "learned": []}
    inbox.set_watch(owner.id, False, updated_by="t")
    body = client.get("/delegation", headers=HEADERS).json()
    assert body["handle_it"]["enabled"] is False


def test_there_is_no_switch_route_of_its_own(client: TestClient, owner: Any) -> None:
    resp = client.put("/delegation/handle-it", json={"enabled": True}, headers=HEADERS)
    assert resp.status_code == 404


def test_the_inbox_watcher_can_still_be_turned_off_without_signed_sign_ins(
    client: TestClient, owner: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OE_LOCAL_LOGIN", raising=False)
    resp = client.put("/delegation/inbox", json={"enabled": False}, headers=HEADERS)
    assert resp.status_code == 200
    assert handle_it.get(owner.id).enabled is False


def test_nobody_else_turns_the_inbox_watcher_on(client: TestClient, owner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.delegation import verified

    inbox.set_watch(owner.id, False, updated_by="t")
    monkeypatch.setattr(verified, "caller_refusal", lambda caller, email: verified.NOT_YOURS)
    resp = client.put("/delegation/inbox", json={"enabled": True}, headers=HEADERS)
    assert resp.status_code == 403 and resp.json()["detail"]["code"] == "not_yours"
    assert handle_it.get(owner.id).enabled is False


@pytest.mark.parametrize(("signing", "local", "kind", "email", "expected"), [
    (True, False, "user", "olivia@co.example", None),
    (True, False, "user", "mallory@co.example", "not_yours"),
    (True, False, "operator", "", "not_yours"),
    (True, True, "operator", "", None),
    (False, True, "open", "", None),
    (False, True, "open", "mallory@co.example", "not_yours"),
    (False, False, "open", "olivia@co.example", "caller_signing_required"),
])
def test_only_a_caller_the_api_knows_is_them(
    monkeypatch: pytest.MonkeyPatch, signing: bool, local: bool, kind: str, email: str, expected: str | None,
) -> None:
    from types import SimpleNamespace

    from openexecutive.api import caller as api_caller
    from openexecutive.delegation import verified
    from openexecutive.utils import deployment

    monkeypatch.setattr(api_caller, "signing_on", lambda: signing)
    monkeypatch.setattr(deployment, "is_local_login", lambda: local)
    who = SimpleNamespace(kind=kind, email=email)
    assert verified.caller_refusal(who, "olivia@co.example") == expected


def test_turning_the_inbox_watcher_off_turns_it_off(client: TestClient, owner: Any) -> None:
    resp = client.put("/delegation/inbox", json={"enabled": False}, headers=HEADERS)
    assert resp.status_code == 200
    assert handle_it.get(owner.id).enabled is False


def test_handled_route_lists_the_callers_own(client: TestClient, owner: Any, models: dict[str, Any]) -> None:
    _on(owner)
    _scan_one(owner)
    replies = client.get("/delegation/handled", headers=HEADERS).json()["replies"]
    assert len(replies) == 1 and replies[0]["to_email"] == DANA
    assert replies[0]["gmail_link"]


# ── the last look for an email that steers an assistant ──────────────────────


def test_an_email_that_steers_an_assistant_waits_for_them(owner: Any, models: dict[str, Any]) -> None:
    _on(owner)
    models["steers"] = True
    mailbox, result = _scan_one(owner)
    assert result.drafted == 1 and result.handled == 0 and mailbox.sent == []
    card = _only_card(owner)
    assert card.gate_mode == "propose" and inbox.card_payload(card)["handle_it_reason"] == "suspicious"
    assert "suspicious" in handle_it.REASONS
    # It read the email as data, in its own block.
    assert models["checked"] and "<email>" in models["checked"][0]


def test_a_check_that_fails_holds_the_reply(owner: Any, models: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.delegation import injection_check

    async def broken(model: str, turn: str) -> dict[str, Any]:
        raise RuntimeError("model down")

    monkeypatch.setattr(injection_check, "_call_model", broken)
    _on(owner)
    mailbox, result = _scan_one(owner)
    assert result.handled == 0 and mailbox.sent == []
    assert inbox.card_payload(_only_card(owner))["handle_it_reason"] == "suspicious"


def test_the_check_runs_only_when_a_reply_would_go(
    owner: Any, models: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Careful: every reply waits anyway, so nothing is checked.
    _careful(monkeypatch)
    _scan_one(owner)
    assert models["checked"] == []


# ── In training: nothing goes as them until they allow the person ────────────


def _in_training(owner: Any) -> None:
    from openexecutive.delegation import training

    training.set_(owner.id, {training.TRAINING: True}, updated_by="test")


@pytest.mark.parametrize("level", ["balanced", "hands_off"])
def test_in_training_every_reply_waits_on_any_level(
    owner: Any, models: dict[str, Any], monkeypatch: pytest.MonkeyPatch, level: str,
) -> None:
    _on(owner)
    _in_training(owner)
    if level == "hands_off":
        _hands_off(monkeypatch)
    mailbox, result = _scan_one(owner)
    assert result.handled == 0 and mailbox.sent == []
    payload = inbox.card_payload(_only_card(owner))
    assert payload["handle_it_reason"] == "training"
    assert handle_it.REASONS["training"].startswith("In training")


def test_once_they_allow_the_person_replies_to_them_go(owner: Any, models: dict[str, Any]) -> None:
    from openexecutive.delegation import training

    _on(owner)
    _in_training(owner)
    allowed = training.allow_person(owner.id, DANA, "Dana Park", decision_id=None)
    assert allowed is not None and allowed.label == "Send to Dana Park (dana@northpeak.example) on its own"
    mailbox, result = _scan_one(owner)
    assert result.handled == 1 and mailbox.sent == ["d1"]
    # Anyone else still gets a card, even someone they know.
    other = _msg("m2", "t2", sender="sam@co.example", name="Sam")
    mailbox, result = _scan_one(owner, other)
    assert result.handled == 0 and mailbox.sent == []
    (card,) = inbox.open_cards(owner.id)
    assert inbox.card_payload(card)["handle_it_reason"] == "training"


def test_everyone_the_reply_reaches_must_be_allowed(owner: Any, models: dict[str, Any]) -> None:
    """In training, a reply to an allowed sender that also goes to someone
    on the email they haven't allowed waits; allowing them too lets it go."""
    from openexecutive.delegation import training

    _on(owner)
    _in_training(owner)
    training.allow_person(owner.id, DANA, "Dana Park", decision_id=None)
    ben = "ben@northpeak.example"
    mailbox, result = _scan_one(owner, _msg("m1", "t1", cc=(ben,)))
    assert result.handled == 0 and mailbox.sent == []
    card = _only_card(owner)
    payload = inbox.card_payload(card)
    assert payload["handle_it_reason"] == "training"
    assert sorted(training.reply_goes_to(payload)) == sorted([DANA, ben])
    training.allow_person(owner.id, ben, "Ben", decision_id=None)
    mailbox, result = _scan_one(owner, _msg("m2", "t2", cc=(ben,)))
    assert result.handled == 1 and mailbox.sent == ["d1"]


def test_someone_with_no_address_on_file_is_never_allowed_by_omission(owner: Any, models: dict[str, Any]) -> None:
    """A card reaching an allowed person and someone with no email on file
    (``action_cards._addresses`` gives "" for them) still waits in training."""
    from openexecutive.delegation import training

    _on(owner)
    _in_training(owner)
    training.allow_person(owner.id, DANA, "Dana Park", decision_id=None)
    assert handle_it.training_hold(owner.id, [DANA]) is None
    assert handle_it.training_hold(owner.id, [DANA, ""]) == "training"
    assert handle_it.training_hold(owner.id, [""]) == "training"


def test_a_card_reaching_several_people_is_allowed_whole_or_not_at_all(
    owner: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With room for one more, a card to two new people is refused up front
    instead of running and then allowing only the first of them."""
    from openexecutive.delegation import training
    from openexecutive.orchestrator import take_the_lead

    monkeypatch.setattr(take_the_lead, "ALLOWED_MAX", 2)
    training.allow_person(owner.id, DANA, "Dana Park", decision_id=None)
    sam = "sam@co.example"
    assert training.room_for_all(owner.id, [DANA, sam])
    assert not training.room_for_all(owner.id, [sam, "lee@co.example"])
    # The same address twice is one allowance; one already allowed costs nothing.
    assert training.room_for_all(owner.id, [sam, "Sam@co.example", DANA])
    assert training.room_for_all(owner.id, [])


def test_an_unreadable_training_setting_holds(owner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.delegation import training

    def broken(*a: Any, **k: Any) -> Any:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(training, "_connect", broken)
    assert handle_it.training_hold(owner.id, [DANA]) == "training"


def test_careful_holds_an_allowed_person_too(owner: Any, models: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.delegation import training

    _on(owner)
    _in_training(owner)
    _careful(monkeypatch)
    training.allow_person(owner.id, DANA, "Dana Park", decision_id=None)
    mailbox, result = _scan_one(owner)
    assert result.handled == 0 and mailbox.sent == []
    assert inbox.card_payload(_only_card(owner))["handle_it_reason"] == "level"
