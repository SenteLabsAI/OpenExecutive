"""The files of one conversation, kept so a later message can still use them.

A file sent in the chat is bound for its own turn only (``workflows.turn_files``),
and a Python job's results are only download links. So "split the PDF I sent
earlier and save both halves" used to need the file sent again. Both are kept
here, by name, for the conversation they belong to: ``file:<name>`` in any
file tool finds them (``orchestrator.file_refs``).

- **Whose:** one folder per conversation, named by a hash of its id, so a file
  is only ever found from the conversation it was sent or made in.
- **How long:** ``KEEP_DAYS``, and at most ``KEEP_TOTAL_BYTES`` in all, oldest
  conversations' files going first.
- **From mail:** a file made from a mail attachment (a job run on one) is
  marked (``from_mail``), so a later turn that reads or uses it is held as a
  mail read, as the turn that opened the attachment was.
- **Names:** the safe name the turn's uploads already use
  (``turn_files.safe_name``); a later file of the same name replaces the
  earlier one, as the newest is the one meant.

Nothing here is shown to anyone: files leave only through a tool that has its
own checks (a draft in the speaker's own mailbox, a save to the company's own
Drive, a sandboxed Python job).
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

KEEP_DAYS = 7
KEEP_TOTAL_BYTES = 500 * 2**20
MAX_FILE_BYTES = 25 * 2**20
# Names of the conversation's files made from mail; a leading dot keeps it
# out of every file name (``turn_files.safe_name`` strips one).
_MAIL_MARKS = ".from_mail.json"
# How often a keep sweeps every conversation's files (``prune`` walks them all).
PRUNE_EVERY_S = 600
_prune_lock = threading.Lock()
_last_prune = 0.0


def root() -> Path:
    from openexecutive.config import get_settings

    return get_settings().company_profile_path.parent / "conversation_files"


def _folder(session_id: str) -> Path:
    digest = hashlib.sha256(session_id.encode()).hexdigest()[:32]
    return root() / digest


def keep(session_id: str | None, files: dict[str, bytes], *, from_mail: bool = False) -> list[str]:
    """Keep ``files`` (safe name -> bytes) for the conversation; returns the
    names kept. ``from_mail``: they were made from a mail attachment. Best
    effort: a file that can't be written is left out."""
    from openexecutive.workflows.turn_files import safe_name

    if not session_id or not files:
        return []
    folder = _folder(session_id)
    kept: list[str] = []
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.warning("conversation_files: couldn't make the folder", exc_info=True)
        return []
    for raw, data in files.items():
        name = safe_name(raw)
        if len(data) > MAX_FILE_BYTES:
            continue
        try:
            (folder / name).write_bytes(data)
            kept.append(name)
        except OSError:
            logger.warning("conversation_files: couldn't keep a file", exc_info=True)
    # Best effort to the end: a caller holds a turn's locks around this, so
    # nothing here may raise into it (a full disk, a read-only folder).
    try:
        _mark(folder, kept, from_mail)
        folder.touch()
        if time.monotonic() - _last_prune >= PRUNE_EVERY_S:
            prune()
    except Exception:
        logger.warning("conversation_files: couldn't finish keeping files", exc_info=True)
    return kept


def _marks(folder: Path) -> set[str]:
    try:
        loaded = json.loads((folder / _MAIL_MARKS).read_text())
    except (OSError, ValueError):
        return set()
    return {str(n) for n in loaded} if isinstance(loaded, list) else set()


def _mark(folder: Path, kept: list[str], from_mail: bool) -> None:
    marks = _marks(folder)
    updated = marks | set(kept) if from_mail else marks - set(kept)
    if updated == marks:
        return
    try:
        (folder / _MAIL_MARKS).write_text(json.dumps(sorted(updated)))
    except OSError:
        logger.warning("conversation_files: couldn't mark files from mail", exc_info=True)


def from_mail(session_id: str | None, name: str) -> bool:
    """Whether the conversation's file of that name was made from mail."""
    from openexecutive.workflows.turn_files import safe_name

    return bool(session_id) and safe_name(name) in _marks(_folder(str(session_id)))


def get(session_id: str | None, name: str) -> bytes | None:
    """The conversation's file of that name, or None."""
    from openexecutive.workflows.turn_files import safe_name

    if not session_id or not name:
        return None
    folder = _folder(session_id).resolve()
    path = (folder / safe_name(name)).resolve()
    if not path.is_relative_to(folder) or not path.is_file():
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def forget(session_id: str | None) -> None:
    """Remove the conversation's files (it was deleted), so nobody who later
    takes its id finds them."""
    if session_id:
        shutil.rmtree(_folder(session_id), ignore_errors=True)


def names(session_id: str | None) -> list[str]:
    if not session_id:
        return []
    folder = _folder(session_id)
    if not folder.is_dir():
        return []
    return sorted(p.name for p in folder.iterdir() if p.is_file() and p.name != _MAIL_MARKS)


def prune() -> None:
    """Drop conversations' files past ``KEEP_DAYS``, then the oldest while
    all of them pass ``KEEP_TOTAL_BYTES``."""
    global _last_prune
    with _prune_lock:
        _last_prune = time.monotonic()
        base = root()
        if not base.is_dir():
            return
        cutoff = time.time() - KEEP_DAYS * 86400
        folders: list[tuple[float, int, Path]] = []
        try:
            for child in base.iterdir():
                if not child.is_dir():
                    continue
                mtime = child.stat().st_mtime
                if mtime < cutoff:
                    shutil.rmtree(child, ignore_errors=True)
                    continue
                size = sum(f.stat().st_size for f in child.iterdir() if f.is_file())
                folders.append((mtime, size, child))
        except FileNotFoundError:
            return
        total = sum(size for _, size, _ in folders)
        for _, size, child in sorted(folders, key=lambda f: f[0]):
            if total <= KEEP_TOTAL_BYTES:
                break
            shutil.rmtree(child, ignore_errors=True)
            total -= size
