"""Read-only Confluence REST client for the space sync (``confluence_sync``).

Talks to the v1 REST API (``<CONFLUENCE_URL>/rest/api``), which Confluence
Cloud and Server/Data Center both serve, so one client covers both. Auth is a
Server/DC personal access token (Bearer) or a username plus API token (Basic,
what Cloud uses). Either way the token acts as the Confluence user who made
it: the sync sees what that user can see, narrowed to the configured spaces.

This is deliberately NOT a Confluence MCP server (e.g. mcp-atlassian) behind
the gateway: the sync runs from the scheduler with no chat turn, reads
structured JSON with version and restriction data, and must not depend on an
optional tool server being configured.
"""
from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import logging
import re
import socket
import ssl
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from openexecutive.config import Settings

logger = logging.getLogger(__name__)

# Confluence content ids are integers. Checked before any id goes into a URL.
_PAGE_ID_RE = re.compile(r"[0-9]{1,20}")
# Global space keys are letters and digits; personal spaces are ``~user``.
_SPACE_KEY_RE = re.compile(r"[A-Za-z0-9_]{1,255}|~[A-Za-z0-9._@-]{1,255}")
_CURSOR_RE = re.compile(r"[A-Za-z0-9_.~+/=%-]{1,2048}")
_MAX_ATTEMPTS = 4
_MAX_RETRY_AFTER_S = 60.0
_MAX_RESPONSE_BYTES = 20 * 1024 * 1024
# A page body: the sync keeps at most 200k characters of its Markdown, so a
# few MB of storage XHTML is already far more than it can use.
MAX_BODY_BYTES = 5 * 1024 * 1024
_PAGE_SIZE = 100
_FALSEY = {"false", "0", "no", "off"}
_TRUTHY = {"", "true", "1", "yes", "on"}


def sanitize_page_id(value: object) -> str | None:
    raw = str(value or "").strip()
    return raw if _PAGE_ID_RE.fullmatch(raw) else None


def sanitize_space_key(value: object) -> str | None:
    raw = str(value or "").strip()
    return raw if _SPACE_KEY_RE.fullmatch(raw) else None


def site_url_problem(url: str | None, *, allow_http: bool = False) -> str | None:
    """Why ``url`` cannot be ``CONFLUENCE_URL``, or None when it can: an
    https:// base URL (http:// only with ``allow_http``) with a host and no
    credentials, query or fragment."""
    allowed = ("https", "http") if allow_http else ("https",)
    try:
        parts = urlsplit((url or "").strip())
        hostname = parts.hostname
    except ValueError:
        return "CONFLUENCE_URL is not a valid URL"
    if parts.scheme not in allowed or not hostname:
        return (
            "CONFLUENCE_URL must be an https:// URL "
            "(set CONFLUENCE_SYNC_ALLOW_HTTP=true to allow http:// on a private network)"
        )
    if parts.username or parts.password or parts.query or parts.fragment:
        return "CONFLUENCE_URL must be the site's base URL, with no credentials, query or fragment"
    return None


def config_problem(
    *,
    url: str | None,
    personal_token: str | None,
    username: str | None,
    api_token: str | None,
    space_keys: list[str],
    allow_http: bool = False,
) -> str | None:
    """Why these settings cannot run the sync, or None when they can. The
    same check ``Settings`` applies when ``CONFLUENCE_SYNC_ENABLED=true``, so
    a caller that writes these settings for someone else can refuse a set
    the app would not start with."""
    problem = site_url_problem(url, allow_http=allow_http)
    if problem:
        return problem
    if not personal_token and not (username and api_token):
        # Both set is fine: the personal access token wins.
        return (
            "the sync needs CONFLUENCE_PERSONAL_TOKEN (Server/Data Center), "
            "or CONFLUENCE_USERNAME and CONFLUENCE_API_TOKEN (Cloud)"
        )
    if not space_keys:
        return "the sync needs CONFLUENCE_SYNC_SPACE_KEYS"
    bad = [k for k in space_keys if not sanitize_space_key(k)]
    if bad:
        return f"CONFLUENCE_SYNC_SPACE_KEYS has invalid space keys: {bad}"
    return None


