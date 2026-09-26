"""SQLite-backed audit log writer + reader.

Design notes:
- Stores into the same `episodic_memory.db` SQLite file used by alerts/episodic
  memory so there is one place to look. New table only — no schema migrations
  for existing tables.
- `summary` is the searchable, human-readable column (max ~300 chars). It is
  what users grep for in the UI. `details_json` carries opaque structured
  fields (token counts, durations, channel refs) and is not searched.
- Writes swallow exceptions: an audit failure must never break a chat turn or
  a tool call. We log a warning and move on.
- A row can be private to the principal (`private_to_principal`): every row
  a turn about their private mail writes, and a row on their own turn that
  names one of their contacts (`people_tools.audit_row_private_to_principal`,
  decided at write time — the `audit.context` scopes included, for rows
  written before a turn binds its session), plus any row a caller marks
  `private=True`. The read API (`api.routes.audit`) leaves those rows out
  for anyone else.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DB_PATH = Path(os.environ.get("EPISODIC_DB_PATH") or "./episodic_memory.db")

# Cap summaries so a runaway tool input/output doesn't bloat the index.
_SUMMARY_MAX_LEN = 300

# Cap stored JSON so detail blobs stay reasonable in size.
_DETAILS_MAX_LEN = 4000

# Cap the un-truncated drill-down payload. memory_snapshot can carry whole
# episodic context dumps and knowledge_retrieval can carry many chunk
# previews — without a cap an active user generates GB-scale audit rows.
# 64 KB per row keeps a year of high-volume usage under a couple GB while
# still being large enough to capture realistic per-turn context.
_FULL_MAX_LEN = 64 * 1024

# Recognised event types — kept as a constant for validation and UI filter
# rendering. New types can be added without a migration, but they should be
# listed here so consumers know what to expect.
EVENT_TYPES: tuple[str, ...] = (
    "chat_turn",
    "specialist_consult",
    "tool_invocation",
    "scheduled_action",
    "alert",
    "integration_inbound",
    "auth_login",
    "auth_logout",
    # Instrumentation events — surface what the agent saw on each turn so
    # /audit/session/{id} can render a flow chart with the full context.
    "knowledge_retrieval",  # RAG retrieve() — chunks, scores, source files
    "cache_event",          # per Anthropic-call token usage + cache stats
    "memory_snapshot",      # episodic context + company profile at turn entry
    "committee_review",     # committee-reviewed draft + critiques (pre-existing emit, now declared)
    "peer_memory",          # Honcho per-person memory — prefetch + sync_turn outcomes
    "memory_extraction",    # episodic extractor — proposed / stored / dropped per pass
    "attunement",           # open loops opened / closed / expired, 👍/👎 on replies
    "executive_paused",     # operator paused autonomous work (scheduler/pause.py)
    "executive_resumed",    # operator resumed it; held work released
    "scheduled_action_cancelled",  # a person cancelled a pending action (DELETE /scheduled/{id})
    "workspace_settings_changed",  # solo/team mode, the user's time zone or the principal's role changed (PUT /workspace)
    "decision_class_mode_changed",  # propose ↔ auto_execute for a decision class (PUT /decisions/classes/…)
    # Act as me (delegation/) — every one written private to the principal.
    "delegation_settings_changed",  # a person turned Act as me on or off (PUT /delegation)
    "delegation_gmail_verified",    # a person's own Gmail was checked and found usable when turning it on
    "delegation_voice_changed",     # "How I write" learned, edited, locked, reset or its signature re-read
    "delegation_drafted",           # ghostwrite_email saved a draft in a person's own Gmail
)


@dataclass(slots=True)
class AuditEvent:
    """One row in the audit_log table."""

    id: int
    ts: str
    event_type: str
    session_id: str | None
    turn_id: str | None
    actor: str | None
    summary: str
    details: dict[str, Any] = field(default_factory=dict)
    # Un-truncated drill-down payload. Only populated by `get(id)`; list/query
    # paths leave this as None so the scan view stays small over the wire.
    full: dict[str, Any] | None = None
    # Department slug owning this event. Added by the Departments feature
    # (Phase 1) so per-department check-ins can filter audit history.
    department: str | None = None
    # Readable by the principal alone (see the module docstring).
    private: bool = False


@contextmanager
def _get_conn(db_path: Path) -> Generator[sqlite3.Connection, None, None]:
    # 5s busy_timeout + WAL so concurrent writers (FastAPI worker, scheduler
    # task, email poller task, Slack/Telegram webhook threads) don't hit
    # "database is locked". Audit writes swallow exceptions, so silent loss
    # under contention would be undetectable.
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _truncate(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "…"


def _escape_like(s: str) -> str:
    """Escape LIKE metacharacters so user input can't match wildcards."""
    return s.replace("\\", r"\\").replace("%", r"\%").replace("_", r"\_")


