"""A direct Gmail client for one person's own mailbox (Act as me).

The Executive's own mailbox is reached through the MCP gateway, where the
model can call any Gmail tool by name. A mailbox someone lent the Executive
must never be reachable that way, so this client talks to the Gmail REST API
itself and is never registered with the gateway: only typed handlers
(``orchestrator.delegation_tools``, the voice learner) call it.

**Credential.** One file per person in ``DELEGATION_GOOGLE_CREDENTIALS_DIR``,
named from a hash of the address and written by
``scripts/connect-own-gmail.py``: ``{"version": 1, "email": ..., "authorized_user":
{refresh_token, client_id, client_secret, token_uri}}``. It is never in
``WORKSPACE_MCP_CREDENTIALS_DIR`` — workspace-mcp picks a credential there by
address, which would hand the model this mailbox. Scopes: ``gmail.readonly``
and ``gmail.compose``.

**Drafts only.** There is deliberately no send method in Phase 1 (a unit test
fails if one appears). ``gmail.compose`` could send; nothing here does.

**Always checked.** ``gmail_status`` asks Google whose mailbox the token opens
and compares it with the person's People email on every use — a roster email
can change, and a client slot swaps the roster — and refuses the Executive's
own address outright (a "shared mailbox" would make everything the Executive
writes look like the person's).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import logging
import re
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, getaddresses
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
SCOPE_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
SCOPE_COMPOSE = "https://www.googleapis.com/auth/gmail.compose"
SCOPES: tuple[str, ...] = (SCOPE_READONLY, SCOPE_COMPOSE)
CREDENTIAL_VERSION = 1

# Marks a draft this package wrote. Best-effort only: Gmail may rebuild a
# draft when it is sent from its own UI.
GHOSTWRITTEN_HEADER = "X-OE-Ghostwritten"

GmailStatus = Literal[
    "connected",
    "not_configured",
    "needs_reconnect",
    "mismatch",
    "no_email",
    "shared_mailbox",
    "error",
]

# What each status tells the person, in the tool result and on Settings.
STATUS_MESSAGES: dict[str, str] = {
    "connected": "Connected.",
    "not_configured": (
        "Your Gmail isn't connected. Run scripts/connect-own-gmail.py as yourself "
        "(Settings → Act as me shows how)."
    ),
    "needs_reconnect": (
        "Google no longer accepts the saved sign-in for your Gmail. Connect it again "
        "with scripts/connect-own-gmail.py."
    ),
    "mismatch": (
        "The connected Gmail isn't the address on your People entry. Connect that "
        "account, or correct your email on the People page."
    ),
    "no_email": "Your People entry has no email address. Add it on the People page first.",
    "shared_mailbox": (
        "Your address is the Executive's own mailbox, so it can't write as you from "
        "it. Give the Executive a Google account of its own first."
    ),
    "error": "Couldn't reach your Gmail just now. Try again in a moment.",
}

# Gmail ids are short hex strings; anything else never reaches a URL path.
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TOKEN_URI_RE = re.compile(r"^https://oauth2\.googleapis\.com/")
_MAX_HEADER = 900
_TOKEN_SLACK_SECONDS = 60
_FETCH_CONCURRENCY = 5

# Access tokens by (address, refresh token) — never written anywhere.
_TOKENS: dict[str, tuple[str, float]] = {}


class GmailError(Exception):
    """A Gmail call failed (network, 5xx, an unexpected response)."""


class GmailNotConfigured(GmailError):
    """No credential for this address."""


class GmailAuthError(GmailError):
    """Google refused the credential: revoked, expired, or missing a scope."""


@dataclass(frozen=True)
class GmailCredential:
    email: str
    refresh_token: str
    client_id: str
    client_secret: str
    token_uri: str = DEFAULT_TOKEN_URI


@dataclass
class MailMessage:
    id: str
    thread_id: str
    from_addr: str = ""
    from_name: str = ""
    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    reply_to: str = ""
    subject: str = ""
    date: str = ""
    message_id_header: str = ""
    references: str = ""
    labels: list[str] = field(default_factory=list)
    text: str = ""
    mailing_list: bool = False
    auto_generated: bool = False
    ghostwritten: bool = False


@dataclass
class MailThread:
    id: str
    messages: list[MailMessage]


@dataclass
class ThreadSummary:
    id: str
    subject: str
    sender: str
    date: str


@dataclass
class DraftSpec:
    to: list[str]
    subject: str
    body: str
    cc: list[str] = field(default_factory=list)
    thread_id: str | None = None
    in_reply_to: str | None = None
    references: str | None = None
    from_name: str = ""


@dataclass
class CreatedDraft:
    draft_id: str
    message_id: str
    thread_id: str


# --------------------------------------------------------------------------- #
# Credential file
# --------------------------------------------------------------------------- #


def normalize_email(email: str | None) -> str:
    return (email or "").strip().lower()


def email_key(email: str) -> str:
    """The credential file's stem for ``email`` (so the address is not in a
    file name on the volume)."""
    return hashlib.sha256(normalize_email(email).encode()).hexdigest()[:16]


def credentials_dir() -> Path:
    from openexecutive.config import get_settings

    return Path(get_settings().delegation_google_credentials_dir)


def credential_path(email: str, *, directory: Path | None = None) -> Path:
    return (directory or credentials_dir()) / f"{email_key(email)}.json"


def load_credential(email: str, *, directory: Path | None = None) -> GmailCredential | None:
    """The stored credential for ``email``, or None when there is none or it
    is unreadable (logged). A file naming another address is ignored."""
    address = normalize_email(email)
    if not address:
        return None
    path = credential_path(address, directory=directory)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("delegation.gmail: unreadable credential file %s", path.name)
        return None
    if not isinstance(data, dict) or normalize_email(str(data.get("email") or "")) != address:
        logger.warning("delegation.gmail: credential file %s is for another address", path.name)
        return None
    user = data.get("authorized_user")
    if not isinstance(user, dict):
        return None
    values = {k: user.get(k) for k in ("refresh_token", "client_id", "client_secret")}
    if not all(isinstance(v, str) and v for v in values.values()):
        logger.warning("delegation.gmail: credential file %s is incomplete", path.name)
        return None
    token_uri = user.get("token_uri") or DEFAULT_TOKEN_URI
    if not isinstance(token_uri, str) or not _TOKEN_URI_RE.match(token_uri):
        logger.warning("delegation.gmail: credential file %s names an unexpected token endpoint", path.name)
        return None
    return GmailCredential(
        email=address,
        refresh_token=str(values["refresh_token"]),
        client_id=str(values["client_id"]),
        client_secret=str(values["client_secret"]),
        token_uri=token_uri,
    )


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def _b64decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _headers(payload: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for h in payload.get("headers") or []:
        if isinstance(h, dict) and isinstance(h.get("name"), str):
            out.setdefault(h["name"].lower(), str(h.get("value") or ""))
    return out


# Everything here runs on mail anyone can send, on the event loop, so each step
# is one forward pass: these patterns end at the next "<" as well as at ">",
# and no two quantifiers compete for the same characters.
_BR_RE = re.compile(r"(?i)<\s*(?:/\s*)?br\b[^<>]*>")
# Opening and closing tags alike: Gmail puts a signature's first line straight
# in its outer <div> and each later line in a <div> of its own.
_BLOCK_EDGE_RE = re.compile(
    r"(?i)<\s*(?:/\s*)?(?:address|article|aside|blockquote|center|dd|div|dl|dt|figcaption|figure|"
    r"footer|h[1-6]|header|hr|li|main|nav|ol|p|pre|section|table|tr|ul)\b[^<>]*>"
)
_HIDDEN = {
    name: (re.compile(rf"(?i)<{name}\b"), re.compile(rf"(?i)</{name}\s*>")) for name in ("script", "style")
}
_DECIMAL_REF_RE = re.compile(r"&#(\d+)(;?)")
_EDGE = "\x00"


def _strip_tags(text: str) -> str:
    """``text`` without its tags — each "<" through the next ">", with
    something between — in one forward pass."""
    out: list[str] = []
    at = 0
    while (lt := text.find("<", at)) != -1:
        gt = text.find(">", lt + 1)
        if gt == -1:
            break  # nothing after this closes a tag
        out.append(text[at:gt + 1] if gt == lt + 1 else text[at:lt])
        at = gt + 1
    out.append(text[at:])
    return "".join(out)


def _short_decimal_ref(match: re.Match[str]) -> str:
    # html.unescape turns the digits into an int, and Python refuses more than
    # 4,300 of them (ValueError). Beyond seven significant digits the value is
    # past Unicode's range, which unescape reads as U+FFFD anyway.
    digits = match.group(1).lstrip("0") or "0"
    return "�" if len(digits) > 7 else f"&#{digits}{match.group(2)}"


def _drop_hidden(markup: str) -> str:
    """``markup`` without its ``<script>`` and ``<style>`` elements, in one
    pass each: an element nothing closes is left for the tag strip."""
    for opening, closing in _HIDDEN.values():
        out: list[str] = []
        at = 0
        while (start := opening.search(markup, at)) is not None:
            end = closing.search(markup, start.end())
            if end is None:
                break
            out.append(markup[at:start.start()])
            at = end.end()
        out.append(markup[at:])
        markup = "".join(out)
    return markup


def html_to_text(markup: str) -> str:
    """Plain text from an HTML body or signature. A ``<br>`` (or a line break
    in the markup) always ends a line; a block's opening or closing tag ends
    one only when it has text, so ``</div><div>`` is a single break and
    ``<div><br></div>`` a blank line."""
    text = _drop_hidden(markup.replace(_EDGE, ""))
    text = _BLOCK_EDGE_RE.sub(_EDGE, _BR_RE.sub("\n", text))
    text = html.unescape(_DECIMAL_REF_RE.sub(_short_decimal_ref, _strip_tags(text)))
    out: list[str] = []
    has_text = False
    for piece in re.split(f"({_EDGE}|\n)", text):
        if piece == "\n" or (piece == _EDGE and has_text):
            out.append("\n")
            has_text = False
        elif piece != _EDGE:
            out.append(piece)
            # Only markup whitespace: a line of &nbsp; is a line the reader sees.
            has_text = has_text or bool(piece.strip(" \t\r\n\f\v"))
    lines = [ln.rstrip() for ln in "".join(out).splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _part_text(part: dict[str, Any]) -> str:
    data = (part.get("body") or {}).get("data")
    if not isinstance(data, str) or not data:
        return ""
    try:
        return _b64decode(data).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return ""


def _body_text(payload: dict[str, Any]) -> str:
    """The message's plain text: its text/plain part, else its HTML as text."""
    plain: list[str] = []
    markup: list[str] = []
    stack = [payload]
    while stack:
        part = stack.pop(0)
        mime = str(part.get("mimeType") or "").lower()
        if mime == "text/plain":
            plain.append(_part_text(part))
        elif mime == "text/html":
            markup.append(_part_text(part))
        stack.extend(p for p in part.get("parts") or [] if isinstance(p, dict))
    if any(t.strip() for t in plain):
        return "\n".join(t for t in plain if t.strip()).strip()
    return html_to_text("\n".join(markup))


