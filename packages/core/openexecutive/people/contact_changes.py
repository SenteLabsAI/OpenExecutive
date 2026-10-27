"""What the Executive changed in the principal's contacts, and how to undo it.

``people.contact_keeper`` adds a contact the principal writes to or meets,
and fills in a name or role from their own mail. Every change it makes, and
every contact added from a chat, is one row here (``contact_changes``) so the
principal sees it ("Changes I made" on Contacts, a line on the person's
page, one line in Done for you) and can take it back:

- **Undo an add** archives the contact and blocks the address, so it is
  never added again.
- **Undo an update** puts the old value back, when nobody changed it since,
  and blocks that value, so the same change is not made again.

The switch (``Keep my contacts up to date``) is ``on`` (make the change,
then say so), ``ask`` (keep it as a suggestion with Add / Not now) or
``off``. It is one row in ``contact_keeper_state``; absent means ``on``.

Contacts are the principal's alone: so is everything here (the routes answer
only the principal, and the audit rows are private and name-free).
"""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

logger = logging.getLogger(__name__)

Mode = Literal["on", "ask", "off"]
MODES: tuple[str, ...] = ("on", "ask", "off")
Action = Literal["add", "update"]
# What a change touched: the whole contact (an add), or one field of it.
Field = Literal[
    "contact", "full_name", "role", "email_alias",
    # Contact details (``Person.details``), one kind each.
    "phone", "mobile", "website", "linkedin", "social", "address",
]
DETAIL_FIELDS = frozenset({"phone", "mobile", "website", "linkedin", "social", "address"})
Status = Literal["applied", "suggested", "undone", "dismissed"]
# Why: the principal emailed them, met them, asked in chat, or a name or role
# read from the person's own mail.
Reason = Literal["emailed", "met", "asked", "from_line", "signature"]

# How far back the lists reach.
LIST_DAYS = 30

_SCHEMA = """
CREATE TABLE IF NOT EXISTS contact_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER,
    action TEXT NOT NULL,
    field TEXT NOT NULL,
    email TEXT NOT NULL DEFAULT '',
    full_name TEXT NOT NULL DEFAULT '',
    old_value TEXT NOT NULL DEFAULT '',
    new_value TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL,
    evidence INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    seen INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_contact_changes_person ON contact_changes(person_id);
CREATE INDEX IF NOT EXISTS idx_contact_changes_created ON contact_changes(created_at);
CREATE TABLE IF NOT EXISTS contact_keeper_blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL,
    field TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(email, field, value)
);
CREATE TABLE IF NOT EXISTS contact_keeper_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    mode TEXT NOT NULL DEFAULT 'on',
    last_run_at TEXT,
    status TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contact_keeper_reads (
    email TEXT PRIMARY KEY,
    read_at TEXT NOT NULL,
    message_id TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 0,
    checked_at TEXT
);
"""
# Columns added to contact_keeper_reads after it first shipped.
_READS_COLUMNS = (
    ("message_id", "TEXT NOT NULL DEFAULT ''"),
    ("version", "INTEGER NOT NULL DEFAULT 0"),
    ("checked_at", "TEXT"),
)


class ContactChange(BaseModel):
    id: int
    person_id: int | None
    action: Action
    field: Field
    email: str
    full_name: str
    old_value: str
    new_value: str
    reason: Reason
    evidence: int
    status: Status
    seen: bool
    created_at: str
    decided_at: str | None = None


class UndoRefused(ValueError):
    """The change can't be taken back or decided (gone, or already decided)."""


def _db(db_path: Path | None) -> Path:
    if db_path is not None:
        return db_path
    from openexecutive.people import store as people_store

    return people_store.DB_PATH


@contextmanager
def _conn(db_path: Path | None = None) -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(str(_db(db_path)))
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        have = {r["name"] for r in conn.execute("PRAGMA table_info(contact_keeper_reads)")}
        for column, decl in _READS_COLUMNS:
            if column not in have:
                conn.execute(f"ALTER TABLE contact_keeper_reads ADD COLUMN {column} {decl}")
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def norm(email: str | None) -> str:
    return (email or "").strip().lower()


