"""Act as me: the files ``save_my_email_attachment`` copied from someone's own
mail into the Executive's Drive or OneDrive, and whose each one is.

The copy lands in the Executive's own storage (``orchestrator.mail_file_save``)
and is shared with that person alone, so anyone else's turn could otherwise
find it there with the Executive's ordinary Drive or OneDrive tools. This
table is what the gateway reads to stop that (``mcp_gateway`` hides a recorded
file from every turn whose speaker is not its owner: ``hidden_file_ids``).

- **Where, never what.** A row is the file's id in that storage, its name and
  when it was saved: never its content, the mail it came from or who sent it.
- **Kept while the file may exist.** Rows are not pruned and are not deleted
  when Act as me is turned off: the copy stays in the Executive's storage
  (only its owner or an operator deletes it there), and the row is what keeps
  it out of other people's turns.
- **The folder is made by the save itself**, never adopted by its name (a
  folder anyone made and called "From your email" could already be shared
  with them), and its id is kept (``remember_folder``). Every folder ever
  made stays listed (``folder_ids``): the gateway refuses any model call that
  names one and hides it from every result, so nobody can share, move or
  list it and so reach the files inside.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openexecutive.delegation.schema import SAVED_FILES_TABLE, SAVED_FOLDER_TABLE, ensure_schema

logger = logging.getLogger(__name__)

_NAME_CHARS = 200
# The folder the copies go in, in both stores.
FOLDER_NAME = "From your email"
# Copies whose row couldn't be written (``quarantine``): hidden from every
# turn, the owner's included, for the life of the process.
_UNRECORDED: set[str] = set()


def _db(db_path: Path | None) -> Path:
    if db_path is not None:
        return db_path
    from openexecutive.memory import episodic

    return Path(episodic.DB_PATH)


def _connect(db_path: Path | None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db(db_path)))
    ensure_schema(conn)
    return conn


def record(
    person_id: int,
    provider: str,
    file_id: str,
    *,
    name: str,
    now: datetime | None = None,
    db_path: Path | None = None,
) -> None:
    """Note that ``file_id`` in ``provider``'s storage is ``person_id``'s.
    A file already recorded keeps its first owner: nobody can claim
    another's."""
    from openexecutive.delegation.ghostwriter import one_line

    conn = _connect(db_path)
    try:
        conn.execute(
            f"INSERT INTO {SAVED_FILES_TABLE} (person_id, provider, file_id, name, saved_at) "  # noqa: S608
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(provider, file_id) DO NOTHING",
            (
                person_id,
                provider,
                file_id,
                one_line(name, _NAME_CHARS),
                (now or datetime.now(UTC)).astimezone(UTC).isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def owner_of(provider: str, file_id: str, *, db_path: Path | None = None) -> int | None:
    """Whose recorded file ``file_id`` is, or None when it isn't one."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            f"SELECT person_id FROM {SAVED_FILES_TABLE} WHERE provider = ? AND file_id = ?",  # noqa: S608
            (provider, file_id),
        ).fetchone()
    finally:
        conn.close()
    return int(row[0]) if row else None


def names(provider: str, *, db_path: Path | None = None) -> set[str]:
    """Every recorded file name in ``provider``'s storage, lowercased: a new
    copy's name must not take one, since a listing on the speaker's turn
    leaves other people's files out (an OneDrive upload overwrites)."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT name FROM {SAVED_FILES_TABLE} WHERE provider = ?",  # noqa: S608
            (provider,),
        ).fetchall()
    finally:
        conn.close()
    return {str(r[0]).lower() for r in rows if r[0]}


def quarantine(file_id: str) -> None:
    """Hide ``file_id`` from every turn until the process ends: a copy made
    whose row couldn't be written (``record`` failed)."""
    _UNRECORDED.add(file_id)


def protected(viewer: int | None, *, db_path: Path | None = None) -> tuple[frozenset[str], frozenset[str]]:
    """``(file ids not viewer's, every folder id)``: the recorded files that
    are not ``viewer``'s (all of them when ``viewer`` is None, plus any
    quarantined copy), and every folder the saves made. Read-only: a database
    that doesn't exist yet, or lacks the tables, has none, and nothing is
    created here (the gateway asks on every Workspace call). Any other
    failure raises, so the caller can fail closed."""
    path = _db(db_path)
    if not path.exists():
        return frozenset(_UNRECORDED), frozenset()
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)

    def rows(sql: str, args: tuple[Any, ...] = ()) -> list[Any]:
        try:
            return conn.execute(sql, args).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return []
            raise

    try:
        files = rows(
            f"SELECT file_id FROM {SAVED_FILES_TABLE} WHERE ? IS NULL OR person_id != ?",  # noqa: S608
            (viewer, viewer),
        )
        folders = rows(f"SELECT folder_id FROM {SAVED_FOLDER_TABLE}")  # noqa: S608
    finally:
        conn.close()
    hidden = {str(r[0]) for r in files if r[0]} | _UNRECORDED
    return frozenset(hidden), frozenset(str(r[0]) for r in folders if r[0])


