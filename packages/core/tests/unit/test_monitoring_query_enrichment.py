"""Unit tests for the query source adapter + capture-time enrichment (P0).

Covers:
  - QuerySource.poll: result→Signal mapping, dedup stability, URL/SSRF drops,
    max_results cap, web-search-disabled + provider-error fail-soft, keyword
    trigger.
  - enrichment.enrich_signal: parse + fail-soft.
  - pipeline integration: enrichment persisted + why_it_matters in alert body,
    relevance gate suppression, fail-open promotion, query kill switch.
  - store: idempotent enrichment_json migration on a pre-existing DB.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from openexecutive.alerts.models import AlertSeverity
from openexecutive.alerts.store import initialize_db as initialize_alerts_db
from openexecutive.memory.episodic import initialize_db as initialize_episodic_db
from openexecutive.monitoring import enrichment as me
from openexecutive.monitoring import pipeline as mp
from openexecutive.monitoring import store as ms
from openexecutive.monitoring.enrichment import SignalEnrichment, enrich_signal
from openexecutive.monitoring.models import (
    OUTCOME_ALERTED,
    OUTCOME_SUPPRESSED_BELOW_FLOOR,
    OUTCOME_SUPPRESSED_LOW_RELEVANCE,
    Signal,
    WatchlistItem,
)
from openexecutive.monitoring.sources import _REGISTRY as SOURCE_REGISTRY
from openexecutive.monitoring.sources.query import (
    QuerySource,
    _coerce_max_results,
    _QueryResearchAgent,
)

# --------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------- #


class _FakeBlock:
    def __init__(self, *, type: str = "tool_use", name: str = "", input: dict | None = None):
        self.type = type
        self.name = name
        self.input = input or {}


class _FakeMessage:
    def __init__(self, content: list[Any]):
        self.content = content


def _query_results_message(results: list[dict[str, Any]]) -> _FakeMessage:
    return _FakeMessage([
        _FakeBlock(type="text", name="", input={}),
        _FakeBlock(name="emit_query_results", input={"results": results}),
    ])


def _make_query_item(
    *,
    slug: str = "q-acme",
    target: str = "Acme Corp layoffs OR funding",
    config: dict | None = None,
    trigger: dict | None = None,
) -> WatchlistItem:
    return WatchlistItem(
        id=1,
        slug=slug,
        signal_type="query",
        target=target,
        config_json=config or {},
        trigger_json=trigger or {},
    )


def _stub_query_agent(monkeypatch: pytest.MonkeyPatch, message: Any) -> dict[str, Any]:
    """Patch the adapter's agent so poll() never makes a provider call.

    Returns a dict that captures the tools/model passed, for assertions.
    """
    captured: dict[str, Any] = {}

    async def fake_analyze_with_tools(self: Any, user_content: str, **kwargs: Any) -> Any:
        captured["user_content"] = user_content
        captured["tools"] = kwargs.get("tools")
        captured["model_override"] = kwargs.get("model_override")
        if isinstance(message, Exception):
            raise message
        return message

    monkeypatch.setattr(
        _QueryResearchAgent, "analyze_with_tools", fake_analyze_with_tools
    )
    return captured


def _allow_all_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the SSRF guard's DNS resolution (unavailable in the sandbox).

    Matches the rss/stock adapter tests, which stub ``validate_target_url``
    for the same reason. The guard's own IP logic is covered in the _http
    tests; here we only need the adapter to treat (True, "") as "keep".
    """
    monkeypatch.setattr(
        "openexecutive.monitoring.sources.query.validate_target_url",
        lambda u: (True, ""),
    )


# --------------------------------------------------------------------- #
# QuerySource.poll
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_query_emits_signal_per_result_with_stable_dedup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _stub_query_agent(
        monkeypatch,
        _query_results_message([
            {
                "title": "Acme cuts 12% of staff",
                "url": "https://news.example/acme-layoffs?utm_source=x",
                "summary": "Acme announced layoffs affecting 12% of staff.",
                "published": "2026-05-30",
            },
            {
                "title": "Acme raises Series C",
                "url": "https://news.example/acme-series-c",
                "summary": "Acme closed a $40M round.",
            },
        ]),
    )
    _allow_all_urls(monkeypatch)

    src = QuerySource()
    item = _make_query_item(config={"label": "Acme watch"})
    signals = await src.poll(item)

    assert len(signals) == 2
    # web_search tool is included alongside emit_query_results.
    assert any(t.get("type", "").startswith("web_search_") for t in captured["tools"])
    # Provenance strips the query string; summary carries the label prefix.
    assert signals[0].provenance_url == "https://news.example/acme-layoffs"
    assert signals[0].normalized_summary.startswith("[Acme watch] ")
    assert all(s.dedup_key.startswith("query:") for s in signals)

    # Dedup is deterministic across polls of the same results.
    again = await src.poll(item)
    assert [s.dedup_key for s in again] == [s.dedup_key for s in signals]


