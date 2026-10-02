"""The direct Outlook client for a person's own mailbox (delegation/outlook.py),
the script that connects it (scripts/connect-own-outlook.py), and how the
Gmail module hands it out (``gmail_for``, ``mailbox_link``)."""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import inspect
import json
import re
import stat
from datetime import UTC, datetime
from email.message import Message
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import unquote

import httpx
import pytest

from openexecutive.delegation import gmail as gm
from openexecutive.delegation import outlook as ol
from openexecutive.delegation.gmail import (
    DelegateGmail,
    DraftSpec,
    GmailAuthError,
    GmailError,
    GmailNotFound,
    GmailRateLimited,
    email_key,
    gmail_status,
)
from openexecutive.delegation.outlook import (
    GHOSTWRITTEN_PROPERTY,
    DelegateOutlook,
    OutlookCredential,
    exchange_authenticated,
    load_credential,
)

EMAIL = "olivia@co.example"
SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "connect-own-outlook.py"
SENT_FOLDER = "SentFolderId=="


@pytest.fixture(autouse=True)
def _fresh_tokens() -> None:
    ol._TOKENS.clear()
    gm._TOKENS.clear()


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("connect_own_outlook", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(directory: Path, *, personal: bool = False, tenant: str = "contoso-tid") -> Path:
    script = _script()
    payload = script.credential_payload(
        EMAIL, "r1", "cid", "consumers" if personal else "11111111-2222-3333-4444-555555555555", personal=personal,
    )
    if tenant != "contoso-tid":
        payload["microsoft"]["tenant"] = tenant
    return script.write_credential(directory, EMAIL, payload)


# --------------------------------------------------------------------------- #
# Credential file and dispatch
# --------------------------------------------------------------------------- #


def test_the_script_writes_what_the_client_reads(tmp_path: Path) -> None:
    script = _script()
    assert script.email_key(" Olivia@Co.Example ") == email_key(EMAIL)
    path = _write(tmp_path / "creds")
    assert path.name == f"{email_key(EMAIL)}.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert load_credential(EMAIL, directory=tmp_path / "creds") == OutlookCredential(
        email=EMAIL, refresh_token="r1", client_id="cid", tenant="11111111-2222-3333-4444-555555555555",
    )
    # The script asks for exactly the scopes the client checks.
    assert list(script.GRAPH_SCOPES) == list(ol.GRAPH_SCOPES)
    assert script.CONSUMER_TENANT_ID == ol.CONSUMER_TENANT_ID


def test_a_personal_account_is_marked_personal(tmp_path: Path) -> None:
    _write(tmp_path, personal=True)
    cred = load_credential(EMAIL, directory=tmp_path)
    assert cred is not None and cred.personal and cred.tenant == "consumers"


def test_the_script_reads_the_tenant_from_the_id_token() -> None:
    script = _script()

    def token(claims: dict[str, Any]) -> str:
        body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        return f"x.{body}.sig"

    assert script.token_tenant(token({"tid": ol.CONSUMER_TENANT_ID})) == ol.CONSUMER_TENANT_ID
    assert script.token_tenant(token({"tid": "../evil"})) == ""
    assert script.token_tenant("garbage") == ""


@pytest.mark.parametrize("tenant", ["evil.example/x", "", "common/../x"])
def test_a_credential_naming_an_odd_tenant_is_ignored(tmp_path: Path, tenant: str) -> None:
    _write(tmp_path, tenant=tenant)
    assert load_credential(EMAIL, directory=tmp_path) is None


def test_each_module_reads_only_its_own_kind_of_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gm, "credentials_dir", lambda: tmp_path)
    assert gm.credential_provider(EMAIL) == "google"  # no file at all
    assert isinstance(gm.gmail_for(EMAIL), DelegateGmail)
    _write(tmp_path)
    assert gm.credential_provider(EMAIL) == "microsoft"
    assert gm.load_credential(EMAIL) is None  # never read as a Gmail sign-in
    assert isinstance(gm.gmail_for(EMAIL), DelegateOutlook)
    # A Gmail file is never read as a Microsoft one.
    (tmp_path / f"{email_key(EMAIL)}.json").write_text(json.dumps({
        "email": EMAIL, "authorized_user": {"refresh_token": "r", "client_id": "c", "client_secret": "s"},
    }))
    assert gm.credential_provider(EMAIL) == "google"
    assert load_credential(EMAIL, directory=tmp_path) is None
    assert isinstance(gm.gmail_for(EMAIL), DelegateGmail)


