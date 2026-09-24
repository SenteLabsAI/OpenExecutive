from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from openexecutive.alerts.models import (
    Alert,
    MuteTopic,
)

logger = logging.getLogger(__name__)

DB_PATH = Path(os.environ.get("EPISODIC_DB_PATH", "./episodic_memory.db"))


def _resolve_db_path(db_path: Path | None) -> Path:
    """Return the caller's path or the current module-level DB_PATH.

    Dynamic resolution lets tests monkeypatch DB_PATH without being foiled
    by default-argument binding (same pattern as people/store.py).
    """
    return db_path if db_path is not None else DB_PATH


@contextmanager
def _get_conn(db_path: Path | None = None) -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(str(_resolve_db_path(db_path)))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def initialize_db(db_path: Path | None = None) -> None:
    with _get_conn(db_path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                external_id TEXT,
                source TEXT NOT NULL,
                severity TEXT NOT NULL,
                headline TEXT NOT NULL,
                body TEXT NOT NULL,
                suggested_action TEXT DEFAULT '',
                topic_tags TEXT DEFAULT '[]',
                channels_attempted TEXT DEFAULT '[]',
                channels_delivered TEXT DEFAULT '[]',
                dedup_key TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'unread',
                created_at TEXT NOT NULL,
                UNIQUE(source, external_id)
            );
            CREATE INDEX IF NOT EXISTS idx_alerts_status_created
                ON alerts(status, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_alerts_dedup
                ON alerts(dedup_key, created_at DESC);
        """)
        # Additive migrations: idempotent via PRAGMA + try/except pattern.
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(alerts)")}
        for col, ddl in (
            # Phase 4: authority-gate routing column.
            ("routed_to_person_id", "INTEGER"),
            # Artifacts gallery soft-delete: NULL = active, ISO ts = archived.
            ("archived_at", "TEXT"),
            # Lifecycle (alerts/lifecycle.py, alerts/review.py). NOT NULL
            # columns carry a DEFAULT — SQLite requires it on ADD COLUMN.
            ("last_seen_at", "TEXT"),
            ("occurrence_count", "INTEGER NOT NULL DEFAULT 1"),
            ("last_reviewed_at", "TEXT"),
            ("review_verdict", "TEXT NOT NULL DEFAULT ''"),
            ("review_note", "TEXT NOT NULL DEFAULT ''"),
            ("recommended_move", "TEXT NOT NULL DEFAULT ''"),
            ("why_now", "TEXT NOT NULL DEFAULT ''"),
            ("due_at", "TEXT"),
            ("superseded_by_alert_id", "INTEGER"),
            ("snoozed_until", "TEXT"),
            ("suggested_workflow", "TEXT NOT NULL DEFAULT ''"),
            # Artifact formats (orchestrator/artifact_formats.py). Only
            # meaningful for source='artifact' rows; `body` holds the stored
            # text for the format, the link url/label live beside it, and
            # `supersedes_id` is the composite id of the version this row
            # revised ('alert:<n>' / 'run:<hex>').
            ("artifact_format", "TEXT NOT NULL DEFAULT 'markdown'"),
            ("artifact_url", "TEXT"),
            ("artifact_link_label", "TEXT"),
            ("supersedes_id", "TEXT"),
        ):
            if col not in existing:
                try:
                    conn.execute(f"ALTER TABLE alerts ADD COLUMN {col} {ddl}")
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise
        conn.executescript("""

            CREATE TABLE IF NOT EXISTS mute_topics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_preferences (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                severity_threshold TEXT NOT NULL DEFAULT 'medium',
                quiet_hours_start TEXT DEFAULT '',
                quiet_hours_end TEXT DEFAULT '',
                quiet_hours_tz TEXT DEFAULT 'UTC',
                channels_enabled TEXT NOT NULL DEFAULT 'web,slack_dm,email,persisted',
                updated_at TEXT NOT NULL
            );

        """)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _row_to_alert(row: sqlite3.Row) -> Alert:
    d = dict(row)
    d["topic_tags"] = json.loads(d.get("topic_tags") or "[]")
    d["channels_attempted"] = json.loads(d.get("channels_attempted") or "[]")
    d["channels_delivered"] = json.loads(d.get("channels_delivered") or "[]")
    return Alert(**d)


def insert_alert(
    *,
    source: str,
    external_id: str,
    severity: str,
    headline: str,
    body: str,
    suggested_action: str = "",
    topic_tags: list[str] | None = None,
    dedup_key: str = "",
    routed_to_person_id: int | None = None,
    artifact_format: str = "markdown",
    artifact_url: str | None = None,
    artifact_link_label: str | None = None,
    supersedes_id: str | None = None,
    db_path: Path | None = None,
) -> int | None:
    """Insert a new alert. Returns alert id, or None if a duplicate was skipped."""
    tags = json.dumps(topic_tags or [])
    with _get_conn(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO alerts
                (external_id, source, severity, headline, body, suggested_action,
                 topic_tags, dedup_key, status, created_at, routed_to_person_id,
                 artifact_format, artifact_url, artifact_link_label, supersedes_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'unread', ?, ?, ?, ?, ?, ?)
            """,
            (
                external_id,
                source,
                severity,
                headline,
                body,
                suggested_action,
                tags,
                dedup_key,
                _now(),
                routed_to_person_id,
                artifact_format,
                artifact_url,
                artifact_link_label,
                supersedes_id,
            ),
        )
        if cursor.rowcount == 0:
            return None
        return cursor.lastrowid