@pytest.mark.asyncio
async def test_query_drops_urlless_and_non_public_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_query_agent(
        monkeypatch,
        _query_results_message([
            {"title": "No URL here", "summary": "model forgot the link"},
            {"title": "SSRF attempt", "url": "http://169.254.169.254/latest",
             "summary": "metadata endpoint"},
            {"title": "Good one", "url": "https://news.example/ok",
             "summary": "legit"},
        ]),
    )
    # Fake guard: reject the metadata IP, allow the rest — exercises the
    # adapter's drop-on-(False) behaviour without needing real DNS.
    monkeypatch.setattr(
        "openexecutive.monitoring.sources.query.validate_target_url",
        lambda u: (False, "non-public") if "169.254" in u else (True, ""),
    )

    src = QuerySource()
    signals = await src.poll(_make_query_item())

    # Only the public, clickable result survives (URL-less + SSRF dropped).
    assert len(signals) == 1
    assert signals[0].provenance_url == "https://news.example/ok"


@pytest.mark.asyncio
async def test_query_respects_max_results_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_query_agent(
        monkeypatch,
        _query_results_message([
            {"title": f"r{i}", "url": f"https://news.example/{i}", "summary": "s"}
            for i in range(5)
        ]),
    )
    _allow_all_urls(monkeypatch)
    src = QuerySource()
    signals = await src.poll(_make_query_item(config={"max_results": 2}))
    assert len(signals) == 2