def test_a_file_for_another_address_is_ignored(tmp_path: Path) -> None:
    path = _write(tmp_path)
    data = json.loads(path.read_text())
    data["email"] = "someone.else@co.example"
    path.write_text(json.dumps(data))
    assert load_credential(EMAIL, directory=tmp_path) is None


def test_mailbox_link_opens_the_right_mailbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gm, "credentials_dir", lambda: tmp_path)
    assert gm.mailbox_link(EMAIL, thread_id="t1", draft_id="d1").startswith("https://mail.google.com/")
    _write(tmp_path)
    assert gm.mailbox_link(EMAIL, thread_id="t1", draft_id="AAk/d+1=") == (
        "https://outlook.office.com/mail/deeplink/read/AAk%2Fd%2B1%3D"
    )
    # A draft's versioned message id opens the draft itself.
    assert gm.mailbox_link(EMAIL, message_id="AAk1.ck9") == "https://outlook.office.com/mail/deeplink/read/AAk1"
    assert gm.mailbox_link(EMAIL, draft_id="bad id'") == "https://outlook.office.com/mail/drafts"
    _write(tmp_path, personal=True)
    assert gm.mailbox_link(EMAIL, draft_id="d1") == "https://outlook.live.com/mail/0/deeplink/read/d1"


# --------------------------------------------------------------------------- #
# A fake Graph
# --------------------------------------------------------------------------- #


def _msg(mid: str, **fields: Any) -> dict[str, Any]:
    sender = fields.pop("sender", "dana@northpeak.example")
    base: dict[str, Any] = {
        "id": mid,
        "conversationId": "conv1",
        "changeKey": "ck1",
        "subject": "Pilot",
        "from": {"emailAddress": {"name": "Dana", "address": sender}} if sender else None,
        "toRecipients": [{"emailAddress": {"address": EMAIL}}],
        "ccRecipients": [],
        "bccRecipients": [],
        "replyTo": [],
        "receivedDateTime": "2026-10-01T09:00:00Z",
        "sentDateTime": "2026-10-01T08:59:00Z",
        "internetMessageId": f"<{mid}@mail.example>",
        "internetMessageHeaders": [],
        "body": {"contentType": "text", "content": "Can we start Monday?"},
        "isDraft": False,
        "inferenceClassification": "focused",
        "parentFolderId": "InboxFolder==",
    }
    base.update(fields)
    return base


