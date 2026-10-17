"""Act as me: copy one file from the speaker's own mail into the storage the
Executive has connected, for ``save_my_email_attachment``
(``orchestrator.mail_read_tools``).

Every call here is built in code. The model chooses only which attachment
(message id and index); everything that leaves the box is fixed here:

- **Where:** a folder named ``FOLDER_NAME`` in the Executive's own Google
  Drive (workspace-mcp) or OneDrive (ms-365-mcp-server), made by the first
  save and remembered by id (``delegation.saved_files``). Never one found by
  its name: anyone could have made a folder of that name and shared it.
- **What:** the attachment's bytes, read by the mailbox client (never through
  the model), under a cleaned file name.
- **Who:** the speaker's own address, as a reader (Google: one ``user``
  grant; OneDrive: an invite with ``requireSignIn`` and the ``read`` role).
  Never a link, a domain or anyone.

The calls go through the gateway like any other, so its gates still run (the
acting account, URL fetches, sheet formulas, the share gates' roster check,
writes kept to the Executive's own drives). Only the mail-read lockdown lets
them through, and only as these exact objects (``mcp_gateway.code_built_call``).
Each saved file is recorded before it is shared, so from then on other
people's turns can't see it, and no model call can name the folder
(``mcp_gateway._saved_files_hidden``). A copy whose row can't be written is
hidden from every turn for the life of the process, not shared, and reported
as failed (``saved_files.quarantine``).

Which storage: the one the Executive's own mailbox runs on
(``EMAIL_PROVIDER``), when the gateway has that server; else the other one if
it has only that. Neither: ``NoStorage``.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import weakref
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any

from openexecutive.delegation.saved_files import FOLDER_NAME

logger = logging.getLogger(__name__)

GOOGLE = "google"
MICROSOFT = "microsoft"
_SERVERS = {GOOGLE: "google_workspace", MICROSOFT: "microsoft_365"}
WHERE = {GOOGLE: f"the {FOLDER_NAME} folder in Google Drive", MICROSOFT: f"the {FOLDER_NAME} folder in OneDrive"}

_NAME_MAX = 120
# What a file name may not hold in Drive or OneDrive (and nothing that reads
# as a path), plus control characters.
_NAME_BAD = re.compile(r'[\x00-\x1f\x7f"*:<>?/\\|#%]')
_MIME_RE = re.compile(r"^[A-Za-z0-9.+_-]+/[A-Za-z0-9.+_-]+$")
_GOOGLE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,200}$")
_GOOGLE_LINK_PREFIXES = ("https://drive.google.com/", "https://docs.google.com/")
# workspace-mcp's create_drive_file / create_drive_folder answer (1.29.0, unchanged in 2.1.0):
# "Successfully created file '<name>' (ID: <id>) in folder '<folder>' for
# <email>. Link: <link>". The name comes first and may hold anything, so the
# greedy match takes the last "' (ID: " — the real one.
_CREATED_RE = re.compile(
    r"Successfully created (?:file|folder) '.*' \(ID: (?P<id>[A-Za-z0-9_-]+)\) in folder .* Link: (?P<link>\S+)",
    re.DOTALL,
)
_UNDISCOVERED_MARKER = "has not been discovered via search_tools"


class NoStorage(Exception):
    """Neither Google Drive nor OneDrive is connected to the Executive."""


class SaveFailed(Exception):
    """The storage refused or failed; ``detail`` says what, for the model."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class Saved:
    provider: str
    file_id: str
    name: str
    link: str
    shared: bool


def clean_name(raw: str, index: int) -> str:
    """A file name safe in both stores: one line, no path or reserved
    characters, no leading or trailing dots or spaces, the extension kept
    when it is cut."""
    from openexecutive.delegation.ghostwriter import one_line

    name = _NAME_BAD.sub("_", one_line(raw or "", 400)).strip(" .")
    if len(name) > _NAME_MAX:
        suffix = PurePath(name).suffix[:12]
        name = name[: _NAME_MAX - len(suffix)].rstrip(" .") + suffix
    return name or f"attachment-{index}"


