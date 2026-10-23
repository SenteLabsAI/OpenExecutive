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
from zoneinfo import ZoneInfo

import httpx
import pytest

from openexecutive.delegation import gmail as gm
from openexecutive.delegation import outlook as ol
from openexecutive.delegation.gmail import (
    CalendarNotGranted,
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
        scopes=frozenset({"user.read", "mail.readwrite", "mail.send"}),
    )
    # No secret is ever written into a person's file, and the token stays out of a repr.
    assert "client_secret" not in path.read_text()
    assert "r1" not in repr(load_credential(EMAIL, directory=tmp_path / "creds"))
    assert [p.name for p in path.parent.iterdir()] == [path.name]  # no temp file left
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
            wanted = request.headers.get("If-Match")
            if wanted is not None and wanted != f'W/"{found["changeKey"]}"':
                return httpx.Response(412)
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
    assert [p.name for p in tmp_path.iterdir()] == [path.name]
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


def test_the_mailbox_address_comes_from_the_directory_never_the_mail() -> None:
    """A personal account with no mail on /me is matched by its sign-in name.
    Sent Items is the owner's to fill, so it never says whose mailbox it is."""
    graph = FakeGraph()
    graph.me = {"mail": None, "userPrincipalName": "olivia@gmail.example"}
    graph.listing = [{"isDraft": False, "from": {"emailAddress": {"address": EMAIL}}}]
    assert asyncio.run(graph.client().profile_email()) == "olivia@gmail.example"
    assert asyncio.run(gmail_status(EMAIL, gmail=graph.client())) == "mismatch"
    assert EMAIL not in asyncio.run(graph.client().send_as_addresses())


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


def test_search_threads_proves_each_sender() -> None:
    def found(address: str, **extra: Any) -> dict[str, Any]:
        return {"conversationId": f"c-{len(graph.listing)}", "subject": "Pilot",
                "from": {"emailAddress": {"name": "Dana", "address": address}},
                "internetMessageHeaders": [
                    {"name": "From", "value": f"Dana <{address}>"},
                    {"name": "Authentication-Results", "value": EXCHANGE_PASS},
                ], **extra}

    graph = FakeGraph()
    graph.listing = []
    graph.listing.append(found("dana@northpeak.example"))
    graph.listing.append(found("dana@elsewhere.example"))  # a verdict for another domain
    graph.listing.append(found("dana@northpeak.example", isDraft=True))
    proven = asyncio.run(graph.client().search_threads("pilot"))
    assert [t.sender_authenticated for t in proven] == [True, False, False]
    assert "internetMessageHeaders" in graph.graph_calls()[-1].url.params["$select"]


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
    # A stamp without EOP's compauth= verdict is not EOP's shape.
    unstamped = "spf=pass smtp.mailfrom=northpeak.example; dmarc=pass action=none header.from=northpeak.example"
    assert exchange_authenticated(_headers(unstamped), "dana@northpeak.example") is False
    assert exchange_authenticated(_headers(EXCHANGE_PASS + ";compauth=pass reason=100"), "dana@northpeak.example") is False


EXCHANGE_NO_DMARC = ("spf=pass (sender IP is 1.2.3.4) smtp.mailfrom=northpeak.example; dkim=pass (signature was "
                     "verified) header.d=northpeak.example;dmarc=none action=none header.from=northpeak.example;"
                     "compauth=pass reason=102")


