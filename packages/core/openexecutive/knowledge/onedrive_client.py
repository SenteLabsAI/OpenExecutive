"""Read-only Microsoft Graph client for the OneDrive folder sync (``onedrive_sync``).

The Microsoft analogue of ``knowledge.drive_client``. It authenticates with an
access token for the Executive's own Microsoft 365 sign-in
(``knowledge.onedrive_account``) and only issues GETs: list a folder, fetch a
file's bytes, and resolve a share link to the folder it names. This is
deliberately NOT the microsoft_365 MCP server the Executive uses inside a
turn: that one's results are display text for the model, where this reads
structured JSON and caps every download.

A OneDrive item is addressed by its drive id and item id together. Neither is
used in a URL until it has matched what Graph issues (``sanitize_id``), and a
paging link is followed only when it points back at Graph.
"""
from __future__ import annotations

import asyncio
import base64
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

GRAPH_API = "https://graph.microsoft.com/v1.0"
_GRAPH_HOST = "graph.microsoft.com"
# Read every file the signed-in account can see, its own and shared with it.
# Files.Read alone covers only the account's own drive, and a folder someone
# shares with the Executive lives in their drive, not the Executive's.
ONEDRIVE_SCOPES = ("Files.Read.All",)

# What Graph issues as a drive id or item id: a personal drive's hex id, a
# business drive's ``b!…`` id, and item ids such as ``D4648F06C91D9D3D!54927``
# or ``01BYE5RZ6QN3ZWBTUFOFD3GSPGOHDJD36K``. Checked before either goes into
# a URL path. No ``/``, ``.`` or ``:``, which is what lets a pair be written
# as ``<drive id>/<item id>`` and keyed as ``<drive id>:<item id>``.
_ID_RE = re.compile(r"[A-Za-z0-9!_-]{1,256}")
_SELECT = (
    "id,name,file,folder,remoteItem,package,size,lastModifiedDateTime,cTag,eTag,webUrl"
)
_PAGE_SIZE = 200
_MAX_ATTEMPTS = 4
_SHARE_HOSTS = ("1drv.ms", "onedrive.live.com", "sharepoint.com")


def sanitize_id(value: object) -> str | None:
    raw = str(value or "").strip()
    return raw if _ID_RE.fullmatch(raw) else None


def parse_folder_entry(value: str) -> tuple[str, str] | None:
    """``<drive id>/<item id>`` as a pair, or None when either part isn't an id."""
    parts = value.strip().split("/")
    if len(parts) != 2:
        return None
    drive_id, item_id = (sanitize_id(p) for p in parts)
    return (drive_id, item_id) if drive_id and item_id else None


def item_key(drive_id: str, item_id: str) -> str:
    """The one string a synced file is recorded and looked up under."""
    return f"{drive_id}:{item_id}"


def parse_item_key(key: str) -> tuple[str, str] | None:
    parts = str(key or "").split(":")
    if len(parts) != 2:
        return None
    drive_id, item_id = (sanitize_id(p) for p in parts)
    if not drive_id or not item_id or item_key(drive_id, item_id) != key:
        return None
    return drive_id, item_id


def share_id(url: str) -> str:
    """The ``u!…`` id Graph's ``/shares`` endpoint takes for a sharing link.
    Raises ``ValueError`` for anything that isn't an https OneDrive or
    SharePoint link."""
    text = (url or "").strip()
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise ValueError("not a OneDrive link") from None
    host = (parts.hostname or "").lower()
    if (
        parts.scheme != "https"
        or parts.username is not None
        or parts.password is not None
        or port is not None
        or not any(host == h or host.endswith("." + h) for h in _SHARE_HOSTS)
        or len(text) > 2048
    ):
        raise ValueError("not a OneDrive or SharePoint https link")
    encoded = base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")
    return f"u!{encoded}"


class OneDriveFileTooLarge(Exception):
    """A download would exceed the caller's byte cap."""


class OneDriveDownloadFailed(Exception):
    """The pre-authenticated download URL refused or failed. The message
    never carries that URL: on a personal drive the credential is in its
    path, so it must not reach a log line."""