def _row_to_event(row: sqlite3.Row) -> AuditEvent:
    raw_details = row["details_json"] or "{}"
    try:
        details = json.loads(raw_details)
        if not isinstance(details, dict):
            details = {"value": details}
    except (ValueError, TypeError):
        details = {"raw": raw_details}
    # `department` is an additive column on older DBs. Index access raises
    # IndexError if it was never SELECTed; use a guard so old call sites
    # (the LIST query, which still SELECTs explicit columns) keep working.
    try:
        department: str | None = row["department"]
    except (IndexError, KeyError):
        department = None
    try:
        private = bool(row["private_to_principal"])
    except (IndexError, KeyError):
        private = False
    return AuditEvent(
        id=int(row["id"]),
        ts=str(row["ts"]),
        event_type=str(row["event_type"]),
        session_id=row["session_id"],
        turn_id=row["turn_id"],
        actor=row["actor"],
        summary=str(row["summary"]),
        details=details,
        department=department,
        private=private,
    )


def _private_to_principal(
    summary: str, details: dict[str, Any] | None, full: dict[str, Any] | None
) -> bool:
    """Whether the row being written is the principal's alone — see
    ``people_tools.audit_row_private_to_principal``. Fails closed: a check
    that cannot run keeps the row to the principal rather than show it to
    everyone."""
    try:
        from openexecutive.orchestrator.people_tools import audit_row_private_to_principal

        return audit_row_private_to_principal(summary, details, full)
    except Exception:
        logger.warning("audit.private_check_failed — row kept to the principal", exc_info=True)
        return True


# Token + cost fields summed by usage_summary(). Tokens are integer counts;
# cost is fractional USD. Kept together so totals/by_day/by_model stay aligned.
_USAGE_INT_FIELDS: tuple[str, ...] = (
    "calls",
    "input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "output_tokens",
    "web_search_requests",
)


def _zero_usage() -> dict[str, float | int]:
    return {**{k: 0 for k in _USAGE_INT_FIELDS}, "cost_usd": 0.0}


def _row_to_usage(row: sqlite3.Row) -> dict[str, float | int]:
    # On an empty match the totals query still returns one row (COUNT(*)=0) but
    # every SUM(...) column is SQL NULL — the `or 0` / `or 0.0` coalescing is
    # what zeroes those out, so this stays correct with no caller-side None.
    out: dict[str, float | int] = {k: int(row[k] or 0) for k in _USAGE_INT_FIELDS}
    out["cost_usd"] = float(row["cost_usd"] or 0.0)
    return out


# SUM expression shared by every usage_summary() query. Token fields cast to
# INTEGER and cost to REAL; COALESCE(..., 0) tolerates NULL/garbled JSON the
# same way _compute_cost_summary's _as_int does in the per-session path.
_USAGE_SUM_COLS = """
    COUNT(*) AS calls,
    SUM(COALESCE(CAST(json_extract(details_json,'$.input_tokens') AS INTEGER),0)) AS input_tokens,
    SUM(COALESCE(CAST(json_extract(details_json,'$.cache_read_input_tokens') AS INTEGER),0)) AS cache_read_input_tokens,
    SUM(COALESCE(CAST(json_extract(details_json,'$.cache_creation_input_tokens') AS INTEGER),0)) AS cache_creation_input_tokens,
    SUM(COALESCE(CAST(json_extract(details_json,'$.output_tokens') AS INTEGER),0)) AS output_tokens,
    SUM(COALESCE(CAST(json_extract(details_json,'$.web_search_requests') AS INTEGER),0)) AS web_search_requests,
    SUM(COALESCE(CAST(json_extract(details_json,'$.cost_usd') AS REAL),0)) AS cost_usd
"""


