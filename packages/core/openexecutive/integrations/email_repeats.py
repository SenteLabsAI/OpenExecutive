"""The same email twice: one answer.

A sender's mail system can deliver one message twice, or the sender sends it
again, as a new message with its own id (sometimes its own thread). Each copy
used to get its own turn, so the sender got two replies and the owner two
cards. ``remember`` keeps a short fingerprint (a hash of sender, subject and
words, never the text) of each email answered for a sender the mail provider
vouched for, for a day, and ``is_copy`` leaves alone a later message that
matches one.

Short mail ("yes", "thanks") is never matched: the same few words can be a
new answer to a new question. Nor is mail with files: the same note can
carry a different attachment. The same request word for word within a day
(a daily "send me today's numbers") is answered once: the window is a day
for that reason.
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

TABLE = "email_repeats"
WINDOW = timedelta(hours=24)
# Fewer words than this and a copy is never matched.
MIN_WORDS = 8
# Where a backend's rendered text (Gmail) starts the body: the header lines
# above it differ between copies (message id, date).
_BODY_MARKER_RE = re.compile(r"^\s*---\s*BODY\s*---\s*$", re.IGNORECASE | re.MULTILINE)
_REPLY_PREFIX_RE = re.compile(r"^(?:\s*(?:re|fwd?|aw|sv)\s*:\s*)+", re.IGNORECASE)


def _db_path(db_path: Path | None) -> Path:
    if db_path is not None:
        return db_path
    from openexecutive.memory import episodic

    return Path(episodic.DB_PATH)


def fingerprint(from_addr: str, subject: str, body: str, *, has_attachments: bool = False) -> str | None:
    """The sender, subject and words of a message as one hash, or None when
    the text is too short to tell a copy from a new message, or it carries
    files."""
    if has_attachments:
        return None
    marker = _BODY_MARKER_RE.search(body)
    if marker is not None:
        body = body[marker.end():]
    words = re.findall(r"\w+", body.casefold())
    if len(words) < MIN_WORDS:
        return None
    topic = " ".join(_REPLY_PREFIX_RE.sub("", subject).casefold().split())
    blob = "\n".join([from_addr.strip().lower(), topic, " ".join(words)])
    return hashlib.sha256(blob.encode()).hexdigest()


def _open(db_path: Path | None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path(db_path)), isolation_level=None)
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {TABLE} (key TEXT PRIMARY KEY, message_id TEXT NOT NULL, seen_at TEXT NOT NULL)"
    )
    return conn


def is_copy(key: str | None, message_id: str, *, now: datetime | None = None, db_path: Path | None = None) -> bool:
    """Whether another message with this fingerprint was answered within the
    last day. Never raises: an error lets the message through."""
    if key is None:
        return False
    since = ((now or datetime.now(UTC)) - WINDOW).isoformat()
    try:
        conn = _open(db_path)
        try:
            row = conn.execute(
                f"SELECT message_id FROM {TABLE} WHERE key = ? AND seen_at >= ?", (key, since)
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        logger.warning("email_repeats: couldn't check for a copy; handling it", exc_info=True)
        return False
    return row is not None and row[0] != message_id


def remember(key: str | None, message_id: str, *, now: datetime | None = None, db_path: Path | None = None) -> None:
    """Note that a message with this fingerprint was answered. Called only
    once the turn ran for a sender the mail provider vouched for, so a forged
    copy sent first can't make the real one look like a repeat. Never raises."""
    if key is None:
        return
    when = now or datetime.now(UTC)
    try:
        conn = _open(db_path)
        try:
            conn.execute(f"DELETE FROM {TABLE} WHERE seen_at < ?", ((when - WINDOW).isoformat(),))
            conn.execute(
                f"INSERT OR REPLACE INTO {TABLE} (key, message_id, seen_at) VALUES (?, ?, ?)",
                (key, message_id, when.isoformat()),
            )
        finally:
            conn.close()
    except Exception:
        logger.warning("email_repeats: couldn't note an answered email", exc_info=True)