def clean_mime(raw: str) -> str:
    value = (raw or "").strip().lower()
    return value if _MIME_RE.fullmatch(value) else "application/octet-stream"


def storage_for(gateway: Any) -> str | None:
    """Which storage to save to: the one the Executive's own mailbox runs on
    (``EMAIL_PROVIDER``) when the gateway has its server, else the other
    when only it is there; None for neither (or no gateway)."""
    from openexecutive.config import get_settings
    from openexecutive.orchestrator.mcp_gateway import (
        gateway_reconnect_needed,
        gateway_server_names,
    )

    if gateway is None:
        return None
    servers = set(gateway_server_names(gateway)) - set(gateway_reconnect_needed(gateway))
    preferred = str(getattr(get_settings(), "email_provider", GOOGLE) or GOOGLE).strip().lower()
    order = [preferred, *(p for p in (GOOGLE, MICROSOFT) if p != preferred)]
    return next((p for p in order if _SERVERS.get(p) in servers), None)


# One save at a time per event loop: two at once would each make the folder,
# and on OneDrive two files of one name would overwrite each other.
_LOCKS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = weakref.WeakKeyDictionary()


def _lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _LOCKS.get(loop)
    if lock is None:
        lock = _LOCKS[loop] = asyncio.Lock()
    return lock


async def _call(gateway: Any, name: str, arguments: dict[str, Any]) -> str:
    """One code-built gateway call (``code_built_call``), discovering the tool
    first if this gateway session hasn't yet."""
    from openexecutive.orchestrator.mcp_gateway import code_built_call

    tool_input = {"name": name, "arguments": arguments}
    with code_built_call(tool_input):
        text = await gateway.call_tool(tool_input)
    if isinstance(text, str) and _UNDISCOVERED_MARKER in text[:600] and await gateway._discover(name):
        with code_built_call(tool_input):
            text = await gateway.call_tool(tool_input)
    return text if isinstance(text, str) else ""


def _failed(text: str) -> bool:
    head = text.lstrip()
    if not head:
        return True
    if head.startswith("{"):
        try:
            parsed = json.loads(head)
        except ValueError:
            return False
        return isinstance(parsed, dict) and "error" in parsed
    return head.lower().startswith(("error", "tool error"))


_MISSING_RE = re.compile(r"\b404\b|not ?found|does not exist|no longer exists|trashed", re.IGNORECASE)


def _folder_missing(text: str) -> bool:
    """Whether a failed upload says its folder is gone: only then is a new
    folder made. A quota, server or file-type error returns at once, so a
    failing save doesn't leave a new empty folder each time."""
    return _failed(text) and bool(_MISSING_RE.search(text))


def _why(text: str) -> str:
    """The error a failed call gave, short, for the model."""
    try:
        parsed = json.loads(text)
        message = parsed.get("error") if isinstance(parsed, dict) else None
    except ValueError:
        message = None
    from openexecutive.delegation.ghostwriter import one_line

    return one_line(str(message if message is not None else text), 300)


async def save(
    gateway: Any, provider: str, *, data: bytes, name: str, mime: str, owner_id: int, owner_email: str
) -> Saved:
    """Copy ``data`` to ``provider``'s storage as ``name``, record it as
    ``owner_id``'s and share it with ``owner_email`` alone."""
    async with _lock():
        if provider == GOOGLE:
            return await _google(gateway, data, name, mime, owner_id, owner_email)
        if provider == MICROSOFT:
            return await _microsoft(gateway, data, name, owner_id, owner_email)
    raise NoStorage(provider)


