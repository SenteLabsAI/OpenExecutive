"""Tables and reads/writes for the reflection's look back at its chats.

Four tables in the episodic DB, created by ``ensure_schema`` (called from
``memory.episodic.initialize_db`` like the other feature schemas):

- ``chat_review_settings`` — one row: whether the owner has the look back on,
  whether approved changes apply on their own, and the ``chat_messages.id``
  watermark the last run reached.
- ``chat_review_runs`` — one row per nightly run: what it read and found.
- ``chat_review_findings`` — one row per thing the reviewer found, with the
  evidence (message ids and quotes) and the proposed change, and what the
  owner did about it.
- ``executive_lessons`` (+ ``_history``) — the approved lessons, rendered into
  every chat turn's ``<lessons>`` block (lessons.py).

Every function takes ``db_path`` so tests isolate it; ``None`` resolves to
``memory.episodic.DB_PATH`` at call time, like the facts store.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

FindingKind = Literal["fact", "lesson", "gap", "product"]
FindingStatus = Literal["proposed", "approved", "dismissed", "filed", "recorded"]
LessonStatus = Literal["active", "retired"]
ApplyMode = Literal["ask", "auto"]

FINDING_KINDS: tuple[str, ...] = ("fact", "lesson", "gap", "product")
SEVERITIES: tuple[str, ...] = ("low", "medium", "high")

# A proposal nobody acted on is filed after this long, so the screen never
# piles up (design: "not taken up").
FILE_AFTER_DAYS = 30
# Apply on its own is offered after this many approvals with no edits.
AUTO_APPLY_AFTER_APPROVALS = 10

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_review_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled INTEGER NOT NULL DEFAULT 1,
    apply_mode TEXT NOT NULL DEFAULT 'ask',
    last_message_id INTEGER NOT NULL DEFAULT 0,
    last_run_at TEXT,
    updated_at TEXT,
    updated_by TEXT
);
CREATE TABLE IF NOT EXISTS chat_review_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    day TEXT NOT NULL,
    sessions_considered INTEGER NOT NULL DEFAULT 0,
    sessions_reviewed INTEGER NOT NULL DEFAULT 0,
    findings_count INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS chat_review_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    session_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'medium',
    title TEXT NOT NULL,
    what_happened TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    proposal TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'proposed',
    resolved_at TEXT,
    resolved_by_person_id INTEGER,
    resolution_note TEXT NOT NULL DEFAULT '',
    lesson_id INTEGER,
    fact_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_chat_review_findings_run ON chat_review_findings(run_id, id);
CREATE INDEX IF NOT EXISTS idx_chat_review_findings_status ON chat_review_findings(status, id);
CREATE TABLE IF NOT EXISTS executive_lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    locked INTEGER NOT NULL DEFAULT 0,
    source_finding_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    approved_by_person_id INTEGER,
    retired_at TEXT,
    retired_reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS executive_lesson_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lesson_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL,
    updated_by TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_executive_lesson_history ON executive_lesson_history(lesson_id, id);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the tables if missing. Idempotent; called from ``initialize_db``."""
    conn.executescript(_SCHEMA)
    conn.execute("INSERT OR IGNORE INTO chat_review_settings (id) VALUES (1)")


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _db(db_path: Path | None) -> Path:
    from openexecutive.memory.episodic import _resolve_db_path

    return _resolve_db_path(db_path)


def _conn(db_path: Path | None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db(db_path)))
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def _has_tables(db_path: Path | None) -> bool:
    path = _db(db_path)
    if not path.exists():
        return False
    try:
        with sqlite3.connect(str(path)) as conn:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_review_findings'"
            ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


@dataclass
class ReviewSettings:
    enabled: bool = True
    apply_mode: str = "ask"
    last_message_id: int = 0
    last_run_at: str | None = None
    updated_at: str | None = None
    updated_by: str | None = None


def get_settings(db_path: Path | None = None) -> ReviewSettings:
    """The owner's settings; defaults (on, ask first) when nothing is stored."""
    if not _has_tables(db_path):
        return ReviewSettings()
    try:
        with _conn(db_path) as conn:
            row = conn.execute("SELECT * FROM chat_review_settings WHERE id = 1").fetchone()
    except sqlite3.Error:
        logger.exception("chat_review: settings read failed — using defaults")
        return ReviewSettings()
    if row is None:
        return ReviewSettings()
    return ReviewSettings(
        enabled=bool(row["enabled"]),
        apply_mode=str(row["apply_mode"] or "ask"),
        last_message_id=int(row["last_message_id"] or 0),
        last_run_at=row["last_run_at"],
        updated_at=row["updated_at"],
        updated_by=row["updated_by"],
    )


