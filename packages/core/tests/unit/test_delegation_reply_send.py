"""Send on your tap (delegation/reply_send.py): the one path that sends, only
the exact draft, only for the person it was written for, and never twice."""
from __future__ import annotations

import ast
import asyncio
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

import openexecutive
from openexecutive.api.caller import Caller
from openexecutive.delegation import drafts, inbox
from openexecutive.delegation.gmail import GmailAuthError, GmailError, GmailRateLimited
from openexecutive.delegation.reply_send import SendRefused, send_approved_reply
from openexecutive.delegation.settings import set_enabled
from openexecutive.memory import decision_ledger as ledger

from . import test_delegation_inbox as _inbox_tests
from .test_delegation_inbox import DANA, NOW, OWNER, FakeInbox, _ledger, _msg, _scan

# Reuse the inbox tests' fixtures (an isolated DB, the owner with both
# switches on, the two scripted model calls) under the same names.
db = _inbox_tests.db
owner = _inbox_tests.owner
models = _inbox_tests.models

LOCAL = Caller("open", "")
SIGNED_OWNER = Caller("user", OWNER)


@pytest.fixture(autouse=True)
def local_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """``make dev``'s local login unless a test says otherwise."""
    monkeypatch.setenv("OE_LOCAL_LOGIN", "1")
    monkeypatch.delenv("OE_PUBLIC_DEPLOYMENT", raising=False)
    monkeypatch.delenv("CALLER_ASSERTION_PUBLIC_KEYS", raising=False)


def _card(owner: Any) -> tuple[FakeInbox, Any]:
    mailbox = FakeInbox()
    mailbox.add(_msg("m1", "t1"))
    _scan(owner, mailbox)
    return mailbox, inbox.open_cards(owner.id)[0]


def _send(card: Any, mailbox: FakeInbox, owner: Any, *, caller: Caller = LOCAL,
          confirm: dict[str, Any] | None = None, resolver: int | None = -1) -> str:
    return asyncio.run(send_approved_reply(
        card, caller=caller, resolver=owner.id if resolver == -1 else resolver,
        confirm=confirm, gmail=mailbox, now=NOW + timedelta(minutes=30),
    ))


def _refused(card: Any, mailbox: FakeInbox, owner: Any, **kw: Any) -> SendRefused:
    with pytest.raises(SendRefused) as err:
        _send(card, mailbox, owner, **kw)
    return err.value


def _status(card: Any) -> str:
    row = ledger.get_decision_instance(card.id)
    assert row is not None
    return row.status


# ── sending ───────────────────────────────────────────────────────────────────


def test_it_sends_that_draft_and_records_it(db: Path, owner: Any, models: dict[str, Any]) -> None:
    mailbox, card = _card(owner)
    assert _send(card, mailbox, owner) == "sent-d1"
    assert mailbox.sent == ["d1"]
    row = ledger.get_decision_instance(card.id)
    assert row is not None and row.status == "approved_unchanged" and row.resolver_person_id == owner.id
    assert row.external_event_id == "sent-d1"
    assert _ledger(db)["m1"] == ("sent", "sent")
    assert drafts.sent_message_ids(owner.id) == {"sent-d1"}
    assert inbox.open_cards(owner.id) == []
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT private_to_principal FROM audit_log WHERE event_type = 'delegation_reply_sent'"
        ).fetchall()
    assert rows == [(1,)]


def test_a_double_tap_sends_once(owner: Any, models: dict[str, Any]) -> None:
    mailbox, card = _card(owner)

    async def both() -> list[Any]:
        return await asyncio.gather(*[
            send_approved_reply(card, caller=LOCAL, resolver=owner.id, gmail=mailbox, now=NOW + timedelta(minutes=30))
            for _ in range(2)
        ], return_exceptions=True)

    results = asyncio.run(both())
    assert mailbox.sent == ["d1"]
    assert sorted(type(r).__name__ for r in results) == ["SendRefused", "str"]
    refused = next(r for r in results if isinstance(r, SendRefused))
    assert refused.code == "already_handled"


def test_an_edited_draft_is_sent_as_edited(owner: Any, models: dict[str, Any]) -> None:
    mailbox, card = _card(owner)
    mailbox.edit("d1")
    _send(card, mailbox, owner)
    assert _status(card) == "approved_with_edit"