def test_a_domain_without_dmarc_passes_on_its_own_dkim_only() -> None:
    dana = "dana@northpeak.example"
    assert exchange_authenticated(_headers(EXCHANGE_NO_DMARC), dana) is True
    best_guess = EXCHANGE_NO_DMARC.replace("dmarc=none", "dmarc=bestguesspass")
    assert exchange_authenticated(_headers(best_guess), dana) is True
    # Microsoft 365's default key is the tenant's onmicrosoft.com domain: not theirs.
    default_key = EXCHANGE_NO_DMARC.replace("header.d=northpeak.example", "header.d=northpeak.onmicrosoft.com")
    assert exchange_authenticated(_headers(default_key), dana) is False
    # SPF alone, a failed signature, or dmarc=none for another domain.
    assert exchange_authenticated(_headers(EXCHANGE_NO_DMARC.replace("dkim=pass", "dkim=none")), dana) is False
    assert exchange_authenticated(_headers(EXCHANGE_NO_DMARC.replace("dkim=pass", "dkim=fail")), dana) is False
    other = EXCHANGE_NO_DMARC.replace("header.from=northpeak", "header.from=evil")
    assert exchange_authenticated(_headers(other), dana) is False
    # A HELO echo carrying a signature of its own, ahead of Exchange's real one.
    helo = ("spf=none (sender IP is 203.0.113.9) smtp.helo=x;dkim=pass header.d=northpeak.example; "
            "dkim=none (message not signed) header.d=none;dmarc=none action=none "
            "header.from=northpeak.example;compauth=fail reason=001")
    assert exchange_authenticated(_headers(helo), dana) is False
    two = EXCHANGE_NO_DMARC.replace("dkim=pass", "dkim=none header.d=none;dkim=pass", 1)
    assert exchange_authenticated(_headers(two), dana) is False


def test_mail_a_list_resent_never_passes() -> None:
    msg = _headers(EXCHANGE_PASS)
    msg["List-Id"] = "<all.northpeak.example>"
    assert exchange_authenticated(msg, "dana@northpeak.example") is False


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


