"""SQLite persistence for the external-condition monitoring layer.

Two tables live in the existing ``episodic_memory.db`` (same file as
``decisions``, ``alerts`` etc.) so a single DB path keeps fixtures and
backups simple:

- ``watchlist`` — the set of external conditions the user wants
  monitored. Mutable per-row state (last_polled_at, last_fired_at,
  fired_count, dismiss_count, trust_score) lives here too so the
  tuning loop in PR-B/PR-C can update entries cheaply.
- ``external_signals`` — the audit trail of every signal an adapter
  emitted. Persisting suppressed signals is intentional: it lets the
  briefing layer answer "what did you ignore overnight" and gives the
  trust loop a denominator for hit-rate metrics.

Schema is created via ``initialize_db()`` called from the FastAPI
lifespan AFTER ``episodic.initialize_db`` (both target the same file;
CREATE TABLE IF NOT EXISTS is order-independent but the alerts-table
side benefits from a deterministic boot order — see api/main.py).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openexecutive.alerts.models import AlertSeverity
from openexecutive.memory.episodic import DB_PATH, _resolve_db_path
from openexecutive.monitoring.models import (
    MODE_ACTIVE,
    OUTCOME_SUPPRESSED_BASELINE,
    Signal,
    WatchlistItem,
    is_valid_mode,
    is_valid_outcome,
)

logger = logging.getLogger(__name__)


@contextmanager
def _get_conn(db_path: Path | None = None) -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(str(_resolve_db_path(db_path)))
    conn.row_factory = sqlite3.Row
    # SQLite ships with foreign-key enforcement OFF by default. Without
    # this PRAGMA the FK on external_signals.watchlist_id is just
    # documentation; with it, inserting a signal for a deleted/unknown
    # watchlist fails loudly. Per-connection setting — must run on every
    # acquisition.
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def initialize_db(db_path: Path | None = None) -> None:
    """Create the watchlist + external_signals tables. Idempotent."""
    with _get_conn(db_path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS watchlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT NOT NULL UNIQUE,
                signal_type TEXT NOT NULL,
                target TEXT NOT NULL,
                config_json TEXT NOT NULL DEFAULT '{}',
                trigger_json TEXT NOT NULL DEFAULT '{}',
                cadence TEXT NOT NULL DEFAULT '15min',
                severity_floor TEXT NOT NULL DEFAULT 'low',
                severity_ceiling TEXT NOT NULL DEFAULT 'urgent',
                route_to_specialist TEXT NOT NULL DEFAULT '',
                route_to_department TEXT NOT NULL DEFAULT '',
                route_to_person_id INTEGER,
                mode TEXT NOT NULL DEFAULT 'active',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                last_polled_at TEXT,
                baselined_at TEXT,
                last_fired_at TEXT,
                fired_count INTEGER NOT NULL DEFAULT 0,
                dismiss_count INTEGER NOT NULL DEFAULT 0,
                trust_score REAL NOT NULL DEFAULT 1.0,
                notes TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_watchlist_enabled_type
                ON watchlist(enabled, signal_type);

            CREATE TABLE IF NOT EXISTS external_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                watchlist_id INTEGER NOT NULL REFERENCES watchlist(id),
                source_kind TEXT NOT NULL,
                source_external_id TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                published_at TEXT,
                normalized_summary TEXT NOT NULL,
                raw_payload_json TEXT NOT NULL DEFAULT '{}',
                provenance_url TEXT NOT NULL,
                severity_hint TEXT NOT NULL DEFAULT 'low',
                dedup_key TEXT NOT NULL UNIQUE,
                processed_at TEXT,
                processed_outcome TEXT,
                promoted_alert_id INTEGER,
                enrichment_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_signals_captured
                ON external_signals(captured_at DESC);
            CREATE INDEX IF NOT EXISTS idx_signals_watchlist
                ON external_signals(watchlist_id, captured_at DESC);

            -- Per-watchlist-row state for the stateful page_watch adapter:
            -- the last-seen normalized content hash + a snapshot used to
            -- summarise the diff on the next change. Keyed by the watchlist
            -- slug (its stable UNIQUE identifier).
            CREATE TABLE IF NOT EXISTS page_watch_state (
                slug TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL,
                text_snapshot TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );

            -- One row per one-time data migration that has been applied.
            -- There is no migration framework in this repo, and column
            -- guards (_ensure_column) only work for migrations that add a
            -- column; a pure data fix-up needs its own marker to run
            -- exactly once. See _apply_once.
            CREATE TABLE IF NOT EXISTS monitoring_migrations (
                name TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
        """)
        # Additive migration for DBs created before enrichment landed.
        # ``CREATE TABLE IF NOT EXISTS`` above is a no-op on an existing
        # table, so a new column has to be ALTERed in explicitly. There is
        # no migration framework in this repo; guard on table_info so the
        # ALTER runs exactly once and initialize_db stays idempotent.
        _ensure_column(
            conn, "external_signals", "enrichment_json",
            "TEXT NOT NULL DEFAULT '{}'",
        )
        # Upstream publish timestamp (issue #80). Nullable: pre-existing rows
        # and sources without an upstream timestamp carry NULL.
        _ensure_column(conn, "external_signals", "published_at", "TEXT")
        # The ALTER and its one-time backfill must land together: the
        # sqlite3 driver autocommits DDL, so without an explicit BEGIN a
        # crash between the two would leave the column present (so the
        # backfill never re-runs) but every already-polled row un-
        # baselined — and the next tick would swallow their new entries.
        # Only open a write transaction when there is actually something to
        # migrate: the steady-state boot must not contend for the write lock
        # on the shared episodic_memory.db (a 5s busy timeout would abort
        # startup). IMMEDIATE takes the lock up front, so the PRAGMA re-check
        # inside _ensure_column sees a settled schema even if another
        # initializer raced us to the ALTER.
        if not _has_column(conn, "watchlist", "baselined_at"):
            _migrate_baselined_at(conn)
        # Add future migrations BELOW, not inside the guard above.
        _migrate_vendor_status_rekey(conn)