def update_delivery(
    alert_id: int,
    attempted: list[str],
    delivered: list[str],
    db_path: Path | None = None,
) -> None:
    with _get_conn(db_path) as conn:
        conn.execute(
            "UPDATE alerts SET channels_attempted = ?, channels_delivered = ? WHERE id = ?",
            (json.dumps(attempted), json.dumps(delivered), alert_id),
        )


def get_alert(alert_id: int, db_path: Path | None = None) -> Alert | None:
    if not _resolve_db_path(db_path).exists():
        return None
    with _get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()
    return _row_to_alert(row) if row else None


def list_alerts(
    status: str | None = None,
    limit: int = 200,
    db_path: Path | None = None,
    exclude_source: str | None = None,
) -> list[Alert]:
    """Recent alerts, newest first. `status` and `exclude_source` are optional
    SQL filters — pushing `exclude_source` into the query (rather than letting
    callers drop rows from the returned page) ensures a caller that hides a
    source isn't starved when the most-recent `limit` rows are dominated by
    that source."""
    if not _resolve_db_path(db_path).exists():
        return []
    clauses: list[str] = []
    params: list[object] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if exclude_source:
        clauses.append("source != ?")
        params.append(exclude_source)
    where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
    params.append(limit)
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM alerts {where}ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
    return [_row_to_alert(r) for r in rows]


def recent_alerts(
    limit: int = 20,
    db_path: Path | None = None,
    exclude_source: str | None = None,
) -> list[Alert]:
    return list_alerts(
        status=None, limit=limit, db_path=db_path, exclude_source=exclude_source,
    )


def list_artifact_alerts(
    limit: int = 200, db_path: Path | None = None, archived: bool = False
) -> list[Alert]:
    """Alerts authored via `draft_artifact` (source='artifact'), newest first.

    Powers the Executive Artifacts section. Deliberately NOT filtered by
    status: a drafted artifact stays browsable after it's acked/dismissed
    out of the `/today` queue — surfacing it is the whole point of the
    Artifacts section (the row persists; `set_status` never deletes it).

    `archived` selects which slice to return: the default (False) lists only
    active artifacts (`archived_at IS NULL`); True lists only archived ones,
    so the gallery's Active / Archived views are clean swaps, not supersets.
    """
    if not _resolve_db_path(db_path).exists():
        return []
    archived_clause = (
        "AND archived_at IS NOT NULL" if archived else "AND archived_at IS NULL"
    )
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM alerts WHERE source = 'artifact' {archived_clause} "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_row_to_alert(r) for r in rows]


def set_status(alert_id: int, status: str, db_path: Path | None = None) -> bool:
    with _get_conn(db_path) as conn:
        cursor = conn.execute(
            "UPDATE alerts SET status = ? WHERE id = ?", (status, alert_id)
        )
        return cursor.rowcount > 0


# --- Lifecycle helpers (alerts/lifecycle.py, alerts/review.py, routes) ---

# Cap on ids per bulk UPDATE (SQLite's default variable limit is 32,766); the
# HTTP route caps its own `alert_ids` at 500, the sweep may need more.
_BULK_MAX_IDS = 5000
# Statuses `reopen_alert` may bring back: the closes the lifecycle / review
# / dismiss paths produce. `ack` (user approved and the Executive executed)
# is not an Undo target.
_REOPENABLE_STATUSES = ("resolved", "expired", "dismissed")