def _record(owner_id: int, provider: str, file_id: str, name: str) -> None:
    """Record the copy as ``owner_id``'s before it is shared. When the row
    can't be written, hide the copy from every turn for the life of the
    process and fail: it is not shared and the model is told it wasn't
    saved."""
    from openexecutive.delegation import saved_files

    try:
        saved_files.record(owner_id, provider, file_id, name=name)
    except Exception as exc:
        saved_files.quarantine(file_id)
        logger.error("save_my_email_attachment: couldn't record a saved copy as private; hidden until restart", exc_info=True)
        raise SaveFailed("It couldn't be kept private, so it was not shared. Try again in a moment.") from exc


def _exec_account() -> str:
    from openexecutive.config import get_settings

    return get_settings().exec_email_address.strip().lower()


# --------------------------------------------------------------------------- #
# Google Drive (workspace-mcp, as the Executive's own account)
# --------------------------------------------------------------------------- #


async def _google_folder(gateway: Any, account: str) -> tuple[str, bool]:
    """The folder copies go in, and whether it was made just now. Made here,
    never found by name."""
    from openexecutive.delegation import saved_files

    remembered = saved_files.remembered_folder(GOOGLE, account)
    if remembered is not None:
        return remembered[0], False
    made = await _call(gateway, "google_workspace__create_drive_folder", {
        "user_google_email": account, "folder_name": FOLDER_NAME, "parent_folder_id": "root",
    })
    created = _CREATED_RE.search(made)
    if _failed(made) or created is None or not _GOOGLE_ID_RE.fullmatch(created.group("id")):
        raise SaveFailed(f"Couldn't make the {FOLDER_NAME} folder in Google Drive: {_why(made)}")
    saved_files.remember_folder(GOOGLE, account, created.group("id"))
    return created.group("id"), True


async def _google(gateway: Any, data: bytes, name: str, mime: str, owner_id: int, owner_email: str) -> Saved:
    from openexecutive.delegation import saved_files

    account = _exec_account()
    arguments: dict[str, Any] = {
        "user_google_email": account,
        "file_name": name,
        "base64_content": base64.b64encode(data).decode("ascii"),
        "content_mime_type": mime,
        "base64_sha256": hashlib.sha256(data).hexdigest(),
    }
    created = None
    made = ""
    while True:
        arguments["folder_id"], fresh = await _google_folder(gateway, account)
        made = await _call(gateway, "google_workspace__create_drive_file", arguments)
        created = None if _failed(made) else _CREATED_RE.search(made)
        if created is not None or fresh or not _folder_missing(made):
            break
        # The remembered folder may be gone: make a new one, once.
        saved_files.forget_folder(GOOGLE, account)
    if created is None:
        raise SaveFailed(f"Google Drive didn't take the file: {_why(made)}")
    file_id, link = created.group("id"), created.group("link")
    if not _GOOGLE_ID_RE.fullmatch(file_id):
        raise SaveFailed("Google Drive answered with a file id that can't be used.")
    _record(owner_id, GOOGLE, file_id, name)
    if not link.startswith(_GOOGLE_LINK_PREFIXES):
        link = f"https://drive.google.com/file/d/{file_id}/view"
    shared = await _call(gateway, "google_workspace__manage_drive_access", {
        "user_google_email": account,
        "file_id": file_id,
        "action": "grant",
        "share_with": owner_email,
        "role": "reader",
        "share_type": "user",
        # No email from Drive carrying the sender's file name: the tool
        # returns the link.
        "send_notification": False,
    })
    ok = not _failed(shared)
    if not ok:
        logger.warning("save_my_email_attachment: sharing the Drive copy failed: %s", _why(shared)[:120])
    return Saved(GOOGLE, file_id, name, link, ok)


# --------------------------------------------------------------------------- #
# OneDrive (ms-365-mcp-server, as the Executive's own Microsoft 365 sign-in)
# --------------------------------------------------------------------------- #


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


def _items(parsed: Any) -> list[dict[str, Any]]:
    values = parsed.get("value") if isinstance(parsed, dict) else None
    return [v for v in values if isinstance(v, dict)] if isinstance(values, list) else []