def save_settings(
    *,
    enabled: bool | None = None,
    apply_mode: str | None = None,
    updated_by: str,
    db_path: Path | None = None,
) -> ReviewSettings:
    """Change the owner's switches. ``apply_mode`` is validated by the route."""
    sets: list[str] = ["updated_at = ?", "updated_by = ?"]
    args: list[Any] = [_iso(_now()), updated_by]
    if enabled is not None:
        sets.append("enabled = ?")
        args.append(1 if enabled else 0)
    if apply_mode is not None:
        if apply_mode not in ("ask", "auto"):
            raise ValueError("apply_mode must be 'ask' or 'auto'")
        sets.append("apply_mode = ?")
        args.append(apply_mode)
    with _conn(db_path) as conn:
        conn.execute(f"UPDATE chat_review_settings SET {', '.join(sets)} WHERE id = 1", args)
    return get_settings(db_path)


def advance_watermark(message_id: int, *, db_path: Path | None = None) -> None:
    """Record the highest ``chat_messages.id`` a run has read, and when."""
    with _conn(db_path) as conn:
        conn.execute(
            "UPDATE chat_review_settings SET last_message_id = MAX(last_message_id, ?), "
            "last_run_at = ? WHERE id = 1",
            (int(message_id), _iso(_now())),
        )


def clean_approvals(db_path: Path | None = None) -> int:
    """How many lesson proposals the owner approved as written (no edit). Apply
    on its own is offered once this reaches ``AUTO_APPLY_AFTER_APPROVALS``."""
    if not _has_tables(db_path):
        return 0
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM chat_review_findings f JOIN executive_lessons l "
            "ON l.id = f.lesson_id WHERE f.kind = 'lesson' AND f.status = 'approved' "
            "AND f.resolution_note = '' AND l.text = f.proposal"
        ).fetchone()
    return int(row[0]) if row else 0


# --------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------


@dataclass
class ReviewRun:
    id: int
    started_at: str
    finished_at: str | None
    status: str
    day: str
    sessions_considered: int
    sessions_reviewed: int
    findings_count: int
    error: str


def _run_row(row: sqlite3.Row) -> ReviewRun:
    return ReviewRun(
        id=int(row["id"]),
        started_at=str(row["started_at"]),
        finished_at=row["finished_at"],
        status=str(row["status"]),
        day=str(row["day"]),
        sessions_considered=int(row["sessions_considered"]),
        sessions_reviewed=int(row["sessions_reviewed"]),
        findings_count=int(row["findings_count"]),
        error=str(row["error"] or ""),
    )


def start_run(*, day: str, sessions_considered: int, db_path: Path | None = None) -> int:
    with _conn(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO chat_review_runs (started_at, status, day, sessions_considered) "
            "VALUES (?, 'running', ?, ?)",
            (_iso(_now()), day, sessions_considered),
        )
        return int(cur.lastrowid or 0)


