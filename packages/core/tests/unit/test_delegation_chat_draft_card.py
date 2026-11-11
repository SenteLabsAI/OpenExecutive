"""The card under a chat draft (delegation.chat_draft_card): it shows the
draft as it is in the person's Drafts and changes it there in place, by hand
or by asking, and never touches a draft they changed in their mailbox."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from openexecutive.api.caller import Caller
from openexecutive.delegation import chat_draft_card, drafts
from openexecutive.delegation.reply_send import SendRefused, send_chat_draft
from openexecutive.orchestrator.action_chips import _draft_fields, drop_replaced

from . import test_delegation_chat_draft_send as _send_tests
from .test_delegation_inbox import NOW, FakeInbox

db = _send_tests.db
owner = _send_tests.owner
local_login = _send_tests.local_login
_chat_draft = _send_tests._chat_draft

LOCAL = Caller("open", "")
LATER = NOW + timedelta(minutes=5)


def _state(owner: Any, row: int, mailbox: FakeInbox) -> dict[str, Any]:
    return asyncio.run(chat_draft_card.state(owner, row, caller=LOCAL, gmail=mailbox, now=LATER))


def _refused(coro: Any) -> SendRefused:
    with pytest.raises(SendRefused) as err:
        asyncio.run(coro)
    return err.value


def test_the_card_shows_the_draft_as_it_is_in_their_drafts(db: Path, owner: Any) -> None:
    mailbox, row = _chat_draft(owner)
    shown = _state(owner, row, mailbox)
    assert shown["status"] == chat_draft_card.OPEN
    assert shown["body"] == "Friday at 10 works.\n\nOlivia"
    assert shown["subject"] == "Friday"


def test_their_own_words_replace_the_draft_and_send_sends_them(db: Path, owner: Any) -> None:
    mailbox, row = _chat_draft(owner)
    saved = asyncio.run(chat_draft_card.set_text(
        owner, row, "Friday at 11 is better.\n\nOlivia", caller=LOCAL, gmail=mailbox, now=LATER,
    ))
    assert saved["body"] == "Friday at 11 is better.\n\nOlivia"
    assert mailbox.drafts["d1"].message.text == "Friday at 11 is better.\n\nOlivia"
    # The row holds the new version, so Send under the chat still sends it.
    assert drafts.get_row(row).message_id == mailbox.drafts["d1"].message.id
    assert _state(owner, row, mailbox)["status"] == chat_draft_card.OPEN
    asyncio.run(send_chat_draft(owner, row, caller=LOCAL, gmail=mailbox, now=LATER))
    assert mailbox.sent == ["d1"]


def test_asking_rewrites_it_in_place(db: Path, owner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    mailbox, row = _chat_draft(owner)
    seen: dict[str, Any] = {}

    async def rewrite(person: Any, payload: dict[str, Any], answers: list[Any], **kw: Any) -> Any:
        seen.update(payload=payload, change=kw.get("change"))
        return (
            "Friday at 10 works. Thanks so much!\n\nOlivia",
            {"conversation": [{"kind": "asked", "text": kw["change"]},
                              {"kind": "note", "text": "I made it warmer."}]},
            [],
        )

    monkeypatch.setattr("openexecutive.delegation.answers.rewrite", rewrite)
    saved, new, note = asyncio.run(chat_draft_card.change(
        owner, row, "Make it warmer.", caller=LOCAL, gmail=mailbox, now=LATER,
    ))
    assert seen["change"] == "Make it warmer."
    assert seen["payload"]["draft_body"] == "Friday at 10 works.\n\nOlivia"
    assert note == "I made it warmer."
    assert new == ["Thanks so much!"]
    assert mailbox.drafts["d1"].message.text == saved["body"]
    assert mailbox.sent == []


def test_a_draft_changed_in_the_mailbox_is_never_overwritten(db: Path, owner: Any) -> None:
    mailbox, row = _chat_draft(owner)
    mailbox.edit("d1")
    assert _state(owner, row, mailbox)["status"] == chat_draft_card.CHANGED
    refused = _refused(chat_draft_card.set_text(owner, row, "Mine", caller=LOCAL, gmail=mailbox, now=LATER))
    assert refused.code == "draft_changed"
    assert "update:d1" not in mailbox.calls


def test_only_its_own_signed_person_changes_it(db: Path, owner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    mailbox, row = _chat_draft(owner)
    monkeypatch.delenv("OE_LOCAL_LOGIN")
    refused = _refused(chat_draft_card.set_text(owner, row, "Mine", caller=LOCAL, gmail=mailbox, now=LATER))
    assert refused.code == "caller_signing_required"
    # Reading it needs the same provable caller: it is their own mail.
    assert _refused(chat_draft_card.state(owner, row, caller=LOCAL, gmail=mailbox)).code == "caller_signing_required"
    assert _refused(chat_draft_card.state(owner, row + 99, caller=LOCAL, gmail=mailbox)).code == "draft_not_found"
    assert "update:d1" not in mailbox.calls


def test_a_replaced_version_folds(db: Path, owner: Any) -> None:
    mailbox, first = _chat_draft(owner)
    made = asyncio.run(mailbox.create_draft(mailbox.specs[0]))
    conn_rows = [
        drafts.record(owner.id, source=drafts.SOURCE_CHAT, thread_id=None, draft_id=d, message_id=m,
                      now=NOW, session_id="s1")
        for d, m in (("dx", "mx"), (made.draft_id, made.message_id))
    ]
    drafts.mark_gone(conn_rows[0], LATER)
    # Gone, with a later draft in the same conversation that didn't replace
    # it (another email): just gone.
    assert _state(owner, conn_rows[0], mailbox)["status"] == chat_draft_card.GONE
    # Retired for the newer version: replaced.
    drafts.mark_replaced(conn_rows[0], conn_rows[1])
    assert _state(owner, conn_rows[0], mailbox)["status"] == chat_draft_card.REPLACED
    drafts.mark_gone(first, LATER)
    assert _state(owner, first, mailbox)["status"] == chat_draft_card.GONE


def test_a_draft_written_again_in_one_reply_keeps_one_card() -> None:
    first = {"type": "action_taken", "tool": "ghostwrite_email", **_draft_fields({"draft_row": 68})}
    other = {"type": "action_taken", "tool": "search_my_email"}
    again = {"type": "action_taken", "tool": "ghostwrite_email",
             **_draft_fields({"draft_row": 69, "replaced_rows": [68, 99, "x"]})}
    assert again["draft"]["replaces"] == [68]
    assert drop_replaced([first, other, again]) == [other, again]
    assert drop_replaced([first, other]) == [first, other]


def test_the_routes_read_and_change_the_callers_own_draft(owner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from openexecutive.api.routes import delegation as route
    from openexecutive.delegation import gmail as gmail_module

    mailbox, row = _chat_draft(owner)
    monkeypatch.setattr(gmail_module, "gmail_for", lambda email: mailbox)
    app = FastAPI()
    app.include_router(route.router)
    client = TestClient(app)
    headers = {"x-caller-email": _send_tests.OWNER}

    resp = client.get(f"/delegation/drafts/{row}", headers=headers)
    assert resp.status_code == 200 and resp.json()["status"] == "open"
    assert resp.json()["body"] == "Friday at 10 works.\n\nOlivia"
    assert client.get(f"/delegation/drafts/{row + 99}", headers=headers).status_code == 404
    resp = client.put(f"/delegation/drafts/{row}/text", json={"text": "Friday at 11.\n\nOlivia"}, headers=headers)
    assert resp.status_code == 200 and resp.json()["body"] == "Friday at 11.\n\nOlivia"
    assert client.post(f"/delegation/drafts/{row}/change", json={"text": ""}, headers=headers).status_code == 422
    # What to change: up to 500 characters.
    assert client.post(f"/delegation/drafts/{row}/change", json={"text": "x" * 501}, headers=headers).status_code == 422

    async def rewrite(person: Any, payload: dict[str, Any], answers: list[Any], **kw: Any) -> Any:
        return "Friday at 11 works.\n\nOlivia", {"conversation": [{"kind": "note", "text": "Done."}]}, []

    monkeypatch.setattr("openexecutive.delegation.answers.rewrite", rewrite)
    resp = client.post(f"/delegation/drafts/{row}/change", json={"text": "x" * 500}, headers=headers)
    assert resp.status_code == 200 and resp.json()["note"] == "Done."
    # Someone else's draft reads the same as none at all.
    theirs = drafts.record(owner.id + 1, source=drafts.SOURCE_CHAT, thread_id=None, draft_id="dz",
                           message_id="mz", now=NOW)
    for resp in (
        client.get(f"/delegation/drafts/{theirs}", headers=headers),
        client.put(f"/delegation/drafts/{theirs}/text", json={"text": "Mine"}, headers=headers),
        client.post(f"/delegation/drafts/{theirs}/change", json={"text": "Shorter"}, headers=headers),
    ):
        assert resp.status_code == 404 and resp.json()["detail"]["code"] == "draft_not_found"
    mailbox.edit("d1")
    resp = client.put(f"/delegation/drafts/{row}/text", json={"text": "Mine"}, headers=headers)
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "draft_changed"
    assert resp.json()["detail"]["state"] == {"status": "changed"}
    # Without signed sign-ins the API can't tie the caller to the owner: no
    # read, no change.
    monkeypatch.delenv("OE_LOCAL_LOGIN")
    for resp in (
        client.get(f"/delegation/drafts/{row}", headers=headers),
        client.put(f"/delegation/drafts/{row}/text", json={"text": "Mine"}, headers=headers),
        client.post(f"/delegation/drafts/{row}/change", json={"text": "Shorter"}, headers=headers),
    ):
        assert resp.status_code == 409 and resp.json()["detail"]["code"] == "caller_signing_required"
    assert mailbox.sent == []


def test_send_waits_while_the_card_is_changing_it(db: Path, owner: Any) -> None:
    mailbox, row = _chat_draft(owner)
    chat_draft_card._CHANGING.add(row)
    try:
        refused = _refused(send_chat_draft(owner, row, caller=LOCAL, gmail=mailbox, now=LATER))
    finally:
        chat_draft_card._CHANGING.discard(row)
    assert refused.code == "busy" and mailbox.sent == []


def test_a_draft_with_files_is_changed_in_the_mailbox(db: Path, owner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.delegation.gmail import DraftChanged

    mailbox, row = _chat_draft(owner)
    before = drafts.get_row(row).message_id

    async def not_plain(*_a: Any, **_k: Any) -> str:
        raise DraftChanged("not_plain")

    monkeypatch.setattr(mailbox, "update_draft_text", not_plain)
    refused = _refused(chat_draft_card.set_text(owner, row, "Mine", caller=LOCAL, gmail=mailbox, now=LATER))
    assert refused.code == "draft_not_plain"
    # The row keeps the version Send compares with.
    assert drafts.get_row(row).message_id == before
    assert not drafts.set_version(row, "not-the-version", "x")


def test_an_edit_the_mailbox_kept_the_old_version_of_is_never_called_saved(
    db: Path, owner: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    mailbox, row = _chat_draft(owner)
    before = drafts.get_row(row).message_id

    async def unchanged(*_a: Any, **_k: Any) -> str:
        return before

    monkeypatch.setattr(mailbox, "update_draft_text", unchanged)
    refused = _refused(chat_draft_card.set_text(owner, row, "Mine", caller=LOCAL, gmail=mailbox, now=LATER))
    assert refused.code == "gmail_error"
    assert drafts.get_row(row).message_id == before


def test_delete_removes_it_from_their_drafts(db: Path, owner: Any) -> None:
    mailbox, row = _chat_draft(owner)
    gone = asyncio.run(chat_draft_card.discard(owner, row, caller=LOCAL, gmail=mailbox, now=LATER))
    assert gone == {"status": chat_draft_card.GONE}
    assert "d1" not in mailbox.drafts and mailbox.sent == []
    # Marked gone, so Home's row for it goes and Send refuses.
    assert drafts.get_row(row).gone_at
    assert _refused(send_chat_draft(owner, row, caller=LOCAL, gmail=mailbox, now=LATER)).code == "draft_gone"
    # Again: already gone, nothing more to do.
    again = asyncio.run(chat_draft_card.discard(owner, row, caller=LOCAL, gmail=mailbox, now=LATER))
    assert again == {"status": chat_draft_card.GONE}


def test_delete_leaves_a_draft_changed_in_the_mailbox(db: Path, owner: Any) -> None:
    mailbox, row = _chat_draft(owner)
    mailbox.edit("d1")
    refused = _refused(chat_draft_card.discard(owner, row, caller=LOCAL, gmail=mailbox, now=LATER))
    assert refused.code == "draft_changed"
    assert "d1" in mailbox.drafts and not drafts.get_row(row).gone_at


def test_delete_never_reports_a_draft_it_left_alone_as_deleted(
    db: Path, owner: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    mailbox, row = _chat_draft(owner)

    async def left_alone(draft_id: str) -> bool:
        # What Outlook's delete_draft does when the draft changed between the
        # read and the delete: leaves it and returns False.
        mailbox.edit(draft_id)
        return False

    monkeypatch.setattr(mailbox, "delete_draft", left_alone)
    refused = _refused(chat_draft_card.discard(owner, row, caller=LOCAL, gmail=mailbox, now=LATER))
    assert refused.code == "draft_changed"
    assert "d1" in mailbox.drafts and not drafts.get_row(row).gone_at

def test_the_delete_route_deletes_only_the_callers_own_draft(owner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from openexecutive.api.routes import delegation as route
    from openexecutive.delegation import gmail as gmail_module

    mailbox, row = _chat_draft(owner)
    monkeypatch.setattr(gmail_module, "gmail_for", lambda email: mailbox)
    app = FastAPI()
    app.include_router(route.router)
    client = TestClient(app)
    headers = {"x-caller-email": _send_tests.OWNER}
    theirs = drafts.record(owner.id + 1, source=drafts.SOURCE_CHAT, thread_id=None, draft_id="dz",
                           message_id="mz", now=NOW)
    resp = client.delete(f"/delegation/drafts/{theirs}", headers=headers)
    assert resp.status_code == 404 and resp.json()["detail"]["code"] == "draft_not_found"
    monkeypatch.delenv("OE_LOCAL_LOGIN")
    resp = client.delete(f"/delegation/drafts/{row}", headers=headers)
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "caller_signing_required"
    assert "d1" in mailbox.drafts
    monkeypatch.setenv("OE_LOCAL_LOGIN", "1")
    resp = client.delete(f"/delegation/drafts/{row}", headers=headers)
    assert resp.status_code == 200 and resp.json()["status"] == "gone"
    assert "d1" not in mailbox.drafts


def test_the_card_names_the_files_on_the_draft(db: Path, owner: Any) -> None:
    from openexecutive.delegation.gmail import MailAttachment

    mailbox, row = _chat_draft(owner)
    assert _state(owner, row, mailbox)["files"] == []
    mailbox.drafts["d1"].message.attachments = [
        MailAttachment(index=1, name="2026 Tax - Lot 1.pdf"), MailAttachment(index=2, name="2026 Tax - Lot 2.pdf"),
    ]
    assert _state(owner, row, mailbox)["files"] == ["2026 Tax - Lot 1.pdf", "2026 Tax - Lot 2.pdf"]