@dataclass(frozen=True)
class OneDriveItem:
    drive_id: str
    id: str
    name: str
    is_folder: bool
    # The file's MIME type as Graph reports it; "" for a folder.
    mime_type: str
    modified: str
    # Changes when the content does (not on a rename or a move).
    ctag: str
    link: str
    size: int | None

    @property
    def key(self) -> str:
        return item_key(self.drive_id, self.id)


def _item(raw: Any, parent_drive: str) -> OneDriveItem | None:
    """A Graph driveItem as an item, or None when it can't be addressed. A
    shortcut to something in another drive (``remoteItem``) is followed to
    where it lives. A OneNote notebook (``package``) is neither a file nor a
    folder the sync can read, so it is dropped."""
    if not isinstance(raw, dict) or raw.get("package") is not None:
        return None
    remote = raw.get("remoteItem")
    source = remote if isinstance(remote, dict) else raw
    parent = source.get("parentReference")
    drive_id = sanitize_id(parent.get("driveId")) if isinstance(parent, dict) else None
    drive_id = drive_id or (None if isinstance(remote, dict) else sanitize_id(parent_drive))
    item_id = sanitize_id(source.get("id"))
    if not drive_id or not item_id:
        return None
    is_folder = isinstance(source.get("folder"), dict) or isinstance(raw.get("folder"), dict)
    file_info = source.get("file") if isinstance(source.get("file"), dict) else raw.get("file")
    if not is_folder and not isinstance(file_info, dict):
        return None
    mime = str(file_info.get("mimeType") or "") if isinstance(file_info, dict) else ""
    link = str(source.get("webUrl") or raw.get("webUrl") or "")
    size_raw = source.get("size", raw.get("size"))
    try:
        size = int(size_raw) if size_raw is not None else None
    except (TypeError, ValueError):
        size = None
    return OneDriveItem(
        drive_id=drive_id,
        id=item_id,
        name=str(raw.get("name") or source.get("name") or "Untitled"),
        is_folder=is_folder,
        mime_type=mime,
        modified=str(source.get("lastModifiedDateTime") or raw.get("lastModifiedDateTime") or ""),
        ctag=str(source.get("cTag") or raw.get("cTag") or source.get("eTag") or ""),
        link=link if link.startswith("https://") else "",
        size=size,
    )


def _is_graph_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and (parts.hostname or "").lower() == _GRAPH_HOST
        and port is None
        and parts.username is None
    )


async def _backoff(attempt: int, response: httpx.Response | None = None) -> None:
    retry_after = response.headers.get("Retry-After") if response is not None else None
    delay = 2.0**attempt
    if retry_after and retry_after.isdigit():
        delay = min(float(retry_after), 60.0)
    await asyncio.sleep(delay)


