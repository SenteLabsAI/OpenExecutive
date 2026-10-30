"""One file, any tool: files named by a short handle instead of by where
they came from.

Every file tool used to take files from one place only: a draft took Drive
files, a Python job the files attached to this message, a save only an email's
attachment, and a job's results went nowhere but a download link. So "split
the PDF Priya emailed and attach both halves to my reply" was refused at
every step. Here a file is named by a handle, and every file tool
(``ghostwrite_email``'s ``files``, ``run_python_job``'s ``files``,
``save_file``) takes any of them:

- ``mail:<message_id>#<n>``: the n-th attachment of a message in the
  speaker's own mailbox, as ``read_my_email`` lists it. Read with their
  Act as me mailbox, so it opens their mailbox like any mail read
  (``delegation_tools._open_mailbox``: the turn counts as having read mail).
- ``drive:<file_id>``: a Google Drive file, downloaded as the Executive's own
  account (``drive_attach.fetch``). The principal's turns only, as for
  ``drive_files``: the Executive's Drive holds much a team member can't open.
- ``file:<name>``: a file sent in this conversation or made by a Python job
  in it (``workflows.turn_files``, ``orchestrator.conversation_files``).

The bytes move in code only: the model names the file and never sees or
retypes what it holds, so naming one is not an outside read. Whatever reads
a file's text (``read_document``, a job's printout) is the reader's to check.
"""
from __future__ import annotations

import logging
import mimetypes
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

MAX_FILES = 15
MAX_TOTAL_BYTES = 25 * 2**20

REF_HELP = (
    "A file is named by its handle: mail:<message_id>#<n> for the n-th "
    "attachment of a message read_my_email listed, drive:<file_id> for a Google "
    "Drive file, file:<name> for a file sent in this conversation or made by a "
    "Python job in it (run_python_job gives each result's handle)."
)

_MAIL_RE = re.compile(r"^mail:(?P<id>[^#\s]{1,200})#(?P<n>\d{1,3})$")
_DRIVE_RE = re.compile(r"^drive:(?P<id>[A-Za-z0-9_-]{10,200})$")
_FILE_RE = re.compile(r"^file:(?P<name>.{1,200})$", re.DOTALL)


class RefError(Exception):
    """A handle that can't be used: the message says why, in plain words."""


@dataclass(frozen=True)
class Ref:
    kind: str  # "mail", "drive" or "file"
    value: str
    index: int = 0

    def __str__(self) -> str:
        if self.kind == "mail":
            return f"mail:{self.value}#{self.index}"
        return f"{self.kind}:{self.value}"


@dataclass(frozen=True)
class FileItem:
    name: str
    mime: str
    data: bytes
    ref: str


@dataclass
class Files:
    items: list[FileItem] = field(default_factory=list)
    # Left off for the caps (MAX_FILES, MAX_TOTAL_BYTES).
    skipped: int = 0


def parse(raw: Any) -> Ref:
    """One handle, or ``RefError``."""
    text = str(raw or "").strip()
    if (m := _MAIL_RE.fullmatch(text)) is not None:
        index = int(m.group("n"))
        if index < 1:
            raise RefError(f"{text[:80]!r}: attachments count from 1")
        return Ref("mail", m.group("id"), index)
    if (m := _DRIVE_RE.fullmatch(text)) is not None:
        return Ref("drive", m.group("id"))
    if (m := _FILE_RE.fullmatch(text)) is not None and "\n" not in m.group("name"):
        return Ref("file", m.group("name").strip())
    raise RefError(f"{text[:80]!r} isn't a file handle. {REF_HELP}")


def parse_all(raw: Any) -> list[Ref]:
    """The handles of a ``files`` list, deduplicated, or ``RefError``."""
    items = raw if isinstance(raw, list) else [raw]
    refs = [parse(i) for i in items if str(i or "").strip()]
    if not refs:
        raise RefError(f"`files` must list file handles. {REF_HELP}")
    unique = list(dict.fromkeys(refs))
    if len(unique) > MAX_FILES:
        raise RefError(f"At most {MAX_FILES} files at once.")
    return unique