def _addresses(value: str) -> list[str]:
    return [normalize_email(addr) for _, addr in getaddresses([value]) if "@" in addr]


def parse_message(raw: dict[str, Any]) -> MailMessage:
    """A Gmail API ``format=full`` message as a ``MailMessage``."""
    raw_payload = raw.get("payload")
    payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
    headers = _headers(payload)
    sender = getaddresses([headers.get("from", "")])
    from_name, from_addr = sender[0] if sender else ("", "")
    auto = headers.get("auto-submitted", "no").strip().lower() not in ("", "no")
    return MailMessage(
        id=str(raw.get("id") or ""),
        thread_id=str(raw.get("threadId") or ""),
        from_addr=normalize_email(from_addr),
        from_name=from_name.strip(),
        to=_addresses(headers.get("to", "")),
        cc=_addresses(headers.get("cc", "")),
        reply_to=",".join(_addresses(headers.get("reply-to", ""))),
        subject=headers.get("subject", "").strip(),
        date=headers.get("date", "").strip(),
        message_id_header=headers.get("message-id", "").strip(),
        references=headers.get("references", "").strip(),
        labels=[str(label) for label in raw.get("labelIds") or []],
        text=_body_text(payload),
        mailing_list=bool(headers.get("list-unsubscribe") or headers.get("list-id")),
        auto_generated=auto or "calendar-notification" in headers.get("sender", "").lower(),
        ghostwritten=bool(headers.get(GHOSTWRITTEN_HEADER.lower())),
    )


