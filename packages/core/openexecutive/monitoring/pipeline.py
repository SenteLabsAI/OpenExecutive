"""External-monitor scan + signal promotion pipeline.

Wires the source adapters into the existing scheduled-action / alert
infrastructure:

  1. ``run_external_monitor_scan`` — one tick. Walks enabled watchlist
     rows whose ``last_polled_at`` is stale enough, calls the matching
     adapter, writes signals, and promotes signals that survive triage
     into the existing alerts pipeline.
  2. ``bootstrap_external_monitor_scan`` / ``enqueue_next_external_monitor_scan``
     — heartbeat lifecycle, identical shape to nudge_engine's
     bootstrap / enqueue_next pair. Idempotent on restart.
  3. ``promote_signal_to_alert`` — turns a Signal into an
     ``AlertEvent`` and fires it through ``alerts.pipeline.schedule_evaluation``
     so the existing triage agent handles severity, channels,
     ``suggested_action``, and dispatch.

Design notes:

- Scan failures on one source must not poison the others — every
  adapter is wrapped in its own try/except, identical to nudge_engine.
- The scan is idempotent at the row level via the UNIQUE(dedup_key)
  constraint on ``external_signals``, so a duplicate tick is harmless.
- Adapter cadence is honoured by checking ``last_polled_at`` against
  the per-source minutes setting; the heartbeat interval is the *floor*
  on poll latency, not the actual poll rate.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from openexecutive.alerts.models import SEVERITY_RANK, AlertEvent, AlertSeverity
from openexecutive.alerts.pipeline import schedule_evaluation
from openexecutive.audit import log_event as audit_log
from openexecutive.config import get_settings
from openexecutive.memory.episodic import insert_scheduled_action
from openexecutive.monitoring import store
from openexecutive.monitoring.enrichment import build_company_context, enrich_signal
from openexecutive.monitoring.models import (
    MODE_ACTIVE,
    MODE_DRY_RUN,
    ORIGIN_RESEARCH_PROPOSED,
    OUTCOME_ALERTED,
    OUTCOME_FAILED,
    OUTCOME_HELD_DIGEST,
    OUTCOME_SUPPRESSED_BASELINE,
    OUTCOME_SUPPRESSED_BELOW_FLOOR,
    OUTCOME_SUPPRESSED_DRY_RUN,
    OUTCOME_SUPPRESSED_LOW_RELEVANCE,
    OUTCOME_SUPPRESSED_STALE,
    PAGE_WATCH_DIGEST_HOURS,
    SOURCE_KIND_PAGE_WATCH,
    SOURCE_KIND_QUERY,
    SOURCE_KIND_RSS,
    TRIGGER_ONLY_BIG,
    Signal,
    WatchlistItem,
)
from openexecutive.monitoring.sources import Source, get_source_for_kind
from openexecutive.monitoring.sources._http import strip_url_query
from openexecutive.monitoring.sources.base import (
    COULD_NOT_REACH,
    collapse_whitespace,
    future_skew_for,
    is_implausibly_future,
    take_observation,
)

logger = logging.getLogger(__name__)

HEARTBEAT_KIND = "external_monitor_scan"
HEARTBEAT_CHANNEL = "__internal__"
HEARTBEAT_CHANNEL_REF = "external_monitor"
HEARTBEAT_INTENT = "External-condition monitoring — periodic source poll."


# Re-exported under a friendlier name for in-module audit calls; the
# behaviour is identical to monitoring.sources._http.strip_url_query.
_safe_url = strip_url_query


# --------------------------------------------------------------------- #
# Per-source cadence resolution
# --------------------------------------------------------------------- #


def _poll_floor_minutes_for(signal_type: str) -> int:
    """Per-source poll-cadence floor in minutes.

    Read directly off the registered adapter's
    ``default_poll_interval_minutes`` — no central switch statement to
    keep in sync as new adapters land. Falls back to the heartbeat
    interval for unknown kinds (a row whose adapter was removed is still
    marked polled at that cadence, see _poll_one_watchlist_item).
    """
    src = get_source_for_kind(signal_type)
    if src is None:
        return get_settings().external_monitor_scan_interval_minutes
    return src.default_poll_interval_minutes


# The row's own ``cadence`` in minutes. ``real_time`` asks for the adapter's
# floor; an unknown value falls back to it too.
_CADENCE_MINUTES: dict[str, int] = {
    "real_time": 0,
    "15min": 15,
    "hourly": 60,
    "daily": 24 * 60,
    "weekly": 7 * 24 * 60,
}


def _poll_interval_minutes(item: WatchlistItem) -> int:
    """How often this row is checked: its own cadence, never faster than the
    adapter allows. A page watch set to weekly is checked weekly, not on the
    adapter's 6-hour floor; one set to 15min is still held to that floor."""
    floor = _poll_floor_minutes_for(item.signal_type)
    return max(floor, _CADENCE_MINUTES.get(item.cadence, 0))


def _due_for_poll(item: WatchlistItem, now: datetime) -> bool:
    if item.last_polled_at is None:
        return True
    try:
        last = datetime.fromisoformat(item.last_polled_at)
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return now - last >= timedelta(minutes=_poll_interval_minutes(item))


# --------------------------------------------------------------------- #
# Freshness gate
# --------------------------------------------------------------------- #


def _published(signal: Signal) -> datetime | None:
    """Parsed, tz-aware ``published_at``; None when absent or unparseable.

    An unparseable value is logged and treated as absent — the adapter
    writes ISO 8601, so a bad value is a bug to surface downstream, not a
    reason to drop the event silently.
    """
    if not signal.published_at:
        return None
    try:
        published = datetime.fromisoformat(signal.published_at)
    except ValueError:
        logger.warning(
            "monitoring.pipeline: unparseable published_at %r on %s — "
            "treating as undated", signal.published_at, signal.dedup_key,
        )
        return None
    if published.tzinfo is None:
        published = published.replace(tzinfo=UTC)
    return published


def _is_stale(signal: Signal, now: datetime, max_age_days: int) -> bool:
    """True when the upstream publish time is older than the age gate.

    Only signals that carry ``published_at`` can be judged; sources without
    an upstream timestamp (stock, page_watch, query) always pass.
    ``max_age_days <= 0`` disables the gate.
    """
    published = _published(signal)
    if published is None or max_age_days <= 0:
        return False
    return now - published > timedelta(days=max_age_days)