def _migrate_baselined_at(conn: sqlite3.Connection) -> None:
    """Add ``watchlist.baselined_at`` and backfill it, atomically."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        if _ensure_column(conn, "watchlist", "baselined_at", "TEXT"):
            # Rows polled before this column existed have already surfaced
            # whatever their feed held; treat them as baselined so the
            # upgrade tick doesn't swallow genuinely new entries.
            conn.execute(
                "UPDATE watchlist SET baselined_at = last_polled_at "
                "WHERE baselined_at IS NULL AND last_polled_at IS NOT NULL"
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


_MIGRATION_VENDOR_STATUS_REKEY = "vendor_status_dedup_rekey_90"


def _apply_once(conn: sqlite3.Connection, name: str) -> bool:
    """Claim a one-time data migration. True only for the caller that won.

    Must be called INSIDE the caller's transaction so the claim and the
    migration's own writes commit or roll back together — a marker without
    its data change would skip the migration for good.
    """
    claimed = conn.execute(
        "INSERT OR IGNORE INTO monitoring_migrations (name, applied_at) "
        "VALUES (?, ?)",
        (name, datetime.now(UTC).isoformat()),
    )
    return claimed.rowcount > 0


def _migrate_vendor_status_rekey(conn: sqlite3.Connection) -> None:
    """One-time: let ``vendor_status`` rows re-seed after the #90 rekey.

    ``vendor_status`` dedup keys used to be ``sha256(slug, entry id)`` and
    are now ``sha256(slug, entry id, <updated>)``, so on the upgrade tick
    every entry in a vendor's history feed hashes to a key the DB has
    never seen. For rows already stamped ``baselined_at`` (the #80
    backfill stamped every previously-polled row) that would replay the
    archive as HIGH alerts — the very bug #90 is about, just once.

    Clearing the stamp makes that tick a first poll under the new scheme:
    the resolved archive is recorded as ``suppressed_baseline`` and only
    incidents that are still open are promoted. Signal rows are left
    alone — the old keys are inert, and deleting them would lose the
    audit trail of what the row surfaced before the upgrade.
    """
    already = conn.execute(
        "SELECT 1 FROM monitoring_migrations WHERE name = ?",
        (_MIGRATION_VENDOR_STATUS_REKEY,),
    ).fetchone()
    if already is not None:
        # Steady-state boot: no write transaction, so startup never
        # contends for the write lock on the shared episodic_memory.db.
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        if _apply_once(conn, _MIGRATION_VENDOR_STATUS_REKEY):
            reset = conn.execute(
                "UPDATE watchlist SET baselined_at = NULL "
                "WHERE signal_type = 'vendor_status' AND baselined_at IS NOT NULL"
            )
            if reset.rowcount:
                logger.info(
                    "monitoring.store: re-seeding %d vendor_status row(s) after "
                    "the #90 dedup rekey", reset.rowcount,
                )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _ensure_column(
    conn: sqlite3.Connection, table: str, column: str, decl: str
) -> bool:
    """Add ``column`` to ``table`` if it isn't already present. Idempotent.

    Returns True when the column was added on this call, so a caller can
    run a one-time backfill exactly once.

    Table + column names are internal constants (never user input), so the
    f-string interpolation here carries no injection surface.
    """
    if _has_column(conn, table, column):
        return False
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    except sqlite3.OperationalError as exc:
        # Another initializer (a CLI run, an overlapping deploy) added the
        # column between our PRAGMA check and the ALTER. That's the same
        # end state as "already present" — and it must not abort boot.
        if "duplicate column" in str(exc).lower():
            return False
        raise
    return True


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    return column in cols


# --------------------------------------------------------------------- #
# Watchlist CRUD
# --------------------------------------------------------------------- #


def _row_to_watchlist(row: sqlite3.Row) -> WatchlistItem:
    d = dict(row)
    d["config_json"] = json.loads(d.get("config_json") or "{}")
    d["trigger_json"] = json.loads(d.get("trigger_json") or "{}")
    d["enabled"] = bool(d.get("enabled", 1))
    # Pydantic v2 coerces "low"/"medium"/... into the enum via the
    # AlertSeverity StrEnum on the model — no manual cast needed.
    return WatchlistItem(**d)


def insert_watchlist_item(
    *,
    slug: str,
    signal_type: str,
    target: str,
    config: dict[str, Any] | None = None,
    trigger: dict[str, Any] | None = None,
    cadence: str = "15min",
    severity_floor: AlertSeverity = AlertSeverity.LOW,
    severity_ceiling: AlertSeverity = AlertSeverity.URGENT,
    route_to_specialist: str = "",
    route_to_department: str = "",
    route_to_person_id: int | None = None,
    mode: str = MODE_ACTIVE,
    enabled: bool = True,
    notes: str = "",
    db_path: Path | None = None,
) -> int:
    """Insert a new watchlist row. Returns its id.

    Raises ``ValueError`` for unknown ``mode`` so callers fail loudly
    rather than silently storing a value the pipeline can't interpret.
    """
    if not is_valid_mode(mode):
        raise ValueError(f"Unknown watchlist mode {mode!r}")
    now = datetime.now(UTC).isoformat()
    with _get_conn(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO watchlist "
            "(slug, signal_type, target, config_json, trigger_json, cadence, "
            "severity_floor, severity_ceiling, route_to_specialist, "
            "route_to_department, route_to_person_id, mode, enabled, "
            "created_at, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                slug,
                signal_type,
                target,
                json.dumps(config or {}),
                json.dumps(trigger or {}),
                cadence,
                severity_floor.value if isinstance(severity_floor, AlertSeverity)
                else str(severity_floor),
                severity_ceiling.value if isinstance(severity_ceiling, AlertSeverity)
                else str(severity_ceiling),
                route_to_specialist,
                route_to_department,
                route_to_person_id,
                mode,
                1 if enabled else 0,
                now,
                notes,
            ),
        )
        return int(cursor.lastrowid or 0)


def get_watchlist_item(
    item_id: int, db_path: Path | None = None
) -> WatchlistItem | None:
    resolved = _resolve_db_path(db_path)
    if not resolved.exists():
        return None
    with _get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM watchlist WHERE id = ?", (item_id,)
        ).fetchone()
    return _row_to_watchlist(row) if row else None


def get_watchlist_item_by_slug(
    slug: str, db_path: Path | None = None
) -> WatchlistItem | None:
    resolved = _resolve_db_path(db_path)
    if not resolved.exists():
        return None
    with _get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM watchlist WHERE slug = ?", (slug,)
        ).fetchone()
    return _row_to_watchlist(row) if row else None


def list_watchlist(
    *,
    enabled_only: bool = False,
    signal_type: str | None = None,
    db_path: Path | None = None,
) -> list[WatchlistItem]:
    resolved = _resolve_db_path(db_path)
    if not resolved.exists():
        return []
    clauses: list[str] = []
    params: list[Any] = []
    if enabled_only:
        clauses.append("enabled = 1")
    if signal_type is not None:
        clauses.append("signal_type = ?")
        params.append(signal_type)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM watchlist {where} ORDER BY id",
            params,
        ).fetchall()
    return [_row_to_watchlist(row) for row in rows]


def mark_polled(
    item_id: int,
    at: datetime,
    db_path: Path | None = None,
) -> None:
    with _get_conn(db_path) as conn:
        conn.execute(
            "UPDATE watchlist SET last_polled_at = ? WHERE id = ?",
            (at.isoformat(), item_id),
        )


def mark_fired(
    item_id: int,
    at: datetime,
    db_path: Path | None = None,
) -> None:
    """Bump fired_count + last_fired_at after a signal was promoted to alert."""
    with _get_conn(db_path) as conn:
        conn.execute(
            "UPDATE watchlist "
            "SET last_fired_at = ?, fired_count = fired_count + 1 "
            "WHERE id = ?",
            (at.isoformat(), item_id),
        )


_ALLOWED_UPDATE_COLUMNS: frozenset[str] = frozenset({
    "enabled",
    "mode",
    "severity_floor",
    "severity_ceiling",
    "cadence",
    "trigger_json",
    "notes",
    "config_json",
    "route_to_specialist",
})


def update_watchlist_fields(
    item_id: int,
    fields: dict[str, Any],
    *,
    db_path: Path | None = None,
) -> int:
    """Apply a single-transaction UPDATE to one watchlist row.

    ``fields`` keys are validated against an allowlist so a buggy
    caller cannot smuggle a column name into the dynamic SQL. Values
    are always passed as bound parameters. Returns the rowcount.

    Use this rather than reaching into ``memory.episodic._get_conn``
    from outside the package — keeps the FK PRAGMA and store's
    transactional boundary in one place.
    """
    if not fields:
        return 0
    bad = [k for k in fields if k not in _ALLOWED_UPDATE_COLUMNS]
    if bad:
        raise ValueError(f"Cannot update watchlist columns: {bad!r}")
    sets = ", ".join(f"{col} = ?" for col in fields)
    params: list[Any] = list(fields.values())
    params.append(item_id)
    with _get_conn(db_path) as conn:
        cursor = conn.execute(
            f"UPDATE watchlist SET {sets} WHERE id = ?",
            params,
        )
        return int(cursor.rowcount)


def delete_watchlist_item(item_id: int, db_path: Path | None = None) -> bool:
    """Hard-delete a watchlist row. Returns True iff a row was removed.

    External_signals rows for this watchlist_id are NOT cascade-deleted —
    the audit trail of past signals stays intact so historical alerts can
    still be traced. Callers who want to suppress future surfacing should
    prefer ``set_enabled(item_id, False)``.
    """
    with _get_conn(db_path) as conn:
        cursor = conn.execute("DELETE FROM watchlist WHERE id = ?", (item_id,))
        return cursor.rowcount > 0


def set_enabled(
    item_id: int,
    enabled: bool,
    db_path: Path | None = None,
) -> bool:
    with _get_conn(db_path) as conn:
        cursor = conn.execute(
            "UPDATE watchlist SET enabled = ? WHERE id = ?",
            (1 if enabled else 0, item_id),
        )
        return cursor.rowcount > 0


# --------------------------------------------------------------------- #
# Signal CRUD
# --------------------------------------------------------------------- #


def _row_to_signal_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["raw_payload"] = json.loads(d.pop("raw_payload_json", None) or "{}")
    # enrichment_json may be absent on rows read through a stale cursor; the
    # additive migration guarantees the column exists, but tolerate either.
    d["enrichment"] = json.loads(d.pop("enrichment_json", None) or "{}")
    return d


def insert_signal(signal: Signal, db_path: Path | None = None) -> int | None:
    """Insert a Signal. Returns its id, or None on UNIQUE(dedup_key) clash.

    Uses ``INSERT OR IGNORE`` so a duplicate dedup_key is a silent no-op
    (sqlite3.IntegrityError surfacing as ``rowcount == 0``) — cleaner
    than catching IntegrityError and string-matching "unique" in the
    message, which the rest of the codebase has stopped doing too
    (see ``alerts/store.py:insert_alert``).
    """
    with _get_conn(db_path) as conn:
        return _insert_signal_row(conn, signal)


def _insert_signal_row(conn: sqlite3.Connection, signal: Signal) -> int | None:
    cursor = conn.execute(
        "INSERT OR IGNORE INTO external_signals "
        "(watchlist_id, source_kind, source_external_id, captured_at, "
        "published_at, normalized_summary, raw_payload_json, "
        "provenance_url, severity_hint, dedup_key) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            signal.watchlist_id,
            signal.source_kind,
            signal.source_external_id,
            signal.captured_at,
            signal.published_at,
            signal.normalized_summary,
            json.dumps(signal.raw_payload),
            signal.provenance_url,
            signal.severity_hint.value,
            signal.dedup_key,
        ),
    )
    if cursor.rowcount == 0:
        return None
    return int(cursor.lastrowid or 0)


def record_baseline(
    item_id: int,
    at: datetime,
    signals: list[Signal],
    db_path: Path | None = None,
) -> list[tuple[int, Signal]] | None:
    """Atomically claim a seeding row's baseline and record its back-catalogue.

    One ``BEGIN IMMEDIATE`` transaction: a compare-and-swap on
    ``baselined_at IS NULL`` stamps the row, then every signal is inserted
    (``INSERT OR IGNORE`` on ``dedup_key``) and marked
    ``suppressed_baseline``. Returns the ``(signal_id, signal)`` pairs that
    were newly recorded, or ``None`` when another scan already holds the
    stamp — in which case nothing was written and the caller takes the
    lost-race path. All-or-nothing: a failure part-way rolls the stamp
    back with the rows, so the next tick baselines the whole feed again
    instead of replaying an unrecorded tail as news.
    """
    with _get_conn(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            claimed = conn.execute(
                "UPDATE watchlist SET baselined_at = ? "
                "WHERE id = ? AND baselined_at IS NULL",
                (at.isoformat(), item_id),
            )
            if claimed.rowcount == 0:
                conn.execute("ROLLBACK")
                return None
            recorded: list[tuple[int, Signal]] = []
            processed_at = datetime.now(UTC).isoformat()
            for signal in signals:
                signal_id = _insert_signal_row(conn, signal)
                if signal_id is None:
                    continue
                conn.execute(
                    "UPDATE external_signals "
                    "SET processed_at = ?, processed_outcome = ? WHERE id = ?",
                    (processed_at, OUTCOME_SUPPRESSED_BASELINE, signal_id),
                )
                recorded.append((signal_id, signal))
            conn.execute("COMMIT")
            return recorded
        except Exception:
            conn.execute("ROLLBACK")
            raise


def mark_signal_processed(
    signal_id: int,
    outcome: str,
    *,
    promoted_alert_id: int | None = None,
    db_path: Path | None = None,
) -> None:
    if not is_valid_outcome(outcome):
        raise ValueError(f"Unknown signal outcome {outcome!r}")
    with _get_conn(db_path) as conn:
        conn.execute(
            "UPDATE external_signals "
            "SET processed_at = ?, processed_outcome = ?, promoted_alert_id = ? "
            "WHERE id = ?",
            (datetime.now(UTC).isoformat(), outcome, promoted_alert_id, signal_id),
        )


def get_page_watch_state(
    slug: str, db_path: Path | None = None
) -> dict[str, Any] | None:
    """Return the last-seen page_watch state for a slug, or None if unseen."""
    resolved = _resolve_db_path(db_path)
    if not resolved.exists():
        return None
    with _get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT slug, content_hash, text_snapshot, updated_at "
            "FROM page_watch_state WHERE slug = ?",
            (slug,),
        ).fetchone()
    return dict(row) if row else None


def upsert_page_watch_state(
    slug: str,
    content_hash: str,
    text_snapshot: str,
    *,
    db_path: Path | None = None,
) -> None:
    """Insert or replace the page_watch baseline for a slug."""
    with _get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO page_watch_state (slug, content_hash, text_snapshot, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(slug) DO UPDATE SET "
            "content_hash = excluded.content_hash, "
            "text_snapshot = excluded.text_snapshot, "
            "updated_at = excluded.updated_at",
            (slug, content_hash, text_snapshot, datetime.now(UTC).isoformat()),
        )


def update_signal_enrichment(
    signal_id: int,
    enrichment: dict[str, Any],
    *,
    db_path: Path | None = None,
) -> None:
    """Persist capture-time enrichment onto an external_signals row.

    Best-effort, called from the scan pipeline after a successful enrich. The
    payload is the serialized ``SignalEnrichment`` (relevance_score,
    why_it_matters, matched_initiative).
    """
    with _get_conn(db_path) as conn:
        conn.execute(
            "UPDATE external_signals SET enrichment_json = ? WHERE id = ?",
            (json.dumps(enrichment), signal_id),
        )


def list_recent_signals(
    *,
    since: datetime | None = None,
    limit: int = 50,
    outcomes: list[str] | None = None,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Read recent signals — used by the reflection context extension in PR-C
    and by the audit/debug endpoints."""
    resolved = _resolve_db_path(db_path)
    if not resolved.exists():
        return []
    clauses: list[str] = []
    params: list[Any] = []
    if since is not None:
        clauses.append("captured_at >= ?")
        params.append(since.isoformat())
    if outcomes:
        placeholders = ", ".join("?" * len(outcomes))
        clauses.append(f"processed_outcome IN ({placeholders})")
        params.extend(outcomes)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM external_signals {where} "
            f"ORDER BY captured_at DESC LIMIT ?",
            params,
        ).fetchall()
    return [_row_to_signal_dict(row) for row in rows]