# --------------------------------------------------------------------------- #
# Drafts
# --------------------------------------------------------------------------- #


def clean_header(value: str) -> str:
    """One header value, with no line breaks or control characters, capped."""
    cleaned = "".join(ch if ch >= " " else " " for ch in value.replace("\r", " ").replace("\n", " "))
    return " ".join(cleaned.split())[:_MAX_HEADER]


_MESSAGE_ID = re.compile(r"<[^<>\s]+>")


def references_header(prior: str, parent: str) -> str | None:
    """The References of a reply to ``parent`` (a Message-ID) whose own
    References were ``prior``: whole ids only, the parent always last. When
    the chain is too long for one header, the oldest ids after the thread's
    first one go — never half an id, and never the parent."""
    parents = _MESSAGE_ID.findall(parent or "")[-1:]
    ids = [i for i in dict.fromkeys(_MESSAGE_ID.findall(prior or "")) if i not in parents] + parents
    if not ids:
        return None
    while len(ids) > 2 and len(" ".join(ids)) > _MAX_HEADER:
        del ids[1]
    if len(" ".join(ids)) > _MAX_HEADER:
        ids = ids[-1:]
    joined = " ".join(ids)
    # A single id too long for the header: none beats half of one.
    return joined if len(joined) <= _MAX_HEADER else None