async def _onedrive_drive(gateway: Any) -> str:
    """The Executive's own OneDrive: its personal or business drive."""
    text = await _call(gateway, "microsoft_365__list-drives", {})
    drives = [d for d in _items(_json(text)) if isinstance(d.get("id"), str) and d["id"]]
    if _failed(text) or not drives:
        raise NoStorage(MICROSOFT)
    own = [d for d in drives if str(d.get("driveType", "")).lower() in ("personal", "business")]
    return str((own or drives)[0]["id"])


async def _onedrive_folder(gateway: Any, account: str, drive_id: str) -> tuple[str, bool]:
    """The folder copies go in, and whether it was made just now. Made here,
    never found by name (Graph renames ours if the name is taken)."""
    from openexecutive.delegation import saved_files

    remembered = saved_files.remembered_folder(MICROSOFT, account)
    if remembered is not None and remembered[1] == drive_id:
        return remembered[0], False
    made = await _call(gateway, "microsoft_365__create-onedrive-folder", {
        "drive-id": drive_id, "driveItem-id": "root", "body": {"name": FOLDER_NAME, "folder": {}},
    })
    parsed = _json(made)
    if _failed(made) or not isinstance(parsed, dict) or not isinstance(parsed.get("id"), str) or not parsed["id"]:
        raise SaveFailed(f"Couldn't make the {FOLDER_NAME} folder in OneDrive: {_why(made)}")
    saved_files.remember_folder(MICROSOFT, account, parsed["id"], drive_id)
    return parsed["id"], True


def _free_name(name: str, taken: set[str]) -> str:
    """``name``, or ``name (2)`` and on: OneDrive's upload overwrites a file of
    the same name. ``taken`` is every name a save has used, since only saves
    write to the folder (no model call can name it)."""
    if name.lower() not in taken:
        return name
    path = PurePath(name)
    stem, suffix = (path.stem, path.suffix) if path.suffix else (name, "")
    n = 2
    while f"{stem} ({n}){suffix}".lower() in taken:
        n += 1
    return f"{stem} ({n}){suffix}"


async def _microsoft(gateway: Any, data: bytes, name: str, owner_id: int, owner_email: str) -> Saved:
    from openexecutive.delegation import saved_files

    account = _exec_account()
    drive_id = await _onedrive_drive(gateway)
    name = _free_name(name, saved_files.names(MICROSOFT))
    while True:
        folder_id, fresh = await _onedrive_folder(gateway, account, drive_id)
        made = await _call(gateway, "microsoft_365__upload-file-content", {
            "drive-id": drive_id,
            "driveItem-id": f"{folder_id}:/{name}:",
            "body": base64.b64encode(data).decode("ascii"),
        })
        parsed = _json(made)
        if (not _failed(made) and isinstance(parsed, dict)) or fresh or not _folder_missing(made):
            break
        # The remembered folder may be gone: make a new one, once.
        saved_files.forget_folder(MICROSOFT, account)
    if _failed(made) or not isinstance(parsed, dict) or not isinstance(parsed.get("id"), str):
        raise SaveFailed(f"OneDrive didn't take the file: {_why(made)}")
    file_id = parsed["id"]
    _record(owner_id, MICROSOFT, file_id, name)
    link = parsed.get("webUrl") if isinstance(parsed.get("webUrl"), str) else ""
    if not str(link).startswith("https://"):
        link = ""
    shared = await _call(gateway, "microsoft_365__share-drive-item", {
        "drive-id": drive_id,
        "driveItem-id": file_id,
        "body": {
            "recipients": [{"email": owner_email}],
            "roles": ["read"],
            "requireSignIn": True,
            # No invitation email carrying the sender's file name: the tool
            # returns the link.
            "sendInvitation": False,
        },
    })
    ok = not _failed(shared)
    if not ok:
        logger.warning("save_my_email_attachment: sharing the OneDrive copy failed: %s", _why(shared)[:120])
    return Saved(MICROSOFT, file_id, str(parsed.get("name") or name), str(link), ok)
