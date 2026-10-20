"""A card on Home when a scheduled workflow run fails, and its Run again.

A run someone starts from /jobs shows its own error while they watch. A
scheduled one (a department check-in, a saved workflow on a cadence) used to
fail with only a log line and an ``error`` row, so nobody knew the round-up
never went out. :func:`file_failed_run_card` files one card for it; repeat
failures of the same workflow fold into that card (the alerts' occurrence
count) instead of stacking new ones.

:func:`queue_rerun` is the card's Run again: it queues the same scheduled
action again, due now, marked as a one-off (``scope_key`` ``rerun:<run id>``)
so the scheduler runs it once without chaining another occurrence.

Lazy imports throughout: the scheduler imports this inside its handlers.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

SOURCE = "workflow_failed"
# Carried on the card so the UI can offer Run again for that run.
RUN_TAG_PREFIX = "failed_run:"
RERUN_SCOPE_PREFIX = "rerun:"
# How many failed runs one coalesced card remembers (newest last).
_KEEP_RUNS = 20
# Serialises queue_rerun's check-then-insert, so two Run again taps at once
# (the route can be called from several requests in parallel) queue it once.
_RERUN_LOCK = threading.Lock()


def is_rerun(scope_key: str | None) -> bool:
    """True for a scheduled action queued by Run again (runs once, no chain)."""
    return bool(scope_key) and str(scope_key).startswith(RERUN_SCOPE_PREFIX)


def failed_run_id(topic_tags: list[str]) -> str | None:
    """The run a failure card offers to run again: the newest one it carries."""
    runs = _run_ids(topic_tags)
    return runs[-1] if runs else None


def _run_ids(topic_tags: list[str]) -> list[str]:
    return [
        tag[len(RUN_TAG_PREFIX):]
        for tag in topic_tags
        if tag.startswith(RUN_TAG_PREFIX) and len(tag) > len(RUN_TAG_PREFIX)
    ]


def _tags(run_ids: list[str]) -> list[str]:
    return ["workflow", *(f"{RUN_TAG_PREFIX}{r}" for r in run_ids[-_KEEP_RUNS:])]


def _has_failure_card(run_id: str) -> bool:
    """Whether a failure card was filed for this run: what marks a run as a
    scheduled one (a run started from /jobs never gets a card)."""
    from openexecutive.alerts import store

    with store._get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM alerts WHERE source = ? AND topic_tags LIKE ? LIMIT 1",
            (SOURCE, f'%"{RUN_TAG_PREFIX}{run_id}"%'),
        ).fetchone()
    return row is not None


def _dedup_key(workflow_name: str, scope: str) -> str:
    return f"{SOURCE}:{workflow_name}:{scope}"


def file_failed_run_card(
    *,
    run_id: str,
    workflow_name: str,
    title: str,
    scope: str,
    routed_to_person_id: int | None = None,
) -> int | None:
    """File (or bump) the card for a scheduled run that ended in error.

    ``scope`` tells repeats apart: the department slug for a check-in, the
    workflow name for a saved workflow. ``title`` is what people call it
    ("Finance check-in"). Never raises; returns the card's id or None.
    """
    from openexecutive.alerts import store

    headline = f"{title} didn't finish"
    body = (
        f"The scheduled run of {title} stopped with an error, so nothing from it "
        "went out. Run it again, or open the run to see what went wrong."
    )
    dedup_key = _dedup_key(workflow_name, scope)
    try:
        hit = store.coalesce_alert(
            source=SOURCE, dedup_key=dedup_key, severity="high", body=body
        )
        if hit is not None:
            # Add the newest failure last, so Run again and Open the run use
            # the run that just failed, and an earlier run of the same card
            # (open on /jobs, or a stale Home tab) still counts as scheduled.
            with store._get_conn() as conn:
                row = conn.execute(
                    "SELECT topic_tags FROM alerts WHERE id = ?", (hit[0],)
                ).fetchone()
                runs = [r for r in _run_ids(json.loads(row[0] or "[]") if row else []) if r != run_id]
                conn.execute(
                    "UPDATE alerts SET topic_tags = ? WHERE id = ?",
                    (json.dumps(_tags([*runs, run_id])), hit[0]),
                )
            return hit[0]
        return store.insert_alert(
            source=SOURCE,
            external_id=f"run:{run_id}",
            severity="high",
            headline=headline,
            body=body,
            suggested_action=f"Run {title} again.",
            topic_tags=_tags([run_id]),
            dedup_key=dedup_key,
            routed_to_person_id=routed_to_person_id,
        )
    except Exception:
        logger.exception("failure_alerts: could not file the card for run %s", run_id)
        return None


class RerunRefused(Exception):
    """Why Run again can't queue this run; ``status`` is the HTTP code."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def rerun_target(run: dict[str, Any]) -> tuple[str, str, int | None]:
    """(kind, channel_ref, assigned_to_person_id) of the scheduled action that
    reruns ``run``, or :class:`RerunRefused` when it can't be rerun this way."""
    if run.get("status") != "error":
        raise RerunRefused(409, "Only a run that failed can be run again.")
    # Only a scheduled run (one with a failure card) reruns this way: a run
    # someone started from /jobs would otherwise be redelivered on the
    # schedule's terms, to whoever the schedule sends to.
    if not _has_failure_card(str(run.get("run_id") or "")):
        raise RerunRefused(409, "Start this one again from its page.")
    name = str(run.get("workflow_name") or "")
    if name == "department_check_in":
        slug = str((run.get("inputs") or {}).get("department_slug") or "")
        from openexecutive.departments import store as dept_store

        if not slug or dept_store.get_department(slug) is None:
            raise RerunRefused(409, "That department no longer exists.")
        return "dept_cadence", slug, None
    from openexecutive.workflows.dynamic_store import get_definition

    defn = get_definition(name)
    if defn is not None and defn.cadence:
        if not defn.is_active:
            raise RerunRefused(409, "That workflow is switched off. Switch it on first.")
        return "dynamic_workflow", name, defn.cadence_person_id
    raise RerunRefused(409, "Start this one again from its page.")