class ConfluenceResponseTooLarge(Exception):
    """A response would exceed the client's byte cap."""


class ConfluenceHostNotPublic(Exception):
    """With ``CONFLUENCE_SYNC_PUBLIC_HOSTS_ONLY``, the site's host resolved to
    a loopback, private, link-local or otherwise non-public address."""


# IPv6 forms that carry an IPv4 address a translator or tunnel may deliver
# to: NAT64 (RFC 6052 well-known and local-use prefixes) and the deprecated
# IPv4-compatible ``::a.b.c.d``. The embedded address is what gets checked.
_EMBEDS_IPV4 = (
    ipaddress.IPv6Network("64:ff9b::/96"),
    ipaddress.IPv6Network("64:ff9b:1::/48"),
    ipaddress.IPv6Network("::/96"),
)
_RESOLVE_TIMEOUT_S = 10.0


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        embedded: ipaddress.IPv4Address | None = ip.ipv4_mapped or ip.sixtofour
        if embedded is None and ip.teredo is not None:
            embedded = ip.teredo[1]
        if embedded is None and any(ip in net for net in _EMBEDS_IPV4):
            embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if embedded is not None and not _is_public(embedded):
            return False
    return ip.is_global and not ip.is_multicast


async def resolve_public_address(host: str, port: int) -> str:
    """One address ``host`` resolves to, refusing the lot when ANY of them is
    not a public address (a name that also resolves inward is not trusted).
    A resolver that does not answer within ``_RESOLVE_TIMEOUT_S`` fails the
    request like an unreachable site."""
    try:
        infos = await asyncio.wait_for(
            asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM),
            timeout=_RESOLVE_TIMEOUT_S,
        )
    except TimeoutError as exc:
        raise httpx.ConnectTimeout(f"resolving {host} timed out") from exc
    addresses: list[str] = []
    for info in infos:
        try:
            ip = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])
        except ValueError as exc:
            raise ConfluenceHostNotPublic(host) from exc
        if not _is_public(ip):
            raise ConfluenceHostNotPublic(host)
        addresses.append(str(ip))
    if not addresses:
        raise ConfluenceHostNotPublic(host)
    return addresses[0]


@dataclass(frozen=True)
class ConfluencePage:
    id: str
    title: str
    space: str
    version: int
    modified: str
    url: str
    ancestors: tuple[str, ...] = ()
    # True / False when the listing carried read restrictions for the page
    # itself, None when it did not (the sync then treats it as restricted).
    restricted: bool | None = None


def ssl_verify(value: str) -> bool | ssl.SSLContext:
    """``CONFLUENCE_SSL_VERIFY``: true / false like mcp-atlassian, or the
    path of a CA bundle for a server with a private certificate authority."""
    raw = (value or "").strip()
    if raw.lower() in _TRUTHY:
        return True
    if raw.lower() in _FALSEY:
        return False
    return ssl.create_default_context(cafile=raw)


def auth_headers(settings: Settings) -> dict[str, str]:
    """Bearer for a personal access token, else Basic with username + token."""
    if settings.confluence_personal_token:
        return {"Authorization": f"Bearer {settings.confluence_personal_token}"}
    pair = f"{settings.confluence_username}:{settings.confluence_api_token}".encode()
    return {"Authorization": f"Basic {base64.b64encode(pair).decode('ascii')}"}


def _restricted(raw: dict[str, Any]) -> bool | None:
    """Whether the page has its own read restriction, from the
    ``restrictions.read.restrictions.{user,group}`` expansion; None when the
    response does not carry it."""
    try:
        groups = raw["restrictions"]["read"]["restrictions"]
        entries = [groups["user"], groups["group"]]
    except (KeyError, TypeError):
        return None
    restricted = False
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        results = entry.get("results")
        size = entry.get("size")
        if (isinstance(results, list) and results) or (isinstance(size, int) and size > 0):
            restricted = True
        elif not isinstance(results, list) and not isinstance(size, int):
            return None
    return restricted


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


