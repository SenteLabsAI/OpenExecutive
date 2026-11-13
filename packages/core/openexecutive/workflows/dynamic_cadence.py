"""Scheduler glue for cadence-enabled dynamic workflows.

A dynamic workflow may carry a cadence string (``daily@HH:MM`` etc.). When it
does, the scheduler should auto-run it and DM the artifact to
``cadence_person_id``. This module owns enqueueing and cancelling those
``scheduled_actions`` rows; the actual run happens in
``scheduler/runner.py``'s ``kind == "dynamic_workflow"`` branch.

Row encoding (mirrors ``departments/cadence.py``):
- ``channel``                = ``"__internal__"`` (never reaches a human directly)
- ``channel_ref``            = the workflow ``name`` (what to run; like dept slug)
- ``assigned_to_person_id``  = ``cadence_person_id`` (who receives the artifact)
- ``kind``                   = ``"dynamic_workflow"``
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, tzinfo
from pathlib import Path

from openexecutive.workflows.dynamic_models import DynamicWorkflowDef

logger = logging.getLogger(__name__)


def _zone() -> tzinfo:
    try:
        from openexecutive.memory.workspace_settings import get_user_timezone

        return get_user_timezone()
    except Exception:
        return UTC


def schedule_dynamic_workflow_cadence(
    defn: DynamicWorkflowDef, *, after: datetime | None = None, db_path: Path | None = None
) -> int | None:
    """Enqueue the next occurrence of ``defn``'s cadence. Returns the row id.

    No-op (returns None) when the definition has no cadence, no
    cadence_person_id, or an unparseable spec.
    """
    if not defn.cadence or defn.cadence_person_id is None:
        return None

    from openexecutive.departments.cadence import _parse_cadence_spec
    from openexecutive.memory.episodic import insert_scheduled_action

    base = (after or datetime.now(UTC)).astimezone(UTC)
    # A cadence's times are the company's own (Settings), summer time and
    # all: "weekly@mon@09:00" is 9 AM there every Monday.
    run_at = _parse_cadence_spec(defn.cadence, base, _zone())
    if run_at is None:
        logger.warning(
            "dynamic_cadence: invalid spec %r for workflow %r", defn.cadence, defn.name
        )
        return None

    try:
        action_id = insert_scheduled_action(
            run_at=run_at.isoformat(),
            channel="__internal__",
            channel_ref=defn.name,
            intent_text=f"Scheduled run of dynamic workflow: {defn.title}",
            kind="dynamic_workflow",
            assigned_to_person_id=defn.cadence_person_id,
            db_path=db_path,
        )
        logger.info(
            "dynamic_cadence: enqueued %r → %s (id=%d)",
            defn.name, run_at.isoformat(), action_id,
        )
        return action_id
    except Exception:
        logger.exception("dynamic_cadence: enqueue failed for %r", defn.name)
        return None


def cancel_cadence_rows(name: str, db_path: Path | None = None) -> int:
    """Cancel pending dynamic_workflow rows for ``name``. Returns count cancelled.

    Idempotent; safe to call when no rows or no table exist.
    """
    from openexecutive.memory.episodic import _get_conn, _resolve_db_path

    resolved = _resolve_db_path(db_path)
    if not resolved.exists():
        return 0
    cancelled = 0
    with _get_conn(resolved) as conn:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scheduled_actions'"
        ).fetchone()
        if table is None:
            return 0
        cur = conn.execute(
            "UPDATE scheduled_actions SET status = 'cancelled' "
            "WHERE kind = 'dynamic_workflow' AND status = 'pending' AND channel_ref = ?",
            (name,),
        )
        cancelled = cur.rowcount
    if cancelled:
        logger.info("dynamic_cadence: cancelled %d row(s) for %r", cancelled, name)
    return cancelled
