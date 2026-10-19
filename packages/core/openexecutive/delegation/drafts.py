"""Every draft Act as me saved in someone's Gmail (``delegation_drafts``).

One row per draft, from chat (``ghostwrite_email``) or the inbox watcher. It
is what the daily limit counts (``delegation.caps``), and what "How I write"
leaves out when it learns from sent mail:

- a chat draft's whole thread, since the person asked for it there;
- an inbox draft only as the message it became once sent. The watcher drafts
  into so many threads that skipping them all would leave little of the
  person's own writing to learn from.

Metadata only: ids and times, never an address, a subject or any text.
Who a waiting chat draft goes to and its subject are read from the mailbox
by ``settle_waiting`` and kept in memory only (``waiting_drafts``), so the
briefs can name the draft.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from openexecutive.delegation.schema import DRAFTS_TABLE, ensure_schema

logger = logging.getLogger(__name__)

SOURCE_CHAT = "chat"
SOURCE_INBOX = "inbox"
_SOURCES = frozenset({SOURCE_CHAT, SOURCE_INBOX})

# How far back ``settle_waiting`` looks for chat drafts still open. The
# briefs only name drafts from their own window (today, or since the last
# brief), so an older one never needs a Gmail read.
SETTLE_WINDOW = timedelta(days=2)
# At most this many Gmail reads per settle pass.
_SETTLE_MAX = 20


@dataclass(frozen=True)
class WaitingDraft:
    """A chat draft still in the person's Drafts, as the briefs name it."""

    created_at: str
    to: tuple[str, ...] = ()
    subject: str = ""


# Row id -> (to, subject) as the last ``settle_waiting`` read them from the
# mailbox. In memory only: the table keeps no address or subject.
_seen: dict[int, tuple[tuple[str, ...], str]] = {}


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
    *,
    source: str,
    thread_id: str | None,
    draft_id: str,
    message_id: str,
    now: datetime,
    db_path: Path | None = None,
) -> int:
    """Store a draft just saved in ``person_id``'s Gmail; returns its row id."""
    if source not in _SOURCES:
        raise ValueError(f"unknown draft source {source!r}")
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            f"INSERT INTO {DRAFTS_TABLE} "  # noqa: S608 — constant table name
            "(person_id, source, thread_id, draft_id, message_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (person_id, source, thread_id, draft_id, message_id, now.isoformat()),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def count_since(
    person_id: int, since: datetime, *, source: str | None = None, db_path: Path | None = None
) -> int:
    """Drafts saved as ``person_id`` at or after ``since``, from chat and the
    inbox alike, or from one ``source``. Raises on a read error: a cap must
    not fail open."""
    sql = f"SELECT COUNT(*) FROM {DRAFTS_TABLE} WHERE person_id = ? AND created_at >= ?"  # noqa: S608
    params: tuple[object, ...] = (person_id, since.isoformat())
    if source is not None:
        sql += " AND source = ?"
        params = (*params, source)
    conn = _connect(db_path)
    try:
        row = conn.execute(sql, params).fetchone()
    finally:
        conn.close()
    return int(row[0]) if row else 0