def hidden_file_ids(viewer: int | None, *, db_path: Path | None = None) -> frozenset[str]:
    """The file ids ``viewer`` may not see (``protected``)."""
    return protected(viewer, db_path=db_path)[0]


def viewer_person_id(session: Any = None) -> int | None:
    """Who may see their own saved files on the current turn: the speaker
    Act as me was offered to (a verified speaker, in a conversation private
    to them), else nobody — a shared chat, a workflow, the email poller or
    any turn that can't tell who is speaking sees none of them."""
    from openexecutive.delegation.settings import turn_delegation

    if session is None:
        from openexecutive.orchestrator.schedule_tools import current_session

        session = current_session.get()
    pinned = turn_delegation(session)
    if pinned is None or not pinned.offered or pinned.person_id is None:
        return None
    return int(pinned.person_id)


def remembered_folder(
    provider: str, account: str, *, db_path: Path | None = None
) -> tuple[str, str] | None:
    """``(folder_id, drive_id)`` of the folder saved files go in now, or None."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            f"SELECT folder_id, drive_id FROM {SAVED_FOLDER_TABLE} "  # noqa: S608
            "WHERE provider = ? AND account = ? AND current = 1 ORDER BY created_at DESC LIMIT 1",
            (provider, account.strip().lower()),
        ).fetchone()
    finally:
        conn.close()
    return (str(row[0]), str(row[1])) if row else None


def remember_folder(
    provider: str, account: str, folder_id: str, drive_id: str = "", *, db_path: Path | None = None
) -> None:
    """Keep the folder a save just made as the one in use (and protected)."""
    conn = _connect(db_path)
    try:
        conn.execute(
            f"UPDATE {SAVED_FOLDER_TABLE} SET current = 0 WHERE provider = ? AND account = ?",  # noqa: S608
            (provider, account.strip().lower()),
        )
        conn.execute(
            f"INSERT INTO {SAVED_FOLDER_TABLE} (provider, account, folder_id, drive_id, created_at, current) "  # noqa: S608
            "VALUES (?, ?, ?, ?, ?, 1) ON CONFLICT(provider, folder_id) DO UPDATE SET current = 1",
            (provider, account.strip().lower(), folder_id, drive_id, datetime.now(UTC).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def forget_folder(provider: str, account: str, *, db_path: Path | None = None) -> None:
    """Stop using the current folder (it was deleted or can't be written to),
    so the next save makes a new one. Its row stays: still protected."""
    conn = _connect(db_path)
    try:
        conn.execute(
            f"UPDATE {SAVED_FOLDER_TABLE} SET current = 0 WHERE provider = ? AND account = ?",  # noqa: S608
            (provider, account.strip().lower()),
        )
        conn.commit()
    finally:
        conn.close()
