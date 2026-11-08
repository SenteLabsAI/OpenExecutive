"""A department's own notes: what was decided, updated or checked in on.

Each department keeps a short timeline of kind-tagged notes in the company's
own database: a decision or advice whose department names it, an initiative
or goal update, a committee decision, a check-in summary. Written by
``honcho_client.append_department_note`` (every call, whether or not peer
memory is on), and read back when one of the department's specialists is
consulted (``for_prompt``), as the ``<department_memory>`` block, when peer
memory is off or has nothing.

Notes are company work, not anyone's private words: anyone whose question
reaches the department's specialist may see them. Each department keeps its
newest ``KEEP`` notes, none older than ``MAX_AGE_DAYS``. A factory reset and
a switch into a blank client slot clear the table. A turn private to the
principal keeps nothing here. Nothing here raises.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

TABLE = "department_notes"
KEEP = 200
MAX_AGE_DAYS = 365
MAX_BODY = 4_000
MAX_CHARS = 1_500
_PER_NOTE = 400

_KIND_LABEL = {
    "decision": "Decision",
    "initiative_update": "Initiative",
    "advice": "Advice",
    "goal_update": "Goal",
    "committee_decision": "Committee decision",
    "cadence_checkin": "Check-in",
}

_STOPWORDS = frozenset(
    ["a", "an", "and", "are", "as", "at", "be", "about", "did", "do", "does", "for", "from", "had", "has", "have", "how", "i", "if", "in", "is", "it", "its", "me", "my", "of", "on", "or", "our", "so", "that", "the", "their", "them", "they", "this", "to", "was", "we", "were", "what", "when", "where", "which", "who", "why", "will", "with", "you", "your", "should", "could", "would", "can"]
)


def _connect(db_path: Path | None) -> sqlite3.Connection:
    from openexecutive.memory.episodic import _resolve_db_path

    conn = sqlite3.connect(str(_resolve_db_path(db_path)))
    conn.row_factory = sqlite3.Row
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {TABLE} ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  department_slug TEXT NOT NULL,"
        "  kind TEXT NOT NULL,"
        "  body TEXT NOT NULL,"
        "  person_id INTEGER,"
        "  created_at TEXT NOT NULL"
        ")"
    )
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_slug ON {TABLE}(department_slug, id DESC)")
    conn.commit()
    return conn


def _private_turn() -> bool:
    """Whether this turn is about something private to the principal (mail
    from one of their contacts, mail they forwarded): nothing from it goes
    into notes anyone else's question can reach. Fails closed."""
    try:
        from openexecutive.orchestrator.schedule_tools import current_session

        return getattr(current_session.get(), "private_to_principal", False) is True
    except Exception:
        return True


def add(
    department_slug: str | None,
    kind: str,
    body: str,
    *,
    person_id: int | None = None,
    db_path: Path | None = None,
    now: datetime | None = None,
) -> int | None:
    """Keep one note for ``department_slug``. Returns its id, or None when
    there is nothing to keep or the write failed."""
    text = (body or "").strip()
    if not department_slug or not text or _private_turn():
        return None
    when = (now or datetime.now(UTC)).isoformat()
    try:
        conn = _connect(db_path)
        try:
            cur = conn.execute(
                f"INSERT INTO {TABLE} (department_slug, kind, body, person_id, created_at) "  # noqa: S608 — constant table name
                "VALUES (?, ?, ?, ?, ?)",
                (department_slug, kind, text[:MAX_BODY], person_id, when),
            )
            conn.execute(
                f"DELETE FROM {TABLE} WHERE department_slug = ? AND id NOT IN ("  # noqa: S608 — constant table name
                f"SELECT id FROM {TABLE} WHERE department_slug = ? ORDER BY id DESC LIMIT ?)",
                (department_slug, department_slug, KEEP),
            )
            cutoff = ((now or datetime.now(UTC)) - timedelta(days=MAX_AGE_DAYS)).isoformat()
            conn.execute(f"DELETE FROM {TABLE} WHERE created_at < ?", (cutoff,))  # noqa: S608 — constant table name
            conn.commit()
            return int(cur.lastrowid) if cur.lastrowid is not None else None
        finally:
            conn.close()
    except Exception:
        logger.warning("department_notes: couldn't keep a note for %s", department_slug, exc_info=True)
        return None


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", text.lower()) if len(w) > 2 and w not in _STOPWORDS}


def _line(row: sqlite3.Row) -> str:
    label = _KIND_LABEL.get(str(row["kind"]), "Note")
    day = str(row["created_at"])[:10]
    body = " ".join(str(row["body"]).split())
    if len(body) > _PER_NOTE:
        body = body[: _PER_NOTE - 1].rstrip() + "…"
    return f"- {day} {label}: {body}"


def for_prompt(department_slug: str | None, query: str, *, db_path: Path | None = None) -> str:
    """The department's notes that bear on ``query``, best match first, then
    the newest ones, within ``MAX_CHARS``. Empty when it has none."""
    if not department_slug:
        return ""
    try:
        conn = _connect(db_path)
        try:
            rows = conn.execute(
                f"SELECT kind, body, created_at FROM {TABLE} WHERE department_slug = ? "  # noqa: S608 — constant table name
                "ORDER BY id DESC LIMIT ?",
                (department_slug, KEEP),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        logger.warning("department_notes: couldn't read notes for %s", department_slug, exc_info=True)
        return ""
    if not rows:
        return ""
    wanted = _words(query)
    ranked = sorted(
        enumerate(rows),
        key=lambda pair: (len(wanted & _words(str(pair[1]["body"]))), -pair[0]),
        reverse=True,
    )
    lines: list[str] = []
    used = 0
    for _, row in ranked:
        line = _line(row)
        if used + len(line) + 1 > MAX_CHARS:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)