def test_it_works_while_the_executive_is_paused(owner: Any, models: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.scheduler import pause

    monkeypatch.setattr(pause, "is_paused", lambda *a, **kw: True)
    mailbox, card = _card(owner)
    assert _send(card, mailbox, owner) == "sent-d1"


# ── who may send ──────────────────────────────────────────────────────────────


def test_without_signed_callers_a_server_never_sends(owner: Any, models: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    mailbox, card = _card(owner)
    monkeypatch.delenv("OE_LOCAL_LOGIN")
    assert _refused(card, mailbox, owner).code == "caller_signing_required"
    # Even for the owner's own email: only a signature proves it.
    assert _refused(card, mailbox, owner, caller=Caller("open", OWNER)).code == "caller_signing_required"
    assert mailbox.sent == [] and _status(card) == "proposed"


def test_with_signed_callers_only_the_owner_signed_in_sends(
    owner: Any, models: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    mailbox, card = _card(owner)
    monkeypatch.delenv("OE_LOCAL_LOGIN")
    monkeypatch.setenv("CALLER_ASSERTION_PUBLIC_KEYS", "k1:" + "A" * 43)
    for caller in (Caller("service", ""), Caller("user", "ben@co.example"), Caller("operator", ""), LOCAL):
        assert _refused(card, mailbox, owner, caller=caller).code == "not_yours"
    assert mailbox.sent == []
    assert _send(card, mailbox, owner, caller=SIGNED_OWNER) == "sent-d1"


def test_only_the_person_it_was_written_for(owner: Any, models: dict[str, Any]) -> None:
    mailbox, card = _card(owner)
    assert _refused(card, mailbox, owner, resolver=owner.id + 1).code == "not_yours"
    assert _refused(card, mailbox, owner, resolver=None).code == "not_yours"
    assert mailbox.sent == []


def test_both_switches_gmail_and_no_client_slot(owner: Any, models: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    mailbox, card = _card(owner)
    inbox.set_watch(owner.id, False, updated_by="test")
    assert _refused(card, mailbox, owner).code == "inbox_off"
    inbox.set_watch(owner.id, True, updated_by="test")
    set_enabled(owner.id, False, updated_by="test")
    assert _refused(card, mailbox, owner).code == "inbox_off"
    set_enabled(owner.id, True, updated_by="test")
    monkeypatch.setattr(inbox, "_client_slot_active", lambda: True)
    assert _refused(card, mailbox, owner).code == "client_slot"
    monkeypatch.setattr(inbox, "_client_slot_active", lambda: False)
    mailbox.profile_error = GmailAuthError("revoked")
    assert _refused(card, mailbox, owner).code == "gmail_needs_reconnect"
    assert mailbox.sent == [] and _status(card) == "proposed"


# ── what it sends ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("edit", "code"), [
    ({"to": []}, "no_recipients"),
    ({"cc": [f"p{i}@x.example" for i in range(10)]}, "too_many_recipients"),
    ({"bcc": ["EXEC"]}, "executive_recipient"),
])
def test_the_recipients_are_checked_again(owner: Any, models: dict[str, Any], edit: dict[str, Any], code: str) -> None:
    from openexecutive.config import get_settings

    exec_address = get_settings().exec_email_address.strip().lower()
    mailbox, card = _card(owner)
    mailbox.edit("d1", **{k: [exec_address if a == "EXEC" else a for a in v] for k, v in edit.items()})
    assert _refused(card, mailbox, owner, confirm={"recipients": ["anything"]}).code == code
    assert mailbox.sent == [] and _status(card) == "proposed"


def test_it_must_be_from_them(owner: Any, models: dict[str, Any]) -> None:
    mailbox, card = _card(owner)
    mailbox.drafts["d1"].message.from_addr = "someone@else.example"
    assert _refused(card, mailbox, owner).code == "not_from_you"


def test_new_recipients_need_a_second_yes(owner: Any, models: dict[str, Any]) -> None:
    mailbox, card = _card(owner)
    mailbox.edit("d1", cc=["sam@northpeak.example"])
    first = _refused(card, mailbox, owner, confirm={"recipients": [DANA]})
    assert first.code == "confirm" and first.extra["reasons"] == ["recipients_changed"]
    assert first.extra["recipients"] == [DANA, "sam@northpeak.example"]
    assert mailbox.sent == []
    # Confirming a list that isn't what the draft holds is not a yes.
    assert _refused(card, mailbox, owner, confirm={"recipients": [DANA, "x@y.example"]}).code == "confirm"
    _send(card, mailbox, owner, confirm={"recipients": first.extra["recipients"]})
    assert mailbox.sent == ["d1"] and _status(card) == "approved_with_edit"


def test_a_newer_message_needs_a_second_yes(owner: Any, models: dict[str, Any]) -> None:
    mailbox, card = _card(owner)
    mailbox.add(_msg("m2", "t1", minutes_ago=-5, text="Actually, Monday works too."))
    first = _refused(card, mailbox, owner)
    assert first.code == "confirm" and first.extra["reasons"] == ["thread_moved_on"]
    _send(card, mailbox, owner, confirm={"thread_moved_on": True})
    assert mailbox.sent == ["d1"]


def test_a_draft_gone_or_answered_closes_the_card(owner: Any, models: dict[str, Any]) -> None:
    mailbox, card = _card(owner)
    del mailbox.drafts["d1"]
    assert _refused(card, mailbox, owner).code == "draft_gone"
    assert _status(card) == "closed_externally"

    mailbox, card = _card_in_thread(owner, "t2")
    mailbox.add(_msg("mine", "t2", sender=OWNER, name="Olivia", minutes_ago=-5, labels=("SENT",), to=[DANA]))
    assert _refused(card, mailbox, owner).code == "you_replied"
    assert _status(card) == "closed_externally" and mailbox.sent == []


def _card_in_thread(owner: Any, thread: str) -> tuple[FakeInbox, Any]:
    mailbox = FakeInbox()
    mailbox.add(_msg("m-" + thread, thread, sender="ben@northpeak.example"))
    _scan(owner, mailbox, now=NOW + timedelta(minutes=1))
    card = next(c for c in inbox.open_cards(owner.id) if inbox.card_payload(c)["thread_id"] == thread)
    return mailbox, card


# ── when Gmail fails ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(("error", "status", "code"), [
    (GmailError("gmail POST returned 400"), 502, "gmail_error"),
    (GmailRateLimited("429"), 429, "rate_limited"),
    (GmailAuthError("revoked"), 409, "gmail_needs_reconnect"),
])
def test_a_send_gmail_refused_hands_the_card_back(
    owner: Any, models: dict[str, Any], error: Exception, status: int, code: str
) -> None:
    mailbox, card = _card(owner)
    mailbox.send_error = error
    refused = _refused(card, mailbox, owner)
    assert (refused.status, refused.code) == (status, code)
    assert _status(card) == "proposed" and mailbox.sent == []
    mailbox.send_error = None
    assert _send(card, mailbox, owner) == "sent-d1"