def _row(row: sqlite3.Row) -> ContactChange:
    return ContactChange(
        id=row["id"], person_id=row["person_id"], action=row["action"], field=row["field"],
        email=row["email"], full_name=row["full_name"], old_value=row["old_value"],
        new_value=row["new_value"], reason=row["reason"], evidence=row["evidence"],
        status=row["status"], seen=bool(row["seen"]), created_at=row["created_at"],
        decided_at=row["decided_at"],
    )


# --------------------------------------------------------------------------- #
# The switch
# --------------------------------------------------------------------------- #


def get_mode(db_path: Path | None = None) -> Mode:
    """The switch. Never raises: an unreadable row reads as ``off``."""
    try:
        with _conn(db_path) as conn:
            row = conn.execute("SELECT mode FROM contact_keeper_state WHERE id = 1").fetchone()
    except Exception:
        logger.warning("contact_changes: couldn't read the switch — treating it as off", exc_info=True)
        return "off"
    mode = row["mode"] if row is not None else "on"
    return mode if mode in MODES else "off"  # type: ignore[return-value]


def set_mode(mode: str, db_path: Path | None = None) -> Mode:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {list(MODES)}")
    with _conn(db_path) as conn:
        conn.execute(
            "INSERT INTO contact_keeper_state (id, mode, updated_at) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET mode = excluded.mode, updated_at = excluded.updated_at",
            (mode, _now()),
        )
    return mode  # type: ignore[return-value]


def last_run(db_path: Path | None = None) -> tuple[datetime | None, str]:
    """When the keeper last looked, and how it went."""
    with _conn(db_path) as conn:
        row = conn.execute("SELECT last_run_at, status FROM contact_keeper_state WHERE id = 1").fetchone()
    if row is None or not row["last_run_at"]:
        return None, row["status"] if row is not None else ""
    try:
        return datetime.fromisoformat(row["last_run_at"]), row["status"]
    except ValueError:
        return None, row["status"]


def note_run(status: str, now: datetime, db_path: Path | None = None) -> None:
    with _conn(db_path) as conn:
        conn.execute(
            "INSERT INTO contact_keeper_state (id, mode, last_run_at, status, updated_at) "
            "VALUES (1, 'on', ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
            "last_run_at = excluded.last_run_at, status = excluded.status, updated_at = excluded.updated_at",
            (now.isoformat(), status[:40], _now()),
        )


# --------------------------------------------------------------------------- #
# Signature reads: again when the person sends new mail
# --------------------------------------------------------------------------- #


class SignatureRead(BaseModel):
    """The last signature read for an address: when, which message it read,
    under which ``version`` of the reader, and when its mail was last checked."""

    read_at: datetime | None = None
    message_id: str = ""
    version: int = 0
    checked_at: datetime | None = None


def _moment(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def last_read(email: str, db_path: Path | None = None) -> SignatureRead | None:
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT read_at, message_id, version, checked_at FROM contact_keeper_reads WHERE email = ?",
            (norm(email),),
        ).fetchone()
    if row is None:
        return None
    return SignatureRead(
        read_at=_moment(row["read_at"]), message_id=row["message_id"] or "",
        version=int(row["version"] or 0), checked_at=_moment(row["checked_at"]),
    )


def checked_times(db_path: Path | None = None) -> dict[str, datetime]:
    """When each address's mail was last checked for a new signature."""
    with _conn(db_path) as conn:
        rows = conn.execute("SELECT email, checked_at FROM contact_keeper_reads").fetchall()
    return {row["email"]: moment for row in rows if (moment := _moment(row["checked_at"])) is not None}


def read_recently(email: str, now: datetime, within: timedelta, db_path: Path | None = None) -> bool:
    found = last_read(email, db_path)
    return found is not None and found.read_at is not None and now - found.read_at < within


def note_read(
    email: str, now: datetime, *, message_id: str = "", version: int = 0, db_path: Path | None = None,
) -> None:
    with _conn(db_path) as conn:
        conn.execute(
            "INSERT INTO contact_keeper_reads (email, read_at, message_id, version, checked_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(email) DO UPDATE SET read_at = excluded.read_at, "
            "message_id = excluded.message_id, version = excluded.version, checked_at = excluded.checked_at",
            (norm(email), now.isoformat(), message_id, version, now.isoformat()),
        )