def build_raw(sender: str, spec: DraftSpec) -> str:
    """The draft as a base64url RFC 2822 message (Gmail's ``raw``)."""
    msg = EmailMessage()
    name = clean_header(spec.from_name)
    msg["From"] = formataddr((name, sender)) if name else sender
    msg["To"] = ", ".join(clean_header(a) for a in spec.to)
    if spec.cc:
        msg["Cc"] = ", ".join(clean_header(a) for a in spec.cc)
    msg["Subject"] = clean_header(spec.subject)
    if spec.in_reply_to:
        msg["In-Reply-To"] = clean_header(spec.in_reply_to)
    if spec.references:
        msg["References"] = clean_header(spec.references)
    msg[GHOSTWRITTEN_HEADER] = "1"
    msg.set_content(spec.body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


def gmail_link(email: str, *, thread_id: str | None = None, message_id: str | None = None) -> str:
    """A link that opens the draft in Gmail, built from a fixed prefix: the
    thread for a reply, the draft itself for a new email."""
    base = f"https://mail.google.com/mail/u/?authuser={quote(normalize_email(email))}"
    if thread_id and _ID_RE.match(thread_id):
        return f"{base}#all/{thread_id}"
    if message_id and _ID_RE.match(message_id):
        return f"{base}#drafts?compose={message_id}"
    return f"{base}#drafts"


def valid_id(value: object) -> bool:
    return isinstance(value, str) and bool(_ID_RE.match(value))


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


# Gmail answers some quota errors with 403, not 429: slow down, don't reconnect.
_RATE_LIMIT_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded", "dailyLimitExceeded", "quotaExceeded"})


def _rate_limited(resp: httpx.Response) -> bool:
    """Whether a 403 is Gmail's rate limit rather than a refused sign-in."""
    try:
        errors = resp.json().get("error", {}).get("errors", [])
    except (ValueError, AttributeError):
        return False
    if not isinstance(errors, list):
        return False
    return any(isinstance(e, dict) and e.get("reason") in _RATE_LIMIT_REASONS for e in errors)


def _token_key(cred: GmailCredential) -> str:
    return hashlib.sha256(f"{cred.email}\n{cred.refresh_token}".encode()).hexdigest()


class DelegateGmail:
    """One person's mailbox. ``transport`` is for tests (httpx MockTransport)."""

    def __init__(
        self,
        email: str,
        *,
        credential: GmailCredential | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.email = normalize_email(email)
        self._credential = credential
        self._transport = transport
        self._timeout = timeout

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._timeout, transport=self._transport)

    def _cred(self) -> GmailCredential:
        cred = self._credential or load_credential(self.email)
        if cred is None:
            raise GmailNotConfigured(self.email)
        return cred

    async def _access_token(self, client: httpx.AsyncClient) -> str:
        cred = self._cred()
        key = _token_key(cred)
        cached = _TOKENS.get(key)
        if cached is not None and cached[1] - _TOKEN_SLACK_SECONDS > time.time():
            return cached[0]
        try:
            resp = await client.post(
                cred.token_uri,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": cred.refresh_token,
                    "client_id": cred.client_id,
                    "client_secret": cred.client_secret,
                },
            )
        except httpx.HTTPError as exc:
            raise GmailError(f"token refresh failed: {type(exc).__name__}") from exc
        if resp.status_code in (400, 401):
            try:
                code = str(resp.json().get("error") or "")
            except ValueError:
                code = ""
            if code in ("invalid_grant", "unauthorized_client", "invalid_client") or resp.status_code == 401:
                raise GmailAuthError(code or "unauthorized")
        if resp.status_code >= 400:
            raise GmailError(f"token refresh returned {resp.status_code}")
        try:
            payload = resp.json()
            token = str(payload["access_token"])
            ttl = int(payload.get("expires_in", 3600))
        except (ValueError, KeyError, TypeError) as exc:
            raise GmailError("token refresh returned an unexpected response") from exc
        granted = str(payload.get("scope") or "")
        if granted and not set(SCOPES) <= set(granted.split()):
            raise GmailAuthError("missing_scope")
        _TOKENS[key] = (token, time.time() + ttl)
        return token

    async def _get(
        self, client: httpx.AsyncClient, path: str, params: Any = None
    ) -> dict[str, Any]:
        return await self._request(client, "GET", path, params=params)

    async def _request(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        params: Any = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        token = await self._access_token(client)
        try:
            resp = await client.request(
                method,
                f"{GMAIL_BASE}{path}",
                params=params,
                json=json_body,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            raise GmailError(f"gmail {method} failed: {type(exc).__name__}") from exc
        if resp.status_code == 401:
            _TOKENS.pop(_token_key(self._cred()), None)
            raise GmailAuthError("unauthorized")
        if resp.status_code == 403 and not _rate_limited(resp):
            raise GmailAuthError("forbidden")
        if resp.status_code >= 400:
            raise GmailError(f"gmail {method} returned {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise GmailError("gmail returned a non-JSON response") from exc
        return data if isinstance(data, dict) else {}

    async def profile_email(self) -> str:
        """The address the credential opens, as Google reports it."""
        async with self._client() as client:
            data = await self._get(client, "/profile")
        return normalize_email(str(data.get("emailAddress") or ""))

    async def search_threads(self, query: str, *, max_results: int = 5) -> list[ThreadSummary]:
        """Threads matching a Gmail search, newest first: subject, sender and
        date only (no body text)."""
        async with self._client() as client:
            data = await self._get(
                client, "/threads", {"q": query[:300], "maxResults": max(1, min(max_results, 10))}
            )
            ids = [str(t.get("id")) for t in data.get("threads") or [] if valid_id(t.get("id"))]
            summaries: list[ThreadSummary] = []
            for thread_id in ids:
                meta = await self._get(
                    client,
                    f"/threads/{thread_id}",
                    [("format", "metadata"), ("metadataHeaders", "Subject"),
                     ("metadataHeaders", "From"), ("metadataHeaders", "Date")],
                )
                messages = [m for m in meta.get("messages") or [] if isinstance(m, dict)]
                headers = _headers(messages[-1].get("payload") or {}) if messages else {}
                summaries.append(ThreadSummary(
                    id=thread_id,
                    subject=headers.get("subject", ""),
                    sender=headers.get("from", ""),
                    date=headers.get("date", ""),
                ))
        return summaries

    async def get_thread(self, thread_id: str) -> MailThread:
        if not valid_id(thread_id):
            raise GmailError("invalid thread id")
        async with self._client() as client:
            data = await self._get(client, f"/threads/{thread_id}", {"format": "full"})
        messages = [parse_message(m) for m in data.get("messages") or [] if isinstance(m, dict)]
        return MailThread(id=thread_id, messages=messages)

    async def list_sent(self, limit: int = 40) -> list[MailMessage]:
        """The person's recent sent mail, newest first (at most ``limit``)."""
        async with self._client() as client:
            data = await self._get(
                client,
                "/messages",
                {"q": "in:sent -in:chats newer_than:1y", "maxResults": max(1, min(limit, 100))},
            )
            ids = [str(m.get("id")) for m in data.get("messages") or [] if valid_id(m.get("id"))]
            gate = asyncio.Semaphore(_FETCH_CONCURRENCY)

            async def fetch(message_id: str) -> MailMessage | GmailAuthError | None:
                # Returns the auth error rather than raising it, so every
                # fetch finishes before the client closes.
                async with gate:
                    try:
                        raw = await self._get(client, f"/messages/{message_id}", {"format": "full"})
                    except GmailAuthError as exc:
                        return exc
                    except GmailError:
                        logger.warning("delegation.gmail: skipped an unreadable sent message")
                        return None
                return parse_message(raw)

            fetched = await asyncio.gather(*(fetch(i) for i in ids))
        refused = next((f for f in fetched if isinstance(f, GmailAuthError)), None)
        if refused is not None:
            raise refused
        return [m for m in fetched if isinstance(m, MailMessage)]

    async def send_as_signature(self) -> str:
        """The signature on the person's primary Gmail address, as plain text."""
        async with self._client() as client:
            data = await self._get(client, "/settings/sendAs")
        entries = [e for e in data.get("sendAs") or [] if isinstance(e, dict)]
        primary = next((e for e in entries if e.get("isPrimary")), None) or next(
            (e for e in entries if normalize_email(str(e.get("sendAsEmail") or "")) == self.email),
            None,
        )
        return html_to_text(str((primary or {}).get("signature") or ""))

    async def create_draft(self, spec: DraftSpec) -> CreatedDraft:
        """Save ``spec`` as a draft in the person's Gmail. Nothing is sent."""
        if spec.thread_id is not None and not valid_id(spec.thread_id):
            raise GmailError("invalid thread id")
        message: dict[str, Any] = {"raw": build_raw(self.email, spec)}
        if spec.thread_id:
            message["threadId"] = spec.thread_id
        async with self._client() as client:
            data = await self._request(client, "POST", "/drafts", json_body={"message": message})
        raw_created = data.get("message")
        created: dict[str, Any] = raw_created if isinstance(raw_created, dict) else {}
        return CreatedDraft(
            draft_id=str(data.get("id") or ""),
            message_id=str(created.get("id") or ""),
            thread_id=str(created.get("threadId") or spec.thread_id or ""),
        )


def gmail_for(email: str) -> DelegateGmail:
    """The client for ``email``'s own mailbox."""
    return DelegateGmail(email)


async def gmail_status(person_email: str | None, *, gmail: Any = None) -> GmailStatus:
    """Whether ``person_email``'s own mailbox can be used right now. Asks
    Google which mailbox the credential opens. Never raises."""
    from openexecutive.config import get_settings

    address = normalize_email(person_email)
    if not address:
        return "no_email"
    try:
        exec_address = normalize_email(get_settings().exec_email_address)
    except Exception:
        logger.warning("delegation.gmail: settings unreadable — refusing", exc_info=True)
        return "error"
    if address == exec_address:
        return "shared_mailbox"
    client = gmail if gmail is not None else gmail_for(address)
    try:
        opened = await client.profile_email()
    except GmailNotConfigured:
        return "not_configured"
    except GmailAuthError:
        return "needs_reconnect"
    except Exception:
        logger.warning("delegation.gmail: status check failed", exc_info=True)
        return "error"
    return "connected" if opened == address else "mismatch"