class AuditLogger:
    """Synchronous SQLite writer/reader for audit events.

    Safe to call from async code: SQLite writes here are sub-millisecond and
    don't justify the complexity of a thread/queue. If a write fails, we log
    and return — auditing never blocks or breaks the caller.
    """

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self._db_path = db_path
        self.initialize_db()

    def initialize_db(self) -> None:
        with _get_conn(self._db_path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    session_id TEXT,
                    turn_id TEXT,
                    actor TEXT,
                    summary TEXT NOT NULL,
                    details_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts DESC);
                CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_log(session_id);
                CREATE INDEX IF NOT EXISTS idx_audit_type ON audit_log(event_type);
            """)
            # Additive migrations. PRAGMA-guarded so a second boot is a no-op
            # (no migration tooling in this repo). Concurrent workers can race
            # the ALTER; SQLite raises OperationalError("duplicate column name")
            # on the loser — treat as success.
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(audit_log)")}
            for column, ddl in (
                ("full_json", "ALTER TABLE audit_log ADD COLUMN full_json TEXT"),
                # Department tag — added by the Departments feature (Phase 1).
                ("department", "ALTER TABLE audit_log ADD COLUMN department TEXT"),
                # 1 = readable by the principal alone (see the module docstring).
                (
                    "private_to_principal",
                    "ALTER TABLE audit_log ADD COLUMN private_to_principal "
                    "INTEGER NOT NULL DEFAULT 0",
                ),
            ):
                if column in cols:
                    continue
                try:
                    conn.execute(ddl)
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise

    def log(
        self,
        event_type: str,
        summary: str,
        *,
        session_id: str | None = None,
        turn_id: str | None = None,
        actor: str | None = None,
        details: dict[str, Any] | None = None,
        full: dict[str, Any] | None = None,
        department: str | None = None,
        private: bool = False,
    ) -> int | None:
        """Insert one audit row. Returns row id, or None on failure.

        `full` is an un-truncated drill-down payload (entire user messages,
        full tool inputs/results, full specialist queries). It is never
        returned by the list/query path — only by `get(id)`.

        `department` tags the row with the owning department slug so
        per-department check-ins can filter audit history by `department=`.

        `private=True` marks the row the principal's alone. Without it the
        row is still private when the turn writing it is (see
        `_private_to_principal`) — a caller can add privacy, never remove it.
        """
        try:
            is_private = bool(private) or _private_to_principal(summary, details, full)
            safe_summary = _truncate(str(summary), _SUMMARY_MAX_LEN)
            details_json: str | None = None
            if details:
                try:
                    details_json = json.dumps(details, default=str)
                except (TypeError, ValueError):
                    details_json = json.dumps({"repr": repr(details)})
                if len(details_json) > _DETAILS_MAX_LEN:
                    details_json = json.dumps({
                        "truncated": True,
                        "preview": _truncate(details_json, _DETAILS_MAX_LEN),
                    })
            full_json: str | None = None
            if full:
                try:
                    full_json = json.dumps(full, default=str)
                except Exception:
                    # `full` is opaque structured data from tool results /
                    # MCP responses — it can be cyclic, contain objects whose
                    # repr() itself raises, or trigger RecursionError. None
                    # of those should ever break the calling chat turn.
                    try:
                        full_json = json.dumps({"repr": repr(full)})
                    except Exception:
                        full_json = json.dumps({"unserializable": True})
                if full_json is not None and len(full_json) > _FULL_MAX_LEN:
                    # Preserve a usable head + flag truncation so the UI can
                    # render the drawer without choking on a multi-MB blob.
                    full_json = json.dumps({
                        "truncated": True,
                        "original_size": len(full_json),
                        "preview": full_json[: _FULL_MAX_LEN - 200],
                    })
            ts_value = _now()
            with _get_conn(self._db_path) as conn:
                cur = conn.execute(
                    """
                    INSERT INTO audit_log
                        (ts, event_type, session_id, turn_id, actor, summary, details_json,
                         full_json, department, private_to_principal)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ts_value,
                        event_type,
                        session_id,
                        turn_id,
                        actor,
                        safe_summary,
                        details_json,
                        full_json,
                        department,
                        1 if is_private else 0,
                    ),
                )
                row_id = int(cur.lastrowid or 0)
            return row_id
        except Exception:
            logger.warning("audit.log_failed event_type=%s", event_type, exc_info=True)
            return None

    def get(self, event_id: int) -> AuditEvent | None:
        """Fetch a single row by id, including the un-truncated `full` payload."""
        if not self._db_path.exists():
            return None
        try:
            with _get_conn(self._db_path) as conn:
                row = conn.execute(
                    "SELECT id, ts, event_type, session_id, turn_id, actor, summary, "
                    "details_json, full_json, department, private_to_principal "
                    "FROM audit_log WHERE id = ?",
                    (event_id,),
                ).fetchone()
        except Exception:
            logger.warning("audit.get_failed id=%s", event_id, exc_info=True)
            return None
        if row is None:
            return None
        event = _row_to_event(row)
        raw_full = row["full_json"]
        if raw_full:
            try:
                parsed = json.loads(raw_full)
                event.full = parsed if isinstance(parsed, dict) else {"value": parsed}
            except (ValueError, TypeError):
                event.full = {"raw": raw_full}
        return event

    def query(
        self,
        *,
        event_type: str | None = None,
        session_id: str | None = None,
        actor: str | None = None,
        q: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 100,
        offset: int = 0,
        department: str | None = None,
        include_private: bool = True,
    ) -> list[AuditEvent]:
        """Rows matching every filter given, newest first. ``include_private``
        False leaves out the rows private to the principal — filtered in SQL,
        so a page is still full and ``count`` agrees with it."""
        limit = max(1, min(limit, 1000))
        offset = max(0, offset)

        clauses: list[str] = []
        params: list[Any] = []
        if not include_private:
            clauses.append("private_to_principal = 0")
        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type)
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        if actor:
            clauses.append("actor = ?")
            params.append(actor)
        if department:
            # Filtered in SQL, not over the newest page: department rows are a
            # sliver of the log, so a Python filter over `limit` rows misses
            # them on a busy day (the department check-in's skip rule).
            clauses.append("department = ?")
            params.append(department)
        if since:
            clauses.append("ts >= ?")
            params.append(since)
        if until:
            clauses.append("ts <= ?")
            params.append(until)
        if q:
            clauses.append(r"summary LIKE ? ESCAPE '\'")
            params.append(f"%{_escape_like(q)}%")

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "SELECT id, ts, event_type, session_id, turn_id, actor, summary, details_json, "
            f"department, private_to_principal FROM audit_log {where} "
            "ORDER BY id DESC LIMIT ? OFFSET ?"
        )
        params.extend([limit, offset])

        if not self._db_path.exists():
            return []
        with _get_conn(self._db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_event(r) for r in rows]

    def count(
        self,
        *,
        event_type: str | None = None,
        session_id: str | None = None,
        actor: str | None = None,
        q: str | None = None,
        since: str | None = None,
        until: str | None = None,
        include_private: bool = True,
    ) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if not include_private:
            clauses.append("private_to_principal = 0")
        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type)
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        if actor:
            clauses.append("actor = ?")
            params.append(actor)
        if since:
            clauses.append("ts >= ?")
            params.append(since)
        if until:
            clauses.append("ts <= ?")
            params.append(until)
        if q:
            clauses.append(r"summary LIKE ? ESCAPE '\'")
            params.append(f"%{_escape_like(q)}%")

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        if not self._db_path.exists():
            return 0
        with _get_conn(self._db_path) as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM audit_log {where}", params
            ).fetchone()
        return int(row["n"]) if row else 0

    def usage_summary(
        self,
        *,
        since: str | None = None,
        until: str | None = None,
        include_private: bool = True,
    ) -> dict[str, Any]:
        """Aggregate token usage + cost from `cache_event` rows over an optional
        time window. Grouping is done in SQL (`json_extract`) so this scales past
        the 1000-row cap on `query()` — a busy DB can have far more cache_event
        rows than that. Returns totals plus by-day, by-model and by-source
        breakdowns (source = the row's `actor`: executive, specialist_research,
        research_synthesis, triage, …; rows without one group as `unknown`).

        Each `cache_event` row carries the per-call token breakdown and (for
        OpenRouter calls) the actual `cost_usd`; missing/garbled fields coalesce
        to 0, and rows that predate cost capture contribute 0 cost. `since`/
        `until` bound the ISO `ts` column with the same string comparison used by
        `query()`/`count()`, and ``include_private`` False leaves out the rows
        private to the principal, as there.
        """
        empty: dict[str, Any] = {
            "totals": _zero_usage(), "by_day": [], "by_model": [], "by_source": [],
        }
        if not self._db_path.exists():
            return empty

        clauses = ["event_type = 'cache_event'"]
        params: list[Any] = []
        if not include_private:
            clauses.append("private_to_principal = 0")
        if since:
            clauses.append("ts >= ?")
            params.append(since)
        if until:
            clauses.append("ts <= ?")
            params.append(until)
        where = "WHERE " + " AND ".join(clauses)

        with _get_conn(self._db_path) as conn:
            totals_row = conn.execute(
                f"SELECT {_USAGE_SUM_COLS} FROM audit_log {where}", params
            ).fetchone()
            # substr(ts,1,10) = the YYYY-MM-DD (UTC) prefix of the ISO timestamp.
            by_day_rows = conn.execute(
                f"SELECT substr(ts,1,10) AS day, {_USAGE_SUM_COLS} "
                f"FROM audit_log {where} GROUP BY day ORDER BY day ASC",
                params,
            ).fetchall()
            by_model_rows = conn.execute(
                "SELECT COALESCE(json_extract(details_json,'$.model'),'unknown') AS model, "
                f"{_USAGE_SUM_COLS} FROM audit_log {where} "
                "GROUP BY model ORDER BY input_tokens DESC",
                params,
            ).fetchall()
            by_source_rows = conn.execute(
                f"SELECT COALESCE(actor,'unknown') AS source, {_USAGE_SUM_COLS} "
                f"FROM audit_log {where} GROUP BY source ORDER BY input_tokens DESC",
                params,
            ).fetchall()

        return {
            "totals": _row_to_usage(totals_row),
            "by_day": [{"day": r["day"], **_row_to_usage(r)} for r in by_day_rows],
            "by_model": [{"model": r["model"], **_row_to_usage(r)} for r in by_model_rows],
            "by_source": [
                {"source": r["source"], **_row_to_usage(r)} for r in by_source_rows
            ],
        }