def claim_run(*, day: str, now: datetime, stale_after: timedelta, db_path: Path | None = None) -> tuple[int | None, str]:
    """Start today's run if none has: the check and the insert share one
    write transaction, so two schedulers waking together get one run between
    them. Returns ``(run_id, "")`` or ``(None, why)``. A run still marked
    running after ``stale_after`` is written off as failed and replaced."""
    with _conn(db_path) as conn:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            if conn.execute(
                "SELECT 1 FROM chat_review_runs WHERE day = ? AND status != 'failed' LIMIT 1", (day,)
            ).fetchone():
                conn.execute("ROLLBACK")
                return None, "already ran today"
            cutoff = _iso(now - stale_after)
            if conn.execute(
                "SELECT 1 FROM chat_review_runs WHERE status = 'running' AND started_at >= ? LIMIT 1",
                (cutoff,),
            ).fetchone():
                conn.execute("ROLLBACK")
                return None, "a run is in progress"
            conn.execute(
                "UPDATE chat_review_runs SET status = 'failed', finished_at = ?, error = 'did not finish' "
                "WHERE status = 'running'",
                (_iso(now),),
            )
            cur = conn.execute(
                "INSERT INTO chat_review_runs (started_at, status, day, sessions_considered) "
                "VALUES (?, 'running', ?, 0)",
                (_iso(now), day),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return int(cur.lastrowid or 0), ""


def set_sessions_considered(run_id: int, count: int, *, db_path: Path | None = None) -> None:
    with _conn(db_path) as conn:
        conn.execute("UPDATE chat_review_runs SET sessions_considered = ? WHERE id = ?", (int(count), run_id))


def finish_run(
    run_id: int,
    *,
    sessions_reviewed: int,
    status: str = "done",
    error: str = "",
    db_path: Path | None = None,
) -> None:
    with _conn(db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM chat_review_findings WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
        conn.execute(
            "UPDATE chat_review_runs SET finished_at = ?, status = ?, sessions_reviewed = ?, "
            "findings_count = ?, error = ? WHERE id = ?",
            (_iso(_now()), status, sessions_reviewed, int(count), error[:500], run_id),
        )


def running_run(db_path: Path | None = None) -> ReviewRun | None:
    """A run still marked running (another pass is on, or one died)."""
    if not _has_tables(db_path):
        return None
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM chat_review_runs WHERE status = 'running' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return _run_row(row) if row else None


def last_run_for_day(day: str, db_path: Path | None = None) -> ReviewRun | None:
    if not _has_tables(db_path):
        return None
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM chat_review_runs WHERE day = ? AND status != 'failed' "
            "ORDER BY id DESC LIMIT 1",
            (day,),
        ).fetchone()
    return _run_row(row) if row else None


def list_runs(*, limit: int = 30, db_path: Path | None = None) -> list[ReviewRun]:
    if not _has_tables(db_path):
        return []
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM chat_review_runs ORDER BY id DESC LIMIT ?", (max(1, limit),)
        ).fetchall()
    return [_run_row(r) for r in rows]


# --------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------


@dataclass
class Evidence:
    message_id: int
    quote: str = ""


@dataclass
class Finding:
    id: int
    run_id: int
    created_at: str
    session_id: str
    kind: str
    severity: str
    title: str
    what_happened: str
    evidence: list[Evidence] = field(default_factory=list)
    proposal: str = ""
    status: str = "proposed"
    resolved_at: str | None = None
    resolved_by_person_id: int | None = None
    resolution_note: str = ""
    lesson_id: int | None = None
    fact_id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _evidence_from_json(raw: str | None) -> list[Evidence]:
    try:
        items = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    out: list[Evidence] = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and "message_id" in item:
            try:
                out.append(Evidence(message_id=int(item["message_id"]), quote=str(item.get("quote", ""))))
            except (TypeError, ValueError):
                continue
    return out


def _finding_row(row: sqlite3.Row) -> Finding:
    return Finding(
        id=int(row["id"]),
        run_id=int(row["run_id"]),
        created_at=str(row["created_at"]),
        session_id=str(row["session_id"]),
        kind=str(row["kind"]),
        severity=str(row["severity"]),
        title=str(row["title"]),
        what_happened=str(row["what_happened"]),
        evidence=_evidence_from_json(row["evidence_json"]),
        proposal=str(row["proposal"] or ""),
        status=str(row["status"]),
        resolved_at=row["resolved_at"],
        resolved_by_person_id=row["resolved_by_person_id"],
        resolution_note=str(row["resolution_note"] or ""),
        lesson_id=row["lesson_id"],
        fact_id=row["fact_id"],
    )


def add_finding(
    *,
    run_id: int,
    session_id: str,
    kind: str,
    severity: str,
    title: str,
    what_happened: str,
    evidence: list[Evidence],
    proposal: str = "",
    status: str = "proposed",
    fact_id: int | None = None,
    db_path: Path | None = None,
) -> int:
    if kind not in FINDING_KINDS:
        raise ValueError(f"unknown finding kind {kind!r}")
    if severity not in SEVERITIES:
        severity = "medium"
    with _conn(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO chat_review_findings (run_id, created_at, session_id, kind, severity, "
            "title, what_happened, evidence_json, proposal, status, fact_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id, _iso(_now()), session_id, kind, severity, title[:200],
                what_happened[:2000], json.dumps([asdict(e) for e in evidence]),
                proposal[:1000], status, fact_id,
            ),
        )
        return int(cur.lastrowid or 0)


def get_finding(finding_id: int, db_path: Path | None = None) -> Finding | None:
    if not _has_tables(db_path):
        return None
    with _conn(db_path) as conn:
        row = conn.execute("SELECT * FROM chat_review_findings WHERE id = ?", (finding_id,)).fetchone()
    return _finding_row(row) if row else None


