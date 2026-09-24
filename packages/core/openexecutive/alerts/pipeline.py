from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from openexecutive.alerts import dispatcher, preferences, store
from openexecutive.alerts.models import (
    AlertEvent,
    TriageDecision,
)

logger = logging.getLogger(__name__)

# Hold strong references to in-flight background tasks so GC cannot cancel them
# mid-flight (mirrors memory.episodic._background_tasks).
_background_tasks: set[asyncio.Task[Any]] = set()

# Simple rolling-window rate limiter on event evaluations. Cost guard against
# runaway cost from a misbehaving integration. Single-process only.
_RATE_LIMIT_PER_MIN = 60
_recent_event_ts: deque[float] = deque(maxlen=_RATE_LIMIT_PER_MIN)
_rate_lock = threading.Lock()


# Severities that justify re-dispatching a coalesced alert (a repeat that
# escalated the open situation into "look now" territory).
_REDISPATCH_SEVERITIES = frozenset({"high", "urgent"})


def _person_id_from_event(event: AlertEvent) -> int | None:
    """Routing target: ONLY the explicit ``routed_to_person_id`` field.

    ``event.user`` is deliberately not parsed: integrations fill it with a
    sender's self-chosen display name (Telegram, Google Chat), so a
    ``person:<id>`` there would let an outsider pick the routing target.
    The create_alert tool sets the explicit field itself.
    """
    return event.routed_to_person_id


def _with_department_tag(tags: list[str], channel: str | None) -> list[str]:
    """Carry a ``department:<slug>`` routing hint into the row's tags.

    Accepts the bare slug (``AlertEvent.department``) or the legacy
    ``department:<slug>`` channel form."""
    chan = (channel or "").strip()
    if chan.startswith("department:"):
        chan = chan.split(":", 1)[1]
    slug = chan.strip().lower()
    if not slug:
        return list(tags)
    tag = f"department:{slug}"
    return list(tags) if tag in tags else [*tags, tag]


def _rate_limited() -> bool:
    """Returns True when the per-minute cap has been hit."""
    now = time.monotonic()
    with _rate_lock:
        cutoff = now - 60.0
        while _recent_event_ts and _recent_event_ts[0] < cutoff:
            _recent_event_ts.popleft()
        if len(_recent_event_ts) >= _RATE_LIMIT_PER_MIN:
            return True
        _recent_event_ts.append(now)
        return False