def note_checked(email: str, now: datetime, db_path: Path | None = None) -> None:
    """Their mail was looked at and nothing needed reading. An address never
    read gets a row with no read in it (``read_at`` older than any day)."""
    with _conn(db_path) as conn:
        conn.execute(
            "INSERT INTO contact_keeper_reads (email, read_at, checked_at) VALUES (?, '', ?) "
            "ON CONFLICT(email) DO UPDATE SET checked_at = excluded.checked_at",
            (norm(email), now.isoformat()),
        )


def reads_since(since: datetime, db_path: Path | None = None) -> int:
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM contact_keeper_reads WHERE read_at >= ?", (since.isoformat(),)
        ).fetchone()
    return int(row["n"])


# --------------------------------------------------------------------------- #
# Blocks: what Undo or Not now said never to do again
# --------------------------------------------------------------------------- #


def block(email: str, field: str, value: str = "", db_path: Path | None = None) -> None:
    with _conn(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO contact_keeper_blocks (email, field, value, created_at) VALUES (?, ?, ?, ?)",
            (norm(email), field, (value or "").strip().casefold(), _now()),
        )


def is_blocked(email: str, field: str, value: str = "", db_path: Path | None = None) -> bool:
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM contact_keeper_blocks WHERE email = ? AND field = ? AND value = ?",
            (norm(email), field, (value or "").strip().casefold()),
        ).fetchone()
    return row is not None


# --------------------------------------------------------------------------- #
# Changes
# --------------------------------------------------------------------------- #


def record(
    *,
    action: Action,
    field: Field,
    reason: Reason,
    status: Status,
    person_id: int | None = None,
    email: str = "",
    full_name: str = "",
    old_value: str = "",
    new_value: str = "",
    evidence: int = 0,
    db_path: Path | None = None,
) -> int:
    with _conn(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO contact_changes (person_id, action, field, email, full_name, old_value, new_value,"
            " reason, evidence, status, seen, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (
                person_id, action, field, norm(email), full_name[:200], old_value[:200], new_value[:200],
                reason, int(evidence), status, _now(),
            ),
        )
        return int(cur.lastrowid or 0)


def get_change(change_id: int, db_path: Path | None = None) -> ContactChange | None:
    with _conn(db_path) as conn:
        row = conn.execute("SELECT * FROM contact_changes WHERE id = ?", (change_id,)).fetchone()
    return _row(row) if row is not None else None


def list_changes(
    *,
    person_id: int | None = None,
    days: int = LIST_DAYS,
    unseen_only: bool = False,
    db_path: Path | None = None,
) -> list[ContactChange]:
    """Newest first: the last ``days`` days, or one person's every change.
    Open suggestions are listed whatever their age."""
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    sql = "SELECT * FROM contact_changes WHERE "
    params: list[object] = []
    if person_id is not None:
        sql += "person_id = ?"
        params.append(person_id)
    else:
        sql += "(created_at >= ? OR status = 'suggested')"
        params.append(since)
    if unseen_only:
        sql += " AND seen = 0 AND status IN ('applied', 'suggested')"
    sql += " ORDER BY id DESC LIMIT 200"
    with _conn(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row(r) for r in rows]


def open_suggestion(email: str, field: str, db_path: Path | None = None) -> bool:
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM contact_changes WHERE email = ? AND field = ? AND status = 'suggested'",
            (norm(email), field),
        ).fetchone()
    return row is not None


def last_applied(person_id: int, field: str, db_path: Path | None = None) -> ContactChange | None:
    """The newest change still standing to ``field`` of ``person_id``."""
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM contact_changes WHERE person_id = ? AND field = ? AND status = 'applied' "
            "ORDER BY id DESC LIMIT 1",
            (person_id, field),
        ).fetchone()
    return _row(row) if row is not None else None


def mark_seen(db_path: Path | None = None) -> int:
    """Got it: clears the card. Suggestions stay until they are answered."""
    with _conn(db_path) as conn:
        cur = conn.execute("UPDATE contact_changes SET seen = 1 WHERE seen = 0 AND status != 'suggested'")
        return cur.rowcount


def _decide(change_id: int, status: Status, db_path: Path | None = None, *, person_id: int | None = None) -> None:
    with _conn(db_path) as conn:
        conn.execute(
            "UPDATE contact_changes SET status = ?, seen = 1, decided_at = ?, person_id = COALESCE(?, person_id) "
            "WHERE id = ?",
            (status, _now(), person_id, change_id),
        )


