"""The direct Gmail client for a person's own mailbox (delegation/gmail.py)
and the script that connects it (scripts/connect-own-gmail.py)."""
from __future__ import annotations

import asyncio
import base64
import email
import importlib.util
import inspect
import json
import re
import stat
import time
from email import policy
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import httpx
import pytest

from openexecutive.delegation import gmail as gm
from openexecutive.delegation.gmail import (
    DelegateGmail,
    DraftSpec,
    GmailAuthError,
    GmailCredential,
    GmailError,
    GmailNotConfigured,
    build_raw,
    email_key,
    gmail_link,
    gmail_status,
    load_credential,
    parse_message,
)

EMAIL = "olivia@co.example"
EXEC = "ceo.test@example.com"  # tests/conftest.py's EXEC_EMAIL_ADDRESS
SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "connect-own-gmail.py"


@pytest.fixture(autouse=True)
def _fresh_tokens() -> None:
    gm._TOKENS.clear()


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("connect_own_gmail", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cred() -> GmailCredential:
    return GmailCredential(email=EMAIL, refresh_token="r1", client_id="cid", client_secret="sec")


# --------------------------------------------------------------------------- #
# Credential file
# --------------------------------------------------------------------------- #


def test_the_script_writes_what_the_client_reads(tmp_path: Path) -> None:
    script = _script()
    assert script.email_key(" Olivia@Co.Example ") == email_key(EMAIL)
    payload = script.credential_payload(EMAIL, "r1", "cid", "sec")
    path = script.write_credential(tmp_path / "creds", EMAIL, payload)
    assert path.name == f"{email_key(EMAIL)}.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert load_credential(EMAIL, directory=tmp_path / "creds") == GmailCredential(
        email=EMAIL, refresh_token="r1", client_id="cid", client_secret="sec",
        token_uri="https://oauth2.googleapis.com/token",
    )
    # The script asks for exactly the scopes the client needs.
    assert list(script.SCOPES) == list(gm.SCOPES)


def _write(directory: Path, content: object, *, name: str = EMAIL) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{email_key(name)}.json").write_text(json.dumps(content))


@pytest.mark.parametrize("content", [
    "not json at all",
    {"email": "someone.else@co.example", "authorized_user": {"refresh_token": "r", "client_id": "c", "client_secret": "s"}},
    {"email": EMAIL, "authorized_user": {"refresh_token": "", "client_id": "c", "client_secret": "s"}},
    {"email": EMAIL},
    {"email": EMAIL, "authorized_user": {"refresh_token": "r", "client_id": "c", "client_secret": "s",
                                         "token_uri": "https://evil.example/token"}},
])
def test_a_bad_credential_file_is_ignored(tmp_path: Path, content: object) -> None:
    directory = tmp_path / "creds"
    if isinstance(content, str):
        directory.mkdir()
        (directory / f"{email_key(EMAIL)}.json").write_text(content)
    else:
        _write(directory, content)
    assert load_credential(EMAIL, directory=directory) is None


def test_no_file_no_credential(tmp_path: Path) -> None:
    assert load_credential(EMAIL, directory=tmp_path) is None
    assert load_credential("", directory=tmp_path) is None


# --------------------------------------------------------------------------- #
# Parsing and drafts
# --------------------------------------------------------------------------- #


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _message(headers: dict[str, str], *, plain: str | None = None, html: str | None = None,
             labels: list[str] | None = None, mid: str = "m1") -> dict[str, Any]:
    parts = []
    if plain is not None:
        parts.append({"mimeType": "text/plain", "body": {"data": _b64(plain)}})
    if html is not None:
        parts.append({"mimeType": "text/html", "body": {"data": _b64(html)}})
    return {
        "id": mid, "threadId": "t1", "labelIds": labels or ["INBOX"],
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [{"name": k, "value": v} for k, v in headers.items()],
            "parts": parts,
        },
    }