async def evaluate_and_dispatch(
    event: AlertEvent,
    db_path: Path | None = None,
) -> tuple[TriageDecision, int | None]:
    """Run triage on a single event and dispatch the alert.

    Returns (decision, alert_id). alert_id is None when the alert is
    suppressed or a duplicate.
    """
    from openexecutive.agents.triage import TriageAgent
    from openexecutive.memory.episodic import get_active_initiatives

    path = db_path or store.DB_PATH

    if _rate_limited():
        logger.warning(
            "alerts.pipeline rate-limited: dropping event source=%s ext_id=%s",
            event.source,
            event.external_id,
        )
        return (
            TriageDecision(
                alert=False,
                dedup_key=f"rate-limited-{event.source}-{event.external_id}",
                reason_if_suppressed="rate_limited",
            ),
            None,
        )

    # Cheap pre-checks: load context for the triage prompt + post-decision mute.
    prefs = preferences.get_preferences(db_path=path)
    mutes = [m.pattern for m in store.list_mutes(db_path=path)]
    recent = [
        {
            "headline": a.headline,
            "severity": a.severity,
            "dedup_key": a.dedup_key,
            "topic_tags": a.topic_tags,
        }
        for a in store.recent_alerts(limit=20, db_path=path)
    ]
    try:
        initiatives = get_active_initiatives()
    except Exception:
        initiatives = []

    agent = TriageAgent()
    decision = await agent.triage(
        event,
        recent_alerts=recent,
        mute_patterns=mutes,
        active_initiatives=initiatives,
    )

    # Post-decision mute check: triage may have produced a tag we mute.
    if decision.alert and preferences.matches_mute(decision.topic_tags, mutes):
        logger.info(
            "alerts.pipeline post-mute suppressing event source=%s ext_id=%s tags=%s",
            event.source,
            event.external_id,
            decision.topic_tags,
        )
        decision = decision.model_copy(
            update={"alert": False, "reason_if_suppressed": "muted_post_triage"}
        )

    if not decision.alert:
        # We still persist a low-severity record so the UI can show "23 events
        # triaged, 1 surfaced" if we ever want it. For now keep it simple:
        # only persist when the decision says alert=true.
        return decision, None

    effective_channels = preferences.resolve_channels(
        decision.channels, decision.severity, prefs
    )

    # A producer that knows the stable identity of the situation (a watch
    # slug) wins over the model's free-text key.
    dedup_key = event.dedup_hint or decision.dedup_key
    topic_tags = _with_department_tag(
        decision.topic_tags, event.department or event.channel,
    )
    routed_to = _person_id_from_event(event)

    # A replay of the SAME upstream event (webhook retry, re-poll) is a
    # no-op, exactly as before: the (source, external_id) row already exists.
    if event.external_id and store.get_alert_by_external(
        event.source, event.external_id, db_path=path
    ) is not None:
        logger.info(
            "alerts.pipeline duplicate suppressed source=%s ext_id=%s",
            event.source, event.external_id,
        )
        return decision, None

    # Coalesce a repeat of an open alert into the existing row instead of
    # stacking another card — and stay quiet unless the repeat is graver
    # (a 2% move followed by a 10% crash must still ping).
    coalesced = store.coalesce_alert(
        source=event.source,
        dedup_key=dedup_key,
        severity=decision.severity.value,
        body=decision.body,
        db_path=path,
    )
    if coalesced is not None:
        existing_id, raised = coalesced
        logger.info(
            "alerts.pipeline coalesced source=%s ext_id=%s into alert=%d raised=%s",
            event.source, event.external_id, existing_id, raised,
        )
        if not (raised and decision.severity.value in _REDISPATCH_SEVERITIES):
            return decision, None
        alert = store.get_alert(existing_id, db_path=path)
        if alert is None:
            return decision, None
        await dispatcher.dispatch_all(
            alert,
            effective_channels,
            db_path=path,
            department_slug=decision.department_slug,
            broadcast_integration=decision.broadcast_integration,
        )
        return decision, existing_id

    alert_id = store.insert_alert(
        source=event.source,
        external_id=event.external_id,
        severity=decision.severity.value,
        headline=decision.headline or (event.subject or event.title or event.source),
        body=decision.body,
        suggested_action=decision.suggested_action,
        topic_tags=topic_tags,
        dedup_key=dedup_key,
        routed_to_person_id=routed_to,
        db_path=path,
    )
    if alert_id is None:
        logger.info(
            "alerts.pipeline duplicate suppressed source=%s ext_id=%s",
            event.source,
            event.external_id,
        )
        return decision, None

    alert = store.get_alert(alert_id, db_path=path)
    if alert is None:
        logger.error("alerts.pipeline could not re-read alert id=%s", alert_id)
        return decision, alert_id

    await dispatcher.dispatch_all(
        alert,
        effective_channels,
        db_path=path,
        # Shift 3: thread the broadcast-routing context through so the
        # DEPARTMENT_CHANNEL / COMPANY_BROADCAST dispatchers can resolve
        # the right room. Empty strings when triage picked per-person
        # channels only — those dispatchers ignore the kwargs.
        department_slug=decision.department_slug,
        broadcast_integration=decision.broadcast_integration,
    )
    return decision, alert_id


def schedule_evaluation(event: AlertEvent) -> None:
    """Fire-and-forget evaluation. Safe to call from sync or async context.

    Mirrors memory.episodic.schedule_extraction's GC-safe pattern, including
    its audit-context snapshot: the triage model call happens in a task that
    starts after the caller's ``with set_turn(...)`` has exited, so without
    carrying the ids explicitly every triage row records with
    ``session_id=NULL``. The thread branch needs it even more — a new thread
    starts from an empty context and inherits nothing.

    A caller with no turn bound (the scheduler, an external-monitor sweep)
    snapshots ``(None, None)`` and is unchanged: those really are
    out-of-turn calls.
    """
    from openexecutive.audit.context import get_active_ids

    audit_sid, audit_tid = get_active_ids()
    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(_evaluate_in_turn(event, audit_sid, audit_tid))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    except RuntimeError:
        threading.Thread(
            target=lambda: asyncio.run(
                _evaluate_in_turn(event, audit_sid, audit_tid)
            ),
            daemon=True,
        ).start()


async def _evaluate_in_turn(
    event: AlertEvent,
    audit_session_id: str | None,
    audit_turn_id: str | None,
) -> None:
    """Re-bind the scheduling caller's audit ids, then evaluate."""
    from openexecutive.audit.context import get_active_ids, set_turn

    # Per-field fallback — see memory.episodic.extract_and_store for why a
    # half-empty snapshot must not erase the ambient counterpart.
    ambient_session, ambient_turn = get_active_ids()
    effective_session = audit_session_id if audit_session_id is not None else ambient_session
    effective_turn = audit_turn_id if audit_turn_id is not None else ambient_turn

    if (effective_session, effective_turn) == (ambient_session, ambient_turn):
        await evaluate_and_dispatch(event)
        return

    with set_turn(session_id=effective_session, turn_id=effective_turn):
        await evaluate_and_dispatch(event)