def list_findings(
    *,
    run_id: int | None = None,
    status: str | None = None,
    kind: str | None = None,
    limit: int = 200,
    db_path: Path | None = None,
) -> list[Finding]:
    if not _has_tables(db_path):
        return []
    where: list[str] = []
    args: list[Any] = []
    if run_id is not None:
        where.append("run_id = ?")
        args.append(run_id)
    if status is not None:
        where.append("status = ?")
        args.append(status)
    if kind is not None:
        where.append("kind = ?")
        args.append(kind)
    sql = "SELECT * FROM chat_review_findings"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, limit))
    with _conn(db_path) as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_finding_row(r) for r in rows]


def resolve_finding(
    finding_id: int,
    *,
    status: str,
    person_id: int | None,
    note: str = "",
    lesson_id: int | None = None,
    db_path: Path | None = None,
) -> Finding | None:
    """Record what the owner did. Only a ``proposed`` finding changes; the
    single conditional UPDATE is what makes two concurrent decisions on one
    finding resolve to exactly one. Returns None when nothing changed (the
    finding was missing or already decided)."""
    if status not in ("approved", "dismissed", "filed", "recorded"):
        raise ValueError(f"not a resolution: {status!r}")
    with _conn(db_path) as conn:
        cur = conn.execute(
            "UPDATE chat_review_findings SET status = ?, resolved_at = ?, resolved_by_person_id = ?, "
            "resolution_note = ?, lesson_id = COALESCE(?, lesson_id) "
            "WHERE id = ? AND status = 'proposed'",
            (status, _iso(_now()), person_id, note[:500], lesson_id, finding_id),
        )
        if cur.rowcount != 1:
            return None
    return get_finding(finding_id, db_path)


def attach_lesson(finding_id: int, lesson_id: int, *, db_path: Path | None = None) -> Finding | None:
    """Point an approved finding at the lesson made from it."""
    with _conn(db_path) as conn:
        conn.execute(
            "UPDATE chat_review_findings SET lesson_id = ? WHERE id = ? AND status = 'approved'",
            (lesson_id, finding_id),
        )
    return get_finding(finding_id, db_path)


def file_stale_proposals(*, days: int = FILE_AFTER_DAYS, db_path: Path | None = None) -> int:
    """File proposals older than ``days`` as not taken up. Returns the count."""
    if not _has_tables(db_path):
        return 0
    cutoff = _iso(_now() - timedelta(days=days))
    with _conn(db_path) as conn:
        cur = conn.execute(
            "UPDATE chat_review_findings SET status = 'filed', resolved_at = ?, "
            "resolution_note = 'not taken up' WHERE status = 'proposed' AND created_at < ?",
            (_iso(_now()), cutoff),
        )
        return int(cur.rowcount)


# --------------------------------------------------------------------------
# Lessons
# --------------------------------------------------------------------------