def mark_sent(
    person_id: int, draft_id: str, sent_message_id: str, *, db_path: Path | None = None
) -> bool:
    """Record the message a draft became when it was sent."""
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            f"UPDATE {DRAFTS_TABLE} SET sent_message_id = ? "  # noqa: S608 — constant table name
            "WHERE person_id = ? AND draft_id = ?",
            (sent_message_id, person_id, draft_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def rename(
    person_id: int, old_draft_id: str, draft_id: str, message_id: str, *, db_path: Path | None = None
) -> bool:
    """A reply kept on its card alone (``card_drafts``) became a real draft
    when it was sent: its row takes the draft's ids, so it is still counted
    once and ``mark_sent`` finds it."""
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            f"UPDATE {DRAFTS_TABLE} SET draft_id = ?, message_id = ? "  # noqa: S608 — constant table name
            "WHERE person_id = ? AND draft_id = ?",
            (draft_id, message_id, person_id, old_draft_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def chat_thread_ids(person_id: int, *, db_path: Path | None = None) -> set[str]:
    """The threads chat drafted into as ``person_id``."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT DISTINCT thread_id FROM {DRAFTS_TABLE} "  # noqa: S608 — constant table name
            "WHERE person_id = ? AND source = ? AND thread_id IS NOT NULL",
            (person_id, SOURCE_CHAT),
        ).fetchall()
    finally:
        conn.close()
    return {str(r[0]) for r in rows}


def sent_message_ids(person_id: int, *, db_path: Path | None = None) -> set[str]:
    """The messages drafts written as ``person_id`` became once sent."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT sent_message_id FROM {DRAFTS_TABLE} "  # noqa: S608 — constant table name
            "WHERE person_id = ? AND sent_message_id IS NOT NULL",
            (person_id,),
        ).fetchall()
    finally:
        conn.close()
    return {str(r[0]) for r in rows}


def usage_since(person_id: int, since: datetime, *, db_path: Path | None = None) -> tuple[int, int]:
    """``(drafts saved, of those sent)`` for ``person_id`` since ``since``:
    counts only, never what they said or to whom (the owner's view of a team
    member's use). Raises on a read error."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            f"SELECT COUNT(*), COUNT(sent_message_id) FROM {DRAFTS_TABLE} "  # noqa: S608 — constant table name
            "WHERE person_id = ? AND created_at >= ?",
            (person_id, since.isoformat()),
        ).fetchone()
    finally:
        conn.close()
    return int(row[0] or 0), int(row[1] or 0)


def _waiting_sql(person_id: int, since: datetime) -> tuple[str, tuple[object, ...]]:
    return (
        f"FROM {DRAFTS_TABLE} WHERE person_id = ? AND source = ? AND created_at >= ? "  # noqa: S608
        "AND sent_message_id IS NULL AND gone_at IS NULL",
        (person_id, SOURCE_CHAT, since.isoformat()),
    )


def count_waiting(person_id: int, since: datetime, *, db_path: Path | None = None) -> int:
    """Chat drafts saved as ``person_id`` at or after ``since`` that are not
    known to be sent or gone: the ones still waiting in their Drafts as far as
    the last ``settle_waiting`` saw."""
    where, params = _waiting_sql(person_id, since)
    conn = _connect(db_path)
    try:
        row = conn.execute(f"SELECT COUNT(*) {where}", params).fetchone()  # noqa: S608
    finally:
        conn.close()
    return int(row[0]) if row else 0


def _waiting(person_id: int, since: datetime, db_path: Path | None) -> list[tuple[int, str]]:
    return [(row[0], row[1]) for row in _waiting_rows(person_id, since, db_path)]


def _waiting_rows(
    person_id: int, since: datetime, db_path: Path | None
) -> list[tuple[int, str, str, str]]:
    """``(row id, draft id, created_at, thread id or "")``, newest first."""
    where, params = _waiting_sql(person_id, since)
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT id, draft_id, created_at, thread_id {where} ORDER BY id DESC LIMIT ?",  # noqa: S608
            (*params, _SETTLE_MAX),
        ).fetchall()
    finally:
        conn.close()
    return [(int(r[0]), str(r[1] or ""), str(r[2] or ""), str(r[3] or "")) for r in rows]


def waiting_drafts(
    person_id: int, since: datetime, *, db_path: Path | None = None
) -> list[WaitingDraft]:
    """``person_id``'s chat drafts since ``since`` still counted as waiting,
    newest first, each with who it goes to and its subject when a settle
    pass has read them (empty until one has)."""
    out = []
    for row_id, _, created_at, _ in _waiting_rows(person_id, since, db_path):
        to, subject = _seen.get(row_id, ((), ""))
        out.append(WaitingDraft(created_at=created_at, to=to, subject=subject))
    return out


def latest_waiting(person_id: int, since: datetime, *, db_path: Path | None = None) -> int | None:
    """The row id of ``person_id``'s newest chat draft since ``since`` still
    counted as waiting, or None."""
    waiting = _waiting(person_id, since, db_path)
    return waiting[0][0] if waiting else None


def mark_gone(row_id: int, now: datetime, *, db_path: Path | None = None) -> None:
    """A chat draft is no longer in the person's Drafts (sent or deleted)."""
    conn = _connect(db_path)
    try:
        conn.execute(
            f"UPDATE {DRAFTS_TABLE} SET gone_at = ? WHERE id = ? AND gone_at IS NULL",  # noqa: S608
            (now.isoformat(), row_id),
        )
        conn.commit()
    finally:
        conn.close()


async def settle_waiting(
    person: Any, *, now: datetime, gmail: Any = None, db_path: Path | None = None
) -> int:
    """Look up ``person``'s recent chat drafts in their mailbox and mark the
    ones no longer there (sent or deleted in Gmail or Outlook), so the briefs
    stop calling them waiting. Nothing records a send made in the mailbox
    itself, and the chat drafts have no card for the inbox watcher to settle.
    A reply draft also stops waiting once they sent their own message in
    that thread after it was saved: they answered without it, so it is not
    theirs to review any more (the draft itself stays in their mailbox).
    Reads only; returns how many it marked. Never raises."""
    person_id = getattr(person, "id", None)
    email = str(getattr(person, "email", "") or "").strip().lower()
    if person_id is None or not email:
        return 0
    try:
        waiting = _waiting_rows(int(person_id), now - SETTLE_WINDOW, db_path)
    except Exception:
        logger.warning("delegation.drafts: couldn't read the waiting drafts", exc_info=True)
        return 0
    if not waiting:
        return 0  # no mailbox call on a day with no chat drafts
    from openexecutive.delegation.gmail import GmailError, gmail_for, valid_id

    client = gmail if gmail is not None else gmail_for(email)
    gone = 0
    for row_id, draft_id, created_at, thread_id in waiting:
        if not valid_id(draft_id):
            continue  # nothing to look up
        try:
            found = await client.get_draft(draft_id)
            if found is not None and not (
                valid_id(thread_id) and await _answered_since(client, thread_id, email, created_at)
            ):
                message = getattr(found, "message", None)
                recipients = [*getattr(message, "to", []), *getattr(message, "cc", [])]
                to = tuple(str(a) for a in recipients[:5] if a)
                _seen[row_id] = (to, str(getattr(message, "subject", "") or ""))
                continue
            _seen.pop(row_id, None)
            mark_gone(row_id, now, db_path=db_path)
            gone += 1
        except GmailError as exc:
            # Not connected, a sign-in problem, the service down: the draft
            # counts as waiting until a later pass can tell.
            logger.info("delegation.drafts: couldn't check a draft (%s)", type(exc).__name__)
            break
        except Exception:
            logger.warning("delegation.drafts: checking a draft failed", exc_info=True)
            break
    return gone


async def _answered_since(client: Any, thread_id: str, own: str, created_at: str) -> bool:
    """Whether ``own`` sent a message of their own in ``thread_id`` after
    ``created_at`` (a draft is never one)."""
    from openexecutive.delegation.threads import mine

    try:
        saved = datetime.fromisoformat(created_at)
    except ValueError:
        return False
    thread = await client.get_thread(thread_id)
    for m in getattr(thread, "messages", None) or []:
        if "DRAFT" in m.labels or not mine(m, own):
            continue
        try:
            sent = datetime.fromisoformat(m.received_at)
        except ValueError:
            continue
        if sent.tzinfo is None:
            sent = sent.replace(tzinfo=saved.tzinfo)
        if sent > saved:
            return True
    return False