@pytest.mark.asyncio
async def test_query_cap_counts_valid_results_not_raw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dropped results at the front must not starve the max_results budget."""
    _stub_query_agent(
        monkeypatch,
        _query_results_message([
            {"title": "no url"},  # dropped: no URL
            {"title": "ssrf", "url": "http://10.0.0.1/x", "summary": "s"},  # dropped
            {"title": "good 1", "url": "https://news.example/1", "summary": "s"},
            {"title": "good 2", "url": "https://news.example/2", "summary": "s"},
        ]),
    )
    monkeypatch.setattr(
        "openexecutive.monitoring.sources.query.validate_target_url",
        lambda u: (False, "private") if "10.0.0.1" in u else (True, ""),
    )
    src = QuerySource()
    # max_results=2 and exactly 2 valid results exist (after 2 leading drops).
    signals = await src.poll(_make_query_item(config={"max_results": 2}))
    assert len(signals) == 2
    assert [s.provenance_url for s in signals] == [
        "https://news.example/1", "https://news.example/2",
    ]


@pytest.mark.asyncio
async def test_query_empty_when_web_search_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No provider stub needed — poll() must bail before any call.
    monkeypatch.setattr(
        "openexecutive.monitoring.sources.query.build_web_search_tool",
        lambda: None,
    )
    src = QuerySource()
    assert await src.poll(_make_query_item()) == []


@pytest.mark.asyncio
async def test_query_empty_on_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_query_agent(monkeypatch, RuntimeError("provider down"))
    src = QuerySource()
    assert await src.poll(_make_query_item()) == []


@pytest.mark.asyncio
async def test_query_empty_target_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_query_agent(monkeypatch, _query_results_message([]))
    src = QuerySource()
    assert await src.poll(_make_query_item(target="   ")) == []


def test_query_keyword_trigger_filters() -> None:
    src = QuerySource()
    sig = Signal(
        watchlist_id=1,
        source_kind="query",
        source_external_id="x",
        captured_at=datetime.now(UTC).isoformat(),
        normalized_summary="[Acme] Acme cuts staff",
        raw_payload={"summary": "layoffs announced"},
        provenance_url="https://news.example/x",
        dedup_key="query:abc",
    )
    assert src.matches_trigger(sig, _make_query_item(trigger={"keywords": ["layoff"]}))
    assert not src.matches_trigger(
        sig, _make_query_item(trigger={"keywords": ["acquisition"]})
    )
    # No keywords → permissive.
    assert src.matches_trigger(sig, _make_query_item())


def test_coerce_max_results_clamps() -> None:
    assert _coerce_max_results(None) == 5
    assert _coerce_max_results("bad") == 5
    assert _coerce_max_results(0) == 1
    assert _coerce_max_results(100) == 10
    assert _coerce_max_results(3) == 3


# --------------------------------------------------------------------- #
# enrichment.enrich_signal
# --------------------------------------------------------------------- #


def _relevance_message(score: float, why: str, initiative: str = "") -> _FakeMessage:
    return _FakeMessage([
        _FakeBlock(name="emit_relevance", input={
            "relevance_score": score,
            "why_it_matters": why,
            "matched_initiative": initiative,
        }),
    ])


def _stub_enrich_agent(monkeypatch: pytest.MonkeyPatch, message: Any) -> None:
    async def fake(self: Any, user_content: str, **kwargs: Any) -> Any:
        if isinstance(message, Exception):
            raise message
        return message

    monkeypatch.setattr(me._EnrichmentAgent, "analyze_with_tools", fake)


def _make_signal(summary: str = "Acme cuts staff") -> Signal:
    return Signal(
        watchlist_id=1,
        source_kind="query",
        source_external_id="x",
        captured_at=datetime.now(UTC).isoformat(),
        normalized_summary=summary,
        raw_payload={"summary": "layoffs"},
        provenance_url="https://news.example/x",
        dedup_key="query:abc",
    )


@pytest.mark.asyncio
async def test_enrich_signal_parses_relevance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_enrich_agent(
        monkeypatch, _relevance_message(0.8, "Hits our vendor-consolidation push", "Vendor consolidation")
    )
    result = await enrich_signal(
        _make_signal(), _make_query_item(), company_ctx="ctx"
    )
    assert isinstance(result, SignalEnrichment)
    assert result.relevance_score == 0.8
    assert "vendor" in result.why_it_matters.lower()


@pytest.mark.asyncio
async def test_enrich_signal_none_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_enrich_agent(monkeypatch, RuntimeError("boom"))
    assert await enrich_signal(_make_signal(), _make_query_item(), company_ctx="") is None


@pytest.mark.asyncio
async def test_enrich_signal_none_on_missing_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_enrich_agent(monkeypatch, _FakeMessage([_FakeBlock(type="text")]))
    assert await enrich_signal(_make_signal(), _make_query_item(), company_ctx="") is None


# --------------------------------------------------------------------- #
# Pipeline integration
# --------------------------------------------------------------------- #


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "test_query_enrich.db"
    monkeypatch.setattr("openexecutive.memory.episodic.DB_PATH", db_path)
    monkeypatch.setattr("openexecutive.alerts.store.DB_PATH", db_path)
    initialize_episodic_db(db_path)
    initialize_alerts_db(db_path)
    ms.initialize_db(db_path)
    return db_path


class _FakeSource:
    kind: str = "vendor_status"
    default_poll_interval_minutes: int = 5
    seed_on_first_poll: bool = False

    def __init__(self, signals: list[Signal]) -> None:
        self._signals = signals

    async def poll(
        self, item: WatchlistItem, *, db_path: Path | None = None
    ) -> list[Signal]:
        return [s.model_copy(update={"watchlist_id": item.id or 0}) for s in self._signals]

    def matches_trigger(self, signal: Signal, item: WatchlistItem) -> bool:
        return True


@pytest.fixture
def install_fake_source(monkeypatch: pytest.MonkeyPatch):
    original = SOURCE_REGISTRY.get("vendor_status")

    def _set(signals: list[Signal]) -> None:
        SOURCE_REGISTRY["vendor_status"] = _FakeSource(signals)  # type: ignore[assignment]

    yield _set
    if original is not None:
        SOURCE_REGISTRY["vendor_status"] = original


def _vendor_signal(dedup_key: str = "vendor_status:e1") -> Signal:
    return Signal(
        watchlist_id=0,
        source_kind="vendor_status",
        source_external_id="up-1",
        captured_at=datetime.now(UTC).isoformat(),
        normalized_summary="AWS us-east-1 degraded",
        raw_payload={"source": "fake"},
        provenance_url="https://status.aws/incident/1",
        severity_hint=AlertSeverity.HIGH,
        dedup_key=dedup_key,
    )


@pytest.mark.asyncio
async def test_enrichment_persisted_and_in_alert_body(
    db: Path, install_fake_source, monkeypatch: pytest.MonkeyPatch,
) -> None:
    promoted: list[Any] = []
    monkeypatch.setattr(mp, "schedule_evaluation", lambda e: promoted.append(e))
    monkeypatch.setattr(mp, "build_company_context", lambda db_path=None: "CTX")

    async def fake_enrich(signal, item, *, company_ctx):
        return SignalEnrichment(
            relevance_score=0.9,
            why_it_matters="Threatens our AWS-dependent launch",
            matched_initiative="Launch",
        )

    monkeypatch.setattr(mp, "enrich_signal", fake_enrich)

    install_fake_source([_vendor_signal()])
    wl_id = ms.insert_watchlist_item(
        slug="vendor-aws", signal_type="vendor_status",
        target="https://status.aws/rss", severity_floor=AlertSeverity.LOW,
        db_path=db,
    )

    written = await mp.run_external_monitor_scan(db_path=db)
    assert written == 1
    assert len(promoted) == 1
    assert "Why this matters: Threatens our AWS-dependent launch" in promoted[0].body

    sigs = ms.list_signals_for_watchlist(wl_id, db_path=db)
    assert sigs[0]["processed_outcome"] == OUTCOME_ALERTED
    assert sigs[0]["enrichment"]["relevance_score"] == 0.9


@pytest.mark.asyncio
async def test_relevance_gate_suppresses(
    db: Path, install_fake_source, monkeypatch: pytest.MonkeyPatch,
) -> None:
    promoted: list[Any] = []
    monkeypatch.setattr(mp, "schedule_evaluation", lambda e: promoted.append(e))
    monkeypatch.setattr(mp, "build_company_context", lambda db_path=None: "CTX")

    async def fake_enrich(signal, item, *, company_ctx):
        return SignalEnrichment(relevance_score=0.1, why_it_matters="meh")

    monkeypatch.setattr(mp, "enrich_signal", fake_enrich)
    # Turn the gate on. get_settings() builds a fresh Settings() per call, so
    # the knob has to come from the environment, not an instance mutation.
    monkeypatch.setenv("EXTERNAL_MONITOR_ENRICHMENT_MIN_RELEVANCE", "0.5")

    install_fake_source([_vendor_signal()])
    wl_id = ms.insert_watchlist_item(
        slug="vendor-aws", signal_type="vendor_status",
        target="https://status.aws/rss", severity_floor=AlertSeverity.LOW,
        db_path=db,
    )
    await mp.run_external_monitor_scan(db_path=db)

    assert promoted == []  # gated out
    sigs = ms.list_signals_for_watchlist(wl_id, db_path=db)
    assert sigs[0]["processed_outcome"] == OUTCOME_SUPPRESSED_LOW_RELEVANCE


@pytest.mark.asyncio
async def test_enrichment_fail_open_still_promotes(
    db: Path, install_fake_source, monkeypatch: pytest.MonkeyPatch,
) -> None:
    promoted: list[Any] = []
    monkeypatch.setattr(mp, "schedule_evaluation", lambda e: promoted.append(e))
    monkeypatch.setattr(mp, "build_company_context", lambda db_path=None: "CTX")

    async def fake_enrich(signal, item, *, company_ctx):
        return None  # miss

    monkeypatch.setattr(mp, "enrich_signal", fake_enrich)

    install_fake_source([_vendor_signal()])
    ms.insert_watchlist_item(
        slug="vendor-aws", signal_type="vendor_status",
        target="https://status.aws/rss", severity_floor=AlertSeverity.LOW,
        db_path=db,
    )
    await mp.run_external_monitor_scan(db_path=db)

    assert len(promoted) == 1
    assert "Why this matters" not in promoted[0].body


@pytest.mark.asyncio
async def test_per_row_enrich_opt_out_falsy(
    db: Path, install_fake_source, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """config_json.enrich = 0 (falsy JSON) disables enrichment for that row."""
    monkeypatch.setattr(mp, "schedule_evaluation", lambda e: None)
    monkeypatch.setattr(mp, "build_company_context", lambda db_path=None: "CTX")

    called = {"n": 0}

    async def fake_enrich(signal, item, *, company_ctx):
        called["n"] += 1
        return None

    monkeypatch.setattr(mp, "enrich_signal", fake_enrich)

    install_fake_source([_vendor_signal()])
    ms.insert_watchlist_item(
        slug="vendor-aws", signal_type="vendor_status",
        target="https://status.aws/rss", severity_floor=AlertSeverity.LOW,
        config={"enrich": 0},
        db_path=db,
    )
    await mp.run_external_monitor_scan(db_path=db)
    assert called["n"] == 0  # enrichment never invoked for an opted-out row


@pytest.mark.asyncio
async def test_query_kill_switch_skips_query_rows(
    db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXTERNAL_MONITOR_QUERY_ENABLED", "false")
    polled: list[str] = []

    class _SpyQuery:
        kind = "query"
        default_poll_interval_minutes = 360
        seed_on_first_poll: bool = False

        async def poll(
            self, item: WatchlistItem, *, db_path: Path | None = None
        ) -> list[Signal]:
            polled.append(item.slug)
            return []

        def matches_trigger(self, signal: Signal, item: WatchlistItem) -> bool:
            return True

    original = SOURCE_REGISTRY.get("query")
    SOURCE_REGISTRY["query"] = _SpyQuery()  # type: ignore[assignment]
    try:
        ms.insert_watchlist_item(
            slug="q-acme", signal_type="query",
            target="Acme layoffs", db_path=db,
        )
        await mp.run_external_monitor_scan(db_path=db)
    finally:
        if original is not None:
            SOURCE_REGISTRY["query"] = original
        else:
            SOURCE_REGISTRY.pop("query", None)

    assert polled == []  # never polled while disabled
    item = ms.get_watchlist_item_by_slug("q-acme", db_path=db)
    assert item is not None and item.last_polled_at is None


# --------------------------------------------------------------------- #
# query severity grading (relevance → severity, before the floor gate)
# --------------------------------------------------------------------- #


class _FakeQuerySource:
    """Query adapter stub — emits the supplied flat-LOW signals (as the real
    adapter does), registered under the 'query' signal_type."""

    kind: str = "query"
    default_poll_interval_minutes: int = 360
    seed_on_first_poll: bool = False

    def __init__(self, signals: list[Signal]) -> None:
        self._signals = signals

    async def poll(
        self, item: WatchlistItem, *, db_path: Path | None = None
    ) -> list[Signal]:
        return [s.model_copy(update={"watchlist_id": item.id or 0}) for s in self._signals]

    def matches_trigger(self, signal: Signal, item: WatchlistItem) -> bool:
        return True


def _query_signal(dedup_key: str = "query:e1") -> Signal:
    # The query adapter hardcodes severity_hint=LOW for every web hit.
    return Signal(
        watchlist_id=0,
        source_kind="query",
        source_external_id="r-1",
        captured_at=datetime.now(UTC).isoformat(),
        normalized_summary="[VC Funding] NEURA Robotics raises $1.2B",
        raw_payload={"summary": "Series B round"},
        provenance_url="https://news.example/neura",
        severity_hint=AlertSeverity.LOW,
        dedup_key=dedup_key,
    )


@pytest.fixture
def install_fake_query_source(monkeypatch: pytest.MonkeyPatch):
    original = SOURCE_REGISTRY.get("query")

    def _set(signals: list[Signal]) -> None:
        SOURCE_REGISTRY["query"] = _FakeQuerySource(signals)  # type: ignore[assignment]

    yield _set
    if original is not None:
        SOURCE_REGISTRY["query"] = original
    else:
        SOURCE_REGISTRY.pop("query", None)


@pytest.mark.asyncio
async def test_query_material_hit_graded_clears_medium_floor(
    db: Path, install_fake_query_source, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A query hit the relevance read scores MEDIUM-band must clear a medium
    floor (the adapter's flat-LOW would otherwise be suppressed_below_floor),
    and reach triage with the graded hint."""
    promoted: list[Any] = []
    monkeypatch.setattr(mp, "schedule_evaluation", lambda e: promoted.append(e))
    monkeypatch.setattr(mp, "build_company_context", lambda db_path=None: "CTX")

    async def fake_enrich(signal, item, *, company_ctx):
        return SignalEnrichment(
            relevance_score=0.6, why_it_matters="Funds a competitor's scale-up"
        )

    monkeypatch.setattr(mp, "enrich_signal", fake_enrich)

    install_fake_query_source([_query_signal()])
    wl_id = ms.insert_watchlist_item(
        slug="q-funding", signal_type="query", target="humanoid VC funding",
        severity_floor=AlertSeverity.MEDIUM, db_path=db,
    )

    written = await mp.run_external_monitor_scan(db_path=db)

    assert written == 1
    assert len(promoted) == 1
    # Graded MEDIUM (0.6) — the hint that reaches triage, and what clears the floor.
    assert "Severity hint: medium" in promoted[0].body
    assert "Why this matters: Funds a competitor's scale-up" in promoted[0].body
    sigs = ms.list_signals_for_watchlist(wl_id, db_path=db)
    assert sigs[0]["processed_outcome"] == OUTCOME_ALERTED