@dataclass
class Lesson:
    id: int
    text: str
    status: str
    locked: bool
    source_finding_id: int | None
    created_at: str
    updated_at: str
    updated_by: str
    approved_by_person_id: int | None
    retired_at: str | None = None
    retired_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _lesson_row(row: sqlite3.Row) -> Lesson:
    return Lesson(
        id=int(row["id"]),
        text=str(row["text"]),
        status=str(row["status"]),
        locked=bool(row["locked"]),
        source_finding_id=row["source_finding_id"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        updated_by=str(row["updated_by"]),
        approved_by_person_id=row["approved_by_person_id"],
        retired_at=row["retired_at"],
        retired_reason=str(row["retired_reason"] or ""),
    )


def _history(conn: sqlite3.Connection, lesson_id: int, text: str, status: str, by: str) -> None:
    conn.execute(
        "INSERT INTO executive_lesson_history (lesson_id, created_at, text, status, updated_by) "
        "VALUES (?, ?, ?, ?, ?)",
        (lesson_id, _iso(_now()), text, status, by),
    )


def create_lesson(
    text: str,
    *,
    source_finding_id: int | None,
    approved_by_person_id: int | None,
    updated_by: str,
    db_path: Path | None = None,
) -> Lesson:
    now = _iso(_now())
    with _conn(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO executive_lessons (text, status, source_finding_id, created_at, updated_at, "
            "updated_by, approved_by_person_id) VALUES (?, 'active', ?, ?, ?, ?, ?)",
            (text, source_finding_id, now, now, updated_by, approved_by_person_id),
        )
        lesson_id = int(cur.lastrowid or 0)
        _history(conn, lesson_id, text, "active", updated_by)
        row = conn.execute("SELECT * FROM executive_lessons WHERE id = ?", (lesson_id,)).fetchone()
    return _lesson_row(row)


def get_lesson(lesson_id: int, db_path: Path | None = None) -> Lesson | None:
    if not _has_tables(db_path):
        return None
    with _conn(db_path) as conn:
        row = conn.execute("SELECT * FROM executive_lessons WHERE id = ?", (lesson_id,)).fetchone()
    return _lesson_row(row) if row else None


def list_lessons(*, active_only: bool = False, db_path: Path | None = None) -> list[Lesson]:
    if not _has_tables(db_path):
        return []
    sql = "SELECT * FROM executive_lessons"
    if active_only:
        sql += " WHERE status = 'active'"
    sql += " ORDER BY id DESC"
    with _conn(db_path) as conn:
        rows = conn.execute(sql).fetchall()
    return [_lesson_row(r) for r in rows]


def update_lesson(
    lesson_id: int,
    *,
    text: str | None = None,
    locked: bool | None = None,
    updated_by: str,
    db_path: Path | None = None,
) -> Lesson | None:
    sets: list[str] = ["updated_at = ?", "updated_by = ?"]
    args: list[Any] = [_iso(_now()), updated_by]
    if text is not None:
        sets.append("text = ?")
        args.append(text)
    if locked is not None:
        sets.append("locked = ?")
        args.append(1 if locked else 0)
    args.append(lesson_id)
    with _conn(db_path) as conn:
        cur = conn.execute(
            f"UPDATE executive_lessons SET {', '.join(sets)} WHERE id = ? AND status = 'active'", args
        )
        if cur.rowcount != 1:
            # Missing or retired: a retired lesson is history, not a draft.
            return None
        row = conn.execute("SELECT * FROM executive_lessons WHERE id = ?", (lesson_id,)).fetchone()
        _history(conn, lesson_id, str(row["text"]), str(row["status"]), updated_by)
    return _lesson_row(row)


def reopen_finding(finding_id: int, *, db_path: Path | None = None) -> None:
    """Put an approved finding back to proposed when making its lesson failed."""
    with _conn(db_path) as conn:
        conn.execute(
            "UPDATE chat_review_findings SET status = 'proposed', resolved_at = NULL, "
            "resolved_by_person_id = NULL, resolution_note = '' WHERE id = ? AND status = 'approved' "
            "AND lesson_id IS NULL",
            (finding_id,),
        )


def retire_lesson(
    lesson_id: int, *, reason: str, updated_by: str, db_path: Path | None = None
) -> Lesson | None:
    now = _iso(_now())
    with _conn(db_path) as conn:
        conn.execute(
            "UPDATE executive_lessons SET status = 'retired', retired_at = ?, retired_reason = ?, "
            "updated_at = ?, updated_by = ? WHERE id = ? AND status = 'active'",
            (now, reason[:300], now, updated_by, lesson_id),
        )
        row = conn.execute("SELECT * FROM executive_lessons WHERE id = ?", (lesson_id,)).fetchone()
        if row is None:
            return None
        _history(conn, lesson_id, str(row["text"]), str(row["status"]), updated_by)
    return _lesson_row(row)


# --------------------------------------------------------------------------
# Summary for the screen
# --------------------------------------------------------------------------


def week_summary(db_path: Path | None = None) -> dict[str, int]:
    """Counts for the top of the Reflection screen, over the last seven days."""
    if not _has_tables(db_path):
        return {"conversations_reviewed": 0, "findings": 0, "lessons_active": 0, "waiting": 0}
    since = _iso(_now() - timedelta(days=7))
    with _conn(db_path) as conn:
        reviewed = conn.execute(
            "SELECT COALESCE(SUM(sessions_reviewed), 0) FROM chat_review_runs WHERE started_at >= ?",
            (since,),
        ).fetchone()[0]
        findings = conn.execute(
            "SELECT COUNT(*) FROM chat_review_findings WHERE created_at >= ?", (since,)
        ).fetchone()[0]
        active = conn.execute(
            "SELECT COUNT(*) FROM executive_lessons WHERE status = 'active'"
        ).fetchone()[0]
        waiting = conn.execute(
            "SELECT COUNT(*) FROM chat_review_findings WHERE status = 'proposed'"
        ).fetchone()[0]
    return {
        "conversations_reviewed": int(reviewed),
        "findings": int(findings),
        "lessons_active": int(active),
        "waiting": int(waiting),
    }
