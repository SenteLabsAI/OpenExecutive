"""A direct Microsoft Graph client for one person's own Outlook mailbox (Act as me).

The Outlook twin of ``delegation.gmail.DelegateGmail``, with the same rules:
the Executive's own Outlook is reached through the Microsoft 365 MCP server,
where the model can call any mail tool by name, so a mailbox someone lent the
Executive is never reachable that way. This client talks to Graph itself and
is never registered with the gateway; only the typed Act as me handlers call
it (they get it from ``gmail.gmail_for``, which picks the client by the
person's credential file).

**Credential.** The same per-person file as Gmail (``gmail.credential_path``,
in ``DELEGATION_GOOGLE_CREDENTIALS_DIR``), so a person has one mailbox for Act
as me: ``{"version": 1, "provider": "microsoft", "email": ..., "account":
"work" | "personal", "microsoft": {refresh_token, client_id, tenant}}``
(no client secret: the sign-in is a public client's). ``scripts/connect-own-outlook.py``
writes it. Delegated scopes: ``User.Read``, ``Mail.ReadWrite`` and
``Mail.Send``, plus ``offline_access``. Microsoft rotates the refresh token on
use; the new one is written back into the same file, keeping every other key.

**One way to send.** It reads mail and saves or deletes drafts. The only send
is ``send_draft``: Graph's ``/messages/{id}/send`` on an existing draft, with
no body, so nothing here can change what goes or to whom. Only
``delegation.reply_send`` calls it; unit tests pin that, as for Gmail.

**Where Outlook differs from Gmail.**

* Ids are Graph *immutable* ids (``Prefer: IdType="ImmutableId"``), so a
  draft keeps its id once sent. A thread is Outlook's ``conversationId``.
* An Outlook draft keeps its id when edited, where Gmail gives it a new
  message id. The callers spot an edit by comparing message ids, so a draft's
  message id here is a version, ``<id>.<changeKey>`` (``draft_version``).
* Graph has no signature setting: ``send_as_signature`` is always empty.
* Gmail's labels are mapped onto what the callers read: ``DRAFT``, ``SENT``
  (in their Sent Items folder, so a From line set to their address by
  someone else never counts), ``INBOX`` (everything else) and
  ``CATEGORY_UPDATES`` for mail Focused Inbox files under Other.
* The ghostwritten mark is a named extended property, not a header.
* A sender counts as authenticated only when Exchange's own
  ``Authentication-Results`` (the topmost one) holds a DMARC pass for the
  From domain (``exchange_authenticated``).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from email.message import Message
from email.utils import parseaddr
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from openexecutive.delegation.gmail import (
    _AUTOREPLY_HEADERS,
    _BULK_PRECEDENCE,
    _REPORT_SENDERS,
    CreatedDraft,
    DraftInfo,
    DraftSpec,
    GmailAuthError,
    GmailError,
    GmailNotConfigured,
    GmailNotFound,
    GmailRateLimited,
    MailMessage,
    MailThread,
    SentMessage,
    ThreadSummary,
    _hide_tokens,
    clean_header,
    credential_path,
    html_to_text,
    normalize_email,
)

logger = logging.getLogger(__name__)

PROVIDER = "microsoft"
GRAPH_ME = "https://graph.microsoft.com/v1.0/me"
LOGIN_BASE = "https://login.microsoftonline.com"
GRAPH_RESOURCE = "https://graph.microsoft.com/"
GRAPH_SCOPES: tuple[str, ...] = ("User.Read", "Mail.ReadWrite", "Mail.Send")
REQUEST_SCOPE = " ".join(["offline_access", *(GRAPH_RESOURCE + s for s in GRAPH_SCOPES)])
# The tenant every personal Microsoft account (Outlook.com, Hotmail) is in.
CONSUMER_TENANT_ID = "9188040d-6c67-4c5b-b112-36a304b66dad"
CREDENTIAL_VERSION = 1

# A named MAPI property in our own namespace: "the Executive wrote this draft".
GHOSTWRITTEN_PROPERTY = "String {3f0a7c52-8d1e-4b6a-9e2f-5c4d7b8a1e60} Name OEGhostwritten"

_PREFER = 'outlook.body-content-type="text", IdType="ImmutableId"'
_SELECT = (
    "id,conversationId,changeKey,subject,from,toRecipients,ccRecipients,bccRecipients,replyTo,"
    "sentDateTime,receivedDateTime,internetMessageId,internetMessageHeaders,body,isDraft,"
    "inferenceClassification,parentFolderId"
)
_EXPAND = f"singleValueExtendedProperties($filter=id eq '{GHOSTWRITTEN_PROPERTY}')"

# Graph ids are base64: letters, digits and - _ = + /. Anything else never
# reaches a URL path or an OData filter.
_ID_RE = re.compile(r"[A-Za-z0-9_=+/-]{1,512}")
_TENANT_RE = re.compile(r"common|organizations|consumers|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_EMAIL_RE = re.compile(r"[\w.+\-']+@[\w\-]+(?:\.[\w\-]+)+")
_DMARC_PASS = re.compile(r"dmarc=pass(?: action=[a-z]+)? header\.from=([a-z0-9.-]+)")
_TOKEN_SLACK_SECONDS = 60
_THREAD_LIMIT = 50

# Access tokens by (address, refresh token) — never written anywhere.
_TOKENS: dict[str, tuple[str, float]] = {}


@dataclass(frozen=True)
class OutlookCredential:
    email: str
    refresh_token: str = field(repr=False)
    client_id: str = ""
    tenant: str = "common"
    personal: bool = False


def valid_id(value: object) -> bool:
    return isinstance(value, str) and bool(_ID_RE.fullmatch(value))


def draft_version(message_id: str, change_key: str) -> str:
    """A draft's message id as the callers compare it: changes on each edit."""
    return f"{message_id}.{change_key}" if change_key else message_id


