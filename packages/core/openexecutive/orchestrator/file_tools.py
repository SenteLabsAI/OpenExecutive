"""``save_file``: put any file (``orchestrator.file_refs``) into a folder of the
Executive's own Google Drive or OneDrive, under the name asked for.

Before this, a file reached Drive only as an email attachment copied into one
"From your email" folder, and nothing put a file a Python job made anywhere:
the Executive split the PDF and then told the person to download both halves
and drop them into the folder themselves. Every call here is built in code;
the model chooses the file, the folder and the name:

- **Who:** the principal (``content_trust.PRINCIPAL_ONLY_TOOLS``). The
  Executive's Drive is the company's.
- **Where:** a folder of the Executive's own storage. On Google Drive one
  that only the Executive and the team can see (``folder_refusal``): a
  folder an outsider owns or can open, or one shared with a group, a domain
  or by link, is refused, so nothing asked for in a document lands where an
  outsider can read it. On OneDrive any folder of its own drive, whose upload
  path can't reach another drive; a name already there gets " (2)". Or,
  with ``folder`` ``documents``, the company's Documents (the Knowledge
  page's uploads, ``_documents``), indexed like an upload.
- **When:** whenever the person asks. A turn that read their mail or anything
  from outside the team still saves, but only when their own words this turn
  ask for file work (``asked_for_file_work``), so text in a document can't
  make it move their files.

The calls go through the gateway like any other (``mcp_gateway.code_built_call``),
so its gates (the acting account, writes kept to the Executive's own drives)
still run.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from pathlib import PurePath
from typing import Any

logger = logging.getLogger(__name__)

SAVE_FILE = "save_file"
_FOLDER_MIME = "application/vnd.google-apps.folder"
_GOOGLE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,200}$")
_ONEDRIVE_ID_RE = re.compile(r"^(?=.*[A-Za-z0-9])[A-Za-z0-9!._-]{1,200}$")
_EMAIL_IN_RE = re.compile(r"\(([^()\s]+@[^()\s]+)\)")
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
# Words that ask for file work, in the person's own message.
_FILE_WORDS = frozenset({
    "attach", "attached", "attaching", "attachment", "attachments",
    "copy", "drive", "drop", "file", "files", "folder", "folders", "move",
    "onedrive", "put", "rename", "save", "saved", "separate", "separated",
    "seperate", "seperated", "split", "store", "upload", "uploaded",
})

SAVE_FILE_TOOL: dict[str, Any] = {
    "name": SAVE_FILE,
    "description": (
        "Save a file into a folder of your own Google Drive or OneDrive "
        "(whichever is connected), under the name you give it. Any file by its "
        "handle: mail:<message_id>#<n> (an attachment of an email in their "
        "mailbox), drive:<file_id> (a copy of a Drive file), file:<name> (a file "
        "sent in this chat or made by a Python job). Find the folder's id with "
        "your Drive tools first (search or list the folder they named), or pass "
        "root for the top of your Drive, or documents for the company's Documents "
        "on the Knowledge page (the place to save when no Drive or OneDrive is "
        "connected; PDF, Word, Excel, CSV, Markdown and text only). Do it whenever they ask for a file to be "
        "saved, put or filed somewhere; never ask first, and never ask them to "
        "download or move it themselves. Call it once per file. Returns the link."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "file": {"type": "string", "description": "The file's handle."},
            "folder": {
                "type": "string",
                "description": "The folder's Drive or OneDrive id, root, or documents.",
            },
            "name": {
                "type": "string",
                "description": "The name to save it under; the file's own name when left out.",
            },
        },
        "required": ["file", "folder"],
    },
}

FILE_TOOLS: list[dict[str, Any]] = [SAVE_FILE_TOOL]


def _error(message: str, **extra: Any) -> str:
    return json.dumps({"error": message, **extra})


def asked_for_file_work() -> bool:
    """Whether the speaker's own words this turn (``own_words``: no quoted
    backstory, no attached file's text) ask to save, move, attach or split a
    file. Fails closed."""
    from openexecutive.delegation.settings import own_words, turn_delegation
    from openexecutive.orchestrator.schedule_tools import current_session

    try:
        pinned = turn_delegation(current_session.get())
        typed = own_words(getattr(pinned, "speaker_text", "") or "")
    except Exception:
        return False
    words = {w.lower() for w in _WORD_RE.findall(typed or "")}
    return bool(words & _FILE_WORDS)


def _read_something_outside() -> bool:
    """Whether this turn read the owner's mail or anything from outside the
    team. Fails closed."""
    from openexecutive.delegation.settings import turn_delegation
    from openexecutive.orchestrator import outside_content
    from openexecutive.orchestrator.schedule_tools import current_session

    try:
        pinned = turn_delegation(current_session.get())
        return (
            bool(getattr(pinned, "read_mail", False) or getattr(pinned, "unchecked_mail", False))
            or outside_content.read_outside()
        )
    except Exception:
        return True


def _named(raw: Any, fallback: str) -> str:
    """The name to save under: the one asked for, cleaned, keeping the file's
    own extension when the asked-for name has none."""
    from openexecutive.orchestrator.mail_file_save import clean_name

    wanted = str(raw or "").strip()
    if not wanted:
        return clean_name(fallback, 1)
    suffix = PurePath(fallback).suffix
    if suffix and not PurePath(wanted).suffix:
        wanted += suffix
    return clean_name(wanted, 1)


def _team_emails() -> set[str]:
    from openexecutive.people.store import list_people

    try:
        people = list_people(include_contacts=False)
    except Exception:
        logger.warning("save_file: couldn't read the team", exc_info=True)
        return set()
    return {a.lower() for p in people for a in [p.email, *p.email_aliases] if a}


def folder_refusal(info: str, folder: str, account: str, team: set[str]) -> str | None:
    """Why a Google Drive folder, by ``get_drive_file_permissions``'s answer,
    is not one to save into, or None.

    The answer starts with the folder's name, which anyone who shares a
    folder chooses and which may hold line breaks, so nothing is read from
    it by search: the ``ID:`` line must be there exactly once, and the type
    and owners are the fixed lines right after it. Then every permission
    listed must be the Executive or the team: a folder in someone else's
    shared drive, one shared with an outsider, a group, a domain or by link
    is refused. The lines after the permissions can only add entries, which
    can only refuse."""
    lines = info.splitlines()
    marks = [i for i, line in enumerate(lines) if line.strip() == f"ID: {folder}"]
    if len(marks) != 1 or len(lines) < marks[0] + 4:
        return "I couldn't tell what that Drive item is"
    kind, owners = lines[marks[0] + 1], lines[marks[0] + 3]
    if not kind.startswith("Type: ") or not owners.startswith("Owners: "):
        return "I couldn't tell what that Drive item is"
    if kind.removeprefix("Type: ").strip() != _FOLDER_MIME:
        return "That Drive id is a file, not a folder"
    allowed = {account} | team
    owner_emails = {e.lower() for e in _EMAIL_IN_RE.findall(owners)}
    if not owner_emails <= allowed:
        return "That folder belongs to someone outside the team, so I don't save into it"
    tail = lines[marks[0] + 4:]
    try:
        start = next(i for i, line in enumerate(tail) if line.strip() == "Sharing Status:")
    except StopIteration:
        return "I couldn't tell who can see that folder"
    for line in tail[start + 1:]:
        entry = line.strip()
        if not entry.startswith("- "):
            continue
        entry = entry[2:]
        if entry.startswith(("User: ", "Group: ")):
            email = entry.split(": ", 1)[1].split(" ", 1)[0].lower()
            if entry.startswith("User: ") and email in allowed:
                continue
        return "That folder is shared with someone outside the team, so I don't save into it"
    if not owner_emails and not any(line.strip().startswith("- User: ") for line in tail[start + 1:]):
        return "I couldn't tell who can see that folder"
    return None


async def _google(gateway: Any, item: Any, folder: str, name: str) -> dict[str, Any]:
    from openexecutive.orchestrator.drive_attach import INFO_TOOL
    from openexecutive.orchestrator.mail_file_save import (
        _CREATED_RE,
        SaveFailed,
        _call,
        _exec_account,
        _failed,
        _why,
    )

    account = _exec_account()
    if folder != "root":
        if not _GOOGLE_ID_RE.fullmatch(folder):
            raise SaveFailed("That isn't a Google Drive folder id.")
        info = await _call(gateway, INFO_TOOL, {"user_google_email": account, "file_id": folder})
        if _failed(info):
            raise SaveFailed(f"Couldn't open that folder: {_why(info)}")
        refused = folder_refusal(info, folder, account, _team_emails())
        if refused is not None:
            raise SaveFailed(refused + ".")
    made = await _call(gateway, "google_workspace__create_drive_file", {
        "user_google_email": account,
        "file_name": name,
        "folder_id": folder,
        "base64_content": base64.b64encode(item.data).decode("ascii"),
        "content_mime_type": item.mime,
        "base64_sha256": hashlib.sha256(item.data).hexdigest(),
    })
    created = None if _failed(made) else _CREATED_RE.search(made)
    if created is None or not _GOOGLE_ID_RE.fullmatch(created.group("id")):
        raise SaveFailed(f"Google Drive didn't take the file: {_why(made)}")
    file_id = created.group("id")
    link = created.group("link")
    if not link.startswith(("https://drive.google.com/", "https://docs.google.com/")):
        link = f"https://drive.google.com/file/d/{file_id}/view"
    return {"file_id": file_id, "link": link, "where": "Google Drive"}


async def _onedrive_names(gateway: Any, drive_id: str, folder: str) -> set[str]:
    from openexecutive.orchestrator.mail_file_save import _call, _failed, _items, _json

    text = await _call(gateway, "microsoft_365__list-folder-files", {"drive-id": drive_id, "driveItem-id": folder})
    if _failed(text):
        return set()
    return {str(i.get("name") or "").lower() for i in _items(_json(text))}


async def _microsoft(gateway: Any, item: Any, folder: str, name: str) -> dict[str, Any]:
    from openexecutive.orchestrator.mail_file_save import (
        SaveFailed,
        _call,
        _failed,
        _free_name,
        _json,
        _onedrive_drive,
        _why,
    )

    if folder != "root" and not _ONEDRIVE_ID_RE.fullmatch(folder):
        raise SaveFailed("That isn't a OneDrive folder id.")
    drive_id = await _onedrive_drive(gateway)
    name = _free_name(name, await _onedrive_names(gateway, drive_id, folder))
    made = await _call(gateway, "microsoft_365__upload-file-content", {
        "drive-id": drive_id,
        "driveItem-id": f"{folder}:/{name}:",
        "body": base64.b64encode(item.data).decode("ascii"),
    })
    parsed = _json(made)
    if _failed(made) or not isinstance(parsed, dict) or not isinstance(parsed.get("id"), str):
        raise SaveFailed(f"OneDrive didn't take the file: {_why(made)}")
    link = parsed.get("webUrl") if isinstance(parsed.get("webUrl"), str) else ""
    return {
        "file_id": parsed["id"],
        "link": link if str(link).startswith("https://") else "",
        "where": "OneDrive",
        "name": str(parsed.get("name") or name),
    }


DOCUMENTS = "documents"


async def _documents(item: Any, name: str) -> dict[str, Any]:
    """Into the company's Documents, as ``POST /documents`` would put it: the
    file in ``company/docs`` and its text in the company collection. A name
    already there gets " (2)" rather than replacing that document."""
    import asyncio
    import tempfile
    from pathlib import Path

    from openexecutive.api.routes.documents import ALLOWED_EXTENSIONS
    from openexecutive.config import get_settings
    from openexecutive.knowledge.isolated import IsolatedError
    from openexecutive.knowledge.loader import ingest_file
    from openexecutive.knowledge.store import ChromaDBStore
    from openexecutive.orchestrator.mail_file_save import SaveFailed, _free_name

    name = PurePath(name).name
    suffix = PurePath(name).suffix.lower()
    if not name or name.startswith(".") or suffix not in ALLOWED_EXTENSIONS:
        raise SaveFailed(
            "Documents take only PDF, Word, Excel, CSV, Markdown and text files."
        )
    docs = get_settings().company_profile_path.parent / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    name = _free_name(name, {p.name.lower() for p in docs.iterdir()})
    # Staged next to the documents, so the move into place stays on one
    # filesystem; written there before it is indexed, so the index never
    # holds a document that isn't on disk.
    try:
        with tempfile.TemporaryDirectory(dir=docs, prefix=".saving-") as tmp:
            staged = Path(tmp) / f"file{suffix}"
            staged.write_bytes(item.data)
            await asyncio.to_thread(staged.replace, docs / name)
    except OSError as exc:
        raise SaveFailed("The file couldn't be written to Documents.") from exc
    try:
        await ingest_file(
            path=docs / name,
            store=ChromaDBStore(persist_directory=get_settings().vector_store_path),
            domain="general",
            collection=ChromaDBStore.COMPANY_COLLECTION,
            source_name=name,
            busy_raises=True,
        )
    except IsolatedError as exc:
        (docs / name).unlink(missing_ok=True)
        raise SaveFailed("The file couldn't be read.") from exc
    return {"name": name, "link": "", "where": "Documents on the Knowledge page"}


async def handle_save_file(tool_input: dict[str, Any]) -> str:
    from openexecutive.audit import log_event
    from openexecutive.orchestrator import file_refs
    from openexecutive.orchestrator import mail_file_save as save
    from openexecutive.orchestrator.mcp_gateway import ensure_active_gateway

    folder = str(tool_input.get("folder") or "").strip()
    if not folder:
        return _error("Pass `folder`: the folder's id from your Drive tools, or root.")
    try:
        files = await file_refs.resolve([tool_input.get("file")])
    except file_refs.RefError as exc:
        return _error(str(exc))
    if not files.items:
        return _error("That file is too large to save.")
    # After resolving: a mail file, or one made from mail, marks the turn
    # as a mail read, which this then sees.
    if _read_something_outside() and not asked_for_file_work():
        return _error(
            "Save a file only when they ask for it in their own words; they haven't "
            "asked for file work in this message. Do not retry until they do.",
            status="not_asked",
        )
    item = files.items[0]
    name = _named(tool_input.get("name"), item.name)
    gateway = await ensure_active_gateway()
    provider = save.storage_for(gateway)
    if folder.lower() == DOCUMENTS:
        provider = DOCUMENTS
    elif provider is None:
        return json.dumps({
            "status": "no_storage",
            "detail": (
                "Neither Google Drive nor OneDrive is connected, so that folder can't be "
                "reached. Pass folder documents to save it to the company's Documents."
            ),
        })
    try:
        async with save._lock():
            if provider == DOCUMENTS:
                saved = await _documents(item, name)
            elif provider == save.GOOGLE:
                saved = await _google(gateway, item, folder, name)
            else:
                saved = await _microsoft(gateway, item, folder, name)
    except save.NoStorage:
        return json.dumps({"status": "no_storage", "detail": "Your OneDrive couldn't be reached."})
    except save.SaveFailed as exc:
        return json.dumps({"status": "failed", "detail": f"Couldn't save {name}. {exc.detail}"})
    try:
        log_event(
            "file_saved",
            f"Saved a file to {saved['where']}",
            actor="executive",
            details={"source": item.ref.split(":", 1)[0], "bytes": len(item.data), "provider": provider},
        )
    except Exception:
        logger.warning("save_file: couldn't audit the save", exc_info=True)
    return json.dumps({
        "status": "saved",
        "name": saved.get("name", name),
        "where": saved["where"],
        "link": saved["link"],
        "detail": "Saved. Give them the link." if saved["link"] else f"Saved to {saved['where']}.",
    })


FILE_TOOL_HANDLERS: dict[str, Any] = {SAVE_FILE: handle_save_file}