@pytest.mark.asyncio
async def test_query_low_relevance_still_suppressed_below_floor(
    db: Path, install_fake_query_source, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A low-relevance query hit stays LOW after grading, so a medium floor
    still filters it as noise — grading rescues material hits, not all of them."""
    promoted: list[Any] = []
    monkeypatch.setattr(mp, "schedule_evaluation", lambda e: promoted.append(e))
    monkeypatch.setattr(mp, "build_company_context", lambda db_path=None: "CTX")

    async def fake_enrich(signal, item, *, company_ctx):
        return SignalEnrichment(relevance_score=0.2, why_it_matters="tangential")

    monkeypatch.setattr(mp, "enrich_signal", fake_enrich)

    install_fake_query_source([_query_signal()])
    wl_id = ms.insert_watchlist_item(
        slug="q-funding", signal_type="query", target="humanoid VC funding",
        severity_floor=AlertSeverity.MEDIUM, db_path=db,
    )

    await mp.run_external_monitor_scan(db_path=db)

    assert promoted == []
    sigs = ms.list_signals_for_watchlist(wl_id, db_path=db)
    assert sigs[0]["processed_outcome"] == OUTCOME_SUPPRESSED_BELOW_FLOOR


@pytest.mark.asyncio
async def test_query_relevance_gate_fires_before_floor(
    db: Path, install_fake_query_source, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the relevance gate on, it suppresses a query signal during the
    pre-floor enrichment — so the outcome is suppressed_low_relevance, not
    suppressed_below_floor (for query, enrichment + its gate run first)."""
    promoted: list[Any] = []
    monkeypatch.setattr(mp, "schedule_evaluation", lambda e: promoted.append(e))
    monkeypatch.setattr(mp, "build_company_context", lambda db_path=None: "CTX")

    async def fake_enrich(signal, item, *, company_ctx):
        return SignalEnrichment(relevance_score=0.2, why_it_matters="tangential")

    monkeypatch.setattr(mp, "enrich_signal", fake_enrich)
    monkeypatch.setenv("EXTERNAL_MONITOR_ENRICHMENT_MIN_RELEVANCE", "0.5")

    install_fake_query_source([_query_signal()])
    wl_id = ms.insert_watchlist_item(
        slug="q-funding", signal_type="query", target="humanoid VC funding",
        severity_floor=AlertSeverity.MEDIUM, db_path=db,
    )

    await mp.run_external_monitor_scan(db_path=db)

    assert promoted == []
    sigs = ms.list_signals_for_watchlist(wl_id, db_path=db)
    assert sigs[0]["processed_outcome"] == OUTCOME_SUPPRESSED_LOW_RELEVANCE


# --------------------------------------------------------------------- #
# store migration
# --------------------------------------------------------------------- #


def test_enrichment_column_migration_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DB created without enrichment_json gets the column added, once."""
    import sqlite3

    db_path = tmp_path / "legacy.db"
    monkeypatch.setattr("openexecutive.memory.episodic.DB_PATH", db_path)
    # Hand-create the OLD external_signals shape (no enrichment_json).
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE external_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            watchlist_id INTEGER NOT NULL,
            source_kind TEXT NOT NULL,
            source_external_id TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            normalized_summary TEXT NOT NULL,
            raw_payload_json TEXT NOT NULL DEFAULT '{}',
            provenance_url TEXT NOT NULL,
            severity_hint TEXT NOT NULL DEFAULT 'low',
            dedup_key TEXT NOT NULL UNIQUE,
            processed_at TEXT,
            processed_outcome TEXT,
            promoted_alert_id INTEGER
        )
    """)
    conn.commit()
    conn.close()

    ms.initialize_db(db_path)
    ms.initialize_db(db_path)  # second call must not error

    conn = sqlite3.connect(str(db_path))
    cols = {row[1] for row in conn.execute("PRAGMA table_info(external_signals)")}
    conn.close()
    assert "enrichment_json" in cols