def test_a_message_is_parsed_into_what_a_reply_needs() -> None:
    parsed = parse_message(_message(
        {
            "From": "Dana Prospect <Dana@NorthPeak.example>",
            "To": "Olivia <olivia@co.example>, ops@co.example",
            "Cc": "sam@northpeak.example",
            "Reply-To": "billing@elsewhere.example",
            "Subject": "Brand refresh pilot",
            "Message-ID": "<abc@mail.example>",
            "References": "<prev@mail.example>",
            "List-Unsubscribe": "<mailto:x@y>",
        },
        plain="Can we start Oct 5?",
        html="<p>ignored when plain exists</p>",
    ))
    assert parsed.from_addr == "dana@northpeak.example"
    assert parsed.from_name == "Dana Prospect"
    assert parsed.to == ["olivia@co.example", "ops@co.example"]
    assert parsed.cc == ["sam@northpeak.example"]
    assert parsed.reply_to == "billing@elsewhere.example"
    assert parsed.message_id_header == "<abc@mail.example>"
    assert parsed.text == "Can we start Oct 5?"
    assert parsed.mailing_list is True


def test_html_only_mail_becomes_text() -> None:
    parsed = parse_message(_message(
        {"From": "a@b.example", "Auto-Submitted": "auto-replied"},
        html="<div>Hello<br>there</div><script>x()</script><p>&amp; bye</p>",
    ))
    assert parsed.text.splitlines() == ["Hello", "there", "& bye"]
    assert parsed.auto_generated is True


@pytest.mark.parametrize(("markup", "lines"), [
    # Gmail's signature editor: the first line sits in the outer <div>, each
    # later line in a <div> of its own.
    (
        '<div dir="ltr">Olivia Owner<div>Fernway Studio</div><div><span>olivia@fernway.example</span></div></div>',
        ["Olivia Owner", "Fernway Studio", "olivia@fernway.example"],
    ),
    ("<div>One</div><div>Two</div>", ["One", "Two"]),  # no blank line between blocks
    ("<div>One</div><div><br></div><div>Three</div>", ["One", "", "Three"]),  # Gmail's blank line
    ("<div>One<br></div><div>Two</div>", ["One", "Two"]),  # a closing <br> adds no line
    ("<p>One</p><p>Two</p>", ["One", "Two"]),
    ("<p>Hi Dana,</p><p>&nbsp;</p><p>Thanks.</p>", ["Hi Dana,", "", "Thanks."]),  # an editor's blank line
    ("Before<table><tr><td>Cell</td></tr></table>After", ["Before", "Cell", "After"]),
    ("Intro<ul><li>One</li><li>Two</li></ul>", ["Intro", "One", "Two"]),
    ("a<br/>b<BR >c", ["a", "b", "c"]),
    ("<picture>Not</picture> a <param>block", ["Not a block"]),
    ("Nul\x00 <div>kept out</div>", ["Nul", "kept out"]),
    ("a<style>p {}</style>b<script>x()</script>c<script>never closed", ["abcnever closed"]),
    # A tag runs to the next ">", whatever its attributes hold.
    ('<a href="https://x.example/" title="<">click</a> here', ["click here"]),
    ("if x<y then <b>bold</b>", ["if xbold"]),
    ("a <> b &#00065; &#" + "1" * 5000 + ";", ["a <> b A \ufffd"]),  # Python refuses to int() 4,301+ digits
])
def test_html_becomes_the_lines_it_shows(markup: str, lines: list[str]) -> None:
    assert gm.html_to_text(markup).split("\n") == lines


@pytest.mark.parametrize("markup", [
    "<" + " " * 100_000,
    "</" + " " * 100_000,
    "<" * 100_000,
    "<div" * 25_000,
    "<br" * 33_000,
    "<script" * 14_000,
    "<script>" + "</script" + " " * 100_000,
    "&#" + "0" * 100_000 + "65;",
])
def test_crafted_html_cannot_stall_the_parser(markup: str) -> None:
    # Anyone can send the mail this parses, and it runs on the event loop: a
    # backtracking pattern took minutes on inputs like these.
    started = time.perf_counter()
    gm.html_to_text(markup)
    assert time.perf_counter() - started < 2