class FakeGraph:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.messages: dict[str, dict[str, Any]] = {}
        self.token_status = 200
        self.token_body: dict[str, Any] = {
            "access_token": "at1", "expires_in": 3600, "refresh_token": "r2",
            "scope": "https://graph.microsoft.com/User.Read https://graph.microsoft.com/Mail.ReadWrite "
                     "https://graph.microsoft.com/Mail.Send",
        }
        self.me = {"mail": EMAIL, "userPrincipalName": EMAIL, "proxyAddresses": ["SMTP:olivia@co.example", "smtp:o@co.example"]}
        self.fail: dict[str, int] = {}
        self.listing: list[dict[str, Any]] = []
        self.created = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = request.url
        if url.host == "login.microsoftonline.com":
            return httpx.Response(self.token_status, json=self.token_body)
        path = unquote(url.raw_path.decode().split("?", 1)[0]).removeprefix("/v1.0/me")
        key = f"{request.method} {path.split('/')[1] if path.count('/') > 0 else path}"
        for prefix, status in self.fail.items():
            if f"{request.method} {path}".startswith(prefix):
                return httpx.Response(status, json={"error": {"code": "x"}})
        if path == "":
            return httpx.Response(200, json=self.me)
        if path == "/mailFolders/sentitems":
            return httpx.Response(200, json={"id": SENT_FOLDER})
        if request.method == "GET" and path in ("/messages", "/mailFolders/inbox/messages", "/mailFolders/sentitems/messages"):
            flt = url.params.get("$filter", "")
            match = re.fullmatch(r"conversationId eq '([^']+)'", flt)
            if match:
                return httpx.Response(200, json={"value": [m for m in self.messages.values() if m["conversationId"] == match.group(1)]})
            return httpx.Response(200, json={"value": self.listing})
        match = re.fullmatch(r"/messages/([^/]+)(/send|/createReply)?", path)
        if match is None:
            return httpx.Response(400)
        mid, action = match.group(1), match.group(2)
        found = self.messages.get(mid)
        if request.method == "POST" and action == "/createReply":
            if found is None:
                return httpx.Response(404)
            self.created += 1
            draft = _msg(f"draft{self.created}", isDraft=True, sender="", conversationId=found["conversationId"],
                         toRecipients=[found["from"]], body={"contentType": "text", "content": ""})
            self.messages[draft["id"]] = draft
            return httpx.Response(201, json=draft)
        if found is None:
            return httpx.Response(404, json={"error": {"code": "ErrorItemNotFound"}})
        if request.method == "GET":
            return httpx.Response(200, json=found)
        if request.method == "PATCH":
            body = json.loads(request.content)
            found.update(body)
            found["changeKey"] = found["changeKey"] + "x"
            return httpx.Response(200, json=found)
        if request.method == "DELETE":
            del self.messages[mid]
            return httpx.Response(204)
        if request.method == "POST" and action == "/send":
            found["isDraft"] = False
            found["parentFolderId"] = SENT_FOLDER
            return httpx.Response(202)
        del key
        return httpx.Response(400)

    def client(self, directory: Path | None = None) -> DelegateOutlook:
        cred = None if directory else OutlookCredential(email=EMAIL, refresh_token="r1", client_id="cid", tenant="common")
        return DelegateOutlook(EMAIL, credential=cred, transport=self.transport(), directory=directory)

    def graph_calls(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == "graph.microsoft.com"]


# --------------------------------------------------------------------------- #
# Sign-in
# --------------------------------------------------------------------------- #


def test_the_token_refresh_asks_for_mail_scopes_and_saves_the_rotated_token(tmp_path: Path) -> None:
    _write(tmp_path)
    path = tmp_path / f"{email_key(EMAIL)}.json"
    data = json.loads(path.read_text())
    data["extra"] = {"kept": True}  # whatever else the file carries survives
    path.write_text(json.dumps(data))
    graph = FakeGraph()
    assert asyncio.run(graph.client(tmp_path).profile_email()) == EMAIL
    refresh = graph.requests[0]
    assert str(refresh.url) == "https://login.microsoftonline.com/11111111-2222-3333-4444-555555555555/oauth2/v2.0/token"
    form = dict(x.split("=", 1) for x in refresh.content.decode().split("&"))
    assert form["grant_type"] == "refresh_token" and form["refresh_token"] == "r1"
    assert "Mail.Send" in unquote(form["scope"]) and "offline_access" in unquote(form["scope"])
    saved = json.loads(path.read_text())
    assert saved["microsoft"]["refresh_token"] == "r2"
    assert saved["extra"] == {"kept": True} and saved["provider"] == "microsoft"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # Every Graph call asks for immutable ids and text bodies.
    assert all('IdType="ImmutableId"' in r.headers["Prefer"] for r in graph.graph_calls())


def test_a_rotated_token_never_overwrites_a_newer_sign_in(tmp_path: Path) -> None:
    _write(tmp_path)
    path = tmp_path / f"{email_key(EMAIL)}.json"
    client = FakeGraph().client(tmp_path)
    data = json.loads(path.read_text())
    data["microsoft"]["refresh_token"] = "reconnected"
    path.write_text(json.dumps(data))
    ol._store_rotated(EMAIL, "r1", "r2", tmp_path)
    assert json.loads(path.read_text())["microsoft"]["refresh_token"] == "reconnected"
    del client