def _id_of(version: str) -> str:
    return version.rsplit(".", 1)[0]


# --------------------------------------------------------------------------- #
# Credential file
# --------------------------------------------------------------------------- #


def load_credential(email: str, *, directory: Path | None = None) -> OutlookCredential | None:
    """The stored Microsoft credential for ``email``, or None when there is
    none, it is a Gmail one, or it is unreadable (logged)."""
    address = normalize_email(email)
    if not address:
        return None
    path = credential_path(address, directory=directory)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("delegation.outlook: unreadable credential file %s", path.name)
        return None
    if not isinstance(data, dict) or data.get("provider") != PROVIDER:
        return None
    if normalize_email(str(data.get("email") or "")) != address:
        logger.warning("delegation.outlook: credential file %s is for another address", path.name)
        return None
    ms = data.get("microsoft")
    if not isinstance(ms, dict):
        return None
    values = {k: ms.get(k) for k in ("refresh_token", "client_id", "tenant")}
    if not all(isinstance(v, str) and v for v in values.values()):
        logger.warning("delegation.outlook: credential file %s is incomplete", path.name)
        return None
    tenant = str(values["tenant"]).strip().lower()
    if not _TENANT_RE.fullmatch(tenant):
        logger.warning("delegation.outlook: credential file %s names an unexpected tenant", path.name)
        return None
    return OutlookCredential(
        email=address,
        refresh_token=str(values["refresh_token"]),
        client_id=str(values["client_id"]),
        tenant=tenant,
        personal=data.get("account") == "personal" or tenant in ("consumers", CONSUMER_TENANT_ID),
    )


def _store_rotated(email: str, old: str, new: str, directory: Path | None) -> None:
    """Write Microsoft's new refresh token into the file, keeping every other
    key, unless the file no longer holds ``old`` (reconnected meanwhile)."""
    path = credential_path(email, directory=directory)
    if not path.is_file():
        return  # a credential handed in directly, with no file behind it
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        ms = data.get("microsoft") if isinstance(data, dict) else None
        if not isinstance(ms, dict) or ms.get("refresh_token") != old:
            return
        ms["refresh_token"] = new
        # A fresh, unpredictable name created exclusively (0600), so nothing
        # planted in the directory can redirect where the token is written.
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except (OSError, ValueError, TypeError):
        logger.warning("delegation.outlook: couldn't save the rotated sign-in for %s", path.name, exc_info=True)


