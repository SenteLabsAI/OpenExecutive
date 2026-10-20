"""What the principal asked of their brief in conversation.

Two kinds of line, both short and both the principal's own:

- ``leave_out``: "remove it from the brief", "stop putting the Halcyon
  renewal in my brief". The brief and the /today header leave the item out
  until the line expires (``LEAVE_OUT_DAYS``) or the principal removes it.
- ``raise``: something the conversation review (``memory.chat_review``)
  found it could not fix itself ("You said the vendor invoice was handled.
  Want me to close it?"). The next brief raises it once, then it expires
  (``RAISE_DAYS``).

The leave-out list renders last in the brief's context, after the quiet-day
check (like the standing facts), so it never makes a quiet day look busy; a
line to raise renders before it, so it is raised even on a quiet day. Both sit
inside the rendered string the /today header hashes, so adding one rewrites
the header. A teammate's header leaves both out.
Only the principal's turns write them (the brief is theirs); a line must be
backed by the principal's own words, checked by the writer.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path

logger = logging.getLogger(__name__)

TABLE = "brief_requests"
KIND_LEAVE_OUT = "leave_out"
KIND_RAISE = "raise"
_KINDS = frozenset({KIND_LEAVE_OUT, KIND_RAISE})
LEAVE_OUT_DAYS = 14
RAISE_DAYS = 2
TEXT_MAX = 200
MAX_ACTIVE = 20
# A line is a short description, never a way to steer the brief: no links,
# handles, markup, identifiers or talk of instructions.
MAX_WORDS = {KIND_LEAVE_OUT: 12, KIND_RAISE: 30}
_DENY = (
    re.compile(r"https?://|www\.|\.(com|io|net|org|ai)\b", re.IGNORECASE),
    re.compile(r"@\w"),
    re.compile(r"[<>{}`\[\]]"),
    re.compile(r"\b\w+_\w+\b"),
    re.compile(
        r"\b(ignore|disregard|override|bypass|instruction|instructions|prompt|prompts|"
        r"tool|tools|system|assistant|anything|everything|all items|any item)\b",
        re.IGNORECASE,
    ),
)

LEAVE_OUT_HEADER = (
    "THE READER ASKED TO LEAVE THESE OUT OF THE BRIEF (don't mention them, "
    "even if the context above shows them):"
)
RAISE_HEADER = (
    "FROM YOUR CHATS (things you said you'd do and couldn't; raise each in one "
    "line and ask if they want it done):"
)


@dataclass(frozen=True)
class BriefRequest:
    id: int
    kind: str
    text: str
    person_id: int | None
    created_at: str
    expires_at: str


def _db(db_path: Path | None) -> Path:
    if db_path is not None:
        return db_path
    from openexecutive.memory import episodic

    return Path(episodic.DB_PATH)


def _connect(db_path: Path | None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db(db_path)))
    conn.row_factory = sqlite3.Row
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {TABLE} ("  # noqa: S608 — constant table name
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  kind TEXT NOT NULL,"
        "  text TEXT NOT NULL,"
        "  source_quote TEXT NOT NULL,"
        "  person_id INTEGER,"
        "  session_id TEXT,"
        "  created_at TEXT NOT NULL,"
        "  expires_at TEXT NOT NULL,"
        "  removed_at TEXT"
        ")"
    )
    return conn


def _now() -> datetime:
    return datetime.now(UTC)


def clean_text(text: str) -> str:
    return " ".join(str(text or "").split())[:TEXT_MAX]


def text_rejection(kind: str, text: str) -> str | None:
    """Why ``text`` may not be a ``kind`` line, or None when it may."""
    if len(text.split()) > MAX_WORDS.get(kind, 0):
        return "too_long"
    if any(p.search(text) for p in _DENY):
        return "denied_content"
    return None


def add(
    kind: str,
    text: str,
    *,
    source_quote: str,
    person_id: int | None,
    session_id: str | None = None,
    now: datetime | None = None,
    db_path: Path | None = None,
) -> BriefRequest | None:
    """Store a line; None when it is empty, unknown, rejected
    (:func:`text_rejection`), a repeat of an active one, or ``MAX_ACTIVE``
    lines are already active."""
    if kind not in _KINDS:
        return None
    body = clean_text(text)
    if not body or text_rejection(kind, body):
        return None
    current = now or _now()
    days = LEAVE_OUT_DAYS if kind == KIND_LEAVE_OUT else RAISE_DAYS
    expires = current + timedelta(days=days)
    conn = _connect(db_path)
    try:
        live = conn.execute(
            f"SELECT id, kind, text FROM {TABLE} WHERE removed_at IS NULL AND expires_at > ?",  # noqa: S608
            (current.isoformat(),),
        ).fetchall()
        if any(r["kind"] == kind and r["text"].casefold() == body.casefold() for r in live):
            return None
        if len(live) >= MAX_ACTIVE:
            return None
        cur = conn.execute(
            f"INSERT INTO {TABLE} (kind, text, source_quote, person_id, session_id, created_at, expires_at) "  # noqa: S608
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (kind, body, clean_text(source_quote), person_id, session_id,
             current.isoformat(), expires.isoformat()),
        )
        conn.commit()
        row_id = int(cur.lastrowid or 0)
    finally:
        conn.close()
    return BriefRequest(row_id, kind, body, person_id, current.isoformat(), expires.isoformat())


def active(*, now: datetime | None = None, db_path: Path | None = None) -> list[BriefRequest]:
    """Lines still in force, oldest first. Never raises."""
    path = _db(db_path)
    if not path.exists():
        return []
    try:
        conn = _connect(db_path)
        try:
            rows = conn.execute(
                f"SELECT id, kind, text, person_id, created_at, expires_at FROM {TABLE} "  # noqa: S608
                "WHERE removed_at IS NULL AND expires_at > ? ORDER BY id",
                ((now or _now()).isoformat(),),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        logger.warning("brief_requests: couldn't read", exc_info=True)
        return []
    return [BriefRequest(**dict(r)) for r in rows]


def remove(request_id: int, *, db_path: Path | None = None) -> bool:
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            f"UPDATE {TABLE} SET removed_at = ? WHERE id = ? AND removed_at IS NULL",  # noqa: S608
            (_now().isoformat(), request_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def _principal_id() -> int | None:
    try:
        from openexecutive.people.store import find_principal_person

        person = find_principal_person()
        return int(person.id) if person is not None and person.id is not None else None
    except Exception:
        logger.warning("brief_requests: couldn't resolve the principal", exc_info=True)
        return None


def render_parts(
    *, now: datetime | None = None, principal_id: int | None = None, db_path: Path | None = None
) -> tuple[str, str]:
    """``(raise_block, leave_out_block)`` for the brief's context; each is ""
    when there is nothing to say. Only the principal's own lines render
    (``principal_id``, resolved when not given). Never raises."""
    owner = principal_id if principal_id is not None else _principal_id()
    if owner is None:
        return "", ""
    rows = [r for r in active(now=now, db_path=db_path) if r.person_id == owner]
    to_raise = [r for r in rows if r.kind == KIND_RAISE]
    leave_out = [r for r in rows if r.kind == KIND_LEAVE_OUT]
    raise_block = "\n".join([RAISE_HEADER, *(f"- {r.text}" for r in to_raise)]) if to_raise else ""
    leave_block = "\n".join([LEAVE_OUT_HEADER, *(f"- {r.text}" for r in leave_out)]) if leave_out else ""
    return raise_block, leave_block


def raise_split(
    tz: tzinfo, *, now: datetime | None = None, db_path: Path | None = None
) -> tuple[list[str], list[str], str]:
    """``(raised_today, raised_before, leave_out_block)`` for Right now: the
    principal's raise lines split by whether they were saved on the local
    day ``tz`` gives, and the leave-out block as :func:`render_parts` renders
    it. One read of the store. Never raises."""
    from openexecutive.alerts.lifecycle import parse_aware

    owner = _principal_id()
    if owner is None:
        return [], [], ""
    when = now or _now()
    rows = [r for r in active(now=when, db_path=db_path) if r.person_id == owner]
    today_local = when.astimezone(tz).date()
    fresh: list[str] = []
    older: list[str] = []
    for r in rows:
        if r.kind != KIND_RAISE:
            continue
        created = parse_aware(r.created_at)
        is_fresh = created is not None and created.astimezone(tz).date() == today_local
        (fresh if is_fresh else older).append(r.text)
    leave_out = [r for r in rows if r.kind == KIND_LEAVE_OUT]
    leave_block = "\n".join([LEAVE_OUT_HEADER, *(f"- {r.text}" for r in leave_out)]) if leave_out else ""
    return fresh, older, leave_block