def test_a_draft_is_threaded_and_cannot_smuggle_a_header() -> None:
    raw = build_raw(EMAIL, DraftSpec(
        to=["dana@northpeak.example"],
        cc=["sam@northpeak.example"],
        subject="Re: Brand refresh pilot\r\nBcc: attacker@evil.example",
        body="Yes to Oct 5.\n\nBest,\nOlivia",
        in_reply_to="<abc@mail.example>",
        references="<prev@mail.example> <abc@mail.example>",
        from_name="Olivia Owner",
    ))
    msg = email.message_from_bytes(base64.urlsafe_b64decode(raw), policy=policy.default)
    assert msg["To"] == "dana@northpeak.example"
    assert msg["Cc"] == "sam@northpeak.example"
    assert msg["Bcc"] is None
    assert "attacker" in msg["Subject"]  # kept as text, not a header
    assert msg["In-Reply-To"] == "<abc@mail.example>"
    assert msg["X-OE-Ghostwritten"] == "1"
    assert "Olivia Owner" in msg["From"] and EMAIL in msg["From"]
    assert msg.get_content().strip().startswith("Yes to Oct 5.")


def test_links_open_the_draft_in_the_right_account() -> None:
    assert gmail_link(EMAIL, thread_id="18c2f") == (
        "https://mail.google.com/mail/u/?authuser=olivia%40co.example#all/18c2f"
    )
    assert gmail_link(EMAIL, message_id="18d") .endswith("#drafts?compose=18d")
    assert gmail_link(EMAIL, thread_id="../evil").endswith("#drafts")


# --------------------------------------------------------------------------- #
# The client, against a mocked Google
# --------------------------------------------------------------------------- #


class FakeGoogle:
    def __init__(self, *, token_status: int = 200, token_error: str = "",
                 scope: str = " ".join(gm.SCOPES), api_status: int = 200,
                 api_reason: str = "") -> None:
        self.token_status = token_status
        self.token_error = token_error
        self.scope = scope
        self.api_status = api_status
        self.api_reason = api_reason
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "oauth2.googleapis.com":
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": self.token_error})
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3600, "scope": self.scope})
        assert request.headers["Authorization"] == "Bearer at"
        if self.api_status != 200:
            body = {"error": {"code": self.api_status, "errors": [{"reason": self.api_reason}]}}
            return httpx.Response(self.api_status, json=body if self.api_reason else {})
        path = request.url.path
        if path.endswith("/profile"):
            return httpx.Response(200, json={"emailAddress": "Olivia@Co.Example"})
        if path.endswith("/drafts") and request.method == "POST":
            return httpx.Response(200, json={"id": "d1", "message": {"id": "m9", "threadId": "t1"}})
        if path.endswith("/settings/sendAs"):
            return httpx.Response(200, json={"sendAs": [
                {"sendAsEmail": EMAIL, "isPrimary": True, "signature": "<div>Olivia Owner<br>Fernway</div>"},
            ]})
        return httpx.Response(404, json={})

    def client(self) -> DelegateGmail:
        return DelegateGmail(EMAIL, credential=_cred(), transport=httpx.MockTransport(self.handler))


def test_the_token_is_refreshed_once_and_reused() -> None:
    google = FakeGoogle()
    client = google.client()
    assert asyncio.run(client.profile_email()) == EMAIL
    assert asyncio.run(client.profile_email()) == EMAIL
    token_calls = [r for r in google.requests if r.url.host == "oauth2.googleapis.com"]
    assert len(token_calls) == 1
    assert b"grant_type=refresh_token" in token_calls[0].content


def test_a_draft_is_saved_in_their_thread() -> None:
    google = FakeGoogle()
    created = asyncio.run(google.client().create_draft(DraftSpec(
        to=["dana@northpeak.example"], subject="Re: Pilot", body="Yes.", thread_id="t1",
    )))
    assert (created.draft_id, created.message_id, created.thread_id) == ("d1", "m9", "t1")
    post = next(r for r in google.requests if r.method == "POST" and r.url.path.endswith("/drafts"))
    sent = json.loads(post.content)
    assert sent["message"]["threadId"] == "t1"
    assert "raw" in sent["message"]


def test_the_signature_comes_from_gmail_settings() -> None:
    assert asyncio.run(FakeGoogle().client().send_as_signature()) == "Olivia Owner\nFernway"