def _token_key(cred: OutlookCredential) -> str:
    return hashlib.sha256(f"{cred.email}\n{cred.refresh_token}".encode()).hexdigest()


def _granted(scope: str) -> set[str]:
    return {s.rsplit("/", 1)[-1].lower() for s in scope.split()}


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def _address(entry: object) -> tuple[str, str]:
    inner = entry.get("emailAddress") if isinstance(entry, dict) else None
    if not isinstance(inner, dict):
        return "", ""
    return str(inner.get("name") or "").strip(), normalize_email(str(inner.get("address") or ""))


def _addresses(entries: object) -> list[str]:
    if not isinstance(entries, list):
        return []
    return [a for _, a in (_address(e) for e in entries) if "@" in a]


def _header_list(raw: dict[str, Any]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for h in raw.get("internetMessageHeaders") or []:
        if isinstance(h, dict) and isinstance(h.get("name"), str):
            out.append((h["name"], str(h.get("value") or "").replace("\r", " ").replace("\n", " ")))
    return out


def _header_message(headers: list[tuple[str, str]]) -> Message:
    msg = Message()
    for name, value in headers:
        try:
            msg[name] = value
        except (ValueError, TypeError):
            continue
    return msg


def exchange_authenticated(headers: Message, from_addr: str) -> bool:
    """Whether Exchange found the From domain authenticated (DMARC pass).

    Exchange writes ``Authentication-Results`` with no authserv-id, as
    ``spf=…; dkim=…; dmarc=pass action=none header.from=example.com;
    compauth=pass …``, above every header the sender wrote, so only the
    topmost one counts, it must read that way, and it must hold one DMARC
    verdict: a pass for From's own domain. It must also carry Exchange
    Online Protection's own ``compauth=`` verdict, so a header the sender
    wrote that reached the mailbox some way EOP never stamped (an on-premises
    connector, an internal relay) doesn't count."""
    from openexecutive.integrations.fact_confirmation import _strip_quotes_and_comments

    address = normalize_email(from_addr)
    if "@" not in address:
        return False
    try:
        _name, raw_from = parseaddr(str(headers.get("From", "")))
        results = headers.get_all("Authentication-Results") or []
    except Exception:  # noqa: BLE001 - an unreadable message is not authenticated.
        return False
    if raw_from.strip().lower() != address or not results:
        return False
    newest = _strip_quotes_and_comments(" ".join(str(results[0]).split()).lower())
    if newest is None or not newest.startswith("spf="):
        return False
    clauses = [c.strip() for c in newest.split(";")]
    verdicts = [c for c in clauses if c.startswith("dmarc=")]
    if len(verdicts) != 1 or sum(c.startswith("compauth=") for c in clauses) != 1:
        return False
    verdict = _DMARC_PASS.fullmatch(verdicts[0])
    return verdict is not None and verdict.group(1) == address.rsplit("@", 1)[1]


def _iso(value: object) -> str:
    if not isinstance(value, str) or not value:
        return ""
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).astimezone(UTC).isoformat()


