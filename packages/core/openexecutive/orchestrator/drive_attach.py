"""Act as me: attach Google Drive files to a draft in the principal's own
mailbox, for ``ghostwrite_email``'s ``drive_files``
(``orchestrator.delegation_tools``).

The model chooses only which files (their Drive ids). Everything else is
fixed here, and nothing a file holds reaches the model:

- **Who:** the principal only. The files come from the Executive's own Drive,
  which can hold much that a team member can't open themselves.
- **How:** per file, one code-built ``get_drive_file_permissions`` call (its
  size, so an oversized file is never downloaded) and one code-built
  ``get_drive_file_download_url`` call (``mcp_gateway.code_built_call``), as
  the Executive's own Google account, which saves the file to the tool
  download folder. The saved copy is found only from workspace-mcp's own
  ``Saved to:`` line (anchored, exactly one, its name ending in the uuid
  workspace-mcp adds: the answer echoes the Drive name first, and anyone who
  can share a file chooses that), read only from inside those folders
  (``tool_catalog._allowed_file_dirs``), and removed. The attached name comes
  from that saved name, not the echoed one. A file's text is never put in
  front of the model, so attaching it is not an outside read
  (``orchestrator.outside_content``).
- **Whose:** a file someone saved from their own mail, and the folder those
  saves go to (``delegation.saved_files``), are refused for anyone else, as
  the gateway refuses a model's call naming them, before and again after the
  download. Fails closed when that record can't be read.
- **How much:** the forward caps (``gmail.FORWARD_MAX_FILES`` files,
  ``gmail.FORWARD_MAX_BYTES`` in all); files past them are left off and
  counted.

It only fills a draft the person reviews in their own mailbox before they
send it.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SERVER = "google_workspace"
DOWNLOAD_TOOL = "google_workspace__get_drive_file_download_url"
INFO_TOOL = "google_workspace__get_drive_file_permissions"
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,200}$")
# workspace-mcp's own lines come after the Drive name it echoes, so each is
# anchored to its line and the last one is the real one.
_SAVED_RE = re.compile(r"^\s*(?:\U0001F4CE\s*)?Saved to:\s*(/.+?)\s*$", re.MULTILINE)
_MIME_LINE_RE = re.compile(r"^MIME Type:\s*(\S+)\s*$", re.MULTILINE)
_SIZE_LINE_RE = re.compile(r"^Size:\s*(\d+|N/A) bytes\s*$", re.MULTILINE)
# The saved file's name: the sanitized stem, "_", a uuid4, the suffix.
_SAVED_NAME_RE = re.compile(
    r"^(?P<stem>.*?)_?[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}(?P<suffix>\.[A-Za-z0-9]{1,10})?$"
)


class AttachError(Exception):
    """A file couldn't be attached: the message says which, in plain words."""


@dataclass
class DriveFiles:
    """The files to attach (name, MIME type, bytes) and how many were left
    off for the caps."""

    files: list[tuple[str, str, bytes]] = field(default_factory=list)
    skipped: int = 0


def valid_ids(raw: Any) -> list[str] | str:
    """The ``drive_files`` ids, deduplicated, or why they are refused."""
    from openexecutive.delegation.gmail import FORWARD_MAX_FILES

    items = raw if isinstance(raw, list) else [raw]
    ids = [str(i).strip() for i in items if isinstance(i, str) and i.strip()]
    if not ids:
        return "`drive_files` must list Drive file ids."
    bad = [i for i in ids if not _ID_RE.fullmatch(i)]
    if bad:
        return f"Not a Drive file id: {', '.join(i[:60] for i in bad[:3])}."
    ids = list(dict.fromkeys(ids))
    if len(ids) > FORWARD_MAX_FILES:
        return f"At most {FORWARD_MAX_FILES} files on one draft."
    return ids