class OneDriveClient:
    """The Graph calls the sync needs: list a folder, download a file
    (optionally converted to PDF by Graph), resolve a share link."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        token: Callable[[], Awaitable[str]],
    ) -> None:
        self._http = http
        self._token = token

    async def _get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        """GET Graph JSON with the bearer token, retrying 429 / 5xx with
        backoff. Any other error status raises ``httpx.HTTPStatusError``."""
        if not _is_graph_url(url):
            raise ValueError("refusing a request that isn't to Microsoft Graph")
        for attempt in range(_MAX_ATTEMPTS):
            headers = {"Authorization": f"Bearer {await self._token()}"}
            response = await self._http.get(
                url, params=params, headers=headers, follow_redirects=False
            )
            retryable = response.status_code == 429 or response.status_code >= 500
            if retryable and attempt < _MAX_ATTEMPTS - 1:
                await _backoff(attempt, response)
                continue
            response.raise_for_status()
            if not response.is_success:
                raise httpx.HTTPStatusError(
                    f"unexpected {response.status_code} from Graph",
                    request=response.request,
                    response=response,
                )
            return response.json()
        raise AssertionError("unreachable: the last attempt returns or raises")

    async def list_folder(
        self, drive_id: str, item_id: str, *, max_items: int
    ) -> tuple[list[OneDriveItem], bool]:
        """Every file and folder directly in the folder, and whether the
        listing was cut short (``max_items``, or a paging link that doesn't
        point at Graph)."""
        d, i = sanitize_id(drive_id), sanitize_id(item_id)
        if not d or not i:
            raise ValueError("unsafe OneDrive folder id")
        url: str | None = f"{GRAPH_API}/drives/{d}/items/{i}/children"
        params: dict[str, Any] | None = {"$select": _SELECT, "$top": _PAGE_SIZE}
        items: list[OneDriveItem] = []
        while url:
            data = await self._get_json(url, params)
            params = None  # the next link carries its own query
            values = data.get("value") if isinstance(data, dict) else None
            for raw in values if isinstance(values, list) else []:
                item = _item(raw, d)
                if item is not None:
                    items.append(item)
                if len(items) >= max_items:
                    return items, True
            nxt = data.get("@odata.nextLink") if isinstance(data, dict) else None
            if not nxt:
                return items, False
            if not isinstance(nxt, str) or not _is_graph_url(nxt):
                return items, True
            url = nxt
        return items, False

    async def get_item(self, drive_id: str, item_id: str) -> OneDriveItem | None:
        d, i = sanitize_id(drive_id), sanitize_id(item_id)
        if not d or not i:
            raise ValueError("unsafe OneDrive item id")
        data = await self._get_json(f"{GRAPH_API}/drives/{d}/items/{i}", {"$select": _SELECT})
        return _item(data, d)

    async def resolve_share(self, url: str) -> OneDriveItem | None:
        """The item a sharing link points to (``/shares/{id}/driveItem``)."""
        data = await self._get_json(
            f"{GRAPH_API}/shares/{share_id(url)}/driveItem", {"$select": _SELECT + ",parentReference"}
        )
        return _item(data, "")

    async def download(
        self, drive_id: str, item_id: str, *, max_bytes: int, as_pdf: bool = False
    ) -> bytes:
        """A file's bytes, or Graph's PDF rendering of it with ``as_pdf``.

        Graph answers ``/content`` with a redirect to a short-lived,
        pre-authenticated download URL on another host. That URL is fetched
        WITHOUT the bearer token (it needs none, and the token must never
        leave Graph), streamed, and abandoned with ``OneDriveFileTooLarge``
        as soon as it passes ``max_bytes``."""
        d, i = sanitize_id(drive_id), sanitize_id(item_id)
        if not d or not i:
            raise ValueError("unsafe OneDrive item id")
        params = {"format": "pdf"} if as_pdf else None
        url = f"{GRAPH_API}/drives/{d}/items/{i}/content"
        for attempt in range(_MAX_ATTEMPTS):
            headers = {"Authorization": f"Bearer {await self._token()}"}
            response = await self._http.get(
                url, params=params, headers=headers, follow_redirects=False
            )
            retryable = response.status_code == 429 or response.status_code >= 500
            if retryable and attempt < _MAX_ATTEMPTS - 1:
                await _backoff(attempt, response)
                continue
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location", "")
                if not location.startswith("https://"):
                    raise OneDriveDownloadFailed("the download redirect is not https")
                return await self._fetch_capped(location, item=item_key(d, i), max_bytes=max_bytes)
            response.raise_for_status()
            if not response.is_success:
                raise httpx.HTTPStatusError(
                    f"unexpected {response.status_code} from Graph",
                    request=response.request,
                    response=response,
                )
            # Small files can come back inline rather than redirected.
            if len(response.content) > max_bytes:
                raise OneDriveFileTooLarge(item_key(d, i))
            return response.content
        raise AssertionError("unreachable: the last attempt returns or raises")

    async def _fetch_capped(self, url: str, *, item: str, max_bytes: int) -> bytes:
        try:
            async with self._http.stream("GET", url, follow_redirects=False) as response:
                if not response.is_success:
                    raise OneDriveDownloadFailed(f"the download answered {response.status_code}")
                length = response.headers.get("Content-Length")
                if length and length.isdigit() and int(length) > max_bytes:
                    raise OneDriveFileTooLarge(item)
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise OneDriveFileTooLarge(item)
                return bytes(body)
        except httpx.HTTPError as exc:
            raise OneDriveDownloadFailed(f"the download failed ({type(exc).__name__})") from None