@pytest.mark.parametrize(("status", "body"), [
    (400, {"error": "invalid_grant"}),
    (400, {"error": "interaction_required"}),
    (401, {}),
    (200, {"access_token": "a", "expires_in": 60, "scope": "https://graph.microsoft.com/User.Read"}),
])
def test_a_refused_or_narrow_sign_in_needs_a_reconnect(status: int, body: dict[str, Any]) -> None:
    graph = FakeGraph()
    graph.token_status, graph.token_body = status, body
    with pytest.raises(GmailAuthError):
        asyncio.run(graph.client().profile_email())
    assert asyncio.run(gmail_status(EMAIL, gmail=graph.client())) == "needs_reconnect"


def test_status_compares_the_mailbox_with_the_people_entry() -> None:
    graph = FakeGraph()
    assert asyncio.run(gmail_status(EMAIL, gmail=graph.client())) == "connected"
    graph.me = {"mail": None, "userPrincipalName": "someone@co.example"}
    assert asyncio.run(gmail_status(EMAIL, gmail=graph.client())) == "mismatch"


@pytest.mark.parametrize(("status", "error"), [
    (401, GmailAuthError), (403, GmailAuthError), (429, GmailRateLimited), (404, GmailNotFound), (503, GmailError),
])
def test_graph_errors_map_onto_the_gmail_ones(status: int, error: type[Exception]) -> None:
    graph = FakeGraph()
    graph.fail["GET /messages/m1"] = status
    graph.messages["m1"] = _msg("m1")
    with pytest.raises(error):
        asyncio.run(graph.client().get_message("m1"))


def test_send_as_addresses_are_the_primary_and_its_aliases() -> None:
    assert asyncio.run(FakeGraph().client().send_as_addresses()) == [EMAIL, "o@co.example"]


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


def test_a_conversation_reads_like_a_gmail_thread() -> None:
    graph = FakeGraph()
    graph.messages["m1"] = _msg("m1", inferenceClassification="other", body={
        "contentType": "html", "content": "<div>Hello<br>there</div>",
    })
    graph.messages["m2"] = _msg("m2", sender=EMAIL, parentFolderId=SENT_FOLDER, receivedDateTime="2026-10-01T10:00:00Z")
    # Mail from their own address that arrived in the inbox is not theirs.
    graph.messages["m3"] = _msg("m3", sender=EMAIL, receivedDateTime="2026-10-01T11:00:00Z")
    graph.messages["d1"] = _msg("d1", sender="", isDraft=True, receivedDateTime="2026-10-01T12:00:00Z",
                                singleValueExtendedProperties=[{"id": GHOSTWRITTEN_PROPERTY, "value": "1"}])
    thread = asyncio.run(graph.client().get_thread("conv1"))
    assert [m.id for m in thread.messages] == ["m1", "m2", "m3", "d1"]
    first, sent, spoofed, draft = thread.messages
    assert first.labels == ["INBOX", "CATEGORY_UPDATES"] and first.text.splitlines() == ["Hello", "there"]
    assert first.from_addr == "dana@northpeak.example" and first.to == [EMAIL]
    assert first.received_at == "2026-10-01T09:00:00+00:00"
    assert sent.labels == ["SENT"]
    assert spoofed.labels == ["INBOX"]
    assert draft.labels == ["DRAFT"] and draft.ghostwritten and draft.from_addr == EMAIL


def test_an_unknown_conversation_is_not_found() -> None:
    with pytest.raises(GmailNotFound):
        asyncio.run(FakeGraph().client().get_thread("nope"))


@pytest.mark.parametrize("bad", ["conv' or 1 eq 1", "a b", "", "x" * 600, None])
def test_an_odd_id_never_reaches_graph(bad: Any) -> None:
    graph = FakeGraph()
    client = graph.client()
    for call in (client.get_thread, client.get_message, client.get_draft, client.delete_draft, client.send_draft):
        with pytest.raises(GmailError):
            asyncio.run(call(bad))
    assert graph.graph_calls() == []


def test_the_inbox_listing_is_focused_mail_from_others() -> None:
    graph = FakeGraph()
    graph.listing = [
        {"id": "m1", "conversationId": "c1", "from": {"emailAddress": {"address": "dana@northpeak.example"}}},
        {"id": "m2", "conversationId": "c2", "from": {"emailAddress": {"address": EMAIL}}},
        {"id": "bad id", "conversationId": "c3"},
    ]
    after = datetime(2026, 10, 1, 9, 30, tzinfo=UTC)
    assert asyncio.run(graph.client().inbox_message_ids(after=after, max_results=500)) == [("m1", "c1")]
    listed = graph.graph_calls()[-1]
    assert listed.url.path == "/v1.0/me/mailFolders/inbox/messages"
    assert listed.url.params["$filter"] == "receivedDateTime ge 2026-10-01T09:30:00Z and inferenceClassification eq 'focused'"
    assert listed.url.params["$top"] == "100"