def test_an_unconfirmed_send_that_did_not_go_is_handed_back_by_the_reconciler(
    db: Path, owner: Any, models: dict[str, Any]
) -> None:
    mailbox, card = _card(owner)
    mailbox.send_error = GmailError("gmail POST returned 503", maybe_done=True)
    assert _refused(card, mailbox, owner).code == "send_unconfirmed"
    assert _status(card) == "executing"
    # The draft is still there and nothing was sent. Gmail can take a moment
    # to settle a send, so one look isn't enough...
    assert asyncio.run(inbox.reconcile(owner, mailbox, now=NOW + timedelta(minutes=31), own={OWNER})) == 0
    assert _status(card) == "executing"
    # ...the second one hands it back to the person.
    assert asyncio.run(inbox.reconcile(owner, mailbox, now=NOW + timedelta(minutes=36), own={OWNER})) == 0
    assert _status(card) == "proposed"
    assert inbox.ledger_flags(owner.id, ["m1"]) == {"m1": ["send_failed"]}


def test_an_unconfirmed_send_whose_draft_lingers_but_went_is_not_handed_back(
    owner: Any, models: dict[str, Any]
) -> None:
    mailbox, card = _card(owner)
    mailbox.send_error = GmailError("gmail POST returned 503", maybe_done=True)
    assert _refused(card, mailbox, owner).code == "send_unconfirmed"
    # Gmail delivered it; the draft hasn't disappeared yet.
    mailbox.add(_msg("sent-late", "t1", sender=OWNER, name="Olivia", minutes_ago=-5, labels=("SENT",), to=[DANA]))
    assert asyncio.run(inbox.reconcile(owner, mailbox, now=NOW + timedelta(minutes=31), own={OWNER})) == 1
    assert _status(card) == "approved_unchanged"