def list_signals_for_watchlist(
    watchlist_id: int,
    *,
    limit: int = 50,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Recent signals for one monitor, newest first.

    Used by the watchlist detail UI to pair each monitor with its
    signal history (fired + suppressed). Hits the
    ``idx_signals_watchlist`` composite index so it stays O(log n + k).
    """
    resolved = _resolve_db_path(db_path)
    if not resolved.exists():
        return []
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM external_signals "
            "WHERE watchlist_id = ? "
            "ORDER BY captured_at DESC LIMIT ?",
            (watchlist_id, limit),
        ).fetchall()
    return [_row_to_signal_dict(row) for row in rows]


# Re-export for callers that prefer ``store.DB_PATH`` over importing from
# memory.episodic — keeps the public surface symmetric with alerts.store.
__all__ = [
    "DB_PATH",
    "delete_watchlist_item",
    "get_watchlist_item",
    "get_watchlist_item_by_slug",
    "initialize_db",
    "insert_signal",
    "insert_watchlist_item",
    "list_recent_signals",
    "list_signals_for_watchlist",
    "list_watchlist",
    "mark_fired",
    "mark_polled",
    "get_page_watch_state",
    "mark_signal_processed",
    "set_enabled",
    "update_signal_enrichment",
    "update_watchlist_fields",
    "upsert_page_watch_state",
]