@pytest.mark.parametrize("google", [
    FakeGoogle(token_status=400, token_error="invalid_grant"),
    FakeGoogle(scope=gm.SCOPE_READONLY),  # consent without compose
    FakeGoogle(api_status=401),
    FakeGoogle(api_status=403, api_reason="insufficientPermissions"),
])
def test_google_refusing_the_credential_is_an_auth_error(google: FakeGoogle) -> None:
    with pytest.raises(GmailAuthError):
        asyncio.run(google.client().profile_email())


def test_no_credential_is_not_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gm, "credentials_dir", lambda: tmp_path)
    with pytest.raises(GmailNotConfigured):
        asyncio.run(DelegateGmail(EMAIL).profile_email())


def test_phase_one_cannot_send() -> None:
    """Drafts only: no method sends, and no send endpoint is ever named."""
    public = {n for n, _ in inspect.getmembers(DelegateGmail, inspect.isfunction) if not n.startswith("_")}
    assert public == {
        "profile_email", "search_threads", "get_thread", "list_sent", "send_as_signature", "create_draft",
    }
    # Gmail's send endpoints (/settings/sendAs is a read, and allowed).
    assert not re.search(r"/(messages|drafts)/send\b", inspect.getsource(gm))


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #


class _Mailbox:
    def __init__(self, opened: str = EMAIL, error: Exception | None = None) -> None:
        self.opened = opened
        self.error = error

    async def profile_email(self) -> str:
        if self.error is not None:
            raise self.error
        return self.opened


@pytest.mark.parametrize(("person_email", "mailbox", "expected"), [
    (EMAIL, _Mailbox(), "connected"),
    (EMAIL, _Mailbox(opened="other@co.example"), "mismatch"),
    (EMAIL, _Mailbox(error=GmailAuthError("invalid_grant")), "needs_reconnect"),
    (EMAIL, _Mailbox(error=GmailNotConfigured(EMAIL)), "not_configured"),
    (EMAIL, _Mailbox(error=RuntimeError("network")), "error"),
    ("", _Mailbox(), "no_email"),
    (EXEC.upper(), _Mailbox(opened=EXEC), "shared_mailbox"),
])
def test_status(person_email: str, mailbox: _Mailbox, expected: str) -> None:
    assert asyncio.run(gmail_status(person_email, gmail=mailbox)) == expected


def test_a_shared_mailbox_is_refused_before_google_is_asked() -> None:
    mailbox = SimpleNamespace(profile_email=None)  # would fail if called
    assert asyncio.run(gmail_status(EXEC, gmail=mailbox)) == "shared_mailbox"


@pytest.mark.parametrize(("status", "reason"), [
    (403, "userRateLimitExceeded"), (403, "rateLimitExceeded"), (429, "rateLimitExceeded"),
])
def test_a_rate_limit_is_not_a_reconnect(status: int, reason: str) -> None:
    google = FakeGoogle(api_status=status, api_reason=reason)
    with pytest.raises(GmailError) as err:
        asyncio.run(google.client().profile_email())
    assert not isinstance(err.value, GmailAuthError)
    assert asyncio.run(gm.gmail_status(EMAIL, gmail=google.client())) == "error"


def test_a_long_reference_chain_keeps_whole_ids_the_root_and_the_parent() -> None:
    chain = " ".join(f"<id{i:02d}-{'x' * 60}@mail.example>" for i in range(20))
    refs = gm.references_header(chain, "<parent@mail.example>")
    assert refs is not None and len(refs) <= 900
    ids = refs.split()
    assert ids[0].startswith("<id00-") and ids[-1] == "<parent@mail.example>"
    assert all(i.startswith("<") and i.endswith(">") for i in ids)
    assert gm.references_header("<a@x> <b@x>", "<b@x>") == "<a@x> <b@x>"
    assert gm.references_header("", "") is None


def test_the_parent_is_last_even_when_the_chain_already_holds_it() -> None:
    assert gm.references_header("<r@x> <p@x> <q@x>", "<p@x>") == "<r@x> <q@x> <p@x>"


def test_a_parent_too_long_for_the_header_gives_no_header() -> None:
    assert gm.references_header("", "<" + "a" * 950 + ">") is None


@pytest.mark.parametrize("body", [{"error": {"errors": 1}}, {"error": "denied"}, ["x"]])
def test_an_odd_403_body_is_not_a_rate_limit(body: object) -> None:
    assert gm._rate_limited(httpx.Response(403, json=body)) is False
