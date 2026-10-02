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
import json
import logging
import re
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


class ConfluenceResponseTooLarge(Exception):
    """A response would exceed the client's byte cap."""


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


class ConfluenceClient:
    """The two calls the sync needs: list a space's pages with version,
    ancestors and read restrictions, and fetch one page's storage body."""

    def __init__(self, http: httpx.AsyncClient, base_url: str, headers: dict[str, str]) -> None:
        self._http = http
        self._base = base_url.rstrip("/")
        self._api = f"{self._base}/rest/api"
        self._headers = {**headers, "Accept": "application/json"}

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
            async with self._http.stream(
                "GET", f"{self._api}{path}", params=params, headers=self._headers
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