def test_an_unconfirmed_send_that_went_is_recorded_by_the_reconciler(
    db: Path, owner: Any, models: dict[str, Any]
) -> None:
    mailbox, card = _card(owner)
    mailbox.send_then_fail = GmailError("gmail POST failed: ReadTimeout", maybe_done=True)
    assert _refused(card, mailbox, owner).code == "send_unconfirmed"
    assert mailbox.sent == ["d1"] and _status(card) == "executing"
    assert asyncio.run(inbox.reconcile(owner, mailbox, now=NOW + timedelta(minutes=31), own={OWNER})) == 1
    row = ledger.get_decision_instance(card.id)
    assert row is not None and row.status == "approved_unchanged" and row.external_event_id == "sent-d1"
    assert _ledger(db)["m1"] == ("sent", "sent")


def test_a_draft_edited_while_sending_is_not_sent(owner: Any, models: dict[str, Any]) -> None:
    """Everything is checked against the draft as it was read; one edited in
    Gmail between that read and the send is refused, not sent unchecked."""
    mailbox, card = _card(owner)
    reads = 0
    get_draft = mailbox.get_draft

    async def edited_on_the_second_read(draft_id: str) -> Any:
        nonlocal reads
        reads += 1
        if reads == 2:
            mailbox.edit(draft_id, bcc=["someone@else.example"])
        return await get_draft(draft_id)

    mailbox.get_draft = edited_on_the_second_read  # type: ignore[method-assign]
    assert _refused(card, mailbox, owner).code == "draft_changed"
    assert mailbox.sent == [] and _status(card) == "proposed"
    # Looked at again, the new recipient needs a yes like any other change.
    confirm = _refused(card, mailbox, owner)
    assert confirm.code == "confirm" and "someone@else.example" in confirm.extra["recipients"]


def test_the_reconciler_leaves_a_card_being_sent_alone(owner: Any, models: dict[str, Any]) -> None:
    mailbox, card = _card(owner)
    assert ledger.claim_for_execution(card.id, resolver_person_id=owner.id)
    inbox.SENDING.add(card.id)
    try:
        del mailbox.drafts["d1"]  # would read as deleted, if it looked
        assert asyncio.run(inbox.reconcile(owner, mailbox, now=NOW + timedelta(minutes=31), own={OWNER})) == 0
        assert _status(card) == "executing"
    finally:
        inbox.SENDING.discard(card.id)


# ── nothing else can reach it ─────────────────────────────────────────────────


_ROOT = Path(openexecutive.__file__).parent


def _trees() -> list[tuple[str, ast.AST]]:
    return [
        (path.relative_to(_ROOT).as_posix(), ast.parse(path.read_text(), filename=str(path)))
        for path in _ROOT.rglob("*.py")
    ]


def _uses(name: str) -> set[str]:
    """Files under openexecutive/ that name ``name`` in code (a call, an
    attribute, an imported name), relative to the package."""
    found: set[str] = set()
    for rel, tree in _trees():
        for node in ast.walk(tree):
            if (
                (isinstance(node, ast.Attribute) and node.attr == name)
                or (isinstance(node, ast.Name) and node.id == name)
                or (isinstance(node, ast.ImportFrom) and any(a.name == name for a in node.names))
            ):
                found.add(rel)
    return found


def _importers(module: str) -> set[str]:
    """Files under openexecutive/ that import ``module`` (openexecutive.a.b),
    however it is spelled."""
    package, _, leaf = module.rpartition(".")
    found: set[str] = set()
    for rel, tree in _trees():
        for node in ast.walk(tree):
            if (
                (isinstance(node, ast.ImportFrom) and node.module == module)
                or (isinstance(node, ast.ImportFrom) and node.module == package
                    and any(a.name == leaf for a in node.names))
                or (isinstance(node, ast.Import) and any(a.name == module for a in node.names))
            ):
                found.add(rel)
    return found


def test_only_the_send_path_sends_and_only_the_approve_route_reaches_it() -> None:
    assert _uses("send_draft") == {"delegation/reply_send.py"}
    assert _uses("send_approved_reply") == {"api/routes/decisions.py"}
    assert _importers("openexecutive.delegation.reply_send") == {"api/routes/decisions.py"}


def test_only_dismiss_deletes_a_draft() -> None:
    assert _uses("delete_draft") == {"delegation/replies.py"}
    assert _importers("openexecutive.delegation.replies") == {"api/routes/decisions.py", "api/routes/delegation.py"}