def saved_copy(raw: str) -> tuple[Path, str]:
    """The copy workspace-mcp saved, and the file name to attach it under,
    from its answer; else ``AttachError``. Exactly one anchored ``Saved to:``
    line, a name ending in workspace-mcp's uuid, inside the download folders."""
    from openexecutive.workflows.tool_catalog import _allowed_file_dirs

    paths = _SAVED_RE.findall(raw)
    if len(paths) != 1:
        raise AttachError("Drive didn't hand the file over")
    named = _SAVED_NAME_RE.fullmatch(Path(paths[0]).name)
    if named is None:
        raise AttachError("Drive didn't hand the file over")
    try:
        path = Path(paths[0]).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise AttachError("Drive didn't hand the file over") from exc
    if not path.is_file() or not any(path.is_relative_to(d) for d in _allowed_file_dirs()):
        raise AttachError("Drive saved the file somewhere I don't read from")
    return path, f"{named.group('stem')}{named.group('suffix') or ''}"


def _check_whose(file_ids: list[str], viewer: int | None) -> None:
    """Refuse a file saved from someone else's mail, or a save folder."""
    from openexecutive.delegation import saved_files

    try:
        hidden, folders = saved_files.protected(viewer)
    except Exception as exc:
        logger.warning("drive_attach: couldn't read the saved-files record", exc_info=True)
        raise AttachError("Couldn't check who those files belong to") from exc
    named = [i for i in file_ids if i in hidden or i in folders]
    if named:
        raise AttachError(f"Drive file {named[0]} isn't one I can attach for you")


async def _declared_size(gateway: Any, account: str, file_id: str) -> int | None:
    """Drive's size for the file, before downloading it; None when Drive
    doesn't say (a Google Doc, exported on download)."""
    from openexecutive.orchestrator.mail_file_save import _call, _failed

    text = await _call(gateway, INFO_TOOL, {"user_google_email": account, "file_id": file_id})
    if _failed(text):
        raise AttachError(f"Couldn't get Drive file {file_id}")
    sizes = _SIZE_LINE_RE.findall(text)
    return int(sizes[-1]) if sizes and sizes[-1].isdigit() else None


async def _download(gateway: Any, account: str, file_id: str, index: int) -> tuple[str, str, Path]:
    """Download one file: its name, MIME type and the saved copy's path."""
    from openexecutive.orchestrator.mail_file_save import _call, _failed, clean_mime, clean_name

    text = await _call(gateway, DOWNLOAD_TOOL, {"user_google_email": account, "file_id": file_id})
    if _failed(text):
        raise AttachError(f"Couldn't get Drive file {file_id}")
    path, name = saved_copy(text)
    mimes = _MIME_LINE_RE.findall(text)
    return clean_name(name, index), clean_mime(mimes[-1] if mimes else ""), path


async def fetch(file_ids: list[str], viewer: int | None) -> DriveFiles:
    """The bytes of ``file_ids`` (already ``valid_ids``), as the Executive's
    Google account, for ``viewer`` (``saved_files.viewer_person_id``).
    Raises ``AttachError`` when Drive isn't connected, a file is someone
    else's saved file, or one can't be fetched."""
    from openexecutive.delegation.gmail import FORWARD_MAX_BYTES, FORWARD_MAX_FILES
    from openexecutive.orchestrator.mail_file_save import _exec_account
    from openexecutive.orchestrator.mcp_gateway import (
        gateway_reconnect_needed,
        gateway_server_names,
        get_active_gateway,
    )

    gateway = get_active_gateway()
    if gateway is None or SERVER not in set(gateway_server_names(gateway)) - set(gateway_reconnect_needed(gateway)):
        raise AttachError("Google Drive isn't connected")
    _check_whose(file_ids, viewer)
    account = _exec_account()
    out = DriveFiles()
    total = 0
    for index, file_id in enumerate(file_ids, 1):
        declared = await _declared_size(gateway, account, file_id)
        if len(out.files) >= FORWARD_MAX_FILES or (declared is not None and total + declared > FORWARD_MAX_BYTES):
            out.skipped += 1
            continue
        name, mime, path = await _download(gateway, account, file_id, index)
        try:
            size = path.stat().st_size
            if total + size > FORWARD_MAX_BYTES:
                out.skipped += 1
                continue
            data = path.read_bytes()
        finally:
            try:
                path.unlink()
            except OSError:
                logger.debug("drive_attach: couldn't remove a downloaded copy")
        total += len(data)
        out.files.append((name, mime, data))
    # A file saved from someone's mail while these downloaded is theirs too.
    _check_whose(file_ids, viewer)
    return out