# --------------------------------------------------------------------------- #
# Module-level singleton so non-FastAPI call sites (Executive, schedule tools,
# integration handlers) can log without threading an instance around.
# --------------------------------------------------------------------------- #

_default_logger: AuditLogger | None = None


def set_audit_logger(instance: AuditLogger | None) -> None:
    global _default_logger
    _default_logger = instance


def get_audit_logger() -> AuditLogger:
    global _default_logger
    if _default_logger is None:
        _default_logger = AuditLogger()
    return _default_logger


def log_event(
    event_type: str,
    summary: str,
    *,
    session_id: str | None = None,
    turn_id: str | None = None,
    actor: str | None = None,
    details: dict[str, Any] | None = None,
    full: dict[str, Any] | None = None,
    department: str | None = None,
    private: bool = False,
) -> None:
    """Fire-and-forget convenience wrapper around the default logger.

    When ``session_id`` / ``turn_id`` aren't passed explicitly, fall back to
    the active audit ContextVars (set via ``set_turn`` / ``bind_turn`` at
    the start of every chat turn). Without this fallback, ad-hoc emitters
    that don't thread the ids themselves — e.g. ``honcho_client``'s
    ``_emit_peer_memory`` — produce rows with ``session_id=NULL``, which
    are then invisible in the session-grouped audit view.
    """
    if session_id is None or turn_id is None:
        from openexecutive.audit.context import get_active_ids
        ctx_session, ctx_turn = get_active_ids()
        if session_id is None:
            session_id = ctx_session
        if turn_id is None:
            turn_id = ctx_turn
    try:
        get_audit_logger().log(
            event_type,
            summary,
            session_id=session_id,
            turn_id=turn_id,
            actor=actor,
            details=details,
            full=full,
            department=department,
            private=private,
        )
    except Exception:
        logger.warning("audit.log_event_failed event_type=%s", event_type, exc_info=True)
