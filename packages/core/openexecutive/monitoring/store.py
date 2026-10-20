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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from openexecutive.alerts.models import AlertSeverity
from openexecutive.memory.episodic import DB_PATH, _resolve_db_path
from openexecutive.monitoring.models import (
    DECLINE_EXPIRED_RETRY_DAYS,
    DECLINE_KIND_EXPIRED,
    MODE_ACTIVE,
    MODE_DRY_RUN,
    ORIGIN_MANUAL,
    ORIGIN_RESEARCH,
    ORIGIN_RESEARCH_PROPOSED,
    OUTCOME_SUPPRESSED_BASELINE,
    Signal,
    WatchlistDecline,
    WatchlistItem,
    is_valid_mode,
    is_valid_origin,
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

            -- Targets the principal declined (or research suggestions that
            -- aged out). Keyed by the normalized target so a re-proposal
            -- under a new slug still hits. Read by the research watch policy.
            CREATE TABLE IF NOT EXISTS watchlist_declines (
                normalized_target TEXT PRIMARY KEY,
                entity TEXT NOT NULL DEFAULT '',
                signal_type TEXT NOT NULL DEFAULT '',
                slug TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                declined_at TEXT NOT NULL
            );

            -- How the research watch policy's guesses turned out
            -- (approved / declined / auto_disabled), keyed by the shape of
            -- the guess so the policy can calibrate itself over time.
            CREATE TABLE IF NOT EXISTS watchlist_policy_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_type TEXT NOT NULL,
                grounding_kind TEXT NOT NULL DEFAULT '',
                specialist TEXT NOT NULL DEFAULT '',
                outcome TEXT NOT NULL,
                at TEXT NOT NULL
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
        # Provenance of a watch (manual / executive / research /
        # research_proposed). Pre-existing rows are 'manual': nothing the
        # research policy owns predates the column.
        _ensure_column(conn, "watchlist", "origin", "TEXT NOT NULL DEFAULT 'manual'")


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
    origin: str = ORIGIN_MANUAL,
    db_path: Path | None = None,
) -> int:
    """Insert a new watchlist row. Returns its id.

    Raises ``ValueError`` for unknown ``mode`` / ``origin`` so callers fail
    loudly rather than silently storing a value the pipeline can't interpret.
    """
    if not is_valid_mode(mode):
        raise ValueError(f"Unknown watchlist mode {mode!r}")
    if not is_valid_origin(origin):
        raise ValueError(f"Unknown watchlist origin {origin!r}")
    now = datetime.now(UTC).isoformat()
    with _get_conn(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO watchlist "
            "(slug, signal_type, target, config_json, trigger_json, cadence, "
            "severity_floor, severity_ceiling, route_to_specialist, "
            "route_to_department, route_to_person_id, mode, enabled, "
            "created_at, notes, origin) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                origin,
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


# Trust feedback: the principal's dismiss / approve on a watch-sourced alert
# teaches the watch. Multiplicative decay with a floor so one bad week never
# silences a watch for good; approvals climb back slowly.
_TRUST_DECAY = 0.8
_TRUST_FLOOR = 0.2
_TRUST_RECOVERY = 0.05


def record_dismissal(slug: str, db_path: Path | None = None) -> bool:
    """A watch-sourced alert was dismissed: bump dismiss_count, decay trust."""
    resolved = _resolve_db_path(db_path)
    if not slug or not resolved.exists():
        return False
    with _get_conn(db_path) as conn:
        cursor = conn.execute(
            "UPDATE watchlist SET dismiss_count = dismiss_count + 1, "
            "trust_score = MAX(?, trust_score * ?) WHERE slug = ?",
            (_TRUST_FLOOR, _TRUST_DECAY, slug),
        )
        return cursor.rowcount > 0


def record_confirmation(slug: str, db_path: Path | None = None) -> bool:
    """A watch-sourced alert was approved / acted on: recover trust a little."""
    resolved = _resolve_db_path(db_path)
    if not slug or not resolved.exists():
        return False
    with _get_conn(db_path) as conn:
        cursor = conn.execute(
            "UPDATE watchlist SET trust_score = MIN(1.0, trust_score + ?) WHERE slug = ?",
            (_TRUST_RECOVERY, slug),
        )
        return cursor.rowcount > 0


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
    "origin",
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


def remove_watchlist_config_key(
    item_id: int, key: str, *, db_path: Path | None = None
) -> int:
    """Remove one top-level key from a watchlist row's ``config_json`` in
    place (SQLite ``json_remove``), so a concurrent write to another key is
    never overwritten by a read-modify-write of a stale snapshot. ``key``
    is validated as a bare identifier because it is spliced into the JSON
    path. Returns the rowcount."""
    if not key.isidentifier():
        raise ValueError(f"Cannot remove config key {key!r}")
    with _get_conn(db_path) as conn:
        cursor = conn.execute(
            "UPDATE watchlist SET config_json = json_remove(config_json, ?) WHERE id = ?",
            (f"$.{key}", item_id),
        )
        return int(cursor.rowcount)


def set_watchlist_config_key(
    item_id: int, key: str, value: Any, *, db_path: Path | None = None
) -> int:
    """Set one top-level key in a watchlist row's ``config_json`` in place
    (SQLite ``json_set``), the counterpart of ``remove_watchlist_config_key``.
    ``value`` is stored as JSON. Returns the rowcount."""
    if not key.isidentifier():
        raise ValueError(f"Cannot set config key {key!r}")
    with _get_conn(db_path) as conn:
        cursor = conn.execute(
            "UPDATE watchlist SET config_json = json_set(config_json, ?, json(?)) WHERE id = ?",
            (f"$.{key}", json.dumps(value), item_id),
        )
        return int(cursor.rowcount)


def recent_alert_statuses(
    watchlist_id: int,
    *,
    after_alert_id: int = 0,
    limit: int = 8,
    db_path: Path | None = None,
) -> list[tuple[int, str]]:
    """``(alert_id, status)`` of the newest alerts this watch raised, newest
    first, counting only alerts with an id above ``after_alert_id``. One row
    per alert, however many signals it absorbed. Empty when the alerts table
    is missing."""
    resolved = _resolve_db_path(db_path)
    if not resolved.exists():
        return []
    try:
        with _get_conn(db_path) as conn:
            rows = conn.execute(
                "SELECT a.id AS id, a.status AS status FROM alerts a "
                "WHERE a.id IN (SELECT promoted_alert_id FROM external_signals "
                "WHERE watchlist_id = ? AND promoted_alert_id IS NOT NULL) "
                "AND a.id > ? ORDER BY a.id DESC LIMIT ?",
                (watchlist_id, after_alert_id, limit),
            ).fetchall()
    except sqlite3.OperationalError:
        logger.debug("recent_alert_statuses: alerts table unavailable", exc_info=True)
        return []
    return [(int(r["id"]), str(r["status"])) for r in rows]


def delete_watchlist_item(item_id: int, db_path: Path | None = None) -> bool:
    """Hard-delete a watchlist row and its ``external_signals`` history.
    Returns True iff a row was removed.

    The FK from external_signals is enforced (PRAGMA foreign_keys=ON), so a
    watch that has ever recorded a signal cannot be deleted on its own; the
    signals go with it, in the same transaction. Alerts already promoted
    from those signals are untouched (they live in ``alerts``), and the
    audit_log keeps the ``external_signal_*`` rows, so what the watch
    surfaced stays traceable. Callers who want to suppress future surfacing
    without losing the row should prefer ``set_enabled(item_id, False)``.
    """
    with _get_conn(db_path) as conn:
        conn.execute("DELETE FROM external_signals WHERE watchlist_id = ?", (item_id,))
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
# Research suggestions, declines memory, policy outcomes
# --------------------------------------------------------------------- #


def list_pending_suggestions(db_path: Path | None = None) -> list[WatchlistItem]:
    """Research suggestions awaiting the principal: ``research_proposed``
    rows still in ``dry_run``. Oldest first."""
    resolved = _resolve_db_path(db_path)
    if not resolved.exists():
        return []
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM watchlist WHERE origin = ? AND mode = ? ORDER BY id",
            (ORIGIN_RESEARCH_PROPOSED, MODE_DRY_RUN),
        ).fetchall()
    return [_row_to_watchlist(row) for row in rows]


