"""Send under a chat draft (reply_send.send_chat_draft): the person's own tap
sends the draft chat wrote as them, exactly as written, through the same
checks as a reply card's Send."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from openexecutive.api.caller import Caller
from openexecutive.delegation import drafts
from openexecutive.delegation.gmail import DraftSpec, GmailError
from openexecutive.delegation.reply_send import SendRefused, send_chat_draft
from openexecutive.delegation.settings import set_enabled
from openexecutive.people import store as people_store

from . import test_delegation_inbox as _inbox_tests
from .test_delegation_inbox import DANA, NOW, OWNER, FakeInbox, _msg

db = _inbox_tests.db
owner = _inbox_tests.owner

LOCAL = Caller("open", "")
SIGNED_OWNER = Caller("user", OWNER)


@pytest.fixture(autouse=True)
def local_login(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OE_LOCAL_LOGIN", "1")
    monkeypatch.delenv("OE_PUBLIC_DEPLOYMENT", raising=False)
    monkeypatch.delenv("CALLER_ASSERTION_PUBLIC_KEYS", raising=False)


def _chat_draft(owner: Any, *, reply: bool = False, to: list[str] | None = None,
                source: str = drafts.SOURCE_CHAT) -> tuple[FakeInbox, int]:
    """A draft chat saved in the owner's mailbox, and its row."""
    mailbox = FakeInbox()
    if reply:
        mailbox.add(_msg("m1", "t1"))
    made = asyncio.run(mailbox.create_draft(DraftSpec(
        to=to or [DANA], subject="Friday", body="Friday at 10 works.\n\nOlivia",
        thread_id="t1" if reply else None,
    )))
    row = drafts.record(
        owner.id, source=source, thread_id="t1" if reply else None,
        draft_id=made.draft_id, message_id=made.message_id, now=NOW,
    )
    return mailbox, row


def _send(owner: Any, row: int, mailbox: FakeInbox, *, caller: Caller = LOCAL,
          confirm: dict[str, Any] | None = None) -> str:
    return asyncio.run(send_chat_draft(
        owner, row, caller=caller, confirm=confirm, gmail=mailbox, now=NOW + timedelta(minutes=10),
    ))


def _refused(owner: Any, row: int, mailbox: FakeInbox, **kw: Any) -> SendRefused:
    with pytest.raises(SendRefused) as err:
        _send(owner, row, mailbox, **kw)
    return err.value


def test_it_sends_the_draft_as_written_and_records_it(db: Path, owner: Any) -> None:
    mailbox, row = _chat_draft(owner)
    assert _send(owner, row, mailbox) == "sent-d1"
    assert mailbox.sent == ["d1"]
    assert drafts.sent_message_ids(owner.id) == {"sent-d1"}
    # Sent: it no longer waits in the briefs, and a second tap sends nothing.
    assert drafts.count_waiting(owner.id, NOW - timedelta(days=1)) == 0
    assert _refused(owner, row, mailbox).code == "already_sent"
    assert mailbox.sent == ["d1"]


def test_a_double_tap_sends_once(owner: Any) -> None:
    mailbox, row = _chat_draft(owner)

    async def both() -> list[Any]:
        return await asyncio.gather(*[
            send_chat_draft(owner, row, caller=LOCAL, gmail=mailbox, now=NOW + timedelta(minutes=10))
            for _ in range(2)
        ], return_exceptions=True)

    results = asyncio.run(both())
    assert mailbox.sent == ["d1"]
    assert sorted(type(r).__name__ for r in results) == ["SendRefused", "str"]


def test_only_its_own_person_and_only_a_chat_draft(owner: Any) -> None:
    mailbox, row = _chat_draft(owner)
    ben = people_store.get_person(people_store.upsert_person(full_name="Ben Team", email="ben@co.example"))
    assert _refused(ben, row, mailbox).code in ("not_yours", "draft_not_found")
    assert _refused(owner, row + 99, mailbox).code == "draft_not_found"
    inbox_box, inbox_row = _chat_draft(owner, source=drafts.SOURCE_INBOX)
    assert _refused(owner, inbox_row, inbox_box).code == "draft_not_found"
    assert mailbox.sent == [] and inbox_box.sent == []