def coalesce_alert(
    *,
    source: str,
    dedup_key: str,
    severity: str,
    body: str,
    db_path: Path | None = None,
) -> tuple[int, bool] | None:
    """Fold a repeat of an open alert into the existing row.

    When an ``unread`` alert with the same ``(source, dedup_key)`` exists,
    bump ``occurrence_count`` and ``last_seen_at``, refresh ``body`` (the
    newest wording of the same situation), and raise ``severity`` if the
    repeat is graver — never lower it. Returns ``(alert_id, severity_raised)``
    on a hit, ``None`` when there is nothing to coalesce into (empty key, or no
    open row) so the caller inserts a fresh alert.

    Uses the ``idx_alerts_dedup`` index; the newest open match wins if a key
    was somehow reused.
    """
    from openexecutive.alerts.models import SEVERITY_RANK

    if not dedup_key or not _resolve_db_path(db_path).exists():
        return None
    with _get_conn(db_path) as conn:
        # Take the write lock up front so two concurrent repeats cannot both
        # miss the row (read-then-update race) or overwrite each other's body.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id, severity FROM alerts "
            "WHERE source = ? AND dedup_key = ? AND status = 'unread' "
            "ORDER BY created_at DESC LIMIT 1",
            (source, dedup_key),
        ).fetchone()
        if row is None:
            return None
        current_rank = SEVERITY_RANK.get(str(row["severity"]), 0)
        new_rank = SEVERITY_RANK.get(severity, 0)
        raised = new_rank > current_rank
        effective = severity if raised else str(row["severity"])
        conn.execute(
            "UPDATE alerts SET last_seen_at = ?, "
            "occurrence_count = COALESCE(occurrence_count, 1) + 1, "
            "body = ?, severity = ? WHERE id = ?",
            (_now(), body, effective, int(row["id"])),
        )
    return int(row["id"]), raised