def parse_message(raw: dict[str, Any], sent_folder: str = "", own: str = "") -> MailMessage:
    """A Graph message as a ``MailMessage`` (``sent_folder`` is the id of the
    mailbox's Sent Items, ``own`` its address), with any one-time answer
    token hidden, as ``gmail.parse_message`` does. A draft with no From is
    sent from the mailbox itself, so it reads as from ``own``."""
    header_list = _header_list(raw)
    headers: dict[str, str] = {}
    for name, value in header_list:
        headers.setdefault(name.lower(), value)
    from_name, from_addr = _address(raw.get("from"))
    draft = raw.get("isDraft") is True
    if draft and not from_addr:
        from_addr = normalize_email(own)
    raw_body = raw.get("body")
    body: dict[str, Any] = raw_body if isinstance(raw_body, dict) else {}
    content = str(body.get("content") or "")
    text = html_to_text(content) if str(body.get("contentType") or "").lower() == "html" else content.strip()
    labels: list[str] = []
    if draft:
        labels.append("DRAFT")
    elif sent_folder and raw.get("parentFolderId") == sent_folder:
        labels.append("SENT")
    else:
        labels.append("INBOX")
        if raw.get("inferenceClassification") == "other":
            labels.append("CATEGORY_UPDATES")
    props = raw.get("singleValueExtendedProperties")
    ghostwritten = isinstance(props, list) and any(
        isinstance(p, dict) and str(p.get("id") or "").lower() == GHOSTWRITTEN_PROPERTY.lower() and p.get("value") == "1"
        for p in props
    )
    auto = headers.get("auto-submitted", "no").strip().lower() not in ("", "no")
    local_part = from_addr.partition("@")[0]
    content_type = headers.get("content-type", "").lower()
    kind = str(raw.get("@odata.type") or "").lower()
    message = _header_message(header_list)
    return MailMessage(
        id=str(raw.get("id") or ""),
        thread_id=str(raw.get("conversationId") or ""),
        from_addr=from_addr,
        from_name=from_name,
        to=_addresses(raw.get("toRecipients")),
        cc=_addresses(raw.get("ccRecipients")),
        reply_to=",".join(_addresses(raw.get("replyTo"))),
        subject=_hide_tokens(str(raw.get("subject") or "").strip()),
        date=_iso(raw.get("sentDateTime")) or _iso(raw.get("receivedDateTime")),
        message_id_header=str(raw.get("internetMessageId") or "").strip(),
        references=headers.get("references", "").strip(),
        labels=labels,
        text=_hide_tokens(text),
        mailing_list=bool(headers.get("list-unsubscribe") or headers.get("list-id")),
        auto_generated=auto or "calendar-notification" in headers.get("sender", "").lower(),
        ghostwritten=ghostwritten,
        bcc=_addresses(raw.get("bccRecipients")),
        received_at=_iso(raw.get("receivedDateTime")),
        bulk=headers.get("precedence", "").strip().lower() in _BULK_PRECEDENCE
        or any(name in headers for name in _AUTOREPLY_HEADERS),
        delivery_report="multipart/report" in content_type or local_part in _REPORT_SENDERS,
        calendar_invite="eventmessage" in kind,
        sender_authenticated=(not draft) and exchange_authenticated(message, from_addr),
    )


def outlook_link(*, personal: bool, message_id: str | None = None) -> str:
    """A link that opens a message (a draft, here) in Outlook on the web,
    built from a fixed prefix; the Drafts folder when there is no id."""
    base = "https://outlook.live.com/mail/0" if personal else "https://outlook.office.com/mail"
    if message_id and valid_id(_id_of(message_id)):
        return f"{base}/deeplink/read/{quote(_id_of(message_id), safe='')}"
    return f"{base}/drafts"


def _recipients(addresses: list[str]) -> list[dict[str, Any]]:
    return [{"emailAddress": {"address": clean_header(a)}} for a in addresses if a]


# Gmail search operators that mean the same in Outlook's search (KQL).
_KQL_FIELDS = frozenset({"from", "to", "cc", "subject"})
_NEWER_THAN = re.compile(r"(\d{1,4})([dmy])")


def _search_text(value: str, *, today: datetime | None = None) -> str:
    """A Gmail-style search as Outlook search (KQL) text, with nothing that
    could end the quoted string: words, ``from:`` ``to:`` ``cc:`` and
    ``subject:`` pass through, ``newer_than:14d`` becomes a received date,
    and any other Gmail operator (``in:``, ``label:``, ``is:``…) is dropped."""
    now = today or datetime.now(UTC)
    terms: list[str] = []
    for word in re.sub(r'["\\()]', " ", value).split():
        field, sep, rest = word.partition(":")
        if not sep:
            terms.append(word)
        elif field.lower() in _KQL_FIELDS and rest:
            terms.append(f"{field.lower()}:{rest}")
        elif field.lower() == "newer_than" and (match := _NEWER_THAN.fullmatch(rest.lower())):
            days = int(match.group(1)) * {"d": 1, "m": 31, "y": 366}[match.group(2)]
            terms.append(f"received>={(now - timedelta(days=days)).strftime('%Y-%m-%d')}")
    return " ".join(terms)[:255]


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