def _is_future(signal: Signal, now: datetime, skew: timedelta) -> bool:
    """True when ``published_at`` is more than ``skew`` ahead of ``now``.

    The feed controls the date, and a future one is a clock bug or a
    forgery. Unlike a stale entry this one can become valid — the date
    will pass — so the caller must NOT record it (recording burns the
    dedup key and would mute the entry forever, handing a feed a way to
    hide an announcement by dating it a day ahead). It is skipped for the
    tick, audited as deferred, and re-judged next time. A non-positive
    ``skew`` disables the check.
    """
    if skew <= timedelta(0):
        return False
    published = _published(signal)
    return published is not None and is_implausibly_future(published, now, skew=skew)


# --------------------------------------------------------------------- #
# Signal → alert promotion
# --------------------------------------------------------------------- #


def _cap_to_ceiling(
    hint: AlertSeverity, item: WatchlistItem,
) -> AlertSeverity:
    # NOTE: this caps the value
    # recorded on the external_signals row (used by the reflection
    # context in PR-C). The user-visible alert severity is independently
    # produced by the triage agent; PR-C adds an external_signal
    # severity hint to the triage prompt so the ceiling reaches the user.
    rank = SEVERITY_RANK[hint.value]
    ceiling_rank = SEVERITY_RANK[item.severity_ceiling.value]
    if rank > ceiling_rank:
        return item.severity_ceiling
    return hint


# Relevance → severity bands for sources that can't judge their own severity.
# The `query` and `page_watch` adapters emit every signal flat-LOW (a web hit
# or a page diff says nothing about how material it is), so the capture-time
# relevance read grades them: a directly-material one (>= _HIGH) is HIGH, a
# moderately-relevant one (>= _MEDIUM) is MEDIUM, the rest stay LOW. The grade
# is what triage reads as the severity hint, and what a watch that asks for
# big changes only (TRIGGER_ONLY_BIG) is held to. It is never a filter by
# itself: a watch tells the person whenever its own trigger is met. Capped to
# the row's ceiling so an operator's explicit cap is honoured.
_QUERY_RELEVANCE_HIGH = 0.8
_QUERY_RELEVANCE_MEDIUM = 0.5
_RELEVANCE_GRADED_SOURCES = frozenset({SOURCE_KIND_QUERY, SOURCE_KIND_PAGE_WATCH})
# What counts as a big change for a watch that asks for those only.
_BIG_RANK = SEVERITY_RANK[AlertSeverity.HIGH.value]


def _only_big(item: WatchlistItem) -> bool:
    """Read ``only_big`` the way the "Tells you when" sentence does (only a
    real ``true``), so the watch never holds back what its sentence says it
    tells you."""
    return (item.trigger_json or {}).get(TRIGGER_ONLY_BIG) is True


def _graded_by_relevance(item: WatchlistItem, signal: Signal) -> bool:
    """Whether this signal's severity comes from the relevance read. A feed
    post is flat-LOW too, so a feed that asks for big changes only is graded
    the same way; otherwise it keeps the adapter's value."""
    return signal.source_kind in _RELEVANCE_GRADED_SOURCES or (
        signal.source_kind == SOURCE_KIND_RSS and _only_big(item)
    )