def test_a_draft_sent_while_it_is_being_deleted_is_left_alone() -> None:
    """Immutable ids: a draft sent between the read and the DELETE keeps its
    id, so the DELETE carries the draft's etag and Graph refuses it."""
    graph = FakeGraph()
    graph.messages["d1"] = _msg("d1", isDraft=True)
    real = graph.handle

    def send_in_between(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            graph.messages["d1"].update(isDraft=False, changeKey="sent", parentFolderId=SENT_FOLDER)
        return real(request)

    client = DelegateOutlook(EMAIL, credential=OutlookCredential(EMAIL, "r1", "cid", "common"),
                             transport=httpx.MockTransport(send_in_between))
    assert asyncio.run(client.delete_draft("d1")) is False
    assert "d1" in graph.messages


def test_update_draft_text_changes_only_the_version_it_saw() -> None:
    from openexecutive.delegation.gmail import DraftChanged

    graph = FakeGraph()
    graph.messages["d1"] = _msg("d1", isDraft=True)
    client = graph.client()
    seen = ol.draft_version("d1", "ck1")
    version = asyncio.run(client.update_draft_text("d1", seen, "Friday at 10 works."))
    assert version == ol.draft_version("d1", "ck1x")
    patch = next(r for r in graph.graph_calls() if r.method == "PATCH")
    assert json.loads(patch.content) == {"body": {"contentType": "text", "content": "Friday at 10 works."}}
    assert patch.headers["If-Match"] == 'W/"ck1"'
    # Changed in Outlook since the card saw it: never overwritten.
    with pytest.raises(DraftChanged) as err:
        asyncio.run(client.update_draft_text("d1", seen, "Monday."))
    assert err.value.reason == "changed"
    assert graph.messages["d1"]["body"]["content"] == "Friday at 10 works."



def test_update_draft_text_can_change_who_it_goes_to() -> None:
    graph = FakeGraph()
    graph.messages["d1"] = _msg("d1", isDraft=True)
    client = graph.client()
    asyncio.run(client.update_draft_text(
        "d1", ol.draft_version("d1", "ck1"), "Hi", to=["dana@x.example"], cc=["ben@x.example"],
    ))
    patch = next(r for r in graph.graph_calls() if r.method == "PATCH")
    sent = json.loads(patch.content)
    assert sent["toRecipients"] == [{"emailAddress": {"address": "dana@x.example"}}]
    assert sent["ccRecipients"] == [{"emailAddress": {"address": "ben@x.example"}}]


@pytest.mark.parametrize("fields", [
    {"hasAttachments": True},
    {"body": {"contentType": "html", "content": "<p>Hi</p>"}},
])
def test_update_draft_text_leaves_a_draft_with_files_or_formatting(fields: dict[str, Any]) -> None:
    from openexecutive.delegation.gmail import DraftChanged

    graph = FakeGraph()
    graph.messages["d1"] = _msg("d1", isDraft=True, **fields)
    with pytest.raises(DraftChanged) as err:
        asyncio.run(graph.client().update_draft_text("d1", ol.draft_version("d1", "ck1"), "Hi"))
    assert err.value.reason == "not_plain"
    assert not any(r.method == "PATCH" for r in graph.graph_calls())


def test_update_draft_text_changed_between_read_and_write_is_refused() -> None:
    """A 412 on the PATCH (If-Match) means someone changed it meanwhile."""
    from openexecutive.delegation.gmail import DraftChanged

    graph = FakeGraph()
    graph.messages["d1"] = _msg("d1", isDraft=True)
    graph.fail["PATCH /messages/d1"] = 412
    with pytest.raises(DraftChanged) as err:
        asyncio.run(graph.client().update_draft_text("d1", ol.draft_version("d1", "ck1"), "Hi"))
    assert err.value.reason == "changed"


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



# --------------------------------------------------------------------------- #
# Read my calendar
# --------------------------------------------------------------------------- #


_DAY = (datetime(2026, 10, 14, 7, tzinfo=UTC), datetime(2026, 10, 15, 7, tzinfo=UTC))
_CALENDAR = {
    "value": [
        {"id": "e1", "subject": "Pilot review", "isAllDay": False,
         "start": {"dateTime": "2026-10-14T16:00:00.0000000", "timeZone": "UTC"},
         "end": {"dateTime": "2026-10-14T16:30:00.0000000", "timeZone": "UTC"},
         "location": {"displayName": "Room 4"},
         "organizer": {"emailAddress": {"name": "Dana", "address": "dana@northpeak.example"}},
         "responseStatus": {"response": "tentativelyAccepted"}},
        {"id": "e2", "subject": "Gone", "isCancelled": True, "start": {"dateTime": "2026-10-14T17:00:00"}},
        {"id": "e3", "subject": "Offsite", "isAllDay": True, "start": {"dateTime": "2026-10-14T00:00:00.0000000"},
         "end": {"dateTime": "2026-10-15T00:00:00.0000000"}},
    ],
}


def _calendar_graph(scope: str, *, status: int = 200) -> tuple[list[httpx.Request], DelegateOutlook]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3600, "scope": scope})
        return httpx.Response(status, json=_CALENDAR if status == 200 else {"error": {"code": "x"}})

    cred = OutlookCredential(email=EMAIL, refresh_token="r1", client_id="cid", tenant="common")
    return seen, DelegateOutlook(EMAIL, credential=cred, transport=httpx.MockTransport(handler))


_MAIL = "https://graph.microsoft.com/User.Read https://graph.microsoft.com/Mail.ReadWrite https://graph.microsoft.com/Mail.Send"


def test_their_outlook_calendar_is_read_in_utc() -> None:
    seen, client = _calendar_graph(f"{_MAIL} https://graph.microsoft.com/Calendars.Read")
    events = asyncio.run(client.list_events(*_DAY))
    assert [e.id for e in events] == ["e1", "e3"]
    pilot, offsite = events
    assert pilot.start == "2026-10-14T16:00:00+00:00" and pilot.response == "tentative"
    assert pilot.location == "Room 4" and pilot.organizer == "Dana <dana@northpeak.example>"
    assert offsite.all_day and offsite.start == "2026-10-14"
    call = seen[-1]
    assert call.url.path == "/v1.0/me/calendarView" and call.headers["Prefer"] == 'outlook.timezone="UTC"'