def test_has_written_to_searches_sent_items() -> None:
    graph = FakeGraph()
    graph.listing = [{"id": "s1"}]
    assert asyncio.run(graph.client().has_written_to("Dana@NorthPeak.example")) is True
    call = graph.graph_calls()[-1]
    assert call.url.path == "/v1.0/me/mailFolders/sentitems/messages"
    assert call.url.params["$search"] == '"to:dana@northpeak.example"'
    graph.listing = []
    assert asyncio.run(graph.client().has_written_to("dana@northpeak.example")) is False
    before = len(graph.requests)
    assert asyncio.run(graph.client().has_written_to('x" OR "y@z.example')) is False
    assert len(graph.requests) == before


def test_search_keeps_what_outlook_understands() -> None:
    today = datetime(2026, 10, 15, tzinfo=UTC)
    assert ol._search_text('from:dana@example.com subject:pilot newer_than:14d', today=today) == (
        "from:dana@example.com subject:pilot received>=2026-10-01"
    )
    assert ol._search_text('in:inbox -in:chats label:x is:unread "budget" (q3)', today=today) == "budget q3"
    assert '"' not in ol._search_text('a" OR "b\\', today=today)


def test_search_threads_lists_each_conversation_once() -> None:
    graph = FakeGraph()
    graph.listing = [
        {"id": "m1", "conversationId": "c1", "subject": "Pilot", "from": {"emailAddress": {"name": "Dana", "address": "d@x.example"}},
         "receivedDateTime": "2026-10-01T09:00:00Z"},
        {"id": "m2", "conversationId": "c1", "subject": "Re: Pilot"},
        {"id": "m3", "conversationId": "c2", "subject": "Other", "from": None},
    ]
    found = asyncio.run(graph.client().search_threads("from:d@x.example pilot"))
    assert [(t.id, t.sender) for t in found] == [("c1", "Dana <d@x.example>"), ("c2", "")]
    assert graph.graph_calls()[-1].url.params["$search"] == '"from:d@x.example pilot"'
    assert asyncio.run(graph.client().search_threads("in:inbox")) == []


def test_list_sent_reads_sent_items_as_sent() -> None:
    graph = FakeGraph()
    graph.listing = [_msg("s1", sender=EMAIL, parentFolderId=SENT_FOLDER)]
    sent = asyncio.run(graph.client().list_sent(500))
    assert [m.labels for m in sent] == [["SENT"]]
    assert graph.graph_calls()[-1].url.params["$top"] == "100"


def test_there_is_no_signature_to_read() -> None:
    graph = FakeGraph()
    assert asyncio.run(graph.client().send_as_signature()) == ""
    assert graph.requests == []


# --------------------------------------------------------------------------- #
# Who sent it
# --------------------------------------------------------------------------- #


def _headers(*results: str, sender: str = "Dana <dana@northpeak.example>") -> Message:
    msg = Message()
    msg["From"] = sender
    for r in results:
        msg["Authentication-Results"] = r
    return msg


EXCHANGE_PASS = ("spf=pass (sender IP is 1.2.3.4) smtp.mailfrom=northpeak.example; dkim=pass (signature was verified) "
                 "header.d=northpeak.example;dmarc=pass action=none header.from=northpeak.example;compauth=pass reason=100")


