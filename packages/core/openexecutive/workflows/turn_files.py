"""The files attached to the chat message being answered, as sent.

The web chat extracts an attachment's text into the message, which is all the
model reads. A Python job needs the file itself (a PDF to split, a workbook to
rework), and having the model retype the extracted text into the job costs
output tokens, takes minutes for a large file and loses everything that isn't
text. So ``POST /chat/upload`` binds the turn's uploads here for the whole
turn, and ``run_python_job`` takes them by name (``attachment_files``).

The binding lasts for the turn that carried the files and no later one, as
the extracted text does (api/routes/chat.py), and holds nothing on disk.
"""
from __future__ import annotations

import contextlib
import re
from collections.abc import Iterator
from contextvars import ContextVar

_files: ContextVar[dict[str, bytes] | None] = ContextVar("turn_files", default=None)

_UNSAFE = re.compile(r"[^A-Za-z0-9 ._()-]")


def safe_name(filename: str) -> str:
    """A plain file name for ``filename``: its last path part, with anything a
    job's file names don't allow replaced, so the name the model saw on the
    attachment finds the file."""
    base = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    cleaned = _UNSAFE.sub("_", base).lstrip("._- ")[:120]
    return cleaned or "attachment"


def collect(uploads: list[tuple[str, bytes]]) -> dict[str, bytes]:
    """The turn's files by safe name; a repeated name gets " (2)", " (3)"."""
    files: dict[str, bytes] = {}
    for filename, data in uploads:
        name = safe_name(filename)
        stem, dot, ext = name.rpartition(".")
        if not dot:
            stem, ext = name, ""
        n = 2
        while name in files:
            name = f"{stem} ({n}){dot}{ext}"
            n += 1
        files[name] = data
    return files


@contextlib.contextmanager
def bind(files: dict[str, bytes] | None) -> Iterator[None]:
    # Save/restore rather than Token.reset(), as set_session and set_turn do:
    # the SSE route can exit this in a different Context than it entered.
    prior = _files.get()
    _files.set(dict(files) if files else None)
    try:
        yield
    finally:
        _files.set(prior)


def get(name: str) -> bytes | None:
    """The attached file of that name (or the name it was sent under)."""
    files = _files.get()
    if not files:
        return None
    if name in files:
        return files[name]
    return files.get(safe_name(name))


def names() -> list[str]:
    return sorted(_files.get() or {})