def test_an_outlook_sign_in_without_the_calendar_says_so() -> None:
    seen, client = _calendar_graph(_MAIL)
    with pytest.raises(CalendarNotGranted):
        asyncio.run(client.list_events(*_DAY))
    assert all(r.url.host == "login.microsoftonline.com" for r in seen)
    _seen, refused = _calendar_graph("", status=403)
    with pytest.raises(CalendarNotGranted):
        asyncio.run(refused.list_events(*_DAY))


def test_an_outlook_sign_in_says_whether_it_includes_the_calendar() -> None:
    _seen, with_it = _calendar_graph(f"{_MAIL} https://graph.microsoft.com/Calendars.Read")
    assert asyncio.run(with_it.calendar_granted()) is True
    ol._TOKENS.clear()
    _seen, without = _calendar_graph(_MAIL)
    assert asyncio.run(without.calendar_granted()) is False


def test_the_outlook_script_asks_for_the_calendar_only_with_the_flag() -> None:
    script = _script()
    assert "Calendars.Read" not in script.SCOPE
    assert "https://graph.microsoft.com/Calendars.Read" in script.scope_for((*script.GRAPH_SCOPES, script.CALENDAR_SCOPE))


def test_the_refresh_asks_for_the_calendar_only_when_it_was_granted(tmp_path: Path) -> None:
    script = _script()
    payload = script.credential_payload(
        EMAIL, "r1", "cid", "common", personal=False, graph_scopes=(*script.GRAPH_SCOPES, script.CALENDAR_SCOPE),
    )
    script.write_credential(tmp_path, EMAIL, payload)
    with_calendar = load_credential(EMAIL, directory=tmp_path)
    assert with_calendar is not None
    # --calendar asks for Calendars.ReadWrite now (it answers invites), and
    # the refresh keeps asking for it.
    assert unquote(_refresh_form(with_calendar)["scope"]).endswith("https://graph.microsoft.com/Calendars.ReadWrite")
    _write(tmp_path)
    mail_only = load_credential(EMAIL, directory=tmp_path)
    assert mail_only is not None and "Calendars" not in unquote(_refresh_form(mail_only)["scope"])


def _refresh_form(cred: OutlookCredential) -> dict[str, str]:
    """The form one token refresh for ``cred`` posts."""
    forms: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        forms.append(dict(pair.split("=", 1) for pair in request.content.decode().split("&")))
        return httpx.Response(200, json={"access_token": "at", "expires_in": 3600, "scope": ""})

    async def refresh() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            await DelegateOutlook(EMAIL, credential=cred)._access_token(http)

    ol._TOKENS.clear()
    asyncio.run(refresh())
    return forms[0]


def test_an_outlook_all_day_event_keeps_its_day_east_and_west_of_utc() -> None:
    def day(start: str, end: str, zone: str) -> tuple[str, str]:
        event = ol.parse_event({"id": "e", "subject": "Offsite", "isAllDay": True,
                                "start": {"dateTime": start}, "end": {"dateTime": end}}, ZoneInfo(zone))
        assert event is not None
        return event.start, event.end

    # The 14th's midnight, shown in UTC: Tokyo 15:00 and Auckland (+13) 11:00
    # on the 13th, New York 04:00 on the 14th.
    assert day("2026-10-13T15:00:00.0000000", "2026-10-14T15:00:00.0000000", "Asia/Tokyo") == ("2026-10-14", "2026-10-15")
    assert day("2026-10-13T11:00:00.0000000", "2026-10-14T11:00:00.0000000", "Pacific/Auckland") == (
        "2026-10-14", "2026-10-15",
    )
    assert day("2026-10-14T04:00:00.0000000", "2026-10-15T04:00:00.0000000", "America/New_York") == (
        "2026-10-14", "2026-10-15",
    )


