"""The Executive doing what an email asks before its reply card reaches the
person (delegation/card_work.py, orchestrator/reply_card_tools.py)."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from openexecutive.delegation import answers, card_work, inbox, replies
from openexecutive.delegation.reply_send import SendRefused
from openexecutive.delegation.settings import DelegationOverride, pin_turn_delegation
from openexecutive.memory import decision_ledger as ledger
from openexecutive.orchestrator import reply_card_tools
from openexecutive.orchestrator.session import Session

from . import test_delegation_card_drafts as _card_tests
from .test_delegation_inbox import NOW, FakeInbox, _msg, _scan

db = _card_tests.db
models = _card_tests.models
owner = _card_tests.owner
local_login = _card_tests.local_login
_card = _card_tests._card
_fresh = _card_tests._fresh
_send = _card_tests._send

# The real one: tests/conftest.py keeps every other test from starting a turn.
_REAL_START = card_work.start

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
SHEET = ("Tax bills.xlsx", XLSX, b"PK-sheet")


def _apply(card: Any, person: Any, mailbox: FakeInbox, *, body: str = "", note: str = "Done.",
           files: list[tuple[str, str, bytes]] | None = None) -> dict[str, Any]:
    return asyncio.run(card_work.apply(card.id, person, body=body, note=note, files=files or [], gmail=mailbox))


def test_wanted_only_when_the_reply_left_an_ask() -> None:
    assert card_work.wanted({"open_questions": ["Can you make the spreadsheet?"]})
    assert not card_work.wanted({"open_questions": []})
    assert not card_work.wanted({"open_questions": ["  "]})
    assert not card_work.wanted({"open_questions": ["Any news?"], "source": "follow_up"})


def test_the_inbox_starts_the_work_and_holds_the_reply(
    db: Path, owner: Any, models: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[tuple[int, int]] = []
    monkeypatch.setattr(card_work, "start", lambda person, card_id, **_k: started.append((person.id, card_id)) or True)
    mailbox = FakeInbox()
    mailbox.add(_msg("m1", "t1"))
    _scan(owner, mailbox)
    card = inbox.open_cards(owner.id)[0]
    assert started == [(owner.id, card.id)]
    assert mailbox.sent == []


def test_the_work_rewrites_the_reply_and_its_files_go_with_send(
    db: Path, owner: Any, models: dict[str, Any],
) -> None:
    mailbox, card = _card(owner)
    changed = _apply(
        card, owner, mailbox, body="Hi Dana,\n\nHere's the spreadsheet: $4,812.36 in all.",
        note="I made the spreadsheet from her 3 bills.", files=[SHEET],
    )
    assert changed["draft_body"].startswith("Hi Dana,\n\nHere's the spreadsheet")
    assert changed["work"] == "done" and changed["questions_settled"] is True
    shown = replies.cards(owner)[0]
    assert shown.files == ["Tax bills.xlsx"] and shown.working is False
    assert shown.conversation[-1]["kind"] == "note"
    assert shown.conversation[-1]["text"] == "I made the spreadsheet from her 3 bills."
    assert shown.gist == "I made the spreadsheet from her 3 bills."
    assert _send(card, mailbox, owner) == "sent-d1"
    assert mailbox.specs[0].attachments == [SHEET]
    assert "$4,812.36" in mailbox.specs[0].body


def test_a_note_alone_keeps_the_reply(db: Path, owner: Any, models: dict[str, Any]) -> None:
    mailbox, card = _card(owner)
    before = inbox.card_payload(_fresh(card))["draft_body"]
    changed = _apply(card, owner, mailbox, note="Signing the check is yours to do.")
    assert changed["draft_body"] == before and changed["files"] == []
    assert replies.cards(owner)[0].conversation[-1]["text"] == "Signing the check is yours to do."


def test_in_the_mailbox_the_draft_is_replaced_by_one_with_the_files(
    db: Path, owner: Any, models: dict[str, Any],
) -> None:
    inbox.set_mailbox_drafts(owner.id, True, updated_by="test")
    mailbox, card = _card(owner)
    old = inbox.card_payload(_fresh(card))["draft_id"]
    assert old
    changed = _apply(card, owner, mailbox, body="Hi Dana,\n\nAttached.", files=[SHEET])
    assert changed["draft_id"] != old and old in mailbox.deleted
    assert mailbox.specs[-1].attachments == [SHEET]


def test_only_its_person_and_only_while_it_waits(db: Path, owner: Any, models: dict[str, Any]) -> None:
    from openexecutive.people import store as people_store

    mailbox, card = _card(owner)
    other = people_store.get_person(people_store.upsert_person(full_name="Sam Other", email="sam@x.example"))
    with pytest.raises(SendRefused) as err:
        _apply(card, other, mailbox)
    assert err.value.code == "not_yours"
    with pytest.raises(SendRefused):
        _apply(card, owner, mailbox, note="")
    with pytest.raises(SendRefused):
        _apply(card, owner, mailbox, files=[SHEET] * (card_work.MAX_FILES + 1))
    ledger.close_externally(card.id, reason="test")
    with pytest.raises(SendRefused) as err:
        _apply(card, owner, mailbox)
    assert err.value.code == "already_handled"


def test_start_runs_one_turn_and_shows_it_working(
    db: Path, owner: Any, models: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    mailbox, card = _card(owner)
    seen: dict[str, Any] = {}

    async def fake_run(person: Any, card_id: int, *, gmail: Any, ask: str) -> None:
        seen["working"] = replies.cards(person)[0].working
        seen["ask"] = ask
        await card_work.apply(card_id, person, body="", note="Looked at it.", files=[], gmail=gmail)
        card_work._RUNNING.discard(card_id)

    monkeypatch.setattr(card_work, "_run", fake_run)

    async def go() -> tuple[bool, bool]:
        first = _REAL_START(owner, card.id, gmail=mailbox, ask="Make the spreadsheet")
        again = _REAL_START(owner, card.id, gmail=mailbox)
        await asyncio.gather(*card_work._TASKS)
        return first, again

    assert asyncio.run(go()) == (True, False)
    assert seen == {"working": True, "ask": "Make the spreadsheet"}
    shown = replies.cards(owner)[0]
    assert shown.working is False
    assert [t["kind"] for t in shown.conversation][-2:] == ["asked", "note"]


def test_a_turn_that_hands_nothing_back_leaves_the_card_as_it_was(
    db: Path, owner: Any, models: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    mailbox, card = _card(owner)

    class _Exec:
        def __init__(self, **_k: Any) -> None: ...

        async def chat(self, **kwargs: Any) -> str:
            session = kwargs["session"]
            # The person's own mailbox, Act as me offered on an unattended turn,
            # and the card it works on.
            pinned = pin_turn_delegation(session, kwargs["user_message"])
            assert pinned.offered and session.unattended and session.reply_card_work == card.id
            assert reply_card_tools.offered(session)
            assert "thread_id t1" in kwargs["user_message"]
            assert kwargs["memory_text"] == ""
            return "Nothing I can do."

    async def no_gateway() -> None:
        return None

    import openexecutive.orchestrator.executive as executive_mod
    import openexecutive.orchestrator.mcp_gateway as gateway_mod

    monkeypatch.setattr(executive_mod, "Executive", _Exec)
    monkeypatch.setattr(gateway_mod, "ensure_active_gateway", no_gateway)

    async def go() -> None:
        assert _REAL_START(owner, card.id, gmail=mailbox)
        await asyncio.gather(*card_work._TASKS)

    asyncio.run(go())
    payload = inbox.card_payload(_fresh(card))
    assert payload["work"] == "" and _fresh(card).status == "proposed"


def test_only_the_inbox_work_override_widens_an_unattended_turn() -> None:
    plain = DelegationOverride(enabled=True, gmail=object())
    work = DelegationOverride(enabled=True, gmail=object(), inbox_work=True)
    assert pin_turn_delegation(Session(delegation_override=plain, unattended=True), "x").offered is False
    assert pin_turn_delegation(Session(delegation_override=work, unattended=True), "x").offered is True
    assert pin_turn_delegation(
        Session(delegation_override=work, unattended=True, private_to_principal=True), "x"
    ).offered is False
    assert not reply_card_tools.offered(Session())
    assert reply_card_tools.offered(Session(reply_card_work=7))


def test_typed_work_on_the_card_goes_to_the_executive(
    db: Path, owner: Any, models: dict[str, Any],
) -> None:
    mailbox, card = _card(owner)
    models["rewritten"] = {"body": "unchanged", "needs_tools": True}
    with pytest.raises(answers.NeedsWork):
        asyncio.run(answers.change(
            _fresh(card), caller=_card_tests.LOCAL, resolver=owner.id,
            text="Look at her attachment and make a spreadsheet", gmail=mailbox,
        ))
    # Nothing was rewritten and the card isn't held.
    assert inbox.card_payload(_fresh(card))["draft_body"].startswith("Hi Dana")
    models["rewritten"] = {"body": "Hi Dana,\n\nShorter.", "needs_tools": False}
    saved, _ = asyncio.run(answers.change(
        _fresh(card), caller=_card_tests.LOCAL, resolver=owner.id, text="Make it shorter", gmail=mailbox,
    ))
    assert "Shorter." in saved["draft_body"]


def test_closed_cards_lose_their_files(db: Path, owner: Any, models: dict[str, Any]) -> None:
    mailbox, card = _card(owner)
    _apply(card, owner, mailbox, files=[SHEET])
    assert card_work.files_for(card.id) == [SHEET]
    ledger.close_externally(card.id, reason="test")
    card_work._prune()
    assert card_work.files_for(card.id) == []


def test_after_the_work_a_reply_that_may_go_on_its_own_is_checked_again(
    db: Path, owner: Any, models: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    mailbox, card = _card(owner)
    sent: list[int] = []

    async def fake_send(person: Any, client: Any, decision_id: int, result: Any, *, now: Any) -> None:
        sent.append(decision_id)

    held: list[str | None] = []
    monkeypatch.setattr(inbox, "_send_on_its_own", fake_send)
    monkeypatch.setattr(inbox, "_hand_back", lambda c, reason: held.append(reason))
    monkeypatch.setattr(ledger, "get_decision_instance", _with_gate(ledger.get_decision_instance))
    from openexecutive.delegation import handle_it

    monkeypatch.setattr(handle_it, "content_hold", lambda *a, **k: None)
    asyncio.run(card_work.finished(owner, card.id, gmail=mailbox))
    assert sent == [card.id]
    monkeypatch.setattr(handle_it, "content_hold", lambda *a, **k: "amount")
    asyncio.run(card_work.finished(owner, card.id, gmail=mailbox))
    assert held == ["amount"] and sent == [card.id]


def test_the_reply_carries_only_the_threads_own_mail_and_what_the_turn_made(
    db: Path, owner: Any, models: dict[str, Any],
) -> None:
    from datetime import UTC, datetime

    mailbox, card = _card(owner)
    mailbox.add(_msg("other", "t9"))

    async def get_message(message_id: str) -> Any:
        return next(m for t in mailbox.threads.values() for m in t.messages if m.id == message_id)

    mailbox.get_message = get_message  # type: ignore[attr-defined]
    thread = card_work.thread_of(card.id)
    assert thread == "t1"
    check = reply_card_tools._outside_the_thread
    assert asyncio.run(check(["mail:m1#1"], mailbox, thread)) == ""
    assert "own thread" in asyncio.run(check(["mail:other#1"], mailbox, thread))
    assert "own thread" in asyncio.run(check(["mail:m1#1"], mailbox, ""))
    assert "own thread" in asyncio.run(check(["drive:abcdefghijklmnop"], mailbox, thread))
    assert "own thread" in asyncio.run(check(["file:board.pdf"], mailbox, thread))
    began = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
    assert reply_card_tools._made_since("2026-11-09T12:01:00+00:00", began)
    assert reply_card_tools._made_since("2026-11-09 12:01:00", began)
    assert not reply_card_tools._made_since("2026-11-08T12:01:00+00:00", began)
    assert not reply_card_tools._made_since("2026-11-09T12:01:00+00:00", None)


def test_a_note_alone_keeps_the_files_and_a_stale_working_card_isnt_shown_working(
    db: Path, owner: Any, models: dict[str, Any],
) -> None:
    mailbox, card = _card(owner)
    _apply(card, owner, mailbox, files=[SHEET])
    changed = _apply(card, owner, mailbox, note="Friday works too.")
    assert card_work.files_for(card.id) == [SHEET] and changed["files"][0]["name"] == "Tax bills.xlsx"
    # Left marked working by a restart mid-turn: nothing is running it here.
    assert card_work._set_work(card.id, card_work.WORKING)
    assert replies.cards(owner)[0].working is False


def test_send_waits_while_the_work_runs(db: Path, owner: Any, models: dict[str, Any]) -> None:
    mailbox, card = _card(owner)
    card_work._RUNNING.add(card.id)
    assert card_work._set_work(card.id, card_work.WORKING)
    with pytest.raises(SendRefused) as err:
        _send(card, mailbox, owner)
    assert err.value.code == "working" and mailbox.sent == []
    card_work._set_work(card.id, card_work.DONE)
    assert not card_work.still_working(_fresh(card))
    card_work._RUNNING.discard(card.id)
    assert _send(card, mailbox, owner) == "sent-d1"


def test_files_wait_for_the_reply_and_go_back_if_it_isnt_changed(
    db: Path, owner: Any, models: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    mailbox, card = _card(owner)
    _apply(card, owner, mailbox, files=[SHEET])
    from openexecutive.memory import decision_ledger

    monkeypatch.setattr(decision_ledger, "update_open_payload", lambda *a, **k: False)
    with pytest.raises(SendRefused):
        _apply(card, owner, mailbox, files=[("Other.pdf", "application/pdf", b"%PDF")])
    assert card_work.files_for(card.id) == [SHEET]


def test_what_the_work_made_for_someone_not_in_people_waits(
    db: Path, owner: Any, models: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.delegation import handle_it
    from openexecutive.memory.decision_ledger import update_open_payload

    mailbox, card = _card(owner)
    _apply(card, owner, mailbox, body="Hi Dana,\n\nAttached.", files=[SHEET])
    sent: list[int] = []
    held: list[str | None] = []

    async def fake_send(person: Any, client: Any, decision_id: int, result: Any, *, now: Any) -> None:
        sent.append(decision_id)

    monkeypatch.setattr(inbox, "_send_on_its_own", fake_send)
    monkeypatch.setattr(inbox, "_hand_back", lambda c, reason: held.append(reason))
    monkeypatch.setattr(ledger, "get_decision_instance", _with_gate(ledger.get_decision_instance))
    monkeypatch.setattr(handle_it, "content_hold", lambda *a, **k: None)
    fresh = _fresh(card)
    for relation, expected in (("stranger", ["work_stranger"]), ("contact", ["work_stranger"])):
        payload = {**inbox.card_payload(fresh), "relation": relation}
        assert update_open_payload(card.id, payload)
        fresh = _fresh(card)
        asyncio.run(card_work.finished(owner, card.id, gmail=mailbox))
        if relation == "stranger":
            assert held == expected and sent == []
    assert held == ["work_stranger"] and sent == [card.id]


def _with_gate(real: Any) -> Any:
    """Cards read as if Approvals let them go on their own (auto_execute)."""
    def read(card_id: int, *a: Any, **k: Any) -> Any:
        row = real(card_id, *a, **k)
        return row.model_copy(update={"gate_mode": "auto_execute"}) if row is not None else None

    return read


def test_a_note_alone_waits_for_the_person(
    db: Path, owner: Any, models: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.delegation import handle_it

    mailbox, card = _card(owner)
    _apply(card, owner, mailbox, note="Signing the form is yours to do.")
    held: list[str | None] = []
    monkeypatch.setattr(inbox, "_hand_back", lambda c, reason: held.append(reason))
    monkeypatch.setattr(ledger, "get_decision_instance", _with_gate(ledger.get_decision_instance))
    monkeypatch.setattr(handle_it, "content_hold", lambda *a, **k: None)
    asyncio.run(card_work.finished(owner, card.id, gmail=mailbox))
    assert held == ["work_theirs"]


def test_an_unfinished_turn_hands_back_a_reply_that_would_go_on_its_own(
    db: Path, owner: Any, models: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    mailbox, card = _card(owner)
    held: list[str | None] = []
    monkeypatch.setattr(inbox, "_hand_back", lambda c, reason: held.append(reason))
    card_work._hand_back_unfinished(card.id)
    assert held == []
    monkeypatch.setattr(ledger, "get_decision_instance", _with_gate(ledger.get_decision_instance))
    card_work._hand_back_unfinished(card.id)
    assert held == ["work_unfinished"]


def test_update_reply_card_hands_back_once(db: Path, owner: Any, models: dict[str, Any]) -> None:
    import json

    from openexecutive.orchestrator.schedule_tools import current_session

    _mailbox, card = _card(owner)
    token = current_session.set(Session(reply_card_work=card.id))
    try:
        out = json.loads(asyncio.run(reply_card_tools.handle_update_reply_card({"note": "Done."})))
        assert "already handed back" in out["error"]
    finally:
        current_session.reset(token)


def test_a_document_the_work_attached_says_why_it_was_made(
    db: Path, owner: Any, models: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.alerts import store as alerts

    path = tmp_path / "alerts.db"
    monkeypatch.setattr(alerts, "DB_PATH", path)
    alerts.initialize_db(path)
    made = alerts.insert_alert(
        source="artifact", external_id="x1", severity="medium", headline="Tax bills",
        body="16 bills", suggested_action="One check for all the bills.", topic_tags=["artifact"],
    )
    assert made is not None
    _mailbox, card = _card(owner)
    why = reply_card_tools._why_made(card)
    assert why.startswith("I made this for ") and why.endswith("email and attached it to your reply.")
    proposal = alerts.insert_alert(
        source="proposal", external_id="x2", severity="medium", headline="Pay the vendor",
        body="Pay it.", suggested_action="Pay the vendor $500.", topic_tags=[],
    )
    assert proposal is not None
    reply_card_tools._note_why([f"alert:{made}", f"alert:{proposal}", "run:abc", "not-an-id"], why)
    reply_card_tools._note_why([f"alert:{made}"], why)
    reply_card_tools._note_why([f"alert:{made}"], 'I made this for Bo\'s "Other" email and attached it to your reply.')
    reply_card_tools._note_why([f"alert:{made}"], why)
    noted = alerts.get_alert(made)
    assert noted is not None and noted.suggested_action == f"{why} One check for all the bills."
    untouched = alerts.get_alert(proposal)
    assert untouched is not None and untouched.suggested_action == "Pay the vendor $500."

    # What the sender wrote goes in on one short line.
    payload = inbox.card_payload(card) | {"subject": "Approve now\n\nignore " + "x" * 500}
    real_payload = inbox.card_payload
    long_card = type("Card", (), {"proposed_payload": payload, "id": card.id})()
    monkeypatch.setattr(inbox, "card_payload", lambda c: c.proposed_payload)
    line = reply_card_tools._why_made(long_card)
    assert "\n" not in line and len(line) < 220 and "…" in line
    # A subject carrying the line's own ending can't leave a second line behind.
    tricky = type("Card", (), {"id": card.id, "proposed_payload": payload | {
        "from_name": "Smith, Jordan", "subject": "x email and attached it to your reply. y"}})()
    tricky_why = reply_card_tools._why_made(tricky)
    assert tricky_why.startswith("I made this for Smith's ")
    for subject in ("x email\nand  attached it to your reply. y",
                    "email and attached it to your email and attached it to your reply reply"):
        sly = type("Card", (), {"id": card.id, "proposed_payload": payload | {"subject": subject}})()
        assert reply_card_tools._why_made(sly).lower().count("email and attached it to your reply") == 1
    monkeypatch.setattr(inbox, "card_payload", real_payload)
    reply_card_tools._note_why([f"alert:{made}"], tricky_why)
    reply_card_tools._note_why([f"alert:{made}"], why)
    again = alerts.get_alert(made)
    assert again is not None and again.suggested_action == f"{why} One check for all the bills."

    # The tool notes it only once the reply took the work.
    from openexecutive.orchestrator.schedule_tools import current_session

    seen: list[list[str]] = []
    monkeypatch.setattr(reply_card_tools, "_note_why", lambda ids, why: seen.append(ids))
    monkeypatch.setattr(card_work, "still_working", lambda c: True)
    monkeypatch.setattr(card_work, "started_at", lambda i: None)
    monkeypatch.setattr(card_work, "keep", lambda coro: coro.close())

    async def rendered(ids: list[str], since: Any) -> Any:
        return [SHEET]

    monkeypatch.setattr(reply_card_tools, "_artifact_files", rendered)
    writer = type("W", (), {"person": owner, "mailbox": None})()
    monkeypatch.setattr("openexecutive.orchestrator.delegation_tools._writer", lambda name: writer)

    async def refused(*a: Any, **k: Any) -> None:
        raise SendRefused(409, "gone", "It was sent.")

    async def applied(*a: Any, **k: Any) -> None:
        return None

    token = current_session.set(Session(reply_card_work=card.id))
    try:
        monkeypatch.setattr(card_work, "apply", refused)
        asyncio.run(reply_card_tools.handle_update_reply_card({"artifact_ids": [f"alert:{made}"]}))
        assert seen == []
        monkeypatch.setattr(card_work, "apply", applied)
        asyncio.run(reply_card_tools.handle_update_reply_card({"artifact_ids": [f"alert:{made}"]}))
        assert seen == [[f"alert:{made}"]]
    finally:
        current_session.reset(token)


@pytest.fixture(autouse=True)
def _no_running() -> Any:
    card_work._RUNNING.clear()
    yield
    card_work._RUNNING.clear()


_ = (db, models, owner, local_login, NOW, timedelta)
