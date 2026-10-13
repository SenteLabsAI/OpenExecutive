"""Act as me: the AI disclosure line on replies sent as a person.

Mail the Executive sends as itself carries ``orchestrator.disclosure``'s
line. Replies the inbox watcher writes in a person's own voice go out as
*them*, so each person decides for their own mailbox, with two switches:

- **Handle it** (on unless the install says otherwise): a reply Handle it
  for me will send on its own, which nobody reads before it goes, ends with
  ``LINE``.
- **Approved sends** (off unless the install says otherwise): a draft the
  person reads and sends with Send ends with it too.

Both start from the install's ``AI_DISCLOSURE_HANDLE_IT`` /
``AI_DISCLOSURE_APPROVED_SENDS`` until the person changes theirs (one row in
``delegation_disclosure``, NULL meaning "as the install says"). The line is
written into the reply when it is drafted (``sign``), so the card, the
mailbox draft and what is sent are the same words, and Send's checks see
them; a switch changed later applies to the next reply. ``AI_DISCLOSURE``
off turns both off. Unreadable settings fall back to the install's.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from openexecutive.delegation.schema import DISCLOSURE_TABLE, ensure_schema

logger = logging.getLogger(__name__)

LINE = "Drafted and sent by my AI assistant."


@dataclass(frozen=True)
class Disclosure:
    person_id: int
    # Effective: the person's own choice, else the install's.
    handle_it: bool
    approved_sends: bool


def _defaults() -> tuple[bool, bool]:
    from openexecutive.config import get_settings

    settings = get_settings()
    return bool(settings.ai_disclosure_handle_it), bool(settings.ai_disclosure_approved_sends)


def _db(db_path: Path | None) -> Path:
    if db_path is not None:
        return db_path
    from openexecutive.memory import episodic

    return Path(episodic.DB_PATH)


def _connect(db_path: Path | None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db(db_path)))
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def get(person_id: int, *, db_path: Path | None = None) -> Disclosure:
    """``person_id``'s two switches. Never raises."""
    handle_it, approved = _defaults()
    try:
        conn = _connect(db_path)
        try:
            row = conn.execute(
                f"SELECT handle_it, approved_sends FROM {DISCLOSURE_TABLE} WHERE person_id = ?",  # noqa: S608
                (person_id,),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        logger.warning("delegation.disclosure: couldn't read the switches — using the install's", exc_info=True)
        row = None
    if row is not None:
        if row["handle_it"] is not None:
            handle_it = bool(row["handle_it"])
        if row["approved_sends"] is not None:
            approved = bool(row["approved_sends"])
    return Disclosure(person_id=person_id, handle_it=handle_it, approved_sends=approved)


def set_(
    person_id: int,
    *,
    handle_it: bool | None = None,
    approved_sends: bool | None = None,
    updated_by: str,
    db_path: Path | None = None,
) -> Disclosure:
    """Change ``person_id``'s own switches (callers authorize first); None
    leaves one as it is."""
    current = get(person_id, db_path=db_path)
    new_handle_it = current.handle_it if handle_it is None else handle_it
    new_approved = current.approved_sends if approved_sends is None else approved_sends
    conn = _connect(db_path)
    try:
        conn.execute(
            f"INSERT INTO {DISCLOSURE_TABLE} (person_id, handle_it, approved_sends, updated_at, updated_by) "  # noqa: S608
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(person_id) DO UPDATE SET handle_it = excluded.handle_it, "
            "approved_sends = excluded.approved_sends, updated_at = excluded.updated_at, "
            "updated_by = excluded.updated_by",
            (person_id, int(new_handle_it), int(new_approved), datetime.now(UTC).isoformat(), updated_by),
        )
        conn.commit()
    finally:
        conn.close()
    return get(person_id, db_path=db_path)


def wanted(person_id: int, *, on_its_own: bool) -> bool:
    """Whether a reply drafted for ``person_id`` gets the line: one Handle it
    will send on its own, or one waiting for them to send."""
    from openexecutive.orchestrator.disclosure import enabled

    if not enabled():
        return False
    switches = get(person_id)
    return switches.handle_it if on_its_own else switches.approved_sends


def sign(person_id: int, body: str, *, on_its_own: bool) -> str:
    """``body`` as the reply should be drafted: with ``LINE`` at the end
    when ``person_id``'s switch for this kind of send is on."""
    if not body.strip() or LINE in body or not wanted(person_id, on_its_own=on_its_own):
        return body
    return f"{body.rstrip()}\n\n{LINE}"