def approve_suggestion(item_id: int, db_path: Path | None = None) -> bool:
    """Flip a pending suggestion to a live research watch. Returns False when
    the row is not a pending suggestion (already approved, or not research)."""
    with _get_conn(db_path) as conn:
        cursor = conn.execute(
            "UPDATE watchlist SET mode = ?, origin = ?, enabled = 1 "
            "WHERE id = ? AND origin = ? AND mode = ?",
            (MODE_ACTIVE, ORIGIN_RESEARCH, item_id, ORIGIN_RESEARCH_PROPOSED, MODE_DRY_RUN),
        )
        return cursor.rowcount > 0


def delete_pending_suggestion(item_id: int, db_path: Path | None = None) -> bool:
    """Delete a row only while it is still a pending research suggestion
    (compare-and-delete, with its signal history). False when it was
    approved, declined or removed in the meantime."""
    with _get_conn(db_path) as conn:
        # Take the write lock up front so the check and the two deletes are
        # one unit: an approve landing in between must not lose the now-live
        # watch's signal history.
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT 1 FROM watchlist WHERE id = ? AND origin = ? AND mode = ?",
                (item_id, ORIGIN_RESEARCH_PROPOSED, MODE_DRY_RUN),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return False
            conn.execute("DELETE FROM external_signals WHERE watchlist_id = ?", (item_id,))
            cursor = conn.execute(
                "DELETE FROM watchlist WHERE id = ? AND origin = ? AND mode = ?",
                (item_id, ORIGIN_RESEARCH_PROPOSED, MODE_DRY_RUN),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return cursor.rowcount > 0


def quiet_pending_suggestion(item_id: int, db_path: Path | None = None) -> bool:
    """The too_noisy remedy: a pending suggestion goes live as a research
    watch with a high severity floor. Conditional on it still being pending."""
    with _get_conn(db_path) as conn:
        cursor = conn.execute(
            "UPDATE watchlist SET mode = ?, origin = ?, enabled = 1, severity_floor = 'high' "
            "WHERE id = ? AND origin = ? AND mode = ?",
            (MODE_ACTIVE, ORIGIN_RESEARCH, item_id, ORIGIN_RESEARCH_PROPOSED, MODE_DRY_RUN),
        )
        return cursor.rowcount > 0


def _row_to_decline(row: sqlite3.Row) -> WatchlistDecline:
    return WatchlistDecline(**dict(row))


def insert_decline(
    *,
    normalized_target: str,
    kind: str,
    reason: str = "",
    entity: str = "",
    signal_type: str = "",
    slug: str = "",
    db_path: Path | None = None,
) -> None:
    """Record (or refresh) a decline for ``normalized_target``. An explicit
    decline always overwrites an expired one; an expiry never downgrades an
    explicit decline."""
    now = datetime.now(UTC).isoformat()
    with _get_conn(db_path) as conn:
        existing = conn.execute(
            "SELECT kind FROM watchlist_declines WHERE normalized_target = ?",
            (normalized_target,),
        ).fetchone()
        if existing is not None and existing["kind"] != DECLINE_KIND_EXPIRED and kind == DECLINE_KIND_EXPIRED:
            return
        conn.execute(
            "INSERT INTO watchlist_declines "
            "(normalized_target, entity, signal_type, slug, kind, reason, declined_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(normalized_target) DO UPDATE SET "
            "entity = excluded.entity, signal_type = excluded.signal_type, "
            "slug = excluded.slug, kind = excluded.kind, reason = excluded.reason, "
            "declined_at = excluded.declined_at",
            (normalized_target, entity[:200], signal_type, slug, kind, reason, now),
        )


def list_declines(db_path: Path | None = None) -> list[WatchlistDecline]:
    resolved = _resolve_db_path(db_path)
    if not resolved.exists():
        return []
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM watchlist_declines ORDER BY declined_at DESC"
        ).fetchall()
    return [_row_to_decline(row) for row in rows]