def counts_by_day(days: int = 7, db_path: Path | None = None) -> list[tuple[str, int, int, str]]:
    """``(day, added, updated, latest created_at)`` for the changes still
    standing, newest day first."""
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT substr(created_at, 1, 10) AS day,"
            " SUM(CASE WHEN action = 'add' THEN 1 ELSE 0 END) AS added,"
            " SUM(CASE WHEN action = 'update' THEN 1 ELSE 0 END) AS updated,"
            " MAX(created_at) AS latest"
            " FROM contact_changes WHERE status = 'applied' AND reason != 'asked' AND created_at >= ?"
            " GROUP BY day ORDER BY day DESC",
            (since,),
        ).fetchall()
    return [(r["day"], int(r["added"]), int(r["updated"]), r["latest"]) for r in rows]


# --------------------------------------------------------------------------- #
# Undo, accept, dismiss
# --------------------------------------------------------------------------- #


def undo(change_id: int, db_path: Path | None = None) -> ContactChange:
    """Take back an applied change. Raises ``UndoRefused``."""
    from openexecutive.people import registry as people_registry
    from openexecutive.people import store as people_store

    change = get_change(change_id, db_path)
    if change is None or change.status != "applied" or change.person_id is None:
        raise UndoRefused("nothing to undo")
    person = people_store.get_person(change.person_id)
    if change.action == "add":
        if person is not None and person.kind == "contact" and not person.archived:
            people_store.archive_person(change.person_id)
        block(change.email, "contact", db_path=db_path)
    elif person is not None and (person.kind == "contact" or change.field == "role" or change.field in DETAIL_FIELDS):
        # A team member's name and addresses are never the keeper's to change.
        if change.field == "email_alias":
            kept = [a for a in person.email_aliases if norm(a) != change.new_value]
            people_store.set_person_emails(change.person_id, kept, source="contact_keeper")
        elif change.field == "full_name" and person.full_name == change.new_value:
            people_store.update_person(change.person_id, full_name=change.old_value or person.full_name)
        elif change.field == "role" and person.role == change.new_value:
            people_store.update_person(change.person_id, role=change.old_value)
        elif change.field in DETAIL_FIELDS:
            if people_store.remove_person_detail(change.person_id, change.field, change.new_value) and change.old_value:
                people_store.add_person_detail(
                    change.person_id, change.field, change.old_value, source="contact_keeper",
                )
        block(change.email, change.field, change.new_value, db_path=db_path)
    people_registry.invalidate()
    _decide(change_id, "undone", db_path)
    _audit("contact_change_undone", f"contact change {change_id} undone", change)
    return get_change(change_id, db_path) or change


def dismiss(change_id: int, db_path: Path | None = None) -> ContactChange:
    """Not now: a suggestion is dropped and not made again."""
    change = get_change(change_id, db_path)
    if change is None or change.status != "suggested":
        raise UndoRefused("no open suggestion")
    block(change.email, change.field, "" if change.field == "contact" else change.new_value, db_path=db_path)
    _decide(change_id, "dismissed", db_path)
    _audit("contact_change_dismissed", f"contact suggestion {change_id} dismissed", change)
    return get_change(change_id, db_path) or change


def accept(change_id: int, db_path: Path | None = None) -> ContactChange:
    """Add: make a suggested change. Raises ``UndoRefused`` when it is no
    longer open, or the address is someone's now."""
    from openexecutive.people import contact_keeper

    change = get_change(change_id, db_path)
    if change is None or change.status != "suggested":
        raise UndoRefused("no open suggestion")
    person_id = contact_keeper.apply_change(change)
    if person_id is None:
        raise UndoRefused("it can't be made now")
    _decide(change_id, "applied", db_path, person_id=person_id)
    _audit("contact_change_accepted", f"contact suggestion {change_id} accepted", change)
    return get_change(change_id, db_path) or change


def _audit(event_type: str, summary: str, change: ContactChange) -> None:
    """Name-free and private: contacts are the principal's alone."""
    try:
        from openexecutive.audit import log_event

        log_event(
            event_type, summary, actor="executive", private=True,
            details={"change_id": change.id, "person_id": change.person_id, "action": change.action,
                     "field": change.field},
        )
    except Exception:
        logger.warning("contact_changes: couldn't write the audit row", exc_info=True)