def test_exchange_authenticated_reads_only_its_own_verdict() -> None:
    assert exchange_authenticated(_headers(EXCHANGE_PASS), "dana@northpeak.example") is True
    # A pass for another domain, a fail, or a forged header below it.
    assert exchange_authenticated(_headers(EXCHANGE_PASS.replace("header.from=northpeak", "header.from=evil")),
                                  "dana@northpeak.example") is False
    assert exchange_authenticated(_headers(EXCHANGE_PASS.replace("dmarc=pass", "dmarc=fail")), "dana@northpeak.example") is False
    forged_below = _headers("spf=none; dmarc=none header.from=northpeak.example", EXCHANGE_PASS)
    assert exchange_authenticated(forged_below, "dana@northpeak.example") is False
    # Two verdicts in one header, or one some other server wrote.
    doubled = EXCHANGE_PASS + ";dmarc=pass header.from=evil.example"
    assert exchange_authenticated(_headers(doubled), "dana@northpeak.example") is False
    assert exchange_authenticated(_headers("mx.google.com; " + EXCHANGE_PASS), "dana@northpeak.example") is False
    assert exchange_authenticated(_headers(EXCHANGE_PASS), "other@northpeak.example") is False


def test_a_parsed_message_carries_the_verdict() -> None:
    raw = _msg("m1", internetMessageHeaders=[
        {"name": "From", "value": "Dana <dana@northpeak.example>"},
        {"name": "Authentication-Results", "value": EXCHANGE_PASS},
        {"name": "List-Unsubscribe", "value": "<mailto:x@y>"},
    ])
    parsed = ol.parse_message(raw, SENT_FOLDER, EMAIL)
    assert parsed.sender_authenticated and parsed.mailing_list
    raw["isDraft"] = True
    assert ol.parse_message(raw, SENT_FOLDER, EMAIL).sender_authenticated is False


# --------------------------------------------------------------------------- #
# Drafts and the one send
# --------------------------------------------------------------------------- #


def test_a_new_email_is_saved_as_a_draft() -> None:
    graph = FakeGraph()

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1.0/me/messages":
            graph.requests.append(request)
            return httpx.Response(201, json={"id": "new1", "changeKey": "k1", "conversationId": "c9"})
        return FakeGraph.handle(graph, request)

    client = DelegateOutlook(EMAIL, credential=OutlookCredential(EMAIL, "r1", "cid", "common"),
                             transport=httpx.MockTransport(handle))
    created = asyncio.run(client.create_draft(DraftSpec(to=["dana@northpeak.example"], subject="Hi\r\nBcc: x", body="Hello")))
    assert (created.draft_id, created.message_id, created.thread_id) == ("new1", "new1.k1", "c9")
    body = json.loads(graph.requests[-1].content)
    assert body["toRecipients"] == [{"emailAddress": {"address": "dana@northpeak.example"}}]
    assert "\n" not in body["subject"] and body["body"] == {"contentType": "text", "content": "Hello"}
    assert body["singleValueExtendedProperties"] == [{"id": GHOSTWRITTEN_PROPERTY, "value": "1"}]


def test_a_reply_answers_the_message_it_names() -> None:
    graph = FakeGraph()
    graph.messages["m1"] = _msg("m1")
    graph.messages["m2"] = _msg("m2", receivedDateTime="2026-10-01T10:00:00Z")
    created = asyncio.run(graph.client().create_draft(DraftSpec(
        to=["dana@northpeak.example"], subject="Re: Pilot", body="Yes.", thread_id="conv1", in_reply_to="<m1@mail.example>",
    )))
    calls = [(r.method, unquote(r.url.path)) for r in graph.graph_calls()]
    assert ("POST", "/v1.0/me/messages/m1/createReply") in calls
    assert created.draft_id == "draft1" and created.message_id == "draft1.ck1x" and created.thread_id == "conv1"
    assert graph.messages["draft1"]["body"]["content"] == "Yes."
    # With no match it answers the newest message.
    asyncio.run(graph.client().create_draft(DraftSpec(to=["a@b.example"], subject="Re", body="x", thread_id="conv1")))
    assert ("POST", "/v1.0/me/messages/m2/createReply") in [(r.method, unquote(r.url.path)) for r in graph.graph_calls()]


def test_a_half_made_reply_is_removed() -> None:
    graph = FakeGraph()
    graph.messages["m1"] = _msg("m1")
    graph.fail["PATCH /messages/draft1"] = 500
    with pytest.raises(GmailError):
        asyncio.run(graph.client().create_draft(DraftSpec(to=["a@b.example"], subject="Re", body="x", thread_id="conv1")))
    assert "draft1" not in graph.messages