def guess_mime(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def _session_id() -> str | None:
    from openexecutive.orchestrator.schedule_tools import current_session

    session = current_session.get()
    session_id = getattr(session, "session_id", None)
    return str(session_id) if session_id else None


def _principal() -> bool:
    from openexecutive.orchestrator.people_tools import is_principal_on_verified_surface
    from openexecutive.orchestrator.schedule_tools import current_session

    return is_principal_on_verified_surface(current_session.get())


def conversation_file(name: str) -> tuple[str, bytes] | None:
    """A file of this conversation: sent with this message, kept from an
    earlier one, or made by a job in it."""
    from openexecutive.orchestrator import conversation_files
    from openexecutive.workflows import turn_files

    data = turn_files.get(name)
    if data is not None:
        return turn_files.safe_name(name), data
    data = conversation_files.get(_session_id(), name)
    if data is not None:
        if conversation_files.from_mail(_session_id(), name):
            _hold_as_mail_read()
        return turn_files.safe_name(name), data
    return None


def _hold_as_mail_read() -> None:
    """A file made from mail is being used: hold the turn as the one that
    opened the attachment was (``_mail_writer``)."""
    from openexecutive.delegation.settings import turn_delegation
    from openexecutive.orchestrator.schedule_tools import current_session

    pinned = turn_delegation(current_session.get())
    if pinned is not None:
        pinned.read_mail = pinned.touched_mail = pinned.unchecked_mail = True


def turn_used_mail_files() -> bool:
    """Whether this turn read mail or used a mail file, so what it makes may
    carry the mail and counts as made from it."""
    from openexecutive.delegation.settings import turn_delegation
    from openexecutive.orchestrator.schedule_tools import current_session

    pinned = turn_delegation(current_session.get())
    return pinned is not None and (pinned.unchecked_mail is True or pinned.read_mail is True)


def available_names() -> list[str]:
    """``file:`` names this conversation has, for a refusal to list."""
    from openexecutive.orchestrator import conversation_files
    from openexecutive.workflows import turn_files

    return sorted({*turn_files.names(), *conversation_files.names(_session_id())})


async def _mail_writer() -> Any:
    """The speaker's own mailbox, opened as a mail read, or ``RefError``."""
    from openexecutive.orchestrator.delegation_tools import _open_mailbox, _writer

    writer = _writer("mail attachments")
    if isinstance(writer, str):
        raise RefError(
            "Their own mailbox isn't open to me on this turn, so mail: files can't be used"
        )
    refused = await _open_mailbox(writer)
    if refused is not None:
        raise RefError("Their mailbox couldn't be opened just now")
    # What the caller makes of the file (a job's printout, a document's
    # text) never passes the mail check, so the turn stays held.
    writer.pinned.unchecked_mail = True
    return writer


async def _mail_file(writer: Any, ref: Ref) -> FileItem:
    from openexecutive.delegation.gmail import GmailError
    from openexecutive.orchestrator.mail_file_save import clean_mime, clean_name
    from openexecutive.orchestrator.mail_read_tools import MAX_ATTACHMENT_BYTES, _id_refusal

    if _id_refusal(writer, ref.value, "message") is not None:
        raise RefError(f"{ref}: that message_id isn't one from their mailbox")
    try:
        listed = await writer.mailbox.list_attachments(ref.value)
        if not 1 <= ref.index <= len(listed):
            raise RefError(f"{ref}: that message has {len(listed)} attachment(s)")
        if listed[ref.index - 1].size > MAX_ATTACHMENT_BYTES:
            raise RefError(f"{ref}: the file is over {MAX_ATTACHMENT_BYTES // 2**20} MB")
        meta, data = await writer.mailbox.attachment_bytes(ref.value, ref.index)
    except GmailError as exc:
        raise RefError(f"{ref}: couldn't read it from their mailbox") from exc
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise RefError(f"{ref}: the file is over {MAX_ATTACHMENT_BYTES // 2**20} MB")
    return FileItem(clean_name(meta.name, ref.index), clean_mime(meta.mime_type), data, str(ref))


async def resolve(
    raw: Any, *, writer: Any = None, allow_drive: bool | None = None, max_bytes: int = MAX_TOTAL_BYTES,
) -> Files:
    """The files the handles in ``raw`` name, in order, past the caps left
    off and counted; else ``RefError``. ``writer`` is the speaker's mailbox
    when the caller has already opened it (``ghostwrite_email``);
    ``allow_drive`` defaults to whether the principal is speaking;
    ``max_bytes`` caps them all together."""
    from openexecutive.delegation.saved_files import viewer_person_id
    from openexecutive.orchestrator.drive_attach import AttachError, fetch

    refs = parse_all(raw)
    if allow_drive is None:
        allow_drive = any(r.kind == "drive" for r in refs) and _principal()
    if not allow_drive and any(r.kind == "drive" for r in refs):
        raise RefError("Only the owner's turns can use drive: files")
    out = Files()
    total = 0

    def add(item: FileItem) -> None:
        nonlocal total
        if len(out.items) >= MAX_FILES or total + len(item.data) > max_bytes:
            out.skipped += 1
            return
        total += len(item.data)
        out.items.append(item)

    for ref in refs:
        if ref.kind == "file":
            found = conversation_file(ref.value)
            if found is None:
                have = available_names()
                raise RefError(
                    f"No file named {ref.value[:80]!r} in this conversation"
                    + (f"; it has: {', '.join(have[:20])}" if have else "")
                )
            add(FileItem(found[0], guess_mime(found[0]), found[1], str(ref)))
        elif ref.kind == "mail":
            if writer is None:
                writer = await _mail_writer()
            add(await _mail_file(writer, ref))
        else:
            try:
                fetched = await fetch([ref.value], viewer_person_id())
            except AttachError as exc:
                raise RefError(f"{ref}: {exc}") from exc
            out.skipped += fetched.skipped
            for name, mime, data in fetched.files:
                add(FileItem(name, mime, data, str(ref)))
    return out