def _new_draft_graph() -> tuple[list[httpx.Request], Any]:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.microsoftonline.com":
            return FakeGraph().handle(request)
        seen.append(request)
        path = unquote(request.url.path)
        if request.method == "POST" and path == "/v1.0/me/messages":
            return httpx.Response(201, json={"id": "new1", "changeKey": "k1", "conversationId": "c9"})
        if request.method == "POST" and path == "/v1.0/me/messages/new1/attachments":
            return httpx.Response(201, json={"id": "a1"})
        if request.method == "GET" and path == "/v1.0/me/messages/new1":
            return httpx.Response(200, json={"id": "new1", "changeKey": "k3", "conversationId": "c9"})
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(400)

    return seen, handle


def test_a_new_draft_carries_its_files_and_leaves_off_one_too_large() -> None:
    seen, handle = _new_draft_graph()
    client = DelegateOutlook(EMAIL, credential=OutlookCredential(EMAIL, "r1", "cid", "common"),
                             transport=httpx.MockTransport(handle))
    big = b"x" * (ol.OUTLOOK_ATTACHMENT_MAX_BYTES + 1)
    created = asyncio.run(client.create_draft(DraftSpec(
        to=["dana@northpeak.example"], subject="Bills", body="Attached.",
        attachments=[("bill.pdf", "application/pdf", b"%PDF-1"), ("huge.pdf", "application/pdf", big)],
    )))
    uploads = [r for r in seen if r.url.path.endswith("/attachments")]
    assert len(uploads) == 1
    body = json.loads(uploads[0].content)
    assert body["name"] == "bill.pdf" and base64.b64decode(body["contentBytes"]) == b"%PDF-1"
    # The version moved with the upload, so a later edit compares the right one.
    assert created.message_id == "new1.k3" and created.skipped_attachments == 1


def test_a_draft_whose_file_fails_is_removed() -> None:
    seen, handle = _new_draft_graph()

    def failing(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/attachments"):
            seen.append(request)
            return httpx.Response(500, json={"error": {"code": "x"}})
        return handle(request)

    client = DelegateOutlook(EMAIL, credential=OutlookCredential(EMAIL, "r1", "cid", "common"),
                             transport=httpx.MockTransport(failing))
    with pytest.raises(GmailError):
        asyncio.run(client.create_draft(DraftSpec(
            to=["a@b.example"], subject="x", body="y", attachments=[("a.pdf", "application/pdf", b"1")],
        )))
    assert any(r.method == "DELETE" and r.url.path.endswith("/messages/new1") for r in seen)


def test_latest_from_asks_for_one_senders_newest_inbox_mail() -> None:
    graph = FakeGraph()
    graph.listing = [_msg("in1", sender="o'neil@x.example")]
    found = asyncio.run(graph.client().latest_from("O'Neil@x.example"))
    assert found is not None and found.id == "in1"
    call = graph.graph_calls()[-1]
    assert call.url.path == "/v1.0/me/mailFolders/inbox/messages"
    flt = call.url.params["$filter"]
    assert flt.startswith("receivedDateTime ge ") and flt.endswith("from/emailAddress/address eq 'o''neil@x.example'")
    assert call.url.params["$orderby"] == "receivedDateTime desc"
    graph.listing = []
    assert asyncio.run(graph.client().latest_from("dana@northpeak.example")) is None
    assert asyncio.run(graph.client().latest_from("not an address")) is None


def test_each_recipient_keeps_the_name_it_was_written_with() -> None:
    raw = _msg("m1", toRecipients=[{"emailAddress": {"name": "Priya Shah", "address": "Priya@N.example"}}],
               ccRecipients=[{"emailAddress": {"address": "bare@x.example"}}])
    assert ol.parse_message(raw, SENT_FOLDER, EMAIL).names == {"priya@n.example": "Priya Shah", "bare@x.example": ""}


# --------------------------------------------------------------------------- #
# Meeting invites (delegation.invites)
# --------------------------------------------------------------------------- #


def _invite_graph(scope: str, message: dict[str, Any] | None = None) -> tuple[list[httpx.Request], DelegateOutlook]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3600, "scope": scope})
        if request.method == "POST":
            return httpx.Response(202)
        return httpx.Response(200, json=message or {})

    cred = OutlookCredential(email=EMAIL, refresh_token="r1", client_id="cid", tenant="common")
    return seen, DelegateOutlook(EMAIL, credential=cred, transport=httpx.MockTransport(handler))