def test_an_edited_draft_gets_a_new_message_id() -> None:
    graph = FakeGraph()
    graph.messages["d1"] = _msg("d1", isDraft=True, sender="")
    client = graph.client()
    before = asyncio.run(client.get_draft("d1"))
    assert before is not None and before.draft_id == "d1" and before.message.id == "d1.ck1"
    graph.messages["d1"]["changeKey"] = "ck2"
    after = asyncio.run(client.get_draft("d1"))
    assert after is not None and after.message.id == "d1.ck2"
    # Sent (no longer a draft) or deleted: gone.
    graph.messages["d1"]["isDraft"] = False
    assert asyncio.run(client.get_draft("d1")) is None
    assert asyncio.run(client.get_draft("missing")) is None


def test_delete_draft_never_deletes_a_sent_message() -> None:
    graph = FakeGraph()
    graph.messages["m1"] = _msg("m1")
    graph.messages["d1"] = _msg("d1", isDraft=True)
    client = graph.client()
    assert asyncio.run(client.delete_draft("m1")) is False and "m1" in graph.messages
    assert asyncio.run(client.delete_draft("d1")) is True and "d1" not in graph.messages
    assert asyncio.run(client.delete_draft("d1")) is False


def test_send_draft_sends_the_draft_by_its_id_and_nothing_else() -> None:
    graph = FakeGraph()
    graph.messages["d1"] = _msg("d1", isDraft=True)
    sent = asyncio.run(graph.client().send_draft("d1"))
    assert sent.id == "d1"
    call = graph.graph_calls()[-1]
    assert (call.method, call.url.path, call.content) == ("POST", "/v1.0/me/messages/d1/send", b"")
    graph.fail["POST /messages/d2/send"] = 503
    graph.messages["d2"] = _msg("d2", isDraft=True)
    with pytest.raises(GmailError) as raised:
        asyncio.run(graph.client().send_draft("d2"))
    assert raised.value.maybe_done


def test_the_only_send_is_an_existing_draft_by_its_id() -> None:
    """Same public surface as the Gmail client, and one way to send."""
    def public(cls: type) -> set[str]:
        return {n for n, _ in inspect.getmembers(cls, inspect.isfunction) if not n.startswith("_")}

    # Gmail's own search syntax stays Gmail's: callers use what it backs.
    assert public(DelegateOutlook) == public(DelegateGmail) - {"list_message_ids"}
    source = inspect.getsource(ol)
    assert re.findall(r"/send\b[^`]", source) == ['/send"']
    assert "sendMail" not in source and "/forward" not in source and "/reply\"" not in source


def test_send_draft_is_only_called_by_reply_send() -> None:
    root = Path(ol.__file__).resolve().parents[1]
    callers = {
        p.relative_to(root).as_posix()
        for p in root.rglob("*.py")
        if ".send_draft(" in p.read_text(encoding="utf-8")
    }
    assert callers == {"delegation/reply_send.py"}


def test_the_ui_links_to_the_same_mailboxes() -> None:
    """The reply cards and chat chip keep only links with these prefixes."""
    ts = (Path(__file__).resolve().parents[3] / "ui" / "src" / "lib" / "replyCards.ts").read_text(encoding="utf-8")
    block = ts.split("MAILBOX_LINK_PREFIXES = [", 1)[1].split("]", 1)[0]
    assert tuple(re.findall(r'"([^"]+)"', block)) == gm.MAILBOX_LINK_PREFIXES


def test_a_chat_chip_links_to_an_outlook_draft() -> None:
    from openexecutive.orchestrator.action_chips import summarize_action

    link = "https://outlook.office.com/mail/deeplink/read/AAk1"
    result = json.dumps({"status": "drafted", "to": ["dana@x.example"], "gmail_link": link})
    chip = summarize_action(tool_name="ghostwrite_email", tool_input={}, tool_result=result)
    assert chip is not None and chip["link"] == link
    evil = json.dumps({"status": "drafted", "to": [], "gmail_link": "https://outlook.office.com.evil.example/mail/"})
    chip = summarize_action(tool_name="ghostwrite_email", tool_input={}, tool_result=evil)
    assert chip is not None and chip["link"] is None