def delete_decline(normalized_target: str, db_path: Path | None = None) -> bool:
    with _get_conn(db_path) as conn:
        cursor = conn.execute(
            "DELETE FROM watchlist_declines WHERE normalized_target = ?",
            (normalized_target,),
        )
        return cursor.rowcount > 0


def is_declined(
    normalized_target: str,
    *,
    now: datetime | None = None,
    db_path: Path | None = None,
) -> bool:
    """True when the target must not be proposed again: an explicit decline
    (permanent), or an unreviewed expiry younger than the retry window."""
    resolved = _resolve_db_path(db_path)
    if not resolved.exists():
        return False
    with _get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT kind, declined_at FROM watchlist_declines WHERE normalized_target = ?",
            (normalized_target,),
        ).fetchone()
    if row is None:
        return False
    if row["kind"] != DECLINE_KIND_EXPIRED:
        return True
    try:
        declined_at = datetime.fromisoformat(row["declined_at"])
    except ValueError:
        return True
    if declined_at.tzinfo is None:
        declined_at = declined_at.replace(tzinfo=UTC)
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now - declined_at < timedelta(days=DECLINE_EXPIRED_RETRY_DAYS)


def record_policy_outcome(
    *,
    signal_type: str,
    grounding_kind: str,
    specialist: str,
    outcome: str,
    db_path: Path | None = None,
) -> None:
    with _get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO watchlist_policy_outcomes "
            "(signal_type, grounding_kind, specialist, outcome, at) VALUES (?, ?, ?, ?, ?)",
            (signal_type, grounding_kind, specialist[:64], outcome, datetime.now(UTC).isoformat()),
        )


