"""What the Executive already wrote back on an email thread.

An email turn knows the thread only through what the sender quoted below
their message, and people trim that. So the answers the poller mails itself
(a mail source's answer) are kept here per thread, and the next turn on that thread from the same person
is shown the last few (``block``). Without it, a reply could say "there was
no draft" right after the previous one sent the draft.

Mail the Executive sends with its own Gmail or Outlook tool (``record_sent``,
from the MCP gateway) is kept the same way, so a reply in one chat is known to the
next email from that person.

A turn is also shown the last few replies to the same person on their other
threads (``block``'s ``also``: the addresses on their People entry), so an
answer given on one thread isn't denied on the next. Only what was sent to
that person's own addresses is shown, so a turn never sees mail that went to
someone else. Kept in the episodic database, trimmed to the last ``KEEP``
per thread. Plain code.
"""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from email.utils import getaddresses
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TABLE = "email_thread_replies"
# Replies shown to the next turn, and how long each may be.
SHOWN = 3
MAX_CHARS = 2000
# Replies kept per thread.
KEEP = 10
# Replies shown from the person's other threads.
SHOWN_ELSEWHERE = 3
# A send seen twice (the poller keeps its answer, and the gateway sees the
# same send) within this long, with the same start, is kept once.
_SAME_SEND = timedelta(minutes=5)
_SAME_PREFIX = 200
_SAME_MIN = 40
# Tests point this at a temporary file (tests/conftest.py).
DB_PATH_OVERRIDE: Path | None = None


def _db(db_path: Path | None) -> Path:
    if db_path is not None:
        return db_path
    if DB_PATH_OVERRIDE is not None:
        return DB_PATH_OVERRIDE
    from openexecutive.memory import episodic

    return Path(episodic.DB_PATH)


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db(db_path)))
    conn.row_factory = sqlite3.Row
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {TABLE} ("  # noqa: S608 - constant table name
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  thread_key TEXT NOT NULL,"
        "  to_addr TEXT NOT NULL,"
        "  body TEXT NOT NULL,"
        "  sent_at TEXT NOT NULL"
        ")"
    )
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_thread ON {TABLE} (thread_key, id)")
    return conn


def record(thread_key: str, to: str, body: str, *, db_path: Path | None = None) -> None:
    """Keep ``body``, sent to ``to`` on ``thread_key``. Never raises."""
    key, addr, text = thread_key.strip(), to.strip().lower(), body.strip()
    if not key or not addr or not text:
        return
    try:
        conn = _connect(db_path)
        try:
            with conn:
                if _already_kept(conn, key, addr, text):
                    return
                conn.execute(
                    f"INSERT INTO {TABLE} (thread_key, to_addr, body, sent_at) VALUES (?, ?, ?, ?)",  # noqa: S608
                    (key, addr, text[:MAX_CHARS * 2], datetime.now(UTC).isoformat()),
                )
                conn.execute(
                    f"DELETE FROM {TABLE} WHERE thread_key = ? AND id NOT IN "  # noqa: S608
                    f"(SELECT id FROM {TABLE} WHERE thread_key = ? ORDER BY id DESC LIMIT ?)",
                    (key, key, KEEP),
                )
        finally:
            conn.close()
    except Exception:
        logger.exception("email: could not keep the reply sent on thread %s", key)


def _start(text: str) -> str:
    return " ".join(text.split())[:_SAME_PREFIX].lower()


def _already_kept(conn: sqlite3.Connection, key: str, addr: str, text: str) -> bool:
    """Whether the same send to ``addr`` on thread ``key`` was kept a moment
    ago: the poller keeps its answer, and the gateway sees the same send with
    the AI line added below it. Only on the same thread, and only when the
    shorter text is long enough to tell, so a short "Thanks!" never swallows
    a longer email that starts the same way."""
    since = (datetime.now(UTC) - _SAME_SEND).isoformat()
    start = _start(text)
    for row in conn.execute(
        f"SELECT body FROM {TABLE} WHERE thread_key = ? AND to_addr = ? AND sent_at >= ? "  # noqa: S608
        "ORDER BY id DESC LIMIT 5",
        (key, addr, since),
    ):
        kept = _start(str(row["body"]))
        if min(len(kept), len(start)) < _SAME_MIN:
            continue
        if kept.startswith(start) or start.startswith(kept):
            return True
    return False


def _addresses(values: Any) -> list[str]:
    items = values if isinstance(values, list) else [values]
    return [a.strip().lower() for _n, a in getaddresses([v for v in items if isinstance(v, str)]) if a.strip()]


def _norm(key: object) -> str:
    return str(key).replace("_", "").replace("-", "").lower()