def bulk_set_status(
    status: str,
    *,
    alert_ids: list[int] | None = None,
    before: str | None = None,
    category: str | None = None,
    only_status: str | None = "unread",
    exclude_sources: tuple[str, ...] = (),
    db_path: Path | None = None,
) -> list[int]:
    """Set ``status`` on many alerts at once. Returns the ids updated.

    Selects candidates by explicit ``alert_ids`` and/or ``created_at <
    before`` (ISO), restricted to ``only_status`` (default ``unread``; pass
    ``None`` for any) and excluding ``exclude_sources``. ``category``
    (``"action"`` / ``"monitoring"``) is applied in Python via
    ``briefing.ranking.categorize`` so the bulk path and ``/today`` agree on
    what counts as monitoring noise. With neither ``alert_ids`` nor ``before``
    nothing is touched (a bulk call must always carry a selector).
    """
    if alert_ids is None and before is None:
        return []
    if not _resolve_db_path(db_path).exists():
        return []
    clauses: list[str] = []
    params: list[object] = []
    if only_status:
        clauses.append("status = ?")
        params.append(only_status)
    if before is not None:
        # Same age anchor as the TTL: a situation that keeps re-firing is
        # not "old" just because its first occurrence was.
        clauses.append("COALESCE(last_seen_at, created_at) < ?")
        params.append(before)
    if alert_ids is not None:
        if not alert_ids:
            return []
        # Bound the IN list well under SQLite's variable limit.
        alert_ids = list(alert_ids)[:_BULK_MAX_IDS]
        clauses.append(f"id IN ({','.join('?' * len(alert_ids))})")
        params.extend(int(i) for i in alert_ids)
    for src in exclude_sources:
        clauses.append("source != ?")
        params.append(src)
    where = " AND ".join(clauses)
    with _get_conn(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")  # select + update under one write lock
        rows = conn.execute(f"SELECT * FROM alerts WHERE {where}", params).fetchall()
        targets = [_row_to_alert(r) for r in rows]
        if category:
            from openexecutive.briefing.ranking import categorize

            targets = [
                a for a in targets
                if categorize(
                    source=a.source, severity=a.severity,
                    routed_to_person_id=a.routed_to_person_id,
                    topic_tags=a.topic_tags or [],
                ) == category
            ]
        ids = [a.id for a in targets if a.id is not None][:_BULK_MAX_IDS]
        if not ids:
            return []
        status_guard = " AND status = ?" if only_status else ""
        guard_params: list[object] = [only_status] if only_status else []
        conn.execute(
            f"UPDATE alerts SET status = ? WHERE id IN ({','.join('?' * len(ids))}){status_guard}",
            (status, *ids, *guard_params),
        )
        return ids


def update_alert_content(
    alert_id: int,
    *,
    headline: str | None = None,
    body: str | None = None,
    severity: str | None = None,
    db_path: Path | None = None,
) -> bool:
    """Rewrite headline / body / severity in place (a ``changed`` verdict)."""
    sets: list[str] = []
    params: list[object] = []
    if headline is not None:
        sets.append("headline = ?")
        params.append(headline[:200])
    if body is not None:
        sets.append("body = ?")
        params.append(body)
    if severity is not None:
        sets.append("severity = ?")
        params.append(severity)
    if not sets or not _resolve_db_path(db_path).exists():
        return False
    params.append(alert_id)
    with _get_conn(db_path) as conn:
        cursor = conn.execute(
            f"UPDATE alerts SET {', '.join(sets)} WHERE id = ?", params
        )
        return cursor.rowcount > 0


def set_review(
    alert_id: int,
    *,
    verdict: str,
    note: str = "",
    recommended_move: str = "",
    why_now: str = "",
    due_at: str | None = None,
    reviewed_at: str | None = None,
    suggested_workflow: str = "",
    db_path: Path | None = None,
) -> bool:
    """Stamp the Executive's latest review verdict on an alert."""
    if not _resolve_db_path(db_path).exists():
        return False
    with _get_conn(db_path) as conn:
        cursor = conn.execute(
            "UPDATE alerts SET last_reviewed_at = ?, review_verdict = ?, "
            "review_note = ?, recommended_move = ?, why_now = ?, due_at = ?, "
            "suggested_workflow = ? WHERE id = ?",
            (
                reviewed_at or _now(), verdict[:32], note[:400],
                recommended_move[:32], why_now[:160], due_at,
                suggested_workflow[:64], alert_id,
            ),
        )
        return cursor.rowcount > 0


def update_alert_routing(
    alert_id: int, person_id: int | None, db_path: Path | None = None
) -> bool:
    """Assign (or clear) the person an alert is routed to."""
    if not _resolve_db_path(db_path).exists():
        return False
    with _get_conn(db_path) as conn:
        cursor = conn.execute(
            "UPDATE alerts SET routed_to_person_id = ? WHERE id = ?",
            (person_id, alert_id),
        )
        return cursor.rowcount > 0


def mark_superseded(
    alert_id: int, by_alert_id: int, db_path: Path | None = None
) -> bool:
    """Fold ``alert_id`` into ``by_alert_id``: the merged row is dismissed and
    points at the survivor, whose ``occurrence_count`` absorbs it."""
    if alert_id == by_alert_id or not _resolve_db_path(db_path).exists():
        return False
    with _get_conn(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        survivor = conn.execute(
            "SELECT 1 FROM alerts WHERE id = ?", (by_alert_id,)
        ).fetchone()
        if survivor is None:
            return False
        merged = conn.execute(
            "SELECT occurrence_count FROM alerts WHERE id = ? AND status = 'unread'",
            (alert_id,),
        ).fetchone()
        if merged is None:
            return False
        absorbed = int(merged["occurrence_count"] or 1)
        conn.execute(
            "UPDATE alerts SET superseded_by_alert_id = ?, status = 'dismissed' "
            "WHERE id = ? AND status = 'unread'",
            (by_alert_id, alert_id),
        )
        conn.execute(
            "UPDATE alerts SET occurrence_count = COALESCE(occurrence_count, 1) + ?, "
            "last_seen_at = ? WHERE id = ?",
            (absorbed, _now(), by_alert_id),
        )
        return True


def count_superseded_by(db_path: Path | None = None) -> dict[int, int]:
    """``{survivor_id: number of rows folded into it}`` in one query."""
    if not _resolve_db_path(db_path).exists():
        return {}
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT superseded_by_alert_id AS sid, COUNT(*) AS n FROM alerts "
            "WHERE superseded_by_alert_id IS NOT NULL GROUP BY superseded_by_alert_id"
        ).fetchall()
    return {int(r["sid"]): int(r["n"]) for r in rows}


def reopen_alert(
    alert_id: int, db_path: Path | None = None, *, exclude_sources: tuple[str, ...] = ()
) -> bool:
    """Bring a ``resolved`` / ``expired`` / ``dismissed`` alert back to ``unread``.

    The Undo for every autonomous close. Clears the review stamps (verdict,
    note, move, why-now, deadline, last_reviewed_at) and any supersede link
    so the row is judged afresh on the very next pass, stamps `last_seen_at`
    = now so a row older than its TTL is live again rather than re-expired
    on the next sweep, and hands back the occurrence count a merge had
    folded into the survivor. Returns
    False when the row is missing, already ``unread``, ``ack``'d (approved
    and executed — not an Undo target) or from an excluded source
    (artifacts / decision-backed alerts have their own lifecycle).
    """
    if not _resolve_db_path(db_path).exists():
        return False
    clauses = [f"status IN ({','.join('?' * len(_REOPENABLE_STATUSES))})"]
    params: list[object] = [*_REOPENABLE_STATUSES]
    for src in exclude_sources:
        clauses.append("source != ?")
        params.append(src)
    with _get_conn(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            f"SELECT superseded_by_alert_id, occurrence_count FROM alerts "
            f"WHERE id = ? AND {' AND '.join(clauses)}",
            (alert_id, *params),
        ).fetchone()
        if row is None:
            return False
        if row["superseded_by_alert_id"] is not None:
            conn.execute(
                "UPDATE alerts SET occurrence_count = MAX(1, COALESCE(occurrence_count, 1) - ?) "
                "WHERE id = ?",
                (int(row["occurrence_count"] or 1), int(row["superseded_by_alert_id"])),
            )
        conn.execute(
            "UPDATE alerts SET status = 'unread', review_verdict = '', "
            "review_note = '', recommended_move = '', why_now = '', due_at = NULL, "
            "last_reviewed_at = NULL, superseded_by_alert_id = NULL, "
            "snoozed_until = NULL, suggested_workflow = '', last_seen_at = ? WHERE id = ?",
            (_now(), alert_id),
        )
        return True


def get_alert_by_external(
    source: str, external_id: str, db_path: Path | None = None
) -> Alert | None:
    """Look up a single alert by its (source, external_id) pair.

    Used to find the companion briefing alert for a decision_instance
    (source='decision_scheduling', external_id='decision:{id}') — e.g. to
    assert it exists in tests or to inspect it before clearing it.
    """
    if not _resolve_db_path(db_path).exists():
        return None
    with _get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM alerts WHERE source = ? AND external_id = ? LIMIT 1",
            (source, external_id),
        ).fetchone()
    return _row_to_alert(row) if row else None