def test_without_signed_callers_a_server_never_sends(owner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    mailbox, row = _chat_draft(owner)
    monkeypatch.delenv("OE_LOCAL_LOGIN")
    assert _refused(owner, row, mailbox).code == "caller_signing_required"
    assert _refused(owner, row, mailbox, caller=Caller("open", OWNER)).code == "caller_signing_required"
    monkeypatch.setenv("CALLER_ASSERTION_PUBLIC_KEYS", "k1:" + "A" * 43)
    for caller in (Caller("service", ""), Caller("user", "ben@co.example"), LOCAL):
        assert _refused(owner, row, mailbox, caller=caller).code == "not_yours"
    assert mailbox.sent == []
    assert _send(owner, row, mailbox, caller=SIGNED_OWNER) == "sent-d1"


def test_act_as_me_must_be_on(owner: Any) -> None:
    mailbox, row = _chat_draft(owner)
    set_enabled(owner.id, False, updated_by="test")
    assert _refused(owner, row, mailbox).code == "act_as_me_off"
    assert mailbox.sent == []


def test_a_draft_changed_in_the_mailbox_is_theirs_to_send_there(owner: Any) -> None:
    mailbox, row = _chat_draft(owner)
    mailbox.edit("d1")
    assert _refused(owner, row, mailbox).code == "draft_changed"
    assert mailbox.sent == []


def test_a_draft_edited_between_the_checks_and_the_send_is_not_sent(owner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    mailbox, row = _chat_draft(owner)
    real = mailbox.get_draft
    reads = {"n": 0}

    async def get_draft(draft_id: str) -> Any:
        reads["n"] += 1
        if reads["n"] == 2:
            mailbox.edit(draft_id, to=["eve@elsewhere.example"])
        return await real(draft_id)

    monkeypatch.setattr(mailbox, "get_draft", get_draft)
    assert _refused(owner, row, mailbox).code == "draft_changed"
    assert mailbox.sent == []
    # Handed back: it waits for them again.
    assert drafts.count_waiting(owner.id, NOW - timedelta(days=1)) == 1


def test_a_deleted_draft_is_gone(owner: Any) -> None:
    mailbox, row = _chat_draft(owner)
    asyncio.run(mailbox.delete_draft("d1"))
    assert _refused(owner, row, mailbox).code == "draft_gone"
    assert drafts.count_waiting(owner.id, NOW - timedelta(days=1)) == 0


def test_recipients_are_checked_like_a_card(owner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.config import get_settings

    exec_address = get_settings().exec_email_address
    mailbox, row = _chat_draft(owner, to=[exec_address])
    assert _refused(owner, row, mailbox).code == "executive_recipient"
    many = [f"p{i}@x.example" for i in range(11)]
    mailbox, row = _chat_draft(owner, to=many)
    assert _refused(owner, row, mailbox).code == "too_many_recipients"
    mailbox, row = _chat_draft(owner)
    mailbox.drafts["d1"].message.from_addr = "someone@else.example"
    assert _refused(owner, row, mailbox).code == "not_from_you"


def test_a_reply_they_answered_themselves_is_not_sent(owner: Any) -> None:
    mailbox, row = _chat_draft(owner, reply=True)
    mailbox.add(_msg("m2", "t1", sender=OWNER, labels=("SENT",), minutes_ago=-5, text="Done."))
    assert _refused(owner, row, mailbox).code == "you_replied"
    assert mailbox.sent == []


def test_a_reply_whose_conversation_moved_on_needs_a_second_yes(owner: Any) -> None:
    mailbox, row = _chat_draft(owner, reply=True)
    mailbox.add(_msg("m2", "t1", minutes_ago=-5, text="Actually, can we do 11?"))
    refused = _refused(owner, row, mailbox)
    assert refused.code == "confirm" and refused.extra["reasons"] == ["thread_moved_on"]
    assert mailbox.sent == []
    assert _send(owner, row, mailbox, confirm={"thread_moved_on": True}) == "sent-d1"


def test_a_reply_with_nothing_new_sends_on_one_tap(owner: Any) -> None:
    mailbox, row = _chat_draft(owner, reply=True)
    assert _send(owner, row, mailbox) == "sent-d1"


def test_an_unconfirmed_send_is_never_sent_again(owner: Any) -> None:
    mailbox, row = _chat_draft(owner)
    mailbox.send_error = GmailError("timeout", maybe_done=True)
    assert _refused(owner, row, mailbox).code == "send_unconfirmed"
    mailbox.send_error = None
    assert _refused(owner, row, mailbox).code == "draft_gone"
    assert mailbox.sent == []


def test_a_refused_send_hands_it_back(owner: Any) -> None:
    mailbox, row = _chat_draft(owner)
    mailbox.send_error = GmailError("boom")
    assert _refused(owner, row, mailbox).code == "gmail_error"
    mailbox.send_error = None
    assert _send(owner, row, mailbox) == "sent-d1"


# ── the route ─────────────────────────────────────────────────────────────────


def test_the_route_sends_the_callers_own_draft_only(owner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from openexecutive.api.routes import delegation as route
    from openexecutive.delegation import gmail as gmail_module

    mailbox, row = _chat_draft(owner)
    monkeypatch.setattr(gmail_module, "gmail_for", lambda email: mailbox)
    app = FastAPI()
    app.include_router(route.router)
    client = TestClient(app)
    headers = {"x-caller-email": OWNER}

    resp = client.post(f"/delegation/drafts/{row + 99}/send", json={}, headers=headers)
    assert resp.status_code == 404 and resp.json()["detail"]["code"] == "draft_not_found"
    resp = client.post(f"/delegation/drafts/{row}/send", json={"extra": 1}, headers=headers)
    assert resp.status_code == 422
    resp = client.post(f"/delegation/drafts/{row}/send", json={}, headers=headers)
    assert resp.status_code == 200 and resp.json() == {"sent": True}
    assert mailbox.sent == ["d1"]
    resp = client.post(f"/delegation/drafts/{row}/send", json={}, headers=headers)
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "already_sent"
