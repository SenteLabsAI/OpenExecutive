"""Per-class decision ledger for the autonomy first-climb.

Records every gated action (proposed / executed / approved / rejected /
reversed) with a stable `decision_instances.id` so proposal → resolution →
reversal are always linked.  A per-class reliability reader (aggregate_reliability)
powers the Proposals UI and the promotion evaluator (Build 3).

Status state-machine (valid transitions only; enforced by compare-and-set):
  proposed → approved_unchanged | approved_with_edit | rejected | auto_no_response | failed
  proposed → executing → approved_unchanged | approved_with_edit | failed
                       → proposed     (release_claim: nothing was done)
  proposed | executing → closed_externally  (settled outside the app)
  executed  → reversed | failed        (auto-execute path, Build 3)
  approved_* → reversed

``executing`` is for an approval with an effect outside the app that must
happen once (sending an email): the approver claims the row
(``claim_for_execution``) BEFORE acting, so of two taps only one acts, and
finishes it after. A failure that leaves it unclear whether the effect
happened keeps it ``executing`` for a reconciler, never retried blindly.
``closed_externally`` records that something outside the app settled it (the
draft was sent or deleted in Gmail, the person replied, it expired); its
reason goes in ``reversal_reason``.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel

import openexecutive.memory.episodic as _episodic
from openexecutive.memory.episodic import _get_conn


# Resolve DB_PATH lazily so tests can monkeypatch episodic.DB_PATH and have
# it take effect in all decision_ledger functions without needing a separate patch.
def _db_path() -> Path:
    return _episodic.DB_PATH

logger = logging.getLogger(__name__)

# Status values — the full lifecycle.
STATUS_PROPOSED = "proposed"
STATUS_EXECUTED = "executed"
STATUS_APPROVED_UNCHANGED = "approved_unchanged"
STATUS_APPROVED_WITH_EDIT = "approved_with_edit"
STATUS_REJECTED = "rejected"
STATUS_AUTO_NO_RESPONSE = "auto_no_response"
STATUS_REVERSED = "reversed"
STATUS_FAILED = "failed"
STATUS_EXECUTING = "executing"
STATUS_CLOSED_EXTERNALLY = "closed_externally"

# Still open: waiting on someone, or being carried out right now.
OPEN_STATUSES: tuple[str, ...] = (STATUS_PROPOSED, STATUS_EXECUTING)

# Severity values for circuit-breaker detection.
SEVERITY_NONE = ""
SEVERITY_HIGH = "high"

# Briefing bridge: a proposed decision is surfaced as a companion alert so it's
# approvable from the briefing. These link the alert back to its ledger row —
# the WRITER (calendar_tools) and the CLEARER (decisions route) must agree on
# them exactly, so they live here as the single source of truth.
DECISION_ALERT_SOURCE = "decision_scheduling"
DECISION_INSTANCE_TAG_PREFIX = "decision_instance:"


def decision_alert_external_id(instance_id: int) -> str:
    """The alerts.external_id (and dedup_key) for instance ``instance_id``."""
    return f"decision:{instance_id}"


def decision_instance_tag(instance_id: int) -> str:
    """The topic tag carrying the decision_instance id on a briefing alert."""
    return f"{DECISION_INSTANCE_TAG_PREFIX}{instance_id}"


# ---------------------------------------------------------------------------
# Pydantic output models
# ---------------------------------------------------------------------------

class DecisionInstance(BaseModel):
    id: int
    decision_class: str
    created_at: str
    department: str
    originating_session_id: str | None
    proposed_payload_json: str
    idempotency_key: str | None
    gate_mode: str
    approver_person_id: int | None
    confidence: float | None
    status: str
    resolved_at: str | None
    resolver_person_id: int | None
    final_payload_json: str | None
    external_event_id: str | None
    reversal_reason: str | None
    severity: str


class CalibrationBucket(BaseModel):
    confidence_min: float
    confidence_max: float
    count: int
    unchanged_rate: float


class ReliabilityCard(BaseModel):
    decision_class: str
    window_start: str
    window_end: str
    volume: int
    # (approved_unchanged) / (approved_unchanged + approved_with_edit + rejected)
    unchanged_approval_rate: float
    edit_rate: float
    rejection_rate: float
    reversal_rate: float
    no_response_rate: float
    high_severity_misses: int
    calibration: list[CalibrationBucket]


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def create_decision_instance(
    *,
    decision_class: str,
    department: str,
    originating_session_id: str | None,
    proposed_payload: dict[str, Any],
    idempotency_key: str | None,
    gate_mode: str,
    approver_person_id: int | None,
    confidence: float | None,
    db_path: Path | None = None,
) -> int:
    """Insert a new decision_instances row and return its id.

    Raises sqlite3.IntegrityError if idempotency_key already exists — callers
    should catch this and return the existing row instead of double-proposing.
    """
    now = datetime.now(UTC).isoformat()
    sql = """
        INSERT INTO decision_instances
            (decision_class, created_at, department, originating_session_id,
             proposed_payload_json, idempotency_key, gate_mode,
             approver_person_id, confidence, status)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """
    with _get_conn(db_path or _db_path()) as conn:
        cur = conn.execute(sql, (
            decision_class, now, department, originating_session_id,
            json.dumps(proposed_payload), idempotency_key, gate_mode,
            approver_person_id, confidence, STATUS_PROPOSED,
        ))
        return cur.lastrowid  # type: ignore[return-value]


def get_decision_instance(
    instance_id: int,
    db_path: Path | None = None,
) -> DecisionInstance | None:
    with _get_conn(db_path or _db_path()) as conn:
        row = conn.execute(
            "SELECT * FROM decision_instances WHERE id = ?", (instance_id,)
        ).fetchone()
    if row is None:
        return None
    return DecisionInstance(**dict(row))


def get_live_by_idem(
    idempotency_key: str,
    db_path: Path | None = None,
) -> DecisionInstance | None:
    """Return a non-resolved instance with this key, if any (one being
    carried out counts: it is not resolved yet)."""
    with _get_conn(db_path or _db_path()) as conn:
        row = conn.execute(
            "SELECT * FROM decision_instances WHERE idempotency_key = ? AND status IN (?, ?)",
            (idempotency_key, *OPEN_STATUSES),
        ).fetchone()
    if row is None:
        return None
    return DecisionInstance(**dict(row))


def mark_resolved(
    instance_id: int,
    status: str,
    *,
    final_payload: dict[str, Any] | None = None,
    resolver_person_id: int | None = None,
    external_event_id: str | None = None,
    db_path: Path | None = None,
) -> bool:
    """Transition an instance from 'proposed' to a terminal status.

    Returns True if the row was updated (compare-and-set — guards double-ack).
    """
    now = datetime.now(UTC).isoformat()
    with _get_conn(db_path or _db_path()) as conn:
        result = conn.execute(
            """
            UPDATE decision_instances
            SET status = ?, resolved_at = ?, resolver_person_id = ?,
                final_payload_json = ?, external_event_id = ?
            WHERE id = ? AND status = ?
            """,
            (
                status, now, resolver_person_id,
                json.dumps(final_payload) if final_payload is not None else None,
                external_event_id,
                instance_id, STATUS_PROPOSED,
            ),
        )
        return result.rowcount == 1


def claim_for_execution(
    instance_id: int,
    *,
    resolver_person_id: int | None = None,
    db_path: Path | None = None,
) -> bool:
    """proposed → executing, compare-and-set. True for the one caller that
    claimed it; everyone else gets False and must not act."""
    with _get_conn(db_path or _db_path()) as conn:
        result = conn.execute(
            "UPDATE decision_instances SET status = ?, resolver_person_id = ? "
            "WHERE id = ? AND status = ?",
            (STATUS_EXECUTING, resolver_person_id, instance_id, STATUS_PROPOSED),
        )
        return result.rowcount == 1


_FINISHED_STATUSES = frozenset({
    STATUS_APPROVED_UNCHANGED, STATUS_APPROVED_WITH_EDIT, STATUS_FAILED,
})


def finish_execution(
    instance_id: int,
    status: str,
    *,
    final_payload: dict[str, Any] | None = None,
    external_event_id: str | None = None,
    db_path: Path | None = None,
) -> bool:
    """executing → approved_unchanged | approved_with_edit | failed,
    compare-and-set. The resolver was recorded when it was claimed."""
    if status not in _FINISHED_STATUSES:
        raise ValueError(f"not a status an execution finishes in: {status!r}")
    now = datetime.now(UTC).isoformat()
    with _get_conn(db_path or _db_path()) as conn:
        result = conn.execute(
            """
            UPDATE decision_instances
            SET status = ?, resolved_at = ?, final_payload_json = ?, external_event_id = ?
            WHERE id = ? AND status = ?
            """,
            (
                status, now,
                json.dumps(final_payload) if final_payload is not None else None,
                external_event_id,
                instance_id, STATUS_EXECUTING,
            ),
        )
        return result.rowcount == 1


def release_claim(instance_id: int, db_path: Path | None = None) -> bool:
    """executing → proposed, compare-and-set: nothing was done, so the
    decision is back where it was, waiting on someone."""
    with _get_conn(db_path or _db_path()) as conn:
        result = conn.execute(
            "UPDATE decision_instances SET status = ?, resolver_person_id = NULL "
            "WHERE id = ? AND status = ?",
            (STATUS_PROPOSED, instance_id, STATUS_EXECUTING),
        )
        return result.rowcount == 1


def close_externally(
    instance_id: int,
    *,
    reason: str,
    db_path: Path | None = None,
) -> bool:
    """proposed | executing → closed_externally, compare-and-set: something
    outside the app settled it. ``reason`` is a short code, kept in
    ``reversal_reason``."""
    now = datetime.now(UTC).isoformat()
    with _get_conn(db_path or _db_path()) as conn:
        result = conn.execute(
            """
            UPDATE decision_instances
            SET status = ?, resolved_at = ?, reversal_reason = ?
            WHERE id = ? AND status IN (?, ?)
            """,
            (STATUS_CLOSED_EXTERNALLY, now, reason[:200], instance_id, *OPEN_STATUSES),
        )
        return result.rowcount == 1


def mark_reversed(
    instance_id: int,
    *,
    reason: str = "",
    db_path: Path | None = None,
) -> bool:
    """Mark an approved/executed instance as reversed."""
    now = datetime.now(UTC).isoformat()
    with _get_conn(db_path or _db_path()) as conn:
        result = conn.execute(
            """
            UPDATE decision_instances
            SET status = ?, resolved_at = ?, reversal_reason = ?
            WHERE id = ? AND status IN (?,?,?,?)
            """,
            (
                STATUS_REVERSED, now, reason,
                instance_id,
                STATUS_APPROVED_UNCHANGED, STATUS_APPROVED_WITH_EDIT,
                STATUS_EXECUTED, STATUS_PROPOSED,
            ),
        )
        return result.rowcount == 1


def mark_executed(
    instance_id: int,
    *,
    external_event_id: str | None = None,
    final_payload: dict[str, Any] | None = None,
    db_path: Path | None = None,
) -> bool:
    """Transition from 'proposed' to 'executed' (auto-execute path, Build 3)."""
    now = datetime.now(UTC).isoformat()
    with _get_conn(db_path or _db_path()) as conn:
        result = conn.execute(
            """
            UPDATE decision_instances
            SET status = ?, resolved_at = ?, external_event_id = ?,
                final_payload_json = ?
            WHERE id = ? AND status = ?
            """,
            (
                STATUS_EXECUTED, now, external_event_id,
                json.dumps(final_payload) if final_payload is not None else None,
                instance_id, STATUS_PROPOSED,
            ),
        )
        return result.rowcount == 1


def record_high_severity_miss(
    instance_id: int,
    *,
    reason: str,
    db_path: Path | None = None,
) -> None:
    """Mark the instance severity='high' — triggers the circuit breaker (Build 3)."""
    with _get_conn(db_path or _db_path()) as conn:
        conn.execute(
            "UPDATE decision_instances SET severity = ? WHERE id = ?",
            (SEVERITY_HIGH, instance_id),
        )


def list_instances(
    decision_class: str,
    *,
    status: str | None = None,
    limit: int = 100,
    db_path: Path | None = None,
) -> list[DecisionInstance]:
    sql = "SELECT * FROM decision_instances WHERE decision_class = ?"
    params: list[Any] = [decision_class]
    if status is not None:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with _get_conn(db_path or _db_path()) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [DecisionInstance(**dict(r)) for r in rows]


def list_recent_resolved(
    limit: int = 20,
    db_path: Path | None = None,
) -> list[DecisionInstance]:
    """Recent resolved decision instances across ALL classes, newest first.

    Unlike `list_instances` (which is per-class), this powers the cross-class
    activity feed: every gated proposal that reached a terminal state, ordered
    by `resolved_at` DESC. Rows still 'proposed' (resolved_at IS NULL) are
    excluded — they're pending approvals, surfaced as proposals, not activity.
    """
    path = db_path or _db_path()
    if not path.exists():
        return []
    with _get_conn(path) as conn:
        rows = conn.execute(
            "SELECT * FROM decision_instances WHERE resolved_at IS NOT NULL "
            "ORDER BY resolved_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [DecisionInstance(**dict(r)) for r in rows]


# ---------------------------------------------------------------------------
# Class-mode state (used by Build 3 gate override + evaluator)
# ---------------------------------------------------------------------------

def get_class_mode(decision_class: str, db_path: Path | None = None) -> str:
    """Return 'propose' or 'auto_execute'. Default-absent = 'propose' (fail-safe)."""
    with _get_conn(db_path or _db_path()) as conn:
        row = conn.execute(
            "SELECT mode FROM decision_class_state WHERE decision_class = ?",
            (decision_class,),
        ).fetchone()
    return row["mode"] if row else "propose"


def set_class_mode(
    decision_class: str,
    mode: str,
    *,
    last_eval: dict[str, Any] | None = None,
    db_path: Path | None = None,
) -> None:
    now = datetime.now(UTC).isoformat()
    with _get_conn(db_path or _db_path()) as conn:
        conn.execute(
            """
            INSERT INTO decision_class_state (decision_class, mode, updated_at, last_eval_json)
            VALUES (?,?,?,?)
            ON CONFLICT(decision_class) DO UPDATE SET
                mode = excluded.mode,
                updated_at = excluded.updated_at,
                last_eval_json = excluded.last_eval_json
            """,
            (decision_class, mode, now, json.dumps(last_eval) if last_eval else None),
        )


# ---------------------------------------------------------------------------
# Reliability reader
# ---------------------------------------------------------------------------

def _idem_key(
    attendee_emails: list[str],
    start_iso: str,
    end_iso: str,
    title: str,
) -> str:
    """Stable idempotency key for a calendar booking proposal."""
    raw = "|".join(sorted(attendee_emails)) + f"|{start_iso}|{end_iso}|{title}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def aggregate_reliability(
    decision_class: str,
    *,
    window_days: int = 30,
    db_path: Path | None = None,
) -> ReliabilityCard:
    """Compute per-class reliability metrics over a trailing window.

    All rates are 0.0 when the denominator is zero (no division errors).
    """
    now = datetime.now(UTC)
    since = (now - timedelta(days=window_days)).isoformat()

    with _get_conn(db_path or _db_path()) as conn:
        rows = conn.execute(
            """
            SELECT status, severity, confidence
            FROM decision_instances
            WHERE decision_class = ? AND created_at >= ?
            """,
            (decision_class, since),
        ).fetchall()

    volume = len(rows)
    counts: dict[str, int] = {}
    high_severity = 0
    conf_with_status: list[tuple[float, str]] = []

    for row in rows:
        s = row["status"]
        counts[s] = counts.get(s, 0) + 1
        if row["severity"] == SEVERITY_HIGH:
            high_severity += 1
        if row["confidence"] is not None:
            conf_with_status.append((float(row["confidence"]), s))

    approved_total = (
        counts.get(STATUS_APPROVED_UNCHANGED, 0)
        + counts.get(STATUS_APPROVED_WITH_EDIT, 0)
    )
    resolved = (
        approved_total
        + counts.get(STATUS_REJECTED, 0)
    )
    executed_or_approved = (
        counts.get(STATUS_EXECUTED, 0) + approved_total
    )

    def _rate(num: int, denom: int) -> float:
        return round(num / denom, 4) if denom > 0 else 0.0

    unchanged_rate = _rate(counts.get(STATUS_APPROVED_UNCHANGED, 0), resolved)
    edit_rate = _rate(counts.get(STATUS_APPROVED_WITH_EDIT, 0), resolved)
    rejection_rate = _rate(counts.get(STATUS_REJECTED, 0), resolved)
    reversal_rate = _rate(counts.get(STATUS_REVERSED, 0), executed_or_approved)
    no_response_rate = _rate(counts.get(STATUS_AUTO_NO_RESPONSE, 0), volume)

    # Calibration: bucket confidence into 0.1-wide bands, compute observed
    # unchanged-approval rate per bucket.
    buckets: dict[tuple[float, float], list[str]] = {}
    for conf, status in conf_with_status:
        lo = round(int(conf * 10) * 0.1, 1)
        hi = round(lo + 0.1, 1)
        key = (lo, hi)
        buckets.setdefault(key, []).append(status)

    calibration = []
    for (lo, hi), statuses in sorted(buckets.items()):
        n = len(statuses)
        n_unchanged = sum(1 for s in statuses if s == STATUS_APPROVED_UNCHANGED)
        calibration.append(CalibrationBucket(
            confidence_min=lo,
            confidence_max=hi,
            count=n,
            unchanged_rate=round(n_unchanged / n, 4) if n > 0 else 0.0,
        ))

    return ReliabilityCard(
        decision_class=decision_class,
        window_start=since,
        window_end=now.isoformat(),
        volume=volume,
        unchanged_approval_rate=unchanged_rate,
        edit_rate=edit_rate,
        rejection_rate=rejection_rate,
        reversal_rate=reversal_rate,
        no_response_rate=no_response_rate,
        high_severity_misses=high_severity,
        calibration=calibration,
    )
