"""Read-only Google Drive v3 client for the folder sync (``drive_sync``).

Authenticates as a service account with the ``drive.readonly`` scope, so the
sync sees only what has been shared with that account (the synced folders)
and can change nothing. This is deliberately NOT the workspace-mcp gateway
the Executive uses inside a turn: that one acts as the Executive's own
account at the ``complete`` tool tier, and its results are display text,
where this reads structured JSON.
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_SCOPES = ("https://www.googleapis.com/auth/drive.readonly",)

FOLDER_MIME = "application/vnd.google-apps.folder"
# What Drive issues as a file or folder id. Checked before any id goes into a
# URL path or a query string.
_DRIVE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_PAGE_TOKEN_RE = re.compile(r"[A-Za-z0-9_.~+/=-]{1,2048}")
_LIST_FIELDS = (
    "nextPageToken,files(id,name,mimeType,modifiedTime,md5Checksum,webViewLink,size)"
)
_MAX_ATTEMPTS = 4


def sanitize_drive_id(value: object) -> str | None:
    raw = str(value or "").strip()
    return raw if _DRIVE_ID_RE.fullmatch(raw) else None


class DriveFileTooLarge(Exception):
    """A download would exceed the caller's byte cap."""


@dataclass(frozen=True)
class DriveItem:
    id: str
    name: str
    mime_type: str
    modified: str
    md5: str
    link: str
    size: int | None

    @property
    def is_folder(self) -> bool:
        return self.mime_type == FOLDER_MIME


def _item(raw: Any) -> DriveItem | None:
    if not isinstance(raw, dict):
        return None
    file_id = sanitize_drive_id(raw.get("id"))
    if not file_id:
        return None
    link = str(raw.get("webViewLink") or "")
    size_raw = raw.get("size")
    try:
        size = int(size_raw) if size_raw is not None else None
    except (TypeError, ValueError):
        size = None
    return DriveItem(
        id=file_id,
        name=str(raw.get("name") or "Untitled"),
        mime_type=str(raw.get("mimeType") or ""),
        modified=str(raw.get("modifiedTime") or ""),
        md5=str(raw.get("md5Checksum") or ""),
        link=link if link.startswith("https://") else "",
        size=size,
    )


def service_account_token_provider(key_file: str) -> Callable[[], Awaitable[str]]:
    """An access-token source for the service account in ``key_file``,
    refreshed (off the event loop) only when the cached token has expired."""
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_file(
        key_file, scopes=list(DRIVE_SCOPES)
    )
    lock = asyncio.Lock()

    def _refresh() -> None:
        import google_auth_httplib2
        import httplib2

        creds.refresh(google_auth_httplib2.Request(httplib2.Http(timeout=30)))

    async def token() -> str:
        async with lock:
            if not creds.valid:
                await asyncio.to_thread(_refresh)
            return str(creds.token)

    return token


async def _backoff(attempt: int) -> None:
    await asyncio.sleep(2 ** attempt)


class DriveClient:
    """The three Drive calls the sync needs: list a folder, export a Google
    file as text, download a stored file (both capped by size)."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        token: Callable[[], Awaitable[str]],
    ) -> None:
        self._http = http
        self._token = token

    async def _get(self, path: str, params: dict[str, Any]) -> httpx.Response:
        """GET with the bearer token, retrying 429 / 5xx with backoff. Any
        other error status raises ``httpx.HTTPStatusError``."""
        for attempt in range(_MAX_ATTEMPTS):
            headers = {"Authorization": f"Bearer {await self._token()}"}
            response = await self._http.get(f"{DRIVE_API}{path}", params=params, headers=headers)
            retryable = response.status_code == 429 or response.status_code >= 500
            if retryable and attempt < _MAX_ATTEMPTS - 1:
                await _backoff(attempt)
                continue
            response.raise_for_status()
            return response
        raise AssertionError("unreachable: the last attempt returns or raises")

    async def _get_bytes(self, path: str, params: dict[str, Any], *, max_bytes: int) -> bytes:
        """GET a body, streamed, aborting with ``DriveFileTooLarge`` as soon
        as it passes ``max_bytes`` — never buffering more than that. Retries
        429 / 5xx like ``_get``."""
        for attempt in range(_MAX_ATTEMPTS):
            headers = {"Authorization": f"Bearer {await self._token()}"}
            async with self._http.stream(
                "GET", f"{DRIVE_API}{path}", params=params, headers=headers
            ) as response:
                retryable = response.status_code == 429 or response.status_code >= 500
                if retryable and attempt < _MAX_ATTEMPTS - 1:
                    await response.aclose()
                    await _backoff(attempt)
                    continue
                if not response.is_success:
                    # A 3xx is not followed (the token stays on googleapis.com)
                    # and must fail the fetch, not read as an empty file.
                    await response.aread()
                    response.raise_for_status()
                    raise httpx.HTTPStatusError(
                        f"unexpected {response.status_code} from Drive",
                        request=response.request,
                        response=response,
                    )
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise DriveFileTooLarge(path)
                return bytes(body)
        raise AssertionError("unreachable: the last attempt returns or raises")

    async def list_folder(
        self, folder_id: str, *, max_items: int
    ) -> tuple[list[DriveItem], bool]:
        """Every item directly in ``folder_id`` (not trashed), and whether
        the listing was cut short (``max_items``, or an unusable page token).
        Items with an id Drive would not issue are dropped."""
        safe = sanitize_drive_id(folder_id)
        if not safe:
            raise ValueError(f"unsafe Drive folder id: {folder_id!r}")
        items: list[DriveItem] = []
        token: str | None = None
        while True:
            params: dict[str, Any] = {
                "q": f"'{safe}' in parents and trashed = false",
                "fields": _LIST_FIELDS,
                "pageSize": 100,
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            if token:
                params["pageToken"] = token
            data = (await self._get("/files", params)).json()
            files = data.get("files") if isinstance(data, dict) else None
            for raw in files if isinstance(files, list) else []:
                item = _item(raw)
                if item is not None:
                    items.append(item)
                if len(items) >= max_items:
                    return items, True
            nxt = data.get("nextPageToken") if isinstance(data, dict) else None
            if not nxt:
                return items, False
            if not isinstance(nxt, str) or not _PAGE_TOKEN_RE.fullmatch(nxt):
                return items, True
            token = nxt

    async def export(self, file_id: str, mime_type: str, *, max_bytes: int) -> bytes:
        """A Google Doc / Sheet / Slides file exported as ``mime_type``."""
        safe = sanitize_drive_id(file_id)
        if not safe:
            raise ValueError(f"unsafe Drive file id: {file_id!r}")
        return await self._get_bytes(
            f"/files/{safe}/export", {"mimeType": mime_type}, max_bytes=max_bytes
        )

    async def download(self, file_id: str, *, max_bytes: int) -> bytes:
        """A stored (non-Google) file's bytes."""
        safe = sanitize_drive_id(file_id)
        if not safe:
            raise ValueError(f"unsafe Drive file id: {file_id!r}")
        return await self._get_bytes(
            f"/files/{safe}", {"alt": "media", "supportsAllDrives": "true"}, max_bytes=max_bytes
        )