class DelegateOutlook:
    """One person's Outlook mailbox. ``transport`` is for tests (httpx MockTransport)."""

    provider = PROVIDER
    valid_id = staticmethod(valid_id)

    def __init__(
        self,
        email: str,
        *,
        credential: OutlookCredential | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 15.0,
        directory: Path | None = None,
    ) -> None:
        self.email = normalize_email(email)
        self._credential = credential
        self._transport = transport
        self._timeout = timeout
        self._directory = directory
        self._sent_folder = ""

    @property
    def personal(self) -> bool:
        cred = self._credential or load_credential(self.email, directory=self._directory)
        return bool(cred and cred.personal)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._timeout, transport=self._transport)

    def _cred(self) -> OutlookCredential:
        cred = self._credential or load_credential(self.email, directory=self._directory)
        if cred is None:
            raise GmailNotConfigured(self.email)
        return cred

    async def _access_token(self, client: httpx.AsyncClient) -> str:
        cred = self._cred()
        cached = _TOKENS.get(_token_key(cred))
        if cached is not None and cached[1] - _TOKEN_SLACK_SECONDS > time.time():
            return cached[0]
        form = {
            "grant_type": "refresh_token",
            "refresh_token": cred.refresh_token,
            "client_id": cred.client_id,
            "scope": REQUEST_SCOPE,
        }
        try:
            resp = await client.post(f"{LOGIN_BASE}/{cred.tenant}/oauth2/v2.0/token", data=form)
        except httpx.HTTPError as exc:
            raise GmailError(f"token refresh failed: {type(exc).__name__}") from exc
        if resp.status_code in (400, 401):
            try:
                code = str(resp.json().get("error") or "")
            except ValueError:
                code = ""
            if code in ("invalid_grant", "interaction_required", "invalid_client", "unauthorized_client") \
                    or resp.status_code == 401:
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
        if granted and not {s.lower() for s in GRAPH_SCOPES} <= _granted(granted):
            raise GmailAuthError("missing_scope")
        rotated = payload.get("refresh_token")
        if isinstance(rotated, str) and rotated and rotated != cred.refresh_token:
            _store_rotated(cred.email, cred.refresh_token, rotated, self._directory)
            cred = replace(cred, refresh_token=rotated)
            self._credential = cred
        _TOKENS[_token_key(cred)] = (token, time.time() + ttl)
        return token

    async def _request(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        params: Any = None,
        json_body: dict[str, Any] | None = None,
        if_match: str = "",
    ) -> dict[str, Any]:
        token = await self._access_token(client)
        headers = {"Authorization": f"Bearer {token}", "Prefer": _PREFER}
        if if_match:
            headers["If-Match"] = if_match
        try:
            if json_body is None and method == "POST":
                resp = await client.request(method, f"{GRAPH_ME}{path}", params=params, content=b"", headers=headers)
            else:
                resp = await client.request(method, f"{GRAPH_ME}{path}", params=params, json=json_body, headers=headers)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # Never reached Microsoft: nothing was done.
            raise GmailError(f"graph {method} failed: {type(exc).__name__}") from exc
        except httpx.HTTPError as exc:
            raise GmailError(f"graph {method} failed: {type(exc).__name__}", maybe_done=True) from exc
        if resp.status_code == 401:
            _TOKENS.pop(_token_key(self._cred()), None)
            raise GmailAuthError("unauthorized")
        if resp.status_code == 403:
            raise GmailAuthError("forbidden")
        if resp.status_code == 429:
            raise GmailRateLimited(f"graph {method} returned 429")
        if resp.status_code in (404, 412):
            # 412: it changed since it was read (If-Match), so it is not the
            # item the caller meant any more.
            raise GmailNotFound(f"graph {method} returned {resp.status_code}")
        if resp.status_code >= 500:
            raise GmailError(f"graph {method} returned {resp.status_code}", maybe_done=True)
        if resp.status_code >= 400:
            raise GmailError(f"graph {method} returned {resp.status_code}")
        if not resp.content:
            return {}  # a send answers 202 and a DELETE 204, with no body
        try:
            data = resp.json()
        except ValueError as exc:
            raise GmailError("graph returned a non-JSON response", maybe_done=True) from exc
        return data if isinstance(data, dict) else {}

    async def _get(self, client: httpx.AsyncClient, path: str, params: Any = None) -> dict[str, Any]:
        return await self._request(client, "GET", path, params=params)

    async def _sent_folder_id(self, client: httpx.AsyncClient) -> str:
        """The id of their Sent Items (what makes a message ``SENT``)."""
        if not self._sent_folder:
            data = await self._get(client, "/mailFolders/sentitems", {"$select": "id"})
            folder = str(data.get("id") or "")
            if not valid_id(folder):
                raise GmailError("graph returned no Sent Items folder")
            self._sent_folder = folder
        return self._sent_folder

    def _parse(self, raw: object) -> MailMessage:
        return parse_message(raw if isinstance(raw, dict) else {}, self._sent_folder, self.email)

    @staticmethod
    def _directory_address(me: dict[str, Any]) -> str:
        """The mailbox's address as Microsoft's directory has it: ``mail``,
        else the sign-in name. Never anything read from the mail itself, which
        its owner controls, so a credential can't claim someone else's
        address. A personal account that signs in with another address (even
        a Gmail one) is matched by that sign-in address."""
        return normalize_email(str(me.get("mail") or me.get("userPrincipalName") or ""))

    async def profile_email(self) -> str:
        """The address of the mailbox the credential opens, from the directory."""
        async with self._client() as client:
            me = await self._get(client, "", {"$select": "mail,userPrincipalName"})
        return self._directory_address(me)

    async def send_as_addresses(self) -> list[str]:
        """The person's primary address and their mailbox's SMTP aliases."""
        async with self._client() as client:
            data = await self._get(client, "", {"$select": "mail,userPrincipalName,proxyAddresses"})
        primary = self._directory_address(data)
        aliases = [
            normalize_email(p.split(":", 1)[1])
            for p in data.get("proxyAddresses") or []
            if isinstance(p, str) and p.lower().startswith("smtp:")
        ]
        return [a for a in dict.fromkeys([primary, *aliases]) if a]

    async def send_as_signature(self) -> str:
        """Always empty: Graph does not expose Outlook's signature setting."""
        return ""

    async def search_threads(self, query: str, *, max_results: int = 5) -> list[ThreadSummary]:
        """Conversations matching a search, best match first: subject, sender
        and date only (no body text)."""
        text = _search_text(query)
        if not text:
            return []
        async with self._client() as client:
            data = await self._get(client, "/messages", {
                "$search": f'"{text}"',
                "$top": 25,
                "$select": "id,conversationId,subject,from,receivedDateTime",
            })
        summaries: dict[str, ThreadSummary] = {}
        for raw in data.get("value") or []:
            if not isinstance(raw, dict) or not valid_id(raw.get("conversationId")):
                continue
            thread_id = str(raw["conversationId"])
            if thread_id in summaries:
                continue
            name, addr = _address(raw.get("from"))
            summaries[thread_id] = ThreadSummary(
                id=thread_id,
                subject=_hide_tokens(str(raw.get("subject") or "")),
                sender=f"{name} <{addr}>" if name else addr,
                date=_iso(raw.get("receivedDateTime")),
            )
            if len(summaries) >= max(1, min(max_results, 10)):
                break
        return list(summaries.values())

    async def _conversation(self, client: httpx.AsyncClient, thread_id: str, select: str) -> list[dict[str, Any]]:
        if not valid_id(thread_id):
            raise GmailError("invalid thread id")
        params: dict[str, Any] = {
            "$filter": f"conversationId eq '{thread_id}'",
            "$top": _THREAD_LIMIT,
            "$select": select,
        }
        if "internetMessageHeaders" in select:
            params["$expand"] = _EXPAND
        data = await self._get(client, "/messages", params)
        found = [m for m in data.get("value") or [] if isinstance(m, dict)]
        if not found:
            raise GmailNotFound("no such conversation")
        return sorted(found, key=lambda m: _iso(m.get("receivedDateTime")) or _iso(m.get("sentDateTime")))

    async def get_thread(self, thread_id: str) -> MailThread:
        if not valid_id(thread_id):
            raise GmailError("invalid thread id")
        async with self._client() as client:
            await self._sent_folder_id(client)
            raw = await self._conversation(client, thread_id, _SELECT)
        return MailThread(id=thread_id, messages=[self._parse(m) for m in raw])

    async def list_sent(self, limit: int = 40) -> list[MailMessage]:
        """The person's recent sent mail, newest first (at most ``limit``)."""
        since = (datetime.now(UTC) - timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")
        async with self._client() as client:
            await self._sent_folder_id(client)
            data = await self._get(client, "/mailFolders/sentitems/messages", {
                "$filter": f"sentDateTime ge {since}",
                "$orderby": "sentDateTime desc",
                "$top": max(1, min(limit, 100)),
                "$select": _SELECT,
                "$expand": _EXPAND,
            })
        return [self._parse(m) for m in data.get("value") or [] if isinstance(m, dict)]

    async def inbox_message_ids(self, *, after: datetime, max_results: int = 25) -> list[tuple[str, str]]:
        """``(message id, thread id)`` for mail that reached the Focused inbox
        since ``after``, newest first, leaving out the person's own."""
        since = after.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        async with self._client() as client:
            data = await self._get(client, "/mailFolders/inbox/messages", {
                "$filter": f"receivedDateTime ge {since} and inferenceClassification eq 'focused'",
                "$orderby": "receivedDateTime desc",
                "$top": max(1, min(max_results, 100)),
                "$select": "id,conversationId,from",
            })
        out: list[tuple[str, str]] = []
        for raw in data.get("value") or []:
            if not isinstance(raw, dict) or not valid_id(raw.get("id")):
                continue
            if _address(raw.get("from"))[1] == self.email:
                continue
            out.append((str(raw["id"]), str(raw.get("conversationId") or "")))
        return out

    async def has_written_to(self, address: str) -> bool:
        """Whether the person's Sent Items hold mail to ``address``."""
        target = normalize_email(address)
        if not _EMAIL_RE.fullmatch(target):
            return False
        async with self._client() as client:
            data = await self._get(client, "/mailFolders/sentitems/messages", {
                "$search": f'"to:{target}"', "$top": 1, "$select": "id",
            })
        return bool(data.get("value"))

    async def get_message(self, message_id: str) -> MailMessage:
        if not valid_id(message_id):
            raise GmailError("invalid message id")
        async with self._client() as client:
            await self._sent_folder_id(client)
            data = await self._get(
                client, f"/messages/{quote(message_id, safe='')}", {"$select": _SELECT, "$expand": _EXPAND}
            )
        return self._parse(data)

    async def get_draft(self, draft_id: str) -> DraftInfo | None:
        """The draft as it is now, or None when it is gone (sent or deleted).
        Its message id is a version (``draft_version``): it changes on each edit."""
        if not valid_id(draft_id):
            raise GmailError("invalid draft id")
        async with self._client() as client:
            try:
                data = await self._get(
                    client, f"/messages/{quote(draft_id, safe='')}", {"$select": _SELECT, "$expand": _EXPAND}
                )
            except GmailNotFound:
                return None
        if data.get("isDraft") is not True:
            return None
        message = self._parse(data)
        return DraftInfo(
            draft_id=draft_id, message=replace(message, id=draft_version(message.id, str(data.get("changeKey") or "")))
        )

    async def delete_draft(self, draft_id: str) -> bool:
        """Delete a draft — never a sent message: anything that isn't a draft
        is left alone, and so is a draft that changed after it was read (it
        may have just been sent, keeping its id). False when it was already
        gone or was left alone."""
        if not valid_id(draft_id):
            raise GmailError("invalid draft id")
        path = f"/messages/{quote(draft_id, safe='')}"
        async with self._client() as client:
            try:
                data = await self._get(client, path, {"$select": "id,isDraft,changeKey"})
            except GmailNotFound:
                return False
            change_key = str(data.get("changeKey") or "")
            etag = str(data.get("@odata.etag") or (f'W/"{change_key}"' if change_key else ""))
            if data.get("isDraft") is not True or not etag:
                return False
            try:
                await self._request(client, "DELETE", path, if_match=etag)
            except GmailNotFound:
                return False
        return True

    async def send_draft(self, draft_id: str) -> SentMessage:
        """Send the draft ``draft_id`` exactly as it is in Outlook now: Graph's
        ``send`` with no body, so nothing here can change what goes, or to
        whom. Only ``delegation.reply_send`` calls this (a unit test holds it
        to that). A ``GmailError`` whose ``maybe_done`` is set may have sent it.
        The sent message keeps the draft's (immutable) id."""
        if not valid_id(draft_id):
            raise GmailError("invalid draft id")
        async with self._client() as client:
            await self._request(client, "POST", f"/messages/{quote(draft_id, safe='')}/send")
        return SentMessage(id=draft_id, thread_id="")

    async def create_draft(self, spec: DraftSpec) -> CreatedDraft:
        """Save ``spec`` as a draft in the person's Outlook. Nothing is sent.
        A reply is made with Graph's ``createReply`` on the message it answers
        (so Outlook threads it), then given ``spec``'s recipients and text."""
        fields: dict[str, Any] = {
            "subject": clean_header(spec.subject),
            "body": {"contentType": "text", "content": spec.body},
            "toRecipients": _recipients(spec.to),
            "ccRecipients": _recipients(spec.cc),
            "singleValueExtendedProperties": [{"id": GHOSTWRITTEN_PROPERTY, "value": "1"}],
        }
        async with self._client() as client:
            if not spec.thread_id:
                data = await self._request(client, "POST", "/messages", json_body=fields)
            else:
                data = await self._reply_draft(client, spec, fields)
        draft_id = str(data.get("id") or "")
        return CreatedDraft(
            draft_id=draft_id,
            message_id=draft_version(draft_id, str(data.get("changeKey") or "")),
            thread_id=str(data.get("conversationId") or spec.thread_id or ""),
        )

    async def _reply_draft(self, client: httpx.AsyncClient, spec: DraftSpec, fields: dict[str, Any]) -> dict[str, Any]:
        thread = await self._conversation(client, str(spec.thread_id), "id,internetMessageId,isDraft,receivedDateTime,sentDateTime")
        sent = [m for m in thread if m.get("isDraft") is not True and valid_id(m.get("id"))]
        wanted = (spec.in_reply_to or "").strip()
        parent = next((m for m in sent if wanted and str(m.get("internetMessageId") or "").strip() == wanted), None)
        parent = parent or (sent[-1] if sent else None)
        if parent is None:
            raise GmailNotFound("nothing in the conversation to reply to")
        created = await self._request(client, "POST", f"/messages/{quote(str(parent['id']), safe='')}/createReply", json_body={})
        draft_id = str(created.get("id") or "")
        if not valid_id(draft_id):
            raise GmailError("createReply returned no draft", maybe_done=True)
        try:
            return await self._request(client, "PATCH", f"/messages/{quote(draft_id, safe='')}", json_body=fields)
        except GmailError:
            # Don't leave a blank reply behind in their Drafts.
            try:
                await self._request(client, "DELETE", f"/messages/{quote(draft_id, safe='')}")
            except GmailError:
                logger.warning("delegation.outlook: couldn't remove a half-made reply draft")
            raise
