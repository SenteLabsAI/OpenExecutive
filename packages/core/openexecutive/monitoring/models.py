"""Data models for the external-condition monitoring layer.

A ``WatchlistItem`` is what the user declares they want monitored: a
ticker, a status-page URL, an RSS feed, etc. A ``Signal`` is what an
adapter emits when it detects a change worth recording. Most signals
never become alerts — the triage pipeline gates that.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from openexecutive.alerts.models import AlertSeverity

# Source-kind taxonomy used by adapters and the watchlist routing layer.
# Free-text in the DB so PR-B / PR-C can ship new adapters without a
# schema migration; this constant is the single source of truth for v1
# names and lets call sites import a symbol instead of stringly-typing.
SOURCE_KIND_VENDOR_STATUS = "vendor_status"
SOURCE_KIND_RSS = "rss"  # reserved for PR-B
SOURCE_KIND_STOCK = "stock"  # reserved for PR-B
# Standing web-search query — runs a natural-language query on a cadence via
# the provider's web_search tool (Anthropic native or OpenRouter's server tool),
# so the watchlist can monitor *arbitrary* conditions, not just feeds.
SOURCE_KIND_QUERY = "query"
# SEC EDGAR filings — polls a company's recent filings (8-K / 10-K / 10-Q /
# Form 4, …) by ticker or CIK; the highest-signal keyless structured source.
SOURCE_KIND_EDGAR = "edgar"
# Generic web-page change detection — hashes a page's normalized visible text
# and emits a Signal when it changes vs the last poll; watches feed-less pages
# (pricing, careers, leadership). Stateful (see monitoring.store page_watch_state).
SOURCE_KIND_PAGE_WATCH = "page_watch"

# page_watch fetch-source marker. Every page_watch row is now fetched with the
# keyless httpx fetcher; rows written before the xcrawl scrape path was removed
# still carry ``config_json["fetch"] == "xcrawl"`` and a baseline captured as
# xcrawl markdown. The adapter treats that marker as "baseline is stale":
# it re-captures the baseline on the next poll without emitting a signal and
# clears the marker, so no phantom change is reported for the switch.
PAGE_WATCH_FETCH_KEY = "fetch"
PAGE_WATCH_FETCH_LEGACY_XCRAWL = "xcrawl"

# Watchlist row "mode" values. v1 default is ``active`` per the user's
# instruction — dry-run is wired but opt-in (an entry created with
# ``mode=dry_run`` has its emitted signals captured + scored but never
# promoted to an alert).
MODE_ACTIVE = "active"
MODE_DRY_RUN = "dry_run"
_VALID_MODES = frozenset({MODE_ACTIVE, MODE_DRY_RUN})

# Who put a row on the watchlist. ``manual`` = the principal (UI / API);
# ``executive`` = the chat tool acting on a request; ``research`` = the
# research pass auto-added it (grounded in company data); and
# ``research_proposed`` = the research pass was unsure, so the row sits in
# ``dry_run`` as a SUGGESTION until the principal approves or declines it.
ORIGIN_MANUAL = "manual"
ORIGIN_EXECUTIVE = "executive"
ORIGIN_RESEARCH = "research"
ORIGIN_RESEARCH_PROPOSED = "research_proposed"
_VALID_ORIGINS = frozenset({
    ORIGIN_MANUAL, ORIGIN_EXECUTIVE, ORIGIN_RESEARCH, ORIGIN_RESEARCH_PROPOSED,
})
# Rows the research policy owns: subject to auto-disable and to the declines
# memory when removed.
RESEARCH_ORIGINS = frozenset({ORIGIN_RESEARCH, ORIGIN_RESEARCH_PROPOSED})

# Declines memory (``watchlist_declines``). ``declined_explicit`` is
# permanent: the principal said no. ``expired_unreviewed`` means a suggestion
# aged out without anyone looking; it may be re-proposed once after
# DECLINE_EXPIRED_RETRY_DAYS so a timer never blacklists a good source.
DECLINE_KIND_EXPLICIT = "declined_explicit"
DECLINE_KIND_EXPIRED = "expired_unreviewed"
DECLINE_EXPIRED_RETRY_DAYS = 90
# Why the principal declined — each maps to a different remedy in the
# research policy (see monitoring.research.watch_policy).
DECLINE_REASON_NOT_RELEVANT = "not_relevant"   # blacklist the entity
DECLINE_REASON_TOO_NOISY = "too_noisy"         # keep, but quieter
DECLINE_REASON_WRONG_SOURCE = "wrong_source"   # blacklist this target only
DECLINE_REASON_EXPIRED = "expired"             # aged out unreviewed
VALID_DECLINE_REASONS = frozenset({
    DECLINE_REASON_NOT_RELEVANT, DECLINE_REASON_TOO_NOISY,
    DECLINE_REASON_WRONG_SOURCE, DECLINE_REASON_EXPIRED,
})
# The reasons a principal (or a tool acting for one) may give; `expired` is
# only ever written by the sweep.
EXPLICIT_DECLINE_REASONS = frozenset({
    DECLINE_REASON_NOT_RELEVANT, DECLINE_REASON_TOO_NOISY, DECLINE_REASON_WRONG_SOURCE,
})

# Trigger key every source understands: tell the person only about big
# changes. It is the one way a watch is made quieter beyond its own trigger,
# and it is part of the "Tells you when" sentence the person reads, so what
# holds a change back is always what the watch says about itself.
TRIGGER_ONLY_BIG = "only_big"

# Watchlist row "cadence" hints — used by the briefing/digest layer in
# PR-C to decide whether a signal surfaces real-time vs in a digest.
# Kept as opaque strings here; pipeline ignores them in PR-A.
_VALID_CADENCES = frozenset({"real_time", "15min", "hourly", "daily", "weekly"})


class WatchlistItem(BaseModel):
    """A single external condition the Executive should watch.

    ``signal_type`` is free-text so PR-B/PR-C can register new source
    kinds without a schema migration; the runtime adapter registry in
    ``monitoring.sources`` maps it to a concrete poll() implementation.

    ``trigger_json`` is an opaque dict consumed by the adapter's
    ``matches_trigger`` — e.g. ``{"abs_change_pct_gte": 5}`` for stocks
    or ``{"any": true}`` for RSS. Adapters validate their own subset.
    """

    # Internal model — fail loud on unexpected columns so a future
    # schema rename doesn't get silently dropped at row→model conversion.
    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    slug: str  # stable user-visible identifier (e.g. "vendor-aws")
    signal_type: str  # e.g. SOURCE_KIND_VENDOR_STATUS
    target: str  # ticker / URL / keyword / dept slug
    config_json: dict[str, Any] = Field(default_factory=dict)
    trigger_json: dict[str, Any] = Field(default_factory=dict)
    cadence: str = "15min"
    # Kept on the row but no longer a filter: the pipeline never holds a
    # change back for being under the floor. A watch tells the person
    # whenever its trigger is met (see TRIGGER_ONLY_BIG for the quieter
    # version). The ceiling still caps the severity recorded on a signal.
    severity_floor: AlertSeverity = AlertSeverity.LOW
    severity_ceiling: AlertSeverity = AlertSeverity.URGENT
    route_to_specialist: str = ""
    route_to_department: str = ""
    route_to_person_id: int | None = None
    mode: str = MODE_ACTIVE
    enabled: bool = True
    created_at: str = ""
    last_polled_at: str | None = None
    # Set once a seeding source (Source.seed_on_first_poll) has completed a
    # poll that returned entries, i.e. the feed's existing back-catalogue
    # has been recorded as seen. NULL until then — a failed or empty first
    # fetch leaves the NEXT successful poll as the baseline. Never set for
    # non-seeding sources.
    baselined_at: str | None = None
    last_fired_at: str | None = None
    fired_count: int = 0
    dismiss_count: int = 0
    trust_score: float = 1.0  # 0..1, decays on dismiss; full trust at creation
    notes: str = ""
    origin: str = ORIGIN_MANUAL
    # The goal this watch is for (``department_goals.id``), when it has one.
    # Who it is for is ``route_to_person_id`` (None = the principal).
    goal_id: int | None = None
    # What the last check found, in plain words, and why it stayed quiet
    # when it did ("Down 1.1% today. Under 5%, so it stayed quiet."). Written
    # by the scan after every poll; "" until the first one.
    last_check_note: str = ""


class WatchlistDecline(BaseModel):
    """A target the principal declined (or a suggestion that aged out)."""

    model_config = ConfigDict(extra="forbid")

    normalized_target: str
    entity: str = ""
    signal_type: str = ""
    slug: str = ""
    kind: str = DECLINE_KIND_EXPLICIT
    reason: str = ""
    declined_at: str = ""


def is_valid_mode(mode: str) -> bool:
    return mode in _VALID_MODES


def is_valid_origin(origin: str) -> bool:
    return origin in _VALID_ORIGINS


def is_valid_cadence(cadence: str) -> bool:
    return cadence in _VALID_CADENCES


class Signal(BaseModel):
    """Adapter output: one detected change worth recording.

    ``dedup_key`` MUST be deterministic over the underlying event so
    re-polling the same source twice doesn't produce two rows. The
    adapter computes it; the store enforces uniqueness.

    ``provenance_url`` is mandatory — every surfaced signal must let
    the principal verify the underlying source in one click. The
    triage / briefing layers depend on this for trust-building.
    """

    # Internal model — fail loud on unexpected columns so a future
    # schema rename doesn't get silently dropped at row→model conversion.
    model_config = ConfigDict(extra="forbid")

    watchlist_id: int
    source_kind: str
    source_external_id: str
    captured_at: str  # ISO 8601 UTC — when WE first saw the event
    # When the upstream says the event happened (ISO 8601 UTC), when the
    # source publishes one — RSS <pubDate>, Atom <updated>, EDGAR filing
    # date. None for sources with no upstream timestamp (stock, page_watch,
    # query). The pipeline's age gate reads this; ``captured_at`` is never
    # a proxy for it (issue #80: a January article discovered in September
    # is not September news).
    published_at: str | None = None
    normalized_summary: str  # one-line human description
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    provenance_url: str
    severity_hint: AlertSeverity = AlertSeverity.LOW
    dedup_key: str


# Outcomes recorded on the ``external_signals`` row after pipeline
# processing. ``alerted`` is the success case; everything else explains
# why the signal did NOT surface so debugging "what did you ignore
# overnight" is a simple SQL query.
OUTCOME_ALERTED = "alerted"
OUTCOME_SUPPRESSED_DRY_RUN = "suppressed_dry_run"
OUTCOME_SUPPRESSED_BELOW_FLOOR = "suppressed_below_floor"  # see note below
OUTCOME_SUPPRESSED_DUP = "suppressed_dup"
OUTCOME_SUPPRESSED_TRIAGE = "suppressed_triage"
# Held back because the watch asks for big changes only (TRIGGER_ONLY_BIG)
# and this one was not graded big. The name predates that: it used to mean
# "under the row's severity floor", a second filter that no longer exists.
# Captured + enriched, but the capture-time relevance score fell below the
# configured floor (external_monitor_enrichment_min_relevance). Recorded, not
# promoted — the gate is opt-in (default threshold 0.0 = off) until calibrated.
OUTCOME_SUPPRESSED_LOW_RELEVANCE = "suppressed_low_relevance"
# Recorded on the FIRST poll of a feed-listing source (rss, edgar): every
# entry already in the feed is persisted as "seen" so dedup covers it, but
# nothing is promoted — the watch reports what changes from now on, not
# the feed's back-catalogue. Mirrors page_watch's first-observation baseline.
OUTCOME_SUPPRESSED_BASELINE = "suppressed_baseline"
# Upstream ``published_at`` is older than EXTERNAL_MONITOR_MAX_SIGNAL_AGE_DAYS
# at capture time. Persisted for the audit trail + dedup; never promoted.
OUTCOME_SUPPRESSED_STALE = "suppressed_stale"
OUTCOME_FAILED = "failed"
# A page_watch change that cleared every gate while its watch had already
# alerted within PAGE_WATCH_DIGEST_HOURS. Held, not dropped: the next scan
# after the window folds every held change into ONE alert (the daily digest
# for a busy page), and each held row then moves to ``alerted``.
OUTCOME_HELD_DIGEST = "held_digest"
PAGE_WATCH_DIGEST_HOURS = 24

_VALID_OUTCOMES = frozenset({
    OUTCOME_ALERTED,
    OUTCOME_HELD_DIGEST,
    OUTCOME_SUPPRESSED_BASELINE,
    OUTCOME_SUPPRESSED_STALE,
    OUTCOME_SUPPRESSED_DRY_RUN,
    OUTCOME_SUPPRESSED_BELOW_FLOOR,
    OUTCOME_SUPPRESSED_DUP,
    OUTCOME_SUPPRESSED_TRIAGE,
    OUTCOME_SUPPRESSED_LOW_RELEVANCE,
    OUTCOME_FAILED,
})


def is_valid_outcome(outcome: str) -> bool:
    return outcome in _VALID_OUTCOMES
