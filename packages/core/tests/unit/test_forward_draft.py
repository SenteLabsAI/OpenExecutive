"""ghostwrite_email's forward: the latest message of a thread, as a draft in
the speaker's own mailbox, to people they know (orchestrator/delegation_tools.py,
delegation/gmail.py, delegation/outlook.py)."""
from __future__ import annotations

import asyncio
import base64
import email
import json
from email import policy
from types import SimpleNamespace
from typing import Any
from urllib.parse import unquote

import httpx
import pytest

from openexecutive.delegation import gmail as gm
from openexecutive.delegation import outlook as ol
from openexecutive.delegation.gmail import DraftSpec, ForwardOf, MailAttachment, build_raw
from openexecutive.orchestrator import delegation_tools as dt
from openexecutive.orchestrator.schedule_tools import set_session
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store

# ghostwrite_email's fakes and autouse fixtures (a temp DB, a fresh turn pin),
# and its stand-in composer.
from .test_delegation_tools import (  # noqa: F401
    DANA,
    OWNER,
    TEAM,
    FakeMailbox,
    _msg,
    _session,
    composer,
    db,
    fresh_turn_state,
)

pytestmark = pytest.mark.usefixtures("composer")
FORWARD = ForwardOf(message_id="m1", header="---------- Forwarded message ---------\nFrom: Dana", text="Pilot on Oct 5?")


@pytest.fixture
def roster() -> SimpleNamespace:
    principal = people_store.upsert_person(full_name="Olivia Owner", is_principal=True, email=OWNER)
    teammate = people_store.upsert_person(full_name="Ben Teammate", role="Ops", email=TEAM)
    people_registry.invalidate()
    return SimpleNamespace(principal=principal, teammate=teammate)


def _ghostwrite(session: Any, tool_input: dict[str, Any]) -> dict[str, Any]:
    async def go() -> str:
        with set_session(session):
            return await dt.handle_ghostwrite_email(tool_input)

    return json.loads(asyncio.run(go()))


# --------------------------------------------------------------------------- #
# The tool
# --------------------------------------------------------------------------- #


def test_a_forward_drafts_the_latest_message_to_someone_they_know(roster: SimpleNamespace) -> None:
    mailbox = FakeMailbox()
    result = _ghostwrite(_session(mailbox, "forward Dana's email to Ben"), {
        "intent": "FYI, can you check the dates?", "forward": "t1", "to": [TEAM],
    })
    assert result["status"] == "drafted"
    spec = mailbox.drafts[0]
    assert spec.to == [TEAM] and spec.cc == [] and spec.thread_id is None
    assert spec.subject == "Fwd: Brand refresh pilot"
    assert spec.forward is not None and spec.forward.message_id == "m1"
    assert "From: Dana <dana@northpeak.example>" in spec.forward.header
    assert spec.forward.text == "Can we start the pilot Oct 5?"
    assert result["subject"] == "Fwd: Brand refresh pilot"


def test_a_forward_goes_only_to_people_they_know(roster: SimpleNamespace) -> None:
    mailbox = FakeMailbox()
    result = _ghostwrite(_session(mailbox, "forward it"), {
        "intent": "FYI", "forward": "t1", "to": ["stranger@elsewhere.example"],
    })
    assert "roster" in result["error"]
    assert mailbox.drafts == []


def test_a_forward_needs_a_real_thread_id(roster: SimpleNamespace) -> None:
    result = _ghostwrite(_session(FakeMailbox(), "forward it"), {"intent": "FYI", "forward": "../x", "to": [TEAM]})
    assert "thread_id" in result["error"]


def test_a_forward_already_marked_keeps_its_subject(roster: SimpleNamespace) -> None:
    mailbox = FakeMailbox()
    mailbox.threads["t1"].messages[0].subject = "Fwd: Brand refresh pilot"
    _ghostwrite(_session(mailbox, "forward it"), {"intent": "FYI", "forward": "t1", "to": [TEAM]})
    assert mailbox.drafts[0].subject == "Fwd: Brand refresh pilot"


# --------------------------------------------------------------------------- #
# Gmail: the original quoted, its files attached
# --------------------------------------------------------------------------- #