def record_sent(arguments: dict[str, Any], *, db_path: Path | None = None) -> None:
    """Keep an email the Executive sent with its mail tool, for each address
    it went to (to, cc and bcc). Keys are read whatever their spelling
    (``thread_id`` / ``threadId``, ``To``, ``Body``). A send in a thread is
    kept under that thread, like the poller's answers; a new one under its
    address. Never raises: the send already happened."""
    try:
        args = {_norm(k): v for k, v in arguments.items()}
        body = args.get("body")
        if not isinstance(body, str) or not body.strip():
            return
        thread_id = str(args.get("threadid") or "").strip()
        recipients = [a for key in ("to", "cc", "bcc") for a in _addresses(args.get(key))]
        for addr in dict.fromkeys(recipients):
            record(f"email:{thread_id}" if thread_id else f"sent:{addr}", addr, body, db_path=db_path)
    except Exception:
        logger.exception("email: could not keep a sent email")


def recent_elsewhere(
    thread_key: str, addresses: Iterable[str], *, limit: int = SHOWN_ELSEWHERE, db_path: Path | None = None
) -> list[tuple[str, str]]:
    """The last ``limit`` replies sent to any of ``addresses`` on threads
    other than ``thread_key``, oldest first, as ``(sent_at, body)``. Empty
    on any error."""
    addrs = sorted({a.strip().lower() for a in addresses if a and a.strip()})
    if not addrs:
        return []
    marks = ", ".join("?" for _ in addrs)
    try:
        conn = _connect(db_path)
        try:
            rows = conn.execute(
                f"SELECT sent_at, body FROM {TABLE} WHERE to_addr IN ({marks}) AND thread_key != ? "  # noqa: S608
                "ORDER BY id DESC LIMIT ?",
                (*addrs, thread_key.strip(), limit),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        logger.exception("email: could not read the replies sent to this person")
        return []
    return [(str(r["sent_at"]), str(r["body"])) for r in reversed(rows)]


def recent(thread_key: str, to: str, *, limit: int = SHOWN, db_path: Path | None = None) -> list[tuple[str, str]]:
    """The last ``limit`` replies sent to ``to`` on ``thread_key``, oldest
    first, as ``(sent_at, body)``. Empty on any error."""
    key, addr = thread_key.strip(), to.strip().lower()
    if not key or not addr:
        return []
    try:
        conn = _connect(db_path)
        try:
            rows = conn.execute(
                f"SELECT sent_at, body FROM {TABLE} WHERE thread_key = ? AND to_addr = ? "  # noqa: S608
                "ORDER BY id DESC LIMIT ?",
                (key, addr, limit),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        logger.exception("email: could not read the replies sent on thread %s", key)
        return []
    return [(str(r["sent_at"]), str(r["body"])) for r in reversed(rows)]


def _shown(sent: list[tuple[str, str]]) -> str:
    parts = []
    for when, body in sent:
        text = body if len(body) <= MAX_CHARS else body[:MAX_CHARS] + "\n[…]"
        parts.append(f"[{when[:16].replace('T', ' ')} UTC]\n{text}")
    return "\n\n".join(parts)


def block(
    thread_key: str, to: str, *, also: Iterable[str] = (), elsewhere: bool = True, db_path: Path | None = None,
) -> str:
    """The turn's note of what it already wrote to ``to`` on this thread,
    and, with ``elsewhere``, to the same person (``to`` and ``also``, their
    addresses) on other threads, or "" when nothing was. Pass ``elsewhere``
    only for a sender the mail proved: a forged From must not read back what
    went to the real person. Goes in the user turn, never a cached block."""
    from openexecutive.orchestrator.content_trust import wrap_untrusted

    here = recent(thread_key, to, db_path=db_path)
    others = recent_elsewhere(thread_key, [to, *also], db_path=db_path) if elsewhere else []
    out: list[str] = []
    # What was sent can quote the sender's own mail, so it is content, not
    # instructions, like the email itself.
    if here:
        out.append(
            "--- YOUR EARLIER REPLIES ON THIS THREAD ---\n"
            "What you already wrote back to this person here, oldest first. Build on it: "
            "don't say something doesn't exist if you sent it, and don't repeat it unless asked.\n"
            + wrap_untrusted(_shown(here), source="earlier_reply", author="you")
            + "\n--- END OF EARLIER REPLIES ---"
        )
    if others:
        out.append(
            "--- WHAT YOU SENT THIS PERSON ON OTHER THREADS ---\n"
            "Your latest emails to this same person in other conversations, oldest first. "
            "Stay consistent with them: don't say you don't know something you already told them.\n"
            + wrap_untrusted(_shown(others), source="earlier_reply", author="you")
            + "\n--- END ---"
        )
    return "\n\n".join(out)