def set_status_by_external(
    source: str, external_id: str, status: str, db_path: Path | None = None
) -> bool:
    """Set the status of the alert keyed by (source, external_id).

    The clean primitive for clearing a decision_instance's companion
    briefing alert when the decision is resolved, without first SELECTing.
    Returns True if a row was updated (False — a harmless no-op — when no
    companion alert exists, e.g. a decision created before the bridge).
    """
    # Don't let a cold-system clear materialize a schemaless DB file; mirror
    # the existence guard on get_alert_by_external / list_alerts.
    if not _resolve_db_path(db_path).exists():
        return False
    with _get_conn(db_path) as conn:
        cursor = conn.execute(
            "UPDATE alerts SET status = ? WHERE source = ? AND external_id = ?",
            (status, source, external_id),
        )
        return cursor.rowcount > 0


def set_alert_archived(
    alert_id: int, archived: bool, db_path: Path | None = None
) -> bool:
    """Archive (soft-hide) or restore an alert-backed artifact.

    Sets `archived_at` to the current time when archiving, or NULL when
    restoring. Returns True if a row was updated. The artifact-source guard
    lives in the API layer (the route must not become a general alert mutator).
    """
    if not _resolve_db_path(db_path).exists():
        return False
    with _get_conn(db_path) as conn:
        cursor = conn.execute(
            "UPDATE alerts SET archived_at = ? WHERE id = ?",
            (_now() if archived else None, alert_id),
        )
        return cursor.rowcount > 0


def delete_alert(alert_id: int, db_path: Path | None = None) -> bool:
    if not _resolve_db_path(db_path).exists():
        return False
    with _get_conn(db_path) as conn:
        cursor = conn.execute("DELETE FROM alerts WHERE id = ?", (alert_id,))
        return cursor.rowcount > 0


# --- Mute topics ---


def add_mute(pattern: str, db_path: Path | None = None) -> int | None:
    pattern = pattern.strip()
    if not pattern:
        raise ValueError("pattern must be non-empty")
    with _get_conn(db_path) as conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO mute_topics (pattern, created_at) VALUES (?, ?)",
            (pattern, _now()),
        )
        if cursor.rowcount == 0:
            existing = conn.execute(
                "SELECT id FROM mute_topics WHERE pattern = ?", (pattern,)
            ).fetchone()
            return int(existing["id"]) if existing else None
        return int(cursor.lastrowid or 0)


def list_mutes(db_path: Path | None = None) -> list[MuteTopic]:
    if not _resolve_db_path(db_path).exists():
        return []
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM mute_topics ORDER BY created_at DESC"
        ).fetchall()
    return [MuteTopic(**dict(r)) for r in rows]


def delete_mute(mute_id: int, db_path: Path | None = None) -> bool:
    if not _resolve_db_path(db_path).exists():
        return False
    with _get_conn(db_path) as conn:
        cursor = conn.execute("DELETE FROM mute_topics WHERE id = ?", (mute_id,))
        return cursor.rowcount > 0