def queue_rerun(run: dict[str, Any]) -> int:
    """Queue the scheduled action that reruns ``run``, due now, and close its
    failure card. Returns the action's id.

    Refuses (409) while a rerun of the same scheduled action is already
    queued (from this run or an earlier one on the same card), so a double
    tap runs it once.
    """
    from openexecutive.memory.episodic import (
        _get_conn,
        _resolve_db_path,
        insert_scheduled_action,
    )

    kind, ref, assignee = rerun_target(run)
    scope_key = f"{RERUN_SCOPE_PREFIX}{run['run_id']}"
    with _RERUN_LOCK:
        with _get_conn(_resolve_db_path(None)) as conn:
            queued = conn.execute(
                "SELECT 1 FROM scheduled_actions WHERE scope_key LIKE ? "
                "AND kind = ? AND channel_ref = ? "
                "AND status IN ('pending', 'running') LIMIT 1",
                (f"{RERUN_SCOPE_PREFIX}%", kind, ref),
            ).fetchone()
        if queued is not None:
            raise RerunRefused(409, "It's already running again.")
        action_id = insert_scheduled_action(
            run_at=datetime.now(UTC).isoformat(),
            channel="__internal__",
            channel_ref=ref,
            intent_text=f"Run again: {run.get('title') or ref}",
            kind=kind,
            assigned_to_person_id=assignee,
            scope_key=scope_key,
        )
    _close_cards(_dedup_key(str(run.get("workflow_name") or ""), ref))
    return action_id


def _close_cards(dedup_key: str) -> None:
    """Ack the open failure card(s) a rerun answers. Never raises: the rerun
    is queued either way, and a card left open only means one more tap."""
    from openexecutive.alerts import store

    try:
        with store._get_conn() as conn:
            conn.execute(
                "UPDATE alerts SET status = 'ack' "
                "WHERE source = ? AND dedup_key = ? AND status = 'unread'",
                (SOURCE, dedup_key),
            )
    except Exception:
        logger.exception("failure_alerts: could not close the card for %s", dedup_key)
