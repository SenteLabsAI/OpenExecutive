"""Attached files in the speaker's own mailbox, as the Act as me clients list
and read them (delegation/gmail.py, delegation/outlook.py)."""
from __future__ import annotations

import asyncio
import base64
from typing import Any
from urllib.parse import unquote

import httpx
import pytest

from openexecutive.delegation import gmail as gm
from openexecutive.delegation import outlook as ol
from openexecutive.delegation.gmail import (
    DelegateGmail,
    GmailCredential,
    GmailError,
    GmailNotFound,
    parse_message,
)
from openexecutive.delegation.outlook import DelegateOutlook, OutlookCredential

EMAIL = "olivia@co.example"
PDF = b"%PDF-1.4 contract"


@pytest.fixture(autouse=True)
def _fresh_tokens() -> None:
    gm._TOKENS.clear()
    ol._TOKENS.clear()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _gmail_message(attachment_id: str = "ATT-first_fetch") -> dict[str, Any]:
    return {
        "id": "m1",
        "threadId": "t1",
        "labelIds": ["INBOX"],
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [{"name": "From", "value": "Sam <sam@client.example>"}, {"name": "Subject", "value": "Contract"}],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64url(b"See attached.")}},
                {"mimeType": "application/pdf", "filename": "contract.pdf",
                 "body": {"attachmentId": attachment_id, "size": len(PDF)}},
                # A signature logo the HTML shows in place: not an attachment.
                {"mimeType": "image/png", "filename": "logo.png",
                 "headers": [{"name": "Content-ID", "value": "<logo>"}], "body": {"attachmentId": "LOGO", "size": 9}},
                {"mimeType": "text/csv", "filename": "budget.csv", "body": {"data": _b64url(b"a,b\n1,2\n"), "size": 8}},
            ],
        },
    }


class FakeGmail:
    def __init__(self) -> None:
        self.fetches = 0
        self.paths: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3600, "scope": " ".join(gm.SCOPES)})
        path = request.url.path
        self.paths.append(path)
        if path.endswith("/messages/m1"):
            # Gmail hands out a new attachment id on every fetch.
            self.fetches += 1
            return httpx.Response(200, json=_gmail_message(f"ATT-fetch{self.fetches}"))
        if path.endswith(f"/messages/m1/attachments/ATT-fetch{self.fetches}"):
            return httpx.Response(200, json={"data": _b64url(PDF), "size": len(PDF)})
        return httpx.Response(404, json={})

    def client(self) -> DelegateGmail:
        cred = GmailCredential(email=EMAIL, refresh_token="r1", client_id="cid", client_secret="sec")
        return DelegateGmail(EMAIL, credential=cred, transport=httpx.MockTransport(self.handler))


def test_a_gmail_message_lists_its_files_but_not_embedded_images() -> None:
    message = parse_message(_gmail_message())
    assert [(a.index, a.name, a.mime_type) for a in message.attachments] == [
        (1, "contract.pdf", "application/pdf"),
        (2, "budget.csv", "text/csv"),
    ]
    assert message.has_attachments is True
    assert parse_message({"id": "m2", "payload": {"mimeType": "text/plain"}}).has_attachments is False


def test_gmail_reads_a_file_by_the_id_of_this_fetch() -> None:
    google = FakeGmail()
    meta, data = asyncio.run(google.client().attachment_bytes("m1", 1))
    assert (meta.name, data) == ("contract.pdf", PDF)


def test_gmail_reads_a_small_file_sent_inline() -> None:
    google = FakeGmail()
    meta, data = asyncio.run(google.client().attachment_bytes("m1", 2))
    assert (meta.name, data) == ("budget.csv", b"a,b\n1,2\n")
    assert not any("/attachments/" in p for p in google.paths)


def test_gmail_refuses_a_bad_message_id_or_index() -> None:
    client = FakeGmail().client()
    with pytest.raises(GmailError):
        asyncio.run(client.attachment_bytes("m1/../../drafts", 1))
    with pytest.raises(GmailNotFound):
        asyncio.run(client.attachment_bytes("m1", 3))


class FakeGraph:
    def __init__(self) -> None:
        self.paths: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={
                "access_token": "at", "expires_in": 3600, "refresh_token": "r2",
                "scope": "https://graph.microsoft.com/User.Read https://graph.microsoft.com/Mail.ReadWrite "
                         "https://graph.microsoft.com/Mail.Send",
            })
        path = unquote(request.url.raw_path.decode().split("?", 1)[0]).removeprefix("/v1.0/me")
        self.paths.append(path)
        if path == "/messages/M1==/attachments":
            return httpx.Response(200, json={"value": [
                {"id": "LOGO==", "name": "image001.png", "contentType": "image/png", "size": 9, "isInline": True},
                {"id": "A1==", "name": "contract.pdf", "contentType": "application/pdf", "size": len(PDF), "isInline": False},
                {"id": "A2==", "name": "Meeting notes", "contentType": "message/rfc822", "size": 400, "isInline": False},
            ]})
        if path == "/messages/M1==/attachments/A1==":
            return httpx.Response(200, json={"contentBytes": base64.b64encode(PDF).decode()})
        if path == "/messages/M1==/attachments/A2==":
            return httpx.Response(200, json={"@odata.type": "#microsoft.graph.itemAttachment"})
        return httpx.Response(404, json={})

    def client(self) -> DelegateOutlook:
        cred = OutlookCredential(email=EMAIL, refresh_token="r1", client_id="cid", tenant="common")
        return DelegateOutlook(EMAIL, credential=cred, transport=httpx.MockTransport(self.handler))


def test_outlook_lists_files_but_not_inline_images() -> None:
    listed = asyncio.run(FakeGraph().client().list_attachments("M1=="))
    assert [(a.index, a.name, a.size) for a in listed] == [(1, "contract.pdf", len(PDF)), (2, "Meeting notes", 400)]


def test_outlook_reads_a_file_and_refuses_an_attached_item() -> None:
    client = FakeGraph().client()
    meta, data = asyncio.run(client.attachment_bytes("M1==", 1))
    assert (meta.name, data) == ("contract.pdf", PDF)
    with pytest.raises(GmailError):
        asyncio.run(client.attachment_bytes("M1==", 2))
    with pytest.raises(GmailNotFound):
        asyncio.run(client.attachment_bytes("M1==", 3))


def test_outlook_says_whether_a_message_has_files() -> None:
    assert ol.parse_message({"id": "M1==", "hasAttachments": True}).has_attachments is True
    assert ol.parse_message({"id": "M2=="}).has_attachments is False
    assert "hasAttachments" in ol._SELECT