def _outcome_tally(
    group_cols: tuple[str, ...], *, where: str = "", db_path: Path | None = None,
) -> list[tuple[tuple[str, ...], str, int]]:
    """``[(group key, outcome, n)]`` from watchlist_policy_outcomes. Column
    names are internal constants, never user input."""
    resolved = _resolve_db_path(db_path)
    if not resolved.exists():
        return []
    cols = ", ".join(group_cols)
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            f"SELECT {cols}, outcome, COUNT(*) AS n FROM watchlist_policy_outcomes "
            f"{where} GROUP BY {cols}, outcome"
        ).fetchall()
    return [
        (tuple(str(row[c]) for c in group_cols), str(row["outcome"]), int(row["n"]))
        for row in rows
    ]


def policy_outcome_counts(
    db_path: Path | None = None,
) -> dict[tuple[str, str], dict[str, int]]:
    """``{(signal_type, grounding_kind): {outcome: n}}`` — the policy's
    track record by the shape of the guess. Empty on a fresh install."""
    out: dict[tuple[str, str], dict[str, int]] = {}
    for key, outcome, n in _outcome_tally(("signal_type", "grounding_kind"), db_path=db_path):
        out.setdefault((key[0], key[1]), {})[outcome] = n
    return out


def specialist_outcome_counts(db_path: Path | None = None) -> dict[str, dict[str, int]]:
    """``{specialist: {outcome: n}}`` for the research turn's calibration line."""
    out: dict[str, dict[str, int]] = {}
    for key, outcome, n in _outcome_tally(
        ("specialist",), where="WHERE specialist != ''", db_path=db_path,
    ):
        out.setdefault(key[0], {})[outcome] = n
    return out


def count_signals_since(
    watchlist_id: int, since: datetime, db_path: Path | None = None,
) -> int:
    """Signals (any outcome) captured for a watch since ``since``."""
    resolved = _resolve_db_path(db_path)
    if not resolved.exists():
        return 0
    with _get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM external_signals "
            "WHERE watchlist_id = ? AND captured_at >= ?",
            (watchlist_id, since.isoformat()),
        ).fetchone()
    return int(row["n"]) if row else 0


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
    "record_confirmation",
    "record_dismissal",
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
    "recent_alert_statuses",
    "remove_watchlist_config_key",
    "set_enabled",
    "set_watchlist_config_key",
    "update_signal_enrichment",
    "update_watchlist_fields",
    "upsert_page_watch_state",
]