def test_an_outlook_invite_is_its_own_event() -> None:
    from openexecutive.delegation.gmail import MailMessage

    ol._TOKENS.clear()
    message = {
        "id": "m1", "meetingMessageType": "meetingRequest",
        "event": {"id": "ev1", "subject": "Pilot review", "isAllDay": False,
                  "start": {"dateTime": "2026-10-14T16:00:00.0000000", "timeZone": "UTC"},
                  "end": {"dateTime": "2026-10-14T16:30:00.0000000", "timeZone": "UTC"},
                  "organizer": {"emailAddress": {"name": "Dana", "address": "dana@northpeak.example"}},
                  "responseStatus": {"response": "notResponded"}},
    }
    seen, client = _invite_graph(f"{_MAIL} https://graph.microsoft.com/Calendars.ReadWrite", message)
    event = asyncio.run(client.find_invite(MailMessage(id="m1", thread_id="c1", calendar_invite=True)))
    assert event is not None and (event.id, event.response) == ("ev1", "needs_action")
    assert event.start == "2026-10-14T16:00:00+00:00"
    call = seen[-1]
    assert call.url.path == "/v1.0/me/messages/m1" and "eventMessage/event" in call.url.params["$expand"]
    ol._TOKENS.clear()
    _seen, cancelled = _invite_graph(_MAIL + " https://graph.microsoft.com/Calendars.ReadWrite",
                                     {**message, "meetingMessageType": "meetingCancelled"})
    assert asyncio.run(cancelled.find_invite(MailMessage(id="m1", thread_id="c1"))) is None


def test_an_outlook_answer_is_graphs_accept_or_decline() -> None:
    ol._TOKENS.clear()
    seen, client = _invite_graph(f"{_MAIL} https://graph.microsoft.com/Calendars.ReadWrite")
    assert asyncio.run(client.calendar_write_granted()) is True
    asyncio.run(client.answer_invite("ev1", accept=True))
    asyncio.run(client.answer_invite("ev1", accept=False))
    posts = [r for r in seen if r.method == "POST" and r.url.host == "graph.microsoft.com"]
    assert [r.url.path for r in posts] == ["/v1.0/me/events/ev1/accept", "/v1.0/me/events/ev1/decline"]
    assert all(json.loads(r.content) == {"sendResponse": True} for r in posts)


def test_an_outlook_read_only_sign_in_cannot_answer() -> None:
    from openexecutive.delegation.gmail import CalendarWriteNotGranted

    ol._TOKENS.clear()
    seen, client = _invite_graph(f"{_MAIL} https://graph.microsoft.com/Calendars.Read")
    assert asyncio.run(client.calendar_granted()) is True
    assert asyncio.run(client.calendar_write_granted()) is False
    with pytest.raises(CalendarWriteNotGranted):
        asyncio.run(client.answer_invite("ev1", accept=True))
    assert all(r.url.host == "login.microsoftonline.com" for r in seen)


def test_an_older_read_only_credential_keeps_asking_for_read(tmp_path: Path) -> None:
    script = _script()
    payload = script.credential_payload(
        EMAIL, "r1", "cid", "common", personal=False, graph_scopes=(*script.GRAPH_SCOPES, "Calendars.Read"),
    )
    script.write_credential(tmp_path, EMAIL, payload)
    older = load_credential(EMAIL, directory=tmp_path)
    assert older is not None
    assert unquote(_refresh_form(older)["scope"]).endswith("https://graph.microsoft.com/Calendars.Read")
