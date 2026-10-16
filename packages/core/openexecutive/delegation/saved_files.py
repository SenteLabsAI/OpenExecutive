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
- **The folder is found or made once** per storage and Executive account
  (``remembered_folder`` / ``remember_folder``).
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


def hidden_file_ids(viewer: int | None, *, db_path: Path | None = None) -> frozenset[str]:
    """The ids of every recorded file that is not ``viewer``'s (all of them
    when ``viewer`` is None). Read-only: a database that doesn't exist yet,
    or has no such table, has no saved files, and nothing is created here
    (the gateway asks on every Drive or OneDrive call). Any other failure
    raises, so the caller can fail closed."""
    path = _db(db_path)
    if not path.exists():
        return frozenset()
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        try:
            rows = conn.execute(
                f"SELECT file_id FROM {SAVED_FILES_TABLE} WHERE ? IS NULL OR person_id != ?",  # noqa: S608
                (viewer, viewer),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return frozenset()
            raise
    finally:
        conn.close()
    return frozenset(str(r[0]) for r in rows if r[0])


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
    """``(folder_id, drive_id)`` of the folder saved files go in, or None."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            f"SELECT folder_id, drive_id FROM {SAVED_FOLDER_TABLE} WHERE provider = ? AND account = ?",  # noqa: S608
            (provider, account.strip().lower()),
        ).fetchone()
    finally:
        conn.close()
    return (str(row[0]), str(row[1])) if row else None


def remember_folder(
    provider: str, account: str, folder_id: str, drive_id: str = "", *, db_path: Path | None = None
) -> None:
    conn = _connect(db_path)
    try:
        conn.execute(
            f"INSERT INTO {SAVED_FOLDER_TABLE} (provider, account, folder_id, drive_id, created_at) "  # noqa: S608
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(provider, account) DO UPDATE SET "
            "folder_id = excluded.folder_id, drive_id = excluded.drive_id, created_at = excluded.created_at",
            (provider, account.strip().lower(), folder_id, drive_id, datetime.now(UTC).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def forget_folder(provider: str, account: str, *, db_path: Path | None = None) -> None:
    """Drop the remembered folder (it was deleted or can't be written to), so
    the next save finds or makes it again."""
    conn = _connect(db_path)
    try:
        conn.execute(
            f"DELETE FROM {SAVED_FOLDER_TABLE} WHERE provider = ? AND account = ?",  # noqa: S608
            (provider, account.strip().lower()),
        )
        conn.commit()
    finally:
        conn.close()
