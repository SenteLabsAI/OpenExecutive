"""Scheduler wiring for the periodic watchlist research cron.

PR-D wired the workflow to two triggers (manual chat tool, onboarding
completion). PR-E adds a third: a cron tick that re-runs the workflow
every ``watchlist_research_interval_minutes`` (default 360). The cost
shape of a research run is real — 7 LLM calls × web_search — so each
tick first computes a fingerprint of the inputs the workflow reads
(company profile, active initiatives, existing watchlist slugs) and
SKIPS the run when nothing has changed since the last successful
research. First tick after any meaningful state change runs
immediately; quiet days are free.

That fingerprint only sees *internal* state, so a static profile would
otherwise let the council sit blind to purely-external change (a
competitor move, a regulation shift) indefinitely. A staleness floor
(``watchlist_research_max_staleness_hours``, default 168) bounds that:
once the floor has elapsed since the last successful run, the next tick
runs even when the fingerprint is unchanged. Set the floor <= 0 to
disable it and rely solely on skip-if-unchanged.

Heartbeat lifecycle mirrors ``monitoring.pipeline`` /
``scheduler.nudge_engine``:

  - ``bootstrap_watchlist_research_scan`` — idempotent seed at boot.
  - ``enqueue_next_watchlist_research_scan`` — chain next tick after
    each fire.
  - ``run_watchlist_research_scan`` — one tick. Compute the
    fingerprint, decide skip-or-run, fire workflow, promote artifact
    into an Alert routed to the principal with a date-stable
    dedup_key (so re-fires the same day collapse).

The baseline hash stored on a successful run is recomputed *after* the
workflow finishes, not the pre-run hash used for the skip decision. The
workflow mutates one of its own fingerprint inputs — its watchlist pass
adds rows the fingerprint hashes — so recording the pre-run hash would
leave every subsequent tick seeing a "changed" state and re-running a
full research pass each interval. Capturing the post-run state as the
baseline is what makes "quiet days are free" actually hold.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from openexecutive.workflows.gate import ensure_workflow_event

logger = logging.getLogger(__name__)

# Heartbeat identity. Mirrors the constants in monitoring.pipeline and
# scheduler.nudge_engine so the runner dispatch + bootstrap share shape.
HEARTBEAT_KIND = "watchlist_research_scan"
HEARTBEAT_CHANNEL = "__internal__"
HEARTBEAT_CHANNEL_REF = "watchlist_research"
HEARTBEAT_INTENT = "Watchlist research — periodic re-run."

# Audit event types — read in the skip-if-unchanged check via
# audit_log query. Defined as constants so tests can grep / assert.
EVENT_RAN = "watchlist_research_periodic_ran"
EVENT_SKIPPED = "watchlist_research_periodic_skipped"
EVENT_FAILED = "watchlist_research_periodic_failed"

# Hard wall-clock ceiling — matches the onboarding hook so a hung
# provider can't leak a forever-task into the scheduler.
_RUN_TIMEOUT_SECONDS = 600


# --------------------------------------------------------------------- #
# State fingerprint
# --------------------------------------------------------------------- #


def compute_research_state_hash(db_path: Path | None = None) -> str:
    """Hash the inputs the research workflow reads.

    The inputs are: the company profile's prompt-form text, the
    titles + statuses of every active initiative, the slug+target list of
    every enabled, ACTIVE watchlist row, each department's watched
    entities / charter scope / goal key results, and the recent episodic
    decisions that name something in the grounding vocabulary. A change
    to any of these invalidates the prior research; everything else is
    irrelevant.

    Only *matching* decisions count: the chat memory extractor writes
    decisions freely, and one that names nothing the policy knows cannot
    change what the policy would do, so it must not trigger a 7-specialist
    re-run.

    Dry-run rows are excluded on purpose: the research pass files its own
    suggestions as dry-run rows, and the principal approving or declining
    one must not read as "state changed" and trigger another full
    7-specialist run within the hour.

    Failure on any source is logged + returns a sentinel hash that
    differs from every real run — so a transient profile-load error
    triggers a fresh research rather than silently masking a real
    state change as "unchanged".
    """
    parts: list[str] = []

    try:
        from openexecutive.onboarding.profile_builder import load_or_create_profile

        profile = load_or_create_profile()
        parts.append("PROFILE:")
        try:
            parts.append(profile.to_prompt_block())
        except Exception:
            logger.exception("research.scheduler: profile.to_prompt_block failed")
            parts.append("PROFILE_ERROR")
    except Exception:
        # Persistent profile load failure must NOT mint a fresh hash
        # every tick — that would burn 7 specialist calls every 2h
        # while ops works through the underlying breakage. Static
        # sentinel keeps skip-if-unchanged engaged; the audit log
        # captures the failure path separately.
        logger.exception("research.scheduler: profile load failed")
        return "error:profile"

    initiatives: list = []
    try:
        from openexecutive.memory.episodic import DB_PATH, get_active_initiatives

        parts.append("INITIATIVES:")
        # `get_active_initiatives` binds DB_PATH at def time; route
        # through the resolver so monkeypatched DB_PATH in tests
        # (and any future multi-DB harness) actually takes effect.
        initiatives = list(get_active_initiatives(db_path=db_path or DB_PATH))
        for i in sorted(initiatives, key=lambda x: (getattr(x, "title", "") or "")):
            parts.append(
                f"{getattr(i, 'title', '')}::{getattr(i, 'status', '')}"
            )
    except Exception:
        logger.exception("research.scheduler: initiatives load failed")
        parts.append("INITIATIVES_ERROR")

    try:
        from openexecutive.monitoring import store as monitoring_store
        from openexecutive.monitoring.models import MODE_ACTIVE

        parts.append("WATCHLIST:")
        for item in sorted(
            monitoring_store.list_watchlist(enabled_only=True, db_path=db_path),
            key=lambda x: x.slug,
        ):
            if item.mode != MODE_ACTIVE:
                continue
            parts.append(f"{item.slug}::{item.signal_type}::{item.target}")
    except Exception:
        logger.exception("research.scheduler: watchlist load failed")
        parts.append("WATCHLIST_ERROR")

    from openexecutive.monitoring.research import watch_policy

    departments = watch_policy.load_departments(db_path)
    _append_part(parts, "DEPARTMENTS", lambda: [
        f"{state.config.slug}::"
        + "|".join(state.config.watched_entities)
        + "::" + "|".join(state.config.charter.scope)
        + "::" + "|".join(g.key_result for g in state.goals)
        for state in sorted(departments, key=lambda d: d.config.slug)
    ])

    def _matching_decisions() -> list[str]:
        # The same vocabulary the policy grounds with (minus watch labels,
        # which never ground), so a decision moves the hash exactly when it
        # would move a classification.
        vocabulary = watch_policy.grounding_vocabulary(profile, initiatives, [], departments)
        return [
            f"{getattr(decision, 'id', '')}::{str(getattr(decision, 'summary', '') or '')[:120]}"
            for decision in watch_policy.recent_decisions(db_path=db_path)
            if any(watch_policy.named_in_decision(term, decision, vocabulary) for term in vocabulary)
        ]

    _append_part(parts, "DECISIONS", _matching_decisions)

    digest = hashlib.sha256("\n".join(parts).encode()).hexdigest()
    return digest


def _append_part(parts: list[str], label: str, lines: Callable[[], list[str]]) -> None:
    """Append one labelled fingerprint section, or a ``<LABEL>_ERROR``
    sentinel when loading it fails (logged; the hash still differs from a
    healthy run so the failure is visible in the audit trail)."""
    parts.append(f"{label}:")
    try:
        parts.extend(lines())
    except Exception:
        logger.exception("research.scheduler: %s load failed", label.lower())
        parts.append(f"{label}_ERROR")


def _last_successful_research_run() -> tuple[str | None, str | None]:
    """Read the (state_hash, ts) of the most recent successful periodic run.

    Returns (None, None) when no successful periodic run has happened yet —
    a fresh install or a recovered DB. Caller treats a None hash as
    "always run". The ts (ISO8601 string) feeds the staleness floor.
    """
    try:
        from openexecutive.audit import get_audit_logger

        logger_inst = get_audit_logger()
        # Most recent first, only the periodic-ran event_type.
        rows = logger_inst.query(event_type=EVENT_RAN, limit=1)
    except Exception:
        logger.exception("research.scheduler: audit lookup failed")
        return None, None
    if not rows:
        return None, None
    try:
        # AuditEvent exposes the parsed dict; details_json is the raw
        # SQL column. Use the parsed view so a malformed row doesn't
        # crash us — AuditLogger has already validated it.
        details = rows[0].details or {}
        h = details.get("state_hash")
        ts = getattr(rows[0], "ts", None)
        return (str(h) if h else None), (str(ts) if ts else None)
    except Exception:
        logger.exception("research.scheduler: audit row parse failed")
        return None, None


def _last_successful_research_hash() -> str | None:
    """Back-compat shim — the state hash of the last successful run only."""
    return _last_successful_research_run()[0]


def _research_run_is_stale(last_run_ts: str | None, now: datetime) -> bool:
    """Return True when the staleness floor says "run anyway".

    The skip-if-unchanged fingerprint only sees *internal* state (profile,
    initiatives, watchlist). It is blind to purely-external change — exactly
    the thing the research council exists to catch. The staleness floor
    bounds that blindness: once ``watchlist_research_max_staleness_hours``
    have elapsed since the last successful run, the next tick runs even if
    the fingerprint is unchanged. A floor <= 0 disables this and falls back
    to pure skip-if-unchanged.
    """
    from openexecutive.config import get_settings

    try:
        floor_hours = get_settings().watchlist_research_max_staleness_hours
    except Exception:
        logger.exception("research.scheduler: staleness-floor settings read failed")
        return False
    if not floor_hours or floor_hours <= 0:
        return False  # floor disabled — pure skip-if-unchanged
    if last_run_ts is None:
        # No recorded successful run; the hash path already forces a run, but
        # treat "unknown last run" as stale so we never suppress on a gap.
        return True
    try:
        last_dt = datetime.fromisoformat(last_run_ts)
    except ValueError:
        # Unparseable timestamp — don't let it suppress a run.
        logger.warning(
            "research.scheduler: unparseable last-run ts %r; treating as stale",
            last_run_ts,
        )
        return True
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=UTC)
    return (now - last_dt) >= timedelta(hours=floor_hours)


def clear_research_run_history(db_path: Path | None = None) -> int:
    """Delete the periodic research run-history rows from ``audit_log``.

    The skip-if-unchanged gate (``_last_successful_research_run``) decides
    skip-or-run from the most recent ``EVENT_RAN`` row. Fixture load/unload
    wipes the profile + initiatives + watchlist that feed the state
    fingerprint, but deliberately preserves ``audit_log`` (the operator's
    /audit history). Without clearing these three event types on load, a
    freshly-loaded fixture whose fingerprint matches a prior run makes the
    seeded ``watchlist_research_scan`` skip itself — the "no findings on a
    seeded run" failure. Returns the number of rows deleted (0 when the DB
    or table is absent — a fresh box has nothing to clear).
    """
    from openexecutive.memory.episodic import _get_conn, _resolve_db_path

    resolved = _resolve_db_path(db_path)
    if not resolved.exists():
        return 0
    with _get_conn(resolved) as conn:
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_log'"
        ).fetchone()
        if not table_exists:
            return 0
        cur = conn.execute(
            "DELETE FROM audit_log WHERE event_type IN (?, ?, ?)",
            (EVENT_RAN, EVENT_SKIPPED, EVENT_FAILED),
        )
        deleted = int(cur.rowcount)
    if deleted:
        logger.info(
            "research.scheduler: cleared %d research run-history audit row(s)",
            deleted,
        )
    return deleted


# --------------------------------------------------------------------- #
# Heartbeat bootstrap / chain — mirrors monitoring.pipeline
# --------------------------------------------------------------------- #


def _heartbeat_pending(db_path: Path | None = None) -> bool:
    from openexecutive.memory.episodic import _get_conn, _resolve_db_path

    resolved = _resolve_db_path(db_path)
    if not resolved.exists():
        return False
    with _get_conn(resolved) as conn:
        row = conn.execute(
            "SELECT 1 FROM scheduled_actions "
            "WHERE kind = ? AND status IN ('pending', 'running') LIMIT 1",
            (HEARTBEAT_KIND,),
        ).fetchone()
    return row is not None


def bootstrap_watchlist_research_scan(
    db_path: Path | None = None,
) -> int | None:
    """Idempotently seed the heartbeat row. Returns the new id or None."""
    from openexecutive.config import get_settings
    from openexecutive.memory.episodic import insert_scheduled_action

    if _heartbeat_pending(db_path):
        return None
    settings = get_settings()
    run_at = datetime.now(UTC) + timedelta(
        minutes=settings.watchlist_research_interval_minutes
    )
    try:
        action_id = insert_scheduled_action(
            run_at=run_at.isoformat(),
            channel=HEARTBEAT_CHANNEL,
            channel_ref=HEARTBEAT_CHANNEL_REF,
            intent_text=HEARTBEAT_INTENT,
            kind=HEARTBEAT_KIND,
            db_path=db_path,
        )
        logger.info(
            "research.scheduler.bootstrap: heartbeat at %s (id=%d)",
            run_at.isoformat(), action_id,
        )
        return action_id
    except Exception:
        logger.exception("research.scheduler.bootstrap: insert failed")
        return None


def enqueue_next_watchlist_research_scan(
    *,
    after: datetime | None = None,
    db_path: Path | None = None,
) -> int | None:
    from openexecutive.config import get_settings
    from openexecutive.memory.episodic import insert_scheduled_action

    settings = get_settings()
    base = (after or datetime.now(UTC)).astimezone(UTC)
    run_at = base + timedelta(
        minutes=settings.watchlist_research_interval_minutes
    )
    try:
        action_id = insert_scheduled_action(
            run_at=run_at.isoformat(),
            channel=HEARTBEAT_CHANNEL,
            channel_ref=HEARTBEAT_CHANNEL_REF,
            intent_text=HEARTBEAT_INTENT,
            kind=HEARTBEAT_KIND,
            db_path=db_path,
        )
        logger.info(
            "research.scheduler.enqueue_next: next at %s (id=%d)",
            run_at.isoformat(), action_id,
        )
        return action_id
    except Exception:
        logger.exception("research.scheduler.enqueue_next: insert failed")
        return None


# --------------------------------------------------------------------- #
# One scan tick
# --------------------------------------------------------------------- #


async def run_watchlist_research_scan(
    now: datetime | None = None,
    *,
    db_path: Path | None = None,
    store: Any | None = None,
) -> int:
    """Run one periodic tick. Returns number of proposals surfaced (0
    on skip / failure).

    Pipeline:
      1. Compute state_hash.
      2. If state_hash matches the last successful run, audit a
         "skipped" event and return 0.
      3. Otherwise run the workflow, capture proposals via the
         WorkflowEvent.type='result' event.
      4. If any proposals surfaced, write a single Alert routed to the
         principal. Dedup_key is date-based so re-runs the same day
         collapse via alerts.UNIQUE(source, external_id).
      5. Audit the outcome with the state_hash so the next tick can
         skip-if-unchanged.
    """
    import asyncio

    from openexecutive.audit import log_event as audit_log

    now = now or datetime.now(UTC)
    state_hash = compute_research_state_hash(db_path=db_path)
    last_hash, last_run_ts = _last_successful_research_run()

    state_unchanged = last_hash is not None and last_hash == state_hash
    stale = _research_run_is_stale(last_run_ts, now)

    if state_unchanged and not stale:
        audit_log(
            EVENT_SKIPPED,
            "Watchlist research skipped — no profile/initiative/watchlist change",
            actor="scheduler",
            details={"state_hash": state_hash},
        )
        logger.info("research.scheduler: skipping — state unchanged")
        return 0

    # Record why we're running so cost/freshness dashboards can tell a
    # cold start apart from a state-driven run apart from a staleness refresh.
    if last_hash is None:
        run_trigger = "cold_start"
    elif state_unchanged:
        run_trigger = "staleness_floor"
    else:
        run_trigger = "state_changed"
    if state_unchanged:
        logger.info(
            "research.scheduler: running despite unchanged state — staleness floor reached"
        )

    try:
        from openexecutive.config import get_settings
        from openexecutive.knowledge.store import ChromaDBStore
        from openexecutive.workflows import WORKFLOW_REGISTRY
        from openexecutive.workflows.persistence import (
            complete_run,
            create_run,
            fail_run,
        )

        workflow = WORKFLOW_REGISTRY["executive_research"]
        input_cls = workflow.input_model()
        run_id = str(uuid.uuid4())
        # The workflow tags every model-usage row it writes with this id, so
        # a run that times out or crashes is still traceable to its calls.
        wf_inputs = input_cls(
            note=f"periodic research tick at {now.isoformat()}",
            run_id=run_id,
        )

        try:
            create_run(
                run_id,
                "executive_research",
                f"{workflow.title} (periodic)",
                wf_inputs.model_dump(),
            )
        except Exception:
            logger.exception("research.scheduler: create_run failed")

        # Tests inject a stub store; production goes through the
        # default ChromaDBStore. We don't construct it unconditionally
        # because the default constructor would crash in environments
        # without a chroma_db directory wired up.
        effective_store = store if store is not None else ChromaDBStore(
            persist_directory=get_settings().vector_store_path
        )
        artifact = ""
        findings: list[dict[str, Any]] = []
        tool_calls: list[dict[str, Any]] = []
        usage: dict[str, Any] | None = None

        async def _drive() -> None:
            nonlocal artifact, findings, tool_calls, usage
            async for event in workflow.run(
                inputs=wf_inputs, store=effective_store,
            ):
                event = ensure_workflow_event(event, site='research.scheduler')
                if event.type == "result" and event.data:
                    raw_findings = event.data.get("findings")
                    if isinstance(raw_findings, list):
                        findings = raw_findings
                    raw_calls = event.data.get("tool_calls")
                    if isinstance(raw_calls, list):
                        tool_calls = raw_calls
                    raw_usage = event.data.get("usage")
                    if isinstance(raw_usage, dict):
                        usage = raw_usage
                elif event.type == "artifact" and event.content:
                    artifact = event.content
                elif event.type == "error" and event.message:
                    raise RuntimeError(event.message)

        await asyncio.wait_for(_drive(), timeout=_RUN_TIMEOUT_SECONDS)

        try:
            complete_run(run_id, artifact or "(no artifact)")
        except Exception:
            logger.exception("research.scheduler: complete_run failed")

        # No briefing Alert insertion here — the workflow's Executive
        # synthesis step already fires create_alert / send_*_dm /
        # send_department_message / add_watchlist_entry / ... directly
        # for each finding it routes. The cron's job is to (a) gate
        # the run with skip-if-unchanged, (b) run the workflow, and
        # (c) audit the outcome so the next tick can short-circuit.
        #
        # Re-fingerprint AFTER the run, not the pre-run hash computed
        # above: the workflow mutates its own inputs — its watchlist pass
        # adds rows via add_watchlist_entry, and the fingerprint hashes
        # the watchlist. Recording the pre-run hash as the baseline would
        # never match the next tick's read of the now-mutated watchlist,
        # so skip-if-unchanged could never engage and a full research pass
        # would burn every interval (the "full research every 2h" bug).
        # Storing the post-run state lets an otherwise-unchanged next tick
        # skip; genuine profile/initiative/user-watchlist edits still
        # diverge and trigger a fresh run. Caveat: an edit that lands while
        # this run is mid-flight is folded into the post-run baseline, so
        # it's picked up by the staleness-floor run rather than the very
        # next tick — bounded, not lost.
        post_state_hash = compute_research_state_hash(db_path=db_path)
        ok_tool_calls = sum(1 for t in tool_calls if t.get("ok"))
        usage_note = ""
        if usage:
            usage_note = (
                f", {usage.get('calls', 0)} model call(s), "
                f"{usage.get('web_search_requests', 0)} search(es)"
            )
        details: dict[str, Any] = {
            "state_hash": post_state_hash,
            "trigger": run_trigger,
            "findings": len(findings),
            "tool_calls": len(tool_calls),
            "ok_tool_calls": ok_tool_calls,
            "run_id": run_id,
        }
        if usage:
            # What the run did: calls, tokens and searches, per source.
            details["usage"] = usage
        audit_log(
            EVENT_RAN,
            (
                f"Executive research ran — {len(findings)} finding(s), "
                f"{ok_tool_calls}/{len(tool_calls)} tool call(s) ok{usage_note}"
            ),
            actor="scheduler",
            details=details,
        )
        return len(findings)
    except TimeoutError:
        logger.warning(
            "research.scheduler: workflow wall-clock timeout after %ds",
            _RUN_TIMEOUT_SECONDS,
        )
        import contextlib
        with contextlib.suppress(Exception):
            fail_run(run_id, "wall-clock timeout")
        audit_log(
            EVENT_FAILED,
            "Watchlist research timed out",
            actor="scheduler",
            details={"state_hash": state_hash, "reason": "timeout", "run_id": run_id},
        )
        return 0
    except Exception as exc:
        logger.exception("research.scheduler: workflow.run crashed")
        import contextlib
        with contextlib.suppress(Exception):
            fail_run(run_id, str(exc)[:200])
        audit_log(
            EVENT_FAILED,
            f"Watchlist research crashed: {exc}",
            actor="scheduler",
            details={"state_hash": state_hash, "error": str(exc)[:300], "run_id": run_id},
        )
        return 0


__all__ = [
    "EVENT_FAILED",
    "EVENT_RAN",
    "EVENT_SKIPPED",
    "HEARTBEAT_CHANNEL",
    "HEARTBEAT_CHANNEL_REF",
    "HEARTBEAT_INTENT",
    "HEARTBEAT_KIND",
    "bootstrap_watchlist_research_scan",
    "clear_research_run_history",
    "compute_research_state_hash",
    "enqueue_next_watchlist_research_scan",
    "run_watchlist_research_scan",
]