def _grade_query_severity(
    enrichment: dict | None, item: WatchlistItem,
) -> AlertSeverity:
    """Map a query signal's capture-time relevance onto a severity band.

    ``enrichment`` is the ``SignalEnrichment.model_dump()`` payload (or ``None``
    when enrichment is disabled / missed, in which case the signal stays LOW —
    grading requires a relevance read). Result is capped to the watchlist row's
    ceiling.
    """
    score = 0.0
    if enrichment:
        try:
            score = float(enrichment.get("relevance_score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
    if score >= _QUERY_RELEVANCE_HIGH:
        graded = AlertSeverity.HIGH
    elif score >= _QUERY_RELEVANCE_MEDIUM:
        graded = AlertSeverity.MEDIUM
    else:
        graded = AlertSeverity.LOW
    return _cap_to_ceiling(graded, item)


def _without_links(text: str) -> str:
    from openexecutive.delegation.reminders import clean_text

    return clean_text(text).replace("[link]", "").strip() if text else ""


def _signal_to_alert_event(
    signal: Signal,
    item: WatchlistItem,
    enrichment: dict | None = None,
) -> AlertEvent:
    """Construct an AlertEvent from a promoted signal.

    The body deliberately interleaves the normalized summary, a
    structured slice of the raw payload, the watchlist slug, the
    adapter's severity hint, and the provenance URL so the triage
    prompt has every cue it needs in one place. ``AlertEvent`` itself
    has no slug or severity field, so anything triage needs to see
    must land in the body — the triage prompt's external-signals
    rules read directly from these lines.

    When capture-time ``enrichment`` is present, its one-line
    ``why_it_matters`` is surfaced high in the body so triage and the
    principal see the company-specific relevance, not just the raw event.
    """
    # The summary is line 1 of a body the triage prompt reads as labeled
    # lines; collapse whitespace here (whatever the adapter did) so no
    # source — feed title, model-emitted query hit — can forge its own
    # ``Severity hint:`` / ``Published:`` line.
    summary = collapse_whitespace(signal.normalized_summary)
    parts: list[str] = [summary]
    if enrichment:
        # The model wrote this line after reading outside text: links and
        # addresses go, so a planted one can't reach the card or a chat DM.
        why = _without_links(collapse_whitespace(str(enrichment.get("why_it_matters") or "")))
        if why:
            parts.append(f"Why this matters: {why}")
    # Slug + severity hint are required by the triage prompt's
    # external-signals rules. Putting them on their own labeled lines
    # makes them robust to the model's tendency to skim long bodies.
    parts.append(f"Watchlist: {item.slug}")
    parts.append(f"Severity hint: {signal.severity_hint.value}")
    # When it happened vs when we noticed it — distinct on purpose (issue
    # #80). The age gate already drops clearly stale items before this
    # point; these lines let triage weigh anything inside the window.
    if signal.published_at:
        parts.append(f"Published: {signal.published_at}")
    parts.append(f"Discovered: {signal.captured_at}")
    # What a page change added, one headline per line, so the card names
    # the news rather than "the page changed". Collapsed like the summary.
    new_links = signal.raw_payload.get("new_links") or []
    for link in new_links[:10] if isinstance(new_links, list) else []:
        if isinstance(link, dict) and link.get("url"):
            text = _without_links(collapse_whitespace(str(link.get("text") or "")))[:200]
            parts.append(f"New: {text} — {_safe_url(str(link['url']))}")
    target = signal.raw_payload.get("target_url")
    if target:
        parts.append(f"Source: {target}")
    if signal.provenance_url:
        parts.append(f"Provenance: {signal.provenance_url}")
    return AlertEvent(
        source=signal.source_kind,
        external_id=signal.dedup_key,
        subject=summary[:200],
        body="\n".join(parts)[:4000],
        # One watch = one open alert: repeats of the same watch coalesce
        # into the existing card (alerts.pipeline) while the signal-level
        # dedup_key keeps every observation in external_signals.
        dedup_hint=f"watch:{item.slug}",
        # A watch that belongs to a department (set by the research policy
        # from the department's watched entities, or by hand) sends its
        # alerts to that department's head and tags them department:<slug>
        # (alerts.pipeline reads both), so they queue on the head's /today
        # instead of the principal's. The slug goes in ``department`` — a
        # tag hint — never in ``channel``, which triage would read as the
        # room the event came from and answer with a team-room broadcast.
        routed_to_person_id=item.route_to_person_id,
        department=item.route_to_department,
    )


async def promote_signal_to_alert(
    signal: Signal,
    item: WatchlistItem,
    *,
    signal_id: int,
    db_path: Path | None = None,
    enrichment: dict | None = None,
) -> bool:
    """Promote a stored signal into the alerts pipeline. True when scheduled.

    Schedules the triage evaluation as fire-and-forget so a slow LLM
    call cannot stall the scan loop. The signal row is marked
    ``alerted`` immediately on schedule — the eventual
    ``promoted_alert_id`` backfill (after triage runs) is left for a
    later phase to avoid coupling the scan to evaluation latency.

    Caller is responsible for the dry-run / below-floor / dup gates;
    this entry point assumes the signal already cleared them.
    ``enrichment`` (when present) adds a "why this matters" line to the
    alert body.
    """
    event = _signal_to_alert_event(signal, item, enrichment)
    try:
        schedule_evaluation(event)
    except Exception:
        # Triage scheduling itself crashed — the signal was NOT alerted.
        # Record OUTCOME_FAILED so the audit trail doesn't lie about
        # what actually happened (and so a dashboard counting
        # `processed_outcome='alerted'` rows isn't inflated by failures).
        logger.exception(
            "monitoring.pipeline: schedule_evaluation failed for signal %d",
            signal_id,
        )
        store.mark_signal_processed(
            signal_id, OUTCOME_FAILED, promoted_alert_id=None, db_path=db_path
        )
        return False

    store.mark_signal_processed(
        signal_id, OUTCOME_ALERTED, promoted_alert_id=None, db_path=db_path
    )
    if item.id is not None:
        store.mark_fired(item.id, datetime.now(UTC), db_path=db_path)
    audit_log(
        "external_signal_promoted",
        f"Signal {signal_id} promoted to alert pipeline ({signal.source_kind})",
        actor="external_monitor",
        details={
            "signal_id": signal_id,
            "watchlist_id": item.id,
            "watchlist_slug": item.slug,
            "source_kind": signal.source_kind,
            "severity_hint": signal.severity_hint.value,
            "dedup_key": signal.dedup_key,
            # Strip query string before logging — a malicious feed could
            # embed a token in the link; we don't want it in audit.
            "provenance_url": _safe_url(signal.provenance_url),
        },
    )
    return True


# --------------------------------------------------------------------- #
# Page-watch daily digest
# --------------------------------------------------------------------- #


def _fired_within(item: WatchlistItem, now: datetime, hours: int) -> bool:
    if not item.last_fired_at:
        return False
    try:
        fired = datetime.fromisoformat(item.last_fired_at)
    except ValueError:
        return False
    if fired.tzinfo is None:
        fired = fired.replace(tzinfo=UTC)
    return now - fired < timedelta(hours=hours)


def _hold_for_digest(item: WatchlistItem, now: datetime) -> bool:
    """A busy page alerts at most once per digest window: the first change
    after a quiet spell goes out at once, later ones wait for the digest."""
    return (
        item.signal_type == SOURCE_KIND_PAGE_WATCH
        and _fired_within(item, now, PAGE_WATCH_DIGEST_HOURS)
    )


def _digest_signal(item: WatchlistItem, held: list[dict]) -> tuple[Signal, dict | None]:
    """Fold a watch's held changes (oldest first) into one Signal, plus the
    newest held enrichment for its "why this matters" line."""
    newest = held[-1]
    links: list[dict] = []
    seen: set[str] = set()
    texts: list[str] = []
    for row in reversed(held):  # newest headlines first
        payload = row.get("raw_payload") or {}
        for link in payload.get("new_links") or []:
            url = str(link.get("url") or "") if isinstance(link, dict) else ""
            if url and url not in seen:
                seen.add(url)
                links.append(link)
        if not payload.get("new_links") and payload.get("added_text"):
            texts.append(str(payload["added_text"])[:300])
    worst = max(
        (AlertSeverity(row.get("severity_hint") or "low") for row in held),
        key=lambda sev: SEVERITY_RANK[sev.value],
    )
    label = (newest.get("raw_payload") or {}).get("label") or item.slug
    first_seen = str(held[0].get("captured_at") or "")[:10]
    if links:
        titles = "; ".join(_without_links(str(link.get("text") or "")) for link in links[:3])
        more = len(links) - 3
        detail = f"new: {titles}" + (f" (+{more} more)" if more > 0 else "")
    else:
        detail = "new text: " + (_without_links(texts[0])[:160] if texts else "see the page")
    summary = f"[{label}] {len(held)} changes since {first_seen} — {detail}"
    readable = "\n".join(
        f"- {_without_links(str(link.get('text') or ''))} ({_safe_url(str(link.get('url') or ''))})"
        for link in links[:10]
    ) or "\n".join(_without_links(t) for t in texts[:3])
    signal = Signal(
        watchlist_id=item.id or 0,
        source_kind=SOURCE_KIND_PAGE_WATCH,
        source_external_id=f"digest:{newest['id']}",
        captured_at=str(newest.get("captured_at") or datetime.now(UTC).isoformat()),
        normalized_summary=summary[:500],
        raw_payload={
            "label": label,
            "target_url": item.target,
            "digest_of": [row["id"] for row in held],
            "new_links": links[:10],
            "summary": readable,
        },
        provenance_url=item.target,
        severity_hint=_cap_to_ceiling(worst, item),
        dedup_key=f"page_watch_digest:{item.id}:{newest['id']}",
    )
    enrichment = newest.get("enrichment") or None
    return signal, enrichment


async def _flush_page_watch_digests(
    now: datetime, *, db_path: Path | None = None,
) -> int:
    """Send one combined alert for each watch whose held changes have waited
    out the digest window. Returns how many digests were sent."""
    sent = 0
    for watchlist_id in store.watchlist_ids_with_held_signals(db_path=db_path):
        item = store.get_watchlist_item(watchlist_id, db_path=db_path)
        # A watch paused or put back in dry-run keeps its held changes until
        # it is live again; a deleted one has no rows left to send.
        if item is None or not item.enabled or item.mode != MODE_ACTIVE:
            continue
        if _fired_within(item, now, PAGE_WATCH_DIGEST_HOURS):
            continue
        held = store.list_held_signals(watchlist_id, db_path=db_path)
        if not held:
            continue
        signal, enrichment = _digest_signal(item, held)
        newest_id = int(held[-1]["id"])
        if not await promote_signal_to_alert(
            signal, item, signal_id=newest_id, db_path=db_path, enrichment=enrichment,
        ):
            # The newest row is marked failed; the rest stay held and go
            # out with the next digest.
            continue
        for row in held[:-1]:
            store.mark_signal_processed(int(row["id"]), OUTCOME_ALERTED, db_path=db_path)
        sent += 1
    return sent


# --------------------------------------------------------------------- #
# Scan orchestration
# --------------------------------------------------------------------- #


def _audit_received(signal_id: int, item: WatchlistItem, signal: Signal) -> None:
    """One ``external_signal_received`` row per signal landed in the DB."""
    audit_log(
        "external_signal_received",
        f"Signal received: {collapse_whitespace(signal.normalized_summary)[:120]}",
        actor="external_monitor",
        details={
            "signal_id": signal_id,
            "watchlist_id": item.id,
            "watchlist_slug": item.slug,
            "source_kind": signal.source_kind,
            "severity_hint": signal.severity_hint.value,
            "dedup_key": signal.dedup_key,
        },
    )


def _entries(n: int) -> str:
    return f"{n} entr{'y' if n == 1 else 'ies'}"


def _audit_deferred(item: WatchlistItem, tally: _PollTally) -> None:
    """One ``external_signal_deferred`` row per poll that skipped entries
    dated implausibly far in the future — aggregated, since a deferred entry
    is by design not recorded and would otherwise re-log on every tick.
    Carries what the row would have shown (summary + link), so an operator
    can tell WHAT was held back without re-fetching the feed."""
    deferred = tally.deferred
    skew = tally.deferred_skew
    logger.debug(
        "monitoring.pipeline: %r deferred %s dated in the future",
        item.slug, _entries(len(deferred)),
    )
    audit_log(
        "external_signal_deferred",
        f"Deferred {_entries(len(deferred))} dated in the future on {item.slug}",
        actor="external_monitor",
        details={
            "watchlist_id": item.id,
            "watchlist_slug": item.slug,
            "source_kind": deferred[0].source_kind,
            "count": len(deferred),
            "future_skew_hours": skew.total_seconds() / 3600,
            "race_tick": tally.race_tick,
            # Enough to find and read them; the same fields the received /
            # promoted rows already carry.
            "entries": [
                {
                    "dedup_key": s.dedup_key,
                    "published_at": s.published_at,
                    "summary": collapse_whitespace(s.normalized_summary)[:120],
                    "provenance_url": _safe_url(s.provenance_url),
                }
                for s in deferred[:10]
            ],
        },
    )


def _audit_suppressed(
    signal_id: int,
    item: WatchlistItem,
    signal: Signal,
    outcome: str,
) -> None:
    """Symmetric audit emission for a suppressed signal — pairs with
    ``external_signal_received`` so a reader sees both 'noticed' and
    'suppressed' rows for the same dedup_key."""
    audit_log(
        "external_signal_suppressed",
        f"Signal {signal_id} suppressed ({outcome}): "
        f"{collapse_whitespace(signal.normalized_summary)[:120]}",
        actor="external_monitor",
        details={
            "signal_id": signal_id,
            "watchlist_id": item.id,
            "watchlist_slug": item.slug,
            "source_kind": signal.source_kind,
            "severity_hint": signal.severity_hint.value,
            "dedup_key": signal.dedup_key,
            "outcome": outcome,
        },
    )


# During a lost race, how far past our own clock an entry may be dated and
# still be judged now (ordinary publisher drift); anything later is deferred
# rather than recorded, since nothing we fetched can honestly be dated later.
# Independent of MAX_FUTURE_SKEW / EXTERNAL_MONITOR_MAX_FUTURE_SKEW_HOURS,
# which bound the normal (non-race) path far more leniently.
_RACE_CLOCK_TOLERANCE = timedelta(minutes=5)


@dataclass(frozen=True)
class _LostRace:
    """How a scan that lost the baseline claim treats the row's entries.

    Another scan baselined the row (stamp ``cutoff``) while our fetch was
    in flight, so we hold (mostly) the same back-catalogue — and every
    entry the winner held is already recorded, so it dedups. Entries
    dated at or before the cutoff are baseline; undated or unparseable
    ones too — the feed controls ``published_at`` and we can't tell old
    from new without it. Entries dated after the cutoff are news, except
    that the caller defers (never records) anything dated more than
    ``_RACE_CLOCK_TOLERANCE`` past its own clock, so a post-dated entry
    that only our fetch holds is re-judged later instead of burned.
    """

    cutoff: str
    # Entries our own adapter carved out of the baseline (see
    # ``_split_baseline_exempt``). The winner didn't baseline these either
    # — it promotes them after its transaction — so treating one as
    # "already recorded" here is exactly how a live outage goes missing:
    # we unblock the moment the winner commits, while it still has up to
    # 200 audit inserts to get through, so the loser reaches the open
    # incident FIRST and would burn its dedup key as baseline. Never
    # covered, whatever its date; the UNIQUE(dedup_key) constraint keeps
    # the two scans from both promoting it.
    exempt: frozenset[str] = frozenset()

    def covers(self, signal: Signal) -> bool:
        if signal.dedup_key in self.exempt:
            return False
        published = _published(signal)
        if published is None:
            # Undated, or a bad date. Fail closed — it must not become an
            # alert on a race tick.
            return True
        try:
            cutoff = datetime.fromisoformat(self.cutoff)
        except ValueError:
            return True
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=UTC)
        return published <= cutoff


def _lost_race(
    item: WatchlistItem,
    fetched_at: datetime,
    exempt: frozenset[str] = frozenset(),
    *,
    db_path: Path | None = None,
) -> _LostRace:
    """Build the lost-race policy from the winner's stamp."""
    assert item.id is not None
    fresh = store.get_watchlist_item(item.id, db_path=db_path)
    cutoff = fresh.baselined_at if fresh is not None and fresh.baselined_at else None
    if cutoff is None:
        # Lost the claim yet no stamp is visible: the row was deleted, or a
        # concurrent initialize_db backfill stamped and rolled back. Fall
        # back to a cutoff past our own fetch (plus the drift tolerance),
        # so everything we fetched is covered: this tick is recorded as
        # baseline, nothing promoted — say so.
        logger.warning(
            "monitoring.pipeline: %r lost the baseline claim but no stamp is "
            "visible — treating this tick's entries as baseline", item.slug,
        )
        cutoff = (fetched_at + _RACE_CLOCK_TOLERANCE).isoformat()
    return _LostRace(cutoff, exempt)


def _split_baseline_exempt(
    src: Source,
    item: WatchlistItem,
    entries: list[Signal],
    matches: Callable[[Signal], bool],
) -> tuple[list[Signal], list[Signal]]:
    """Split a seeding row's first-poll entries into (archive, exempt).

    ``exempt`` is what the adapter's optional ``promote_on_baseline`` hook
    claims is live news rather than back-catalogue (see
    ``sources.base.Source``); ``archive`` is everything else and is
    baselined exactly as before. An adapter without the hook exempts
    nothing, which is the historical behaviour for ``rss`` / ``edgar``.

    An entry that misses the row's trigger filter stays in ``archive``
    however live it is. The baseline's guarantee is that EVERYTHING in the
    feed is recorded, trigger misses included, so that widening a trigger
    later cannot resurface what was already there — and an exempt entry
    that missed the trigger would be dropped unrecorded by the cascade,
    punching a hole in exactly that guarantee.

    A hook that raises is treated as "not exempt" — the same fail-quiet
    stance ``matches_trigger`` takes, and the safe one: a crashing hook
    must not turn a first poll into a back-catalogue replay.
    """
    hook = getattr(src, "promote_on_baseline", None)
    if hook is None:
        return entries, []
    archive: list[Signal] = []
    exempt: list[Signal] = []
    for signal in entries:
        try:
            is_exempt = bool(hook(signal, item)) and matches(signal)
        except Exception:
            logger.exception(
                "monitoring.pipeline: promote_on_baseline crashed on %r — "
                "treating as baseline", item.slug,
            )
            is_exempt = False
        (exempt if is_exempt else archive).append(signal)
    return archive, exempt


def _record_baseline(
    item: WatchlistItem,
    entries: list[Signal],
    fetched_at: datetime,
    *,
    db_path: Path | None = None,
) -> int | None:
    """Try to baseline a seeding row: claim the stamp and record every
    entry it is handed — the row's back-catalogue, trigger misses
    included, adapter-exempted entries excluded (see
    ``_split_baseline_exempt``) — as ``suppressed_baseline`` in ONE
    transaction.

    Returns how many rows were recorded, or ``None`` when another scan won
    the claim first (nothing written). The claim is a compare-and-swap on
    ``baselined_at IS NULL``: two overlapping scans (heartbeat + client
    rotation, or a slow tick) can both load the row un-baselined, and
    exactly one of them may record the back-catalogue. The write is
    all-or-nothing, so a failure part-way leaves the stamp NULL and the
    next tick baselines the whole feed again rather than replaying an
    unrecorded tail as news.
    """
    assert item.id is not None
    recorded = store.record_baseline(item.id, fetched_at, entries, db_path=db_path)
    if recorded is None:
        return None
    for signal_id, signal in recorded:
        _audit_received(signal_id, item, signal)
        _audit_suppressed(signal_id, item, signal, OUTCOME_SUPPRESSED_BASELINE)
    logger.info(
        "monitoring.pipeline: baselined %r (%s) — %s recorded, none promoted",
        item.slug, item.signal_type, _entries(len(recorded)),
    )
    return len(recorded)

@dataclass
class _ScanBudget:
    """The scan-wide ``max_signals_per_scan`` allowance.

    Mutable and decremented in place by ``_poll_one_watchlist_item`` as it
    admits signals to the alert pipeline, so the accounting survives a row
    that raises part-way (after promoting) — a returned count would be
    lost with the exception and the cap would fail open.
    """

    remaining: int

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0

    def admit(self) -> None:
        self.remaining -= 1


@dataclass
class _PollTally:
    """Per-row outcome of ``_poll_one_watchlist_item``, filled in place.

    ``written`` is the number of rows landed in ``external_signals`` this
    tick (NOT the number alerted — some get suppressed by floor / dry_run
    / dup). Mutable so the caller still sees a true count when the poll
    raises part-way (the audit row must not claim "0 signals" for a row
    that inserted four and then died). ``failed`` is set for a poll that
    could not run at all (no adapter, adapter crash); an exception that
    escapes the poll is the other failure signal and is handled by the
    caller.
    """

    written: int = 0
    failed: bool = False
    # Entries skipped this tick for being dated implausibly far ahead. Kept
    # here (not as a loop local) so the caller can audit them even when
    # the poll raises part-way — the audit row is their only trace.
    deferred: list[Signal] = field(default_factory=list)
    deferred_skew: timedelta = timedelta(0)
    race_tick: bool = False
    # What this check did with what it found, for the watch's last-check
    # note: summaries it told the person about, ``(outcome, summary)`` for
    # what it held back, how many entries it noted as already there, and
    # what the adapter said when it emitted nothing.
    told: list[str] = field(default_factory=list)
    held: list[tuple[str, str]] = field(default_factory=list)
    baselined: int = 0
    observation: str = ""


def _suppress(
    signal_id: int,
    item: WatchlistItem,
    signal: Signal,
    outcome: str,
    *,
    tally: _PollTally | None = None,
    db_path: Path | None = None,
) -> None:
    """Record a suppression outcome on the row and emit its paired audit
    event. Every branch of the cascade goes through here so the two stay
    in lockstep."""
    store.mark_signal_processed(signal_id, outcome, db_path=db_path)
    _audit_suppressed(signal_id, item, signal, outcome)
    if tally is not None:
        tally.held.append((outcome, signal.normalized_summary))


# --------------------------------------------------------------------- #
# Last-check note
# --------------------------------------------------------------------- #

_NOTE_SUMMARY_CHARS = 140
# Why a change was noticed and not passed on, said to the person.
_HELD_WORDS: dict[str, str] = {
    OUTCOME_SUPPRESSED_DRY_RUN: "It is watching quietly, so it did not tell you.",
    OUTCOME_SUPPRESSED_BELOW_FLOOR: "Not a big change, so it stayed quiet.",
    OUTCOME_SUPPRESSED_LOW_RELEVANCE: "Not close enough to your company, so it stayed quiet.",
    OUTCOME_HELD_DIGEST: "It already told you today, so this waits for the next summary.",
}


def _note_summary(text: str) -> str:
    """A signal's one-line summary as the note quotes it: the leading
    ``[label]`` the adapters add is dropped (the note sits under the
    watch's own name), links go, and it is cut to a line."""
    line = _without_links(collapse_whitespace(text))
    if line.startswith("[") and "] " in line[:90]:
        line = line.split("] ", 1)[1]
    line = line.strip().rstrip(".")
    if len(line) > _NOTE_SUMMARY_CHARS:
        line = line[: _NOTE_SUMMARY_CHARS - 1].rstrip() + "…"
    return line[:1].upper() + line[1:]


def check_note(item: WatchlistItem, tally: _PollTally, *, failed: bool = False) -> str:
    """What this check found, and why it stayed quiet when it did."""
    if failed:
        return COULD_NOT_REACH
    if tally.told:
        more = len(tally.told) - 1
        return f"Told you: {_note_summary(tally.told[-1])}." + (f" And {more} more." if more > 0 else "")
    for outcome, summary in reversed(tally.held):
        if outcome in _HELD_WORDS:
            why = _HELD_WORDS[outcome]
            if outcome == OUTCOME_SUPPRESSED_DRY_RUN and item.origin == ORIGIN_RESEARCH_PROPOSED:
                why = "It is waiting for your OK, so it did not tell you."
            return f"Noticed: {_note_summary(summary)}. {why}"
    if any(outcome == OUTCOME_SUPPRESSED_STALE for outcome, _ in tally.held):
        return "Found only older items, so it stayed quiet."
    if tally.baselined:
        return "First look: it noted what is already there. It tells you about what is new from here on."
    return tally.observation or "Nothing new."


async def _poll_one_watchlist_item(
    item: WatchlistItem,
    now: datetime,
    *,
    budget: _ScanBudget,
    tally: _PollTally,
    company_ctx: str = "",
    db_path: Path | None = None,
) -> None:
    """Poll one watchlist row, persist signals, promote where appropriate.

    Rows landed in ``external_signals`` are counted on ``tally`` and each
    signal admitted to the cascade is charged to ``budget`` — both in
    place, so the accounting survives a poll that raises part-way.
    Baseline rows are written but never charged: they don't reach the
    alert pipeline the cap protects, and counting them would truncate the
    baseline mid-feed and let the tail replay as news on the next tick.
    """
    src = get_source_for_kind(item.signal_type)
    if src is None:
        logger.warning(
            "monitoring.pipeline: no registered adapter for signal_type=%r "
            "(watchlist slug=%r) — skipping",
            item.signal_type, item.slug,
        )
        tally.failed = True
        # Still consume the row's cadence slot, or it would re-fire (and
        # emit a failed audit row) on every heartbeat until fixed.
        if item.id is not None:
            store.mark_polled(item.id, now, db_path=db_path)
        return

    max_age_days = get_settings().external_monitor_max_signal_age_days

    try:
        take_observation(item)  # drop a stale one from a poll that raised
        emitted = await src.poll(item, db_path=db_path)
        tally.observation = take_observation(item)
        fetched_at = datetime.now(UTC)
        # Inside the guard so a settings/config surprise here is handled
        # like an adapter crash (logged, row still marked polled).
        future_skew = future_skew_for(item)
    except Exception:
        logger.exception(
            "monitoring.pipeline: adapter %s crashed for watchlist %r",
            item.signal_type, item.slug,
        )
        tally.failed = True
        return
    finally:
        # Always mark polled even on adapter crash so a broken source
        # doesn't get hammered on every tick.
        if item.id is not None:
            store.mark_polled(item.id, now, db_path=db_path)

    if not emitted:
        # Failed or empty fetch: nothing to baseline, and ``baselined_at``
        # stays NULL so the next successful poll IS the baseline.
        return

    capped = [
        raw.model_copy(update={"severity_hint": _cap_to_ceiling(raw.severity_hint, item)})
        for raw in emitted
    ]

    # Feed-listing sources (Source.seed_on_first_poll) are baselined on
    # the first poll that returns entries: EVERYTHING in the feed —
    # including entries that miss the trigger filter — is recorded as
    # seen, nothing is promoted. Recording the misses too means the first
    # entry that ever matches a keyword is news, not "back-catalogue",
    # and widening a trigger later cannot resurface what was already in
    # the feed. Keyed on ``baselined_at`` rather than ``last_polled_at``
    # so a transient fetch failure on the first tick can't forfeit the
    # baseline. ``item`` is the pre-poll snapshot, so the in-memory value
    # is unaffected by ``mark_polled``. Baseline rows never reach the
    # alert pipeline, so they are never charged to the scan budget.
    #
    # An adapter may exempt individual entries from its own baseline via
    # the optional ``promote_on_baseline`` hook (see ``sources.base``):
    # ``vendor_status`` uses it so an incident that is still OPEN when the
    # watch is added surfaces on that first poll while the resolved
    # archive is still swallowed. Exempt entries skip the baseline write
    # and fall through to the ordinary cascade below.
    # Apply the per-item trigger filter (default: True). Entries that miss
    # it are dropped — unless this scan lost the baseline race, in which
    # case a miss the winner didn't hold is still recorded as baseline so a
    # later, wider trigger can't resurface it (same guarantee the winner
    # gives by recording misses).
    def _matches(signal: Signal) -> bool:
        try:
            return bool(src.matches_trigger(signal, item))
        except Exception:
            logger.exception(
                "monitoring.pipeline: matches_trigger crashed on %r — "
                "treating as no-match", item.slug
            )
            return False

    lost_race: _LostRace | None = None
    # Entries the adapter carved out of its own baseline. They still face
    # the age gate below. That gate was briefly lifted for them, on the
    # reasoning that a long-running incident updates rarely and would be
    # recorded as stale; four review rounds then showed what it was
    # holding back. An adapter's "this is live" is a reading of markup the
    # VENDOR controls, and every way that reading went wrong reported a
    # years-old resolved incident as live — with the gate lifted, nothing
    # stood between an archive and a HIGH alert. So the exemption buys an
    # entry past the baseline, never past its own timestamp: whatever the
    # adapter believes, an entry the feed dates outside the age window is
    # not news. The cost is the case that lifted it — an open incident
    # silent for longer than the window is reported on its next update
    # rather than immediately — and that is the direction to be wrong in.
    exempt_keys: set[str] = set()
    if (
        getattr(src, "seed_on_first_poll", False)
        and item.id is not None
        and item.baselined_at is None
    ):
        archive, exempt = _split_baseline_exempt(src, item, capped, _matches)
        exempt_keys = {signal.dedup_key for signal in exempt}
        recorded = _record_baseline(item, archive, fetched_at, db_path=db_path)
        if recorded is not None:
            tally.written += recorded
            tally.baselined += recorded
            if not exempt:
                return
            # The stamp is claimed and the archive is recorded, so this row
            # is no longer seeding: the exempt entries run the ordinary
            # cascade. Dying here leaves them unrecorded, and the next tick
            # (no longer a first poll) promotes them normally instead of
            # replaying the archive. The one gap is a concurrent scan: if
            # it lost the CAS while we were fetching, our stamp covers the
            # exempt entries it also holds, so it may record one as
            # baseline before we promote it — muted until its next
            # <updated>, which for a live incident is minutes away.
            capped = exempt
        else:
            # Lost the claim: keep the whole feed (the winner's stamp
            # decides what counts as baseline) but carry the exempt keys
            # into that policy, so an open incident is judged as news here
            # too rather than swallowed by the winner's cutoff.
            lost_race = _lost_race(
                item, fetched_at, frozenset(exempt_keys), db_path=db_path,
            )

    # On a race tick the deferral bound is the tight drift tolerance, not
    # the lenient configured skew: nothing we fetched can honestly be dated
    # much later than our own clock, and deferring (rather than recording)
    # is what keeps a post-dated entry from being burned for good.
    skew = _RACE_CLOCK_TOLERANCE if lost_race is not None else future_skew
    tally.deferred_skew = skew
    tally.race_tick = lost_race is not None
    cap_logged = False
    for signal in capped:
        baseline_entry = lost_race is not None and lost_race.covers(signal)
        # The three guards below apply only to entries that are NOT baseline;
        # a baseline entry is always recorded (and never charged).
        if not baseline_entry and not _matches(signal):
            continue
        if not baseline_entry and _is_future(signal, fetched_at, skew):
            tally.deferred.append(signal)  # not recorded: see _is_future
            continue
        if not baseline_entry and budget.exhausted:
            # Skip (don't break): baseline-covered entries later in the
            # list must still be recorded, or they'd replay next tick.
            if not cap_logged:
                logger.info(
                    "monitoring.pipeline: max_signals_per_scan cap hit at "
                    "watchlist %r (signal_type=%s) — dropping the remaining "
                    "non-baseline entries this tick",
                    item.slug, item.signal_type,
                )
                cap_logged = True
            continue

        signal_id = store.insert_signal(signal, db_path=db_path)
        if signal_id is None:
            # UNIQUE(dedup_key) clash — same upstream event, already
            # recorded. No-op. We don't even log debug because the same
            # vendor status page returns the same 25 entries every poll.
            continue

        tally.written += 1
        if not baseline_entry:
            budget.admit()
        _audit_received(signal_id, item, signal)

        # Suppression cascade — order matters. Each branch emits a
        # paired external_signal_suppressed audit event so a reader can
        # tell from the audit log alone (without joining external_signals)
        # what was noticed but not surfaced.
        #
        # Freshness gates come first: a baseline entry or a stale one is
        # "already happened" news regardless of mode / floor, and neither
        # should spend an enrichment call.
        if baseline_entry:
            _suppress(
                signal_id, item, signal, OUTCOME_SUPPRESSED_BASELINE,
                db_path=db_path,
            )
            tally.baselined += 1
            continue

        if _is_stale(signal, now, max_age_days):
            _suppress(
                signal_id, item, signal, OUTCOME_SUPPRESSED_STALE,
                tally=tally, db_path=db_path,
            )
            continue

        if item.mode == MODE_DRY_RUN:
            _suppress(
                signal_id, item, signal, OUTCOME_SUPPRESSED_DRY_RUN,
                tally=tally, db_path=db_path,
            )
            continue

        # The watch's own trigger was met (``_matches`` above), so this
        # change reaches the person. There is no severity floor: the "Tells
        # you when" sentence is the only filter, and the one thing it can add
        # is "only when it is a big change" (TRIGGER_ONLY_BIG).
        #
        # Sources that can't judge their own severity (query, page_watch, and
        # a feed that asks for big changes only) arrive flat-LOW, so the
        # capture-time relevance read runs first and grades them. Sources
        # that grade themselves (stock, edgar, vendor_status) are held to
        # "big" on the adapter's value before any model call is spent.
        only_big = _only_big(item)
        enrichment_payload: dict | None = None
        enriched = False
        if _graded_by_relevance(item, signal):
            should_promote, enrichment_payload = await _maybe_enrich(
                signal, item, signal_id, company_ctx=company_ctx, tally=tally, db_path=db_path,
            )
            if not should_promote:
                # Relevance gate already recorded + audited the suppression.
                continue
            enriched = True
            signal = signal.model_copy(update={
                "severity_hint": _grade_query_severity(enrichment_payload, item),
            })
            # No relevance read (enrichment off, or a miss) means there is
            # nothing to call it small on: it goes through.
            judged = enrichment_payload is not None
        else:
            judged = True

        if only_big and judged and SEVERITY_RANK[signal.severity_hint.value] < _BIG_RANK:
            _suppress(
                signal_id, item, signal, OUTCOME_SUPPRESSED_BELOW_FLOOR,
                tally=tally, db_path=db_path,
            )
            continue

        # Capture-time enrichment for self-graded sources runs here, after
        # the gates above, so no model call is spent on a signal already
        # held back. Fail-open: a miss leaves enrichment None and the signal
        # promotes unchanged. The optional relevance gate is the only branch
        # that can suppress here (default threshold 0.0 = off).
        if not enriched:
            should_promote, enrichment_payload = await _maybe_enrich(
                signal, item, signal_id, company_ctx=company_ctx, tally=tally, db_path=db_path,
            )
            if not should_promote:
                continue

        if _hold_for_digest(item, now):
            # The watch already alerted inside the digest window: keep this
            # change for the one combined alert the window's end sends.
            # The row was written LOW, before grading; keep the graded value
            # so the digest goes out at the gravest held change's severity.
            store.set_signal_severity(signal_id, signal.severity_hint.value, db_path=db_path)
            _suppress(signal_id, item, signal, OUTCOME_HELD_DIGEST, tally=tally, db_path=db_path)
            continue

        if await promote_signal_to_alert(
            signal, item, signal_id=signal_id, db_path=db_path,
            enrichment=enrichment_payload,
        ):
            tally.told.append(signal.normalized_summary)



async def _maybe_enrich(
    signal: Signal,
    item: WatchlistItem,
    signal_id: int,
    *,
    company_ctx: str,
    tally: _PollTally | None = None,
    db_path: Path | None = None,
) -> tuple[bool, dict | None]:
    """Enrich a promotable signal, persist it, and apply the relevance gate.

    Returns ``(should_promote, enrichment_payload)``:
      - ``(True, dict)``  — enriched; promote with the payload.
      - ``(True, None)``  — enrichment disabled or a miss; promote unchanged.
      - ``(False, dict)`` — relevance gate fired; already recorded + audited,
        do NOT promote.
    """
    settings = get_settings()
    # Per-row opt-out via config_json["enrich"] = false; default on when the
    # global switch is enabled, so enrichment applies to every source.
    if not settings.external_monitor_enrichment_enabled:
        return True, None
    # Per-row opt-out: any falsy config value (false / 0 / "") disables it.
    # ``is False`` would miss JSON ``0``; a truthiness test is what we want.
    if not item.config_json.get("enrich", True):
        return True, None

    result = await enrich_signal(signal, item, company_ctx=company_ctx)
    if result is None:
        return True, None

    payload = result.model_dump()
    try:
        store.update_signal_enrichment(signal_id, payload, db_path=db_path)
    except Exception:
        logger.exception(
            "monitoring.pipeline: persisting enrichment failed for signal %d",
            signal_id,
        )

    if result.relevance_score < settings.external_monitor_enrichment_min_relevance:
        _suppress(
            signal_id, item, signal, OUTCOME_SUPPRESSED_LOW_RELEVANCE,
            tally=tally, db_path=db_path,
        )
        return False, payload

    return True, payload


async def run_external_monitor_scan(
    now: datetime | None = None,
    *,
    db_path: Path | None = None,
) -> int:
    """Run one scan pass over the enabled watchlist. Returns total signals written.

    "Signals written" counts rows landed in ``external_signals``, which
    includes suppressed-dry-run / suppressed-below-floor entries — they
    still get the audit trail. Excludes UNIQUE(dedup_key) clashes
    (no row written) and adapter crashes (no row written).
    """
    now = now or datetime.now(UTC)
    settings = get_settings()
    cap_total = settings.external_monitor_max_signals_per_scan

    items = store.list_watchlist(enabled_only=True, db_path=db_path)
    if not items:
        return 0

    # Company context for enrichment is the same for every signal this tick, so
    # load it once (one profile + initiatives read) rather than per signal.
    # Empty string when enrichment is globally off — _maybe_enrich short-circuits
    # before it's used, so we skip the load entirely in that case.
    company_ctx = (
        build_company_context(db_path=db_path)
        if settings.external_monitor_enrichment_enabled
        else ""
    )

    total_written = 0
    budget = _ScanBudget(remaining=cap_total)
    poll_start = datetime.now(UTC)
    for item in items:
        if budget.exhausted:
            break
        # Billed standing-query adapter has its own kill switch — skip those
        # rows entirely (no poll, no mark_polled) when disabled.
        if (
            item.signal_type == SOURCE_KIND_QUERY
            and not settings.external_monitor_query_enabled
        ):
            continue
        if not _due_for_poll(item, now):
            continue
        per_source_start = datetime.now(UTC)
        tally = _PollTally()
        failed = False
        try:
            await _poll_one_watchlist_item(
                item, now, budget=budget, tally=tally,
                company_ctx=company_ctx, db_path=db_path,
            )
        except Exception:
            # A store failure on one row (e.g. the shared DB locked during
            # its baseline transaction) must not skip every row after it;
            # the poll is retried on the next tick. Anything it admitted
            # before failing is already charged to the budget.
            failed = True
            logger.exception(
                "monitoring.pipeline: polling %r (%s) failed — continuing "
                "with the remaining watchlist rows", item.slug, item.signal_type,
            )
        except BaseException:
            # Cancellation (shutdown mid-scan) must propagate, but the
            # audit row below must not read as a clean, empty poll.
            failed = True
            raise
        finally:
            # The poll audit row is emitted even for a failed row, so the
            # audit log stands on its own as the record of what the
            # monitor did this tick — including how many rows landed
            # before a failure.
            failed = failed or tally.failed
            total_written += tally.written
            if item.id is not None:
                try:
                    store.set_last_check_note(
                        item.id, check_note(item, tally, failed=failed), db_path=db_path,
                    )
                except Exception:
                    logger.exception("monitoring.pipeline: last-check note failed for %r", item.slug)
            if tally.deferred:
                _audit_deferred(item, tally)
            duration_ms = int(
                (datetime.now(UTC) - per_source_start).total_seconds() * 1000
            )
            status = "FAILED" if failed else f"{tally.written} signal(s)"
            audit_log(
                "external_monitor_poll",
                f"Polled {item.slug} ({item.signal_type}) — {status}",
                actor="external_monitor",
                details={
                    "watchlist_id": item.id,
                    "watchlist_slug": item.slug,
                    "signal_type": item.signal_type,
                    "signals_emitted": tally.written,
                    "duration_ms": duration_ms,
                    "failed": failed,
                },
            )

    try:
        await _flush_page_watch_digests(now, db_path=db_path)
    except Exception:
        logger.exception("monitoring.pipeline: page-watch digest flush failed")

    scan_duration_ms = int(
        (datetime.now(UTC) - poll_start).total_seconds() * 1000
    )
    logger.info(
        "monitoring.pipeline: scan complete — %d signal(s) across %d watchlist "
        "row(s) in %dms (cap_total=%d, budget_remaining=%d)",
        total_written, len(items), scan_duration_ms, cap_total, budget.remaining,
    )
    return total_written


# --------------------------------------------------------------------- #
# Heartbeat bootstrap / chain — mirrors scheduler/nudge_engine.py
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


def bootstrap_external_monitor_scan(
    db_path: Path | None = None,
) -> int | None:
    """Ensure exactly one pending external_monitor_scan row exists.

    Idempotent — safe to call on every boot. Returns the new action id
    if one was inserted, else None.
    """
    if _heartbeat_pending(db_path):
        return None
    settings = get_settings()
    run_at = datetime.now(UTC) + timedelta(
        minutes=settings.external_monitor_scan_interval_minutes
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
            "monitoring.bootstrap: heartbeat scheduled at %s (id=%d)",
            run_at.isoformat(), action_id,
        )
        return action_id
    except Exception:
        logger.exception(
            "monitoring.bootstrap: failed to enqueue heartbeat"
        )
        return None


def enqueue_next_external_monitor_scan(
    *,
    after: datetime | None = None,
    db_path: Path | None = None,
) -> int | None:
    """Schedule the next external_monitor_scan tick. Called by the runner
    after each fire."""
    settings = get_settings()
    base = (after or datetime.now(UTC)).astimezone(UTC)
    run_at = base + timedelta(
        minutes=settings.external_monitor_scan_interval_minutes
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
            "monitoring.enqueue_next: next scan at %s (id=%d)",
            run_at.isoformat(), action_id,
        )
        return action_id
    except Exception:
        logger.exception(
            "monitoring.enqueue_next: insert failed"
        )
        return None


__all__ = [
    "HEARTBEAT_CHANNEL",
    "HEARTBEAT_CHANNEL_REF",
    "HEARTBEAT_INTENT",
    "HEARTBEAT_KIND",
    "bootstrap_external_monitor_scan",
    "enqueue_next_external_monitor_scan",
    "promote_signal_to_alert",
    "run_external_monitor_scan",
]