def test_gmail_quotes_the_original_under_the_note_and_attaches_files() -> None:
    spec = DraftSpec(
        to=[TEAM], subject="Fwd: Pilot", body="FYI, Ben.", forward=FORWARD,
        attachments=[("scope.pdf", "application/pdf", b"%PDF-1.4 x")],
    )
    raw = base64.urlsafe_b64decode(build_raw(OWNER, spec))
    msg = email.message_from_bytes(raw, policy=policy.default)
    body = msg.get_body(preferencelist=("plain",))
    assert body is not None
    text = body.get_content()
    assert text.index("FYI, Ben.") < text.index("Forwarded message") < text.index("Pilot on Oct 5?")
    files = [(p.get_filename(), p.get_content()) for p in msg.iter_attachments()]
    assert files == [("scope.pdf", b"%PDF-1.4 x")]


class _FakeGmailFiles(gm.DelegateGmail):
    """A DelegateGmail whose reads and draft POST are stubbed."""

    def __init__(self, sizes: list[int]) -> None:
        super().__init__(OWNER, credential=gm.GmailCredential(
            email=OWNER, refresh_token="r", client_id="c", client_secret="s",
        ))
        self.sizes = sizes
        self.posted: dict[str, Any] = {}

    async def list_attachments(self, message_id: str) -> list[MailAttachment]:
        return [MailAttachment(index=i, name=f"f{i}.pdf", mime_type="application/pdf", size=s)
                for i, s in enumerate(self.sizes, 1)]

    async def attachment_bytes(self, message_id: str, index: int) -> tuple[MailAttachment, bytes]:
        meta = (await self.list_attachments(message_id))[index - 1]
        return meta, b"x" * meta.size

    async def _request(self, client: Any, method: str, path: str, **kw: Any) -> dict[str, Any]:
        self.posted = kw.get("json_body") or {}
        return {"id": "d1", "message": {"id": "m9", "threadId": "t9"}}


def test_gmail_attaches_what_fits_and_says_what_it_left_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gm, "FORWARD_MAX_BYTES", 100)
    client = _FakeGmailFiles([40, 70, 50])
    created = asyncio.run(client.create_draft(DraftSpec(to=[TEAM], subject="Fwd: x", body="FYI", forward=FORWARD)))
    raw = base64.urlsafe_b64decode(client.posted["message"]["raw"])
    names = [p.get_filename() for p in email.message_from_bytes(raw, policy=policy.default).iter_attachments()]
    assert names == ["f1.pdf", "f3.pdf"]
    assert created.skipped_attachments == 1


# --------------------------------------------------------------------------- #
# Outlook: Graph's own forward, the body never patched
# --------------------------------------------------------------------------- #


def test_outlook_forwards_with_create_forward_and_never_patches_the_body() -> None:
    seen: list[tuple[str, str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={
                "access_token": "at", "expires_in": 3600, "refresh_token": "r2",
                "scope": "https://graph.microsoft.com/User.Read https://graph.microsoft.com/Mail.ReadWrite "
                         "https://graph.microsoft.com/Mail.Send",
            })
        path = unquote(request.url.raw_path.decode().split("?", 1)[0]).removeprefix("/v1.0/me")
        seen.append((request.method, path, json.loads(request.content or b"{}")))
        return httpx.Response(201, json={"id": "D1==", "changeKey": "ck", "conversationId": "C1=="})

    ol._TOKENS.clear()
    client = ol.DelegateOutlook(
        OWNER, credential=ol.OutlookCredential(email=OWNER, refresh_token="r1", client_id="cid", tenant="common"),
        transport=httpx.MockTransport(handler),
    )
    forward = ForwardOf(message_id="M1==", header="", text="")
    created = asyncio.run(client.create_draft(DraftSpec(to=[TEAM], subject="Fwd: x", body="FYI, Ben.", forward=forward)))
    assert created.draft_id == "D1=="
    assert [(m, p) for m, p, _ in seen] == [("POST", "/messages/M1==/createForward")]
    sent = seen[0][2]
    assert sent["comment"] == "FYI, Ben."
    assert sent["message"]["toRecipients"] == [{"emailAddress": {"address": TEAM}}]
    assert "body" not in sent["message"]