async def _sleep_before_retry(response: httpx.Response, attempt: int) -> None:
    delay = float(2**attempt)
    header = response.headers.get("Retry-After", "")
    if header.isascii() and header.isdigit():
        delay = min(float(header), _MAX_RETRY_AFTER_S)
    await asyncio.sleep(delay)


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


class ConfluenceClient:
    """The two calls the sync needs: list a space's pages with version,
    ancestors and read restrictions, and fetch one page's storage body."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        base_url: str,
        headers: dict[str, str],
        *,
        public_hosts_only: bool = False,
    ) -> None:
        self._http = http
        self._base = base_url.rstrip("/")
        self._api = f"{self._base}/rest/api"
        self._headers = {**headers, "Accept": "application/json"}
        self._public_hosts_only = public_hosts_only

    async def _target(self, path: str) -> tuple[str, dict[str, str], dict[str, Any]]:
        """URL, headers and request extensions for one call. With
        ``public_hosts_only`` the host is resolved here, checked, and the
        request sent to that very address (Host header and TLS server name
        kept, so the certificate is still checked against the site's name).
        Pinning the address closes the gap a re-resolving name would leave
        between the check and the connection."""
        url = httpx.URL(f"{self._api}{path}")
        if not self._public_hosts_only:
            return str(url), self._headers, {}
        # ASCII forms: an IDN site's punycode, and an IPv6 literal in brackets.
        host = url.raw_host.decode("ascii")
        port = url.port or (443 if url.scheme == "https" else 80)
        address = await resolve_public_address(host.strip("[]"), port)
        extensions: dict[str, Any] = {}
        if url.scheme == "https" and not _is_ip_literal(host):
            # An IP-literal site has no name to send; its certificate is
            # checked against the address instead.
            extensions["sni_hostname"] = host
        return (
            str(url.copy_with(host=address)),
            {**self._headers, "Host": url.netloc.decode("ascii")},
            extensions,
        )

    def web_url(self, webui: object) -> str:
        """Absolute link for a ``_links.webui`` path, or "" when it is not a
        plain path on the configured site."""
        path = str(webui or "")
        if not path.startswith("/") or path.startswith("//") or re.search(r"[\s\\]", path):
            return ""
        return f"{self._base}{path}"

    async def _get_json(
        self, path: str, params: dict[str, Any], *, max_bytes: int | None = None
    ) -> Any:
        """GET JSON, retrying 429 / 5xx (honouring a short Retry-After).
        Redirects are not followed, so the credentials never leave the
        configured site; a 3xx fails like any other error status. The body is
        streamed and abandoned past ``max_bytes``, and parsed off the event
        loop."""
        cap = _MAX_RESPONSE_BYTES if max_bytes is None else max_bytes
        for attempt in range(_MAX_ATTEMPTS):
            url, headers, extensions = await self._target(path)
            async with self._http.stream(
                "GET", url, params=params, headers=headers, extensions=extensions
            ) as response:
                retryable = response.status_code == 429 or response.status_code >= 500
                if retryable and attempt < _MAX_ATTEMPTS - 1:
                    await response.aclose()
                    await _sleep_before_retry(response, attempt)
                    continue
                if not response.is_success:
                    await response.aread()
                    response.raise_for_status()
                    raise httpx.HTTPStatusError(
                        f"unexpected {response.status_code} from Confluence",
                        request=response.request,
                        response=response,
                    )
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > cap:
                        raise ConfluenceResponseTooLarge(path)
                return await asyncio.to_thread(json.loads, bytes(body))
        raise AssertionError("unreachable: the last attempt returns or raises")

    def _page(self, raw: Any, space: str, *, with_restrictions: bool) -> ConfluencePage | None:
        if not isinstance(raw, dict):
            return None
        page_id = sanitize_page_id(raw.get("id"))
        version = _dict(raw.get("version"))
        number = version.get("number")
        if not page_id or not isinstance(number, int):
            return None
        links = _dict(raw.get("_links"))
        raw_ancestors = raw.get("ancestors")
        ancestors: list[Any] = raw_ancestors if isinstance(raw_ancestors, list) else []
        restricted = _restricted(raw)
        if with_restrictions and not isinstance(raw_ancestors, list):
            # Without its ancestors a child of a restricted page reads as a
            # root page, so an inherited restriction would go unseen.
            restricted = None
        return ConfluencePage(
            id=page_id,
            title=str(raw.get("title") or "Untitled"),
            space=space,
            version=number,
            modified=str(version.get("when") or ""),
            url=self.web_url(links.get("webui")),
            ancestors=tuple(
                a for a in (sanitize_page_id(x.get("id")) for x in ancestors if isinstance(x, dict))
                if a
            ),
            restricted=restricted,
        )

    async def list_space(
        self, space_key: str, *, max_items: int, with_restrictions: bool
    ) -> tuple[list[ConfluencePage], bool]:
        """Every current page in ``space_key``, and whether the listing was
        cut short (``max_items``, or a next link it could not follow)."""
        key = sanitize_space_key(space_key)
        if not key:
            raise ValueError(f"unsafe Confluence space key: {space_key!r}")
        expand = "version,ancestors"
        if with_restrictions:
            expand += ",restrictions.read.restrictions.user,restrictions.read.restrictions.group"
        pages: list[ConfluencePage] = []
        cursor: dict[str, str] = {}
        while True:
            params: dict[str, Any] = {
                # The key matched _SPACE_KEY_RE, so it holds no quote to escape.
                "cql": f'space = "{key}" and type = page order by lastmodified desc',
                "expand": expand,
                "limit": _PAGE_SIZE,
                **cursor,
            }
            data = await self._get_json("/content/search", params)
            results = data.get("results") if isinstance(data, dict) else None
            for raw in results if isinstance(results, list) else []:
                page = self._page(raw, key, with_restrictions=with_restrictions)
                if page is not None:
                    pages.append(page)
                if len(pages) >= max_items:
                    return pages, True
            links = data.get("_links") if isinstance(data, dict) else None
            nxt = links.get("next") if isinstance(links, dict) else None
            if not nxt:
                return pages, False
            cursor = self._next_params(nxt)
            if not cursor:
                return pages, True

    @staticmethod
    def _next_params(nxt: object) -> dict[str, str]:
        """The paging parameters of a ``_links.next`` link: ``cursor`` on
        Cloud, ``start`` on Server/DC. Only those are taken, and the request
        goes to this client's own endpoint, never to the link itself."""
        if not isinstance(nxt, str):
            return {}
        query = parse_qs(urlsplit(nxt).query)
        out: dict[str, str] = {}
        cursor = (query.get("cursor") or [""])[0]
        if cursor and _CURSOR_RE.fullmatch(cursor):
            out["cursor"] = cursor
        start = (query.get("start") or [""])[0]
        if start.isascii() and start.isdigit():
            out["start"] = start
        return out

    async def page_body(self, page: ConfluencePage) -> tuple[ConfluencePage, str]:
        """The page's storage-format body, and the page as that fetch saw it
        (its version can be newer than the listing's)."""
        page_id = sanitize_page_id(page.id)
        if not page_id:
            raise ValueError(f"unsafe Confluence page id: {page.id!r}")
        data = await self._get_json(
            f"/content/{page_id}", {"expand": "body.storage,version"}, max_bytes=MAX_BODY_BYTES
        )
        body = data.get("body") if isinstance(data, dict) else None
        storage = body.get("storage") if isinstance(body, dict) else None
        value = storage.get("value") if isinstance(storage, dict) else None
        version = _dict(data.get("version") if isinstance(data, dict) else None)
        number = version.get("number")
        seen = page
        if isinstance(number, int) and number != page.version:
            seen = ConfluencePage(
                id=page.id,
                title=str(data.get("title") or page.title),
                space=page.space,
                version=number,
                modified=str(version.get("when") or page.modified),
                url=page.url,
                ancestors=page.ancestors,
                restricted=page.restricted,
            )
        return seen, value if isinstance(value, str) else ""
