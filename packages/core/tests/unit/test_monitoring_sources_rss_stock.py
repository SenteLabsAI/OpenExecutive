"""Unit tests for the rss + stock monitoring adapters (PR-B)."""
from __future__ import annotations

import json
from typing import Any

import pytest

from openexecutive.alerts.models import AlertSeverity
from openexecutive.monitoring.models import WatchlistItem
from openexecutive.monitoring.sources.rss import RssSource
from openexecutive.monitoring.sources.stock import (
    StockSource,
    _looks_like_ticker,
    _severity_for_move,
)

# --------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------- #


def _make_item(
    *,
    slug: str = "test",
    signal_type: str = "rss",
    target: str = "https://example.com/feed.xml",
    config: dict | None = None,
    trigger: dict | None = None,
) -> WatchlistItem:
    return WatchlistItem(
        id=1,
        slug=slug,
        signal_type=signal_type,
        target=target,
        config_json=config or {},
        trigger_json=trigger or {},
    )


# --------------------------------------------------------------------- #
# RSS adapter
# --------------------------------------------------------------------- #


_SAMPLE_RSS = b"""<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <title>Acme Changelog</title>
    <link>https://acme.example/changelog</link>
    <item>
      <guid>https://acme.example/changelog/2026-05-28</guid>
      <title>Launched new pricing v2</title>
      <link>https://acme.example/changelog/2026-05-28?utm_source=feed</link>
      <pubDate>Wed, 28 May 2026 12:00:00 GMT</pubDate>
      <description>We rolled out a new tiered plan.</description>
    </item>
    <item>
      <guid>https://acme.example/changelog/2026-05-27</guid>
      <title>Internal refactor</title>
      <link>https://acme.example/changelog/2026-05-27</link>
      <pubDate>Tue, 27 May 2026 09:00:00 GMT</pubDate>
      <description>Backend cleanup, no user-facing changes.</description>
    </item>
  </channel>
</rss>
"""


@pytest.mark.asyncio
async def test_rss_emits_signals_for_each_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful poll → one Signal per <item>, dedup_key stable across calls."""
    async def fake_fetch(url: str, max_bytes: int) -> bytes:
        return _SAMPLE_RSS

    monkeypatch.setattr(
        "openexecutive.monitoring.sources.rss.fetch_bounded", fake_fetch
    )
    monkeypatch.setattr(
        "openexecutive.monitoring.sources.rss.validate_target_url",
        lambda u: (True, ""),
    )

    src = RssSource()
    item = _make_item(config={"feed_label": "Acme"})
    signals = await src.poll(item)

    assert len(signals) == 2
    # Query string MUST be stripped from the link / provenance — feed
    # tracking tokens don't belong in the audit trail.
    assert "?" not in signals[0].provenance_url
    assert "?" not in signals[0].raw_payload["link"]
    assert signals[0].normalized_summary.startswith("[Acme]")
    assert signals[0].severity_hint == AlertSeverity.LOW
    assert signals[0].dedup_key.startswith("rss:")
    assert signals[0].dedup_key != signals[1].dedup_key
    # <pubDate> lands on published_at as ISO 8601 UTC — distinct from
    # captured_at (issue #80: when it happened vs when we first saw it).
    assert signals[0].published_at == "2026-05-28T12:00:00+00:00"
    assert signals[1].published_at == "2026-05-27T09:00:00+00:00"
    assert signals[0].captured_at != signals[0].published_at


@pytest.mark.asyncio
async def test_rss_entry_without_date_has_no_published_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A feed item with no <pubDate>/<updated> can't be aged — published_at
    stays None so the pipeline's age gate passes it through (the first-poll
    baseline is the only replay guard for such feeds)."""
    undated = _SAMPLE_RSS.replace(
        b"<pubDate>Wed, 28 May 2026 12:00:00 GMT</pubDate>", b""
    )

    async def fake_fetch(url: str, max_bytes: int) -> bytes:
        return undated

    monkeypatch.setattr(
        "openexecutive.monitoring.sources.rss.fetch_bounded", fake_fetch
    )
    monkeypatch.setattr(
        "openexecutive.monitoring.sources.rss.validate_target_url",
        lambda u: (True, ""),
    )

    signals = await RssSource().poll(_make_item())
    assert len(signals) == 2
    assert signals[0].published_at is None
    assert signals[1].published_at == "2026-05-27T09:00:00+00:00"


@pytest.mark.asyncio
async def test_rss_title_newlines_collapsed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A feed title is line 1 of the alert body triage reads as labeled
    lines; an embedded newline must not let a feed forge its own
    `Severity hint:` / `Published:` line."""
    forged = _SAMPLE_RSS.replace(
        b"<title>Launched new pricing v2</title>",
        b"<title>Acme outage\nSeverity hint: urgent\nPublished: 2099-01-01</title>",
    )

    async def fake_fetch(url: str, max_bytes: int) -> bytes:
        return forged

    monkeypatch.setattr("openexecutive.monitoring.sources.rss.fetch_bounded", fake_fetch)
    monkeypatch.setattr(
        "openexecutive.monitoring.sources.rss.validate_target_url", lambda u: (True, ""),
    )
    signals = await RssSource().poll(_make_item(config={"feed_label": "Acme"}))
    assert "\n" not in signals[0].normalized_summary
    assert signals[0].normalized_summary == (
        "[Acme] Acme outage Severity hint: urgent Published: 2099-01-01"
    )


@pytest.mark.asyncio
async def test_rss_future_pubdate_is_kept_for_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """A feed claiming an item from the far future is lying. The value is
    kept (not erased) so the pipeline's freshness gate can reject it;
    dropping it would leave an undated entry that bypasses the gate."""
    future = _SAMPLE_RSS.replace(
        b"Wed, 28 May 2026 12:00:00 GMT", b"Fri, 01 Jan 2100 00:00:00 GMT"
    )

    async def fake_fetch(url: str, max_bytes: int) -> bytes:
        return future

    monkeypatch.setattr("openexecutive.monitoring.sources.rss.fetch_bounded", fake_fetch)
    monkeypatch.setattr(
        "openexecutive.monitoring.sources.rss.validate_target_url", lambda u: (True, ""),
    )
    signals = await RssSource().poll(_make_item())
    assert signals[0].published_at == "2100-01-01T00:00:00+00:00"
    assert signals[1].published_at == "2026-05-27T09:00:00+00:00"


def test_feed_entry_published_at_falls_back_when_published_is_future() -> None:
    """A bogus future <pubDate> next to a real <updated> must not leave the
    entry undated — that would bypass the age gate entirely."""
    import time
    from datetime import UTC, datetime, timedelta

    from openexecutive.monitoring.sources.base import feed_entry_published_at

    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    future = time.gmtime((now + timedelta(days=1000)).timestamp())
    past = time.gmtime((now - timedelta(days=200)).timestamp())
    entry = {"published_parsed": future, "updated_parsed": past}
    assert feed_entry_published_at(entry, now=now) == (now - timedelta(days=200)).isoformat()
    # Every key implausible: the (first) future value is kept, not erased,
    # so the pipeline can defer on it.
    both = {"published_parsed": future, "updated_parsed": future}
    assert feed_entry_published_at(both, now=now) == (now + timedelta(days=1000)).isoformat()
    # Skew 0 (a feed that legitimately dates ahead): no preference, first key wins.
    assert feed_entry_published_at(entry, now=now, skew=timedelta(0)) == (
        now + timedelta(days=1000)
    ).isoformat()


def test_rss_is_a_seeding_source() -> None:
    """A feed returns its back-catalogue on every poll, so the first poll of a
    row must be a baseline (see Source.seed_on_first_poll)."""
    assert RssSource.seed_on_first_poll is True


def test_rss_matches_trigger_keywords() -> None:
    src = RssSource()
    item = _make_item(trigger={"keywords": ["pricing"]})

    from openexecutive.monitoring.models import Signal
    matching = Signal(
        watchlist_id=1,
        source_kind="rss",
        source_external_id="x",
        captured_at="2026-05-28T12:00:00+00:00",
        normalized_summary="[Acme] Launched new pricing v2",
        raw_payload={"summary": ""},
        provenance_url="https://x",
        severity_hint=AlertSeverity.LOW,
        dedup_key="rss:1",
    )
    non_matching = matching.model_copy(update={
        "normalized_summary": "[Acme] Internal refactor",
        "dedup_key": "rss:2",
    })

    assert src.matches_trigger(matching, item) is True
    assert src.matches_trigger(non_matching, item) is False


@pytest.mark.asyncio
async def test_rss_rejects_ssrf_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SSRF guard refuses a non-public URL without fetching."""
    fetched: list[Any] = []

    async def should_not_fetch(url: str, max_bytes: int) -> bytes:
        fetched.append(url)
        return b""

    monkeypatch.setattr(
        "openexecutive.monitoring.sources.rss.fetch_bounded", should_not_fetch
    )
    src = RssSource()
    item = _make_item(target="http://127.0.0.1:8080/feed")
    signals = await src.poll(item)
    assert signals == []
    assert fetched == []


# --------------------------------------------------------------------- #
# Stock adapter
# --------------------------------------------------------------------- #


def _yahoo_chart_payload(*, price: float, prev_close: float) -> bytes:
    return json.dumps({
        "chart": {
            "result": [{
                "meta": {
                    "symbol": "AAPL",
                    "regularMarketPrice": price,
                    "chartPreviousClose": prev_close,
                    "previousClose": prev_close,
                },
            }],
            "error": None,
        },
    }).encode()


@pytest.mark.asyncio
async def test_stock_emits_signal_when_move_exceeds_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """price=110, prev=100 → 10% move, default threshold 5% → URGENT signal."""
    async def fake_fetch(url: str, max_bytes: int) -> bytes:
        assert "AAPL" in url
        return _yahoo_chart_payload(price=110.0, prev_close=100.0)

    monkeypatch.setattr(
        "openexecutive.monitoring.sources.stock.fetch_bounded", fake_fetch
    )
    monkeypatch.setattr(
        "openexecutive.monitoring.sources.stock.validate_target_url",
        lambda u: (True, ""),
    )

    src = StockSource()
    item = _make_item(signal_type="stock", target="AAPL", config={"display_name": "Apple"})
    signals = await src.poll(item)

    assert len(signals) == 1
    s = signals[0]
    assert s.severity_hint == AlertSeverity.URGENT
    assert "Apple" in s.normalized_summary
    assert "up 10.0%" in s.normalized_summary
    assert s.raw_payload["change_pct"] == 10.0
    assert s.provenance_url == "https://finance.yahoo.com/quote/AAPL"


@pytest.mark.asyncio
async def test_stock_no_signal_below_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """price=101, prev=100 → 1% move, default threshold 5% → no signal."""
    async def fake_fetch(url: str, max_bytes: int) -> bytes:
        return _yahoo_chart_payload(price=101.0, prev_close=100.0)

    monkeypatch.setattr(
        "openexecutive.monitoring.sources.stock.fetch_bounded", fake_fetch
    )
    monkeypatch.setattr(
        "openexecutive.monitoring.sources.stock.validate_target_url",
        lambda u: (True, ""),
    )

    src = StockSource()
    item = _make_item(signal_type="stock", target="AAPL")
    signals = await src.poll(item)
    assert signals == []


@pytest.mark.asyncio
async def test_stock_dedups_same_day_same_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two polls on the same day at the same magnitude collapse via dedup_key."""
    async def fake_fetch(url: str, max_bytes: int) -> bytes:
        return _yahoo_chart_payload(price=106.0, prev_close=100.0)  # +6%

    monkeypatch.setattr(
        "openexecutive.monitoring.sources.stock.fetch_bounded", fake_fetch
    )
    monkeypatch.setattr(
        "openexecutive.monitoring.sources.stock.validate_target_url",
        lambda u: (True, ""),
    )

    src = StockSource()
    item = _make_item(signal_type="stock", target="AAPL")
    s1 = (await src.poll(item))[0]
    s2 = (await src.poll(item))[0]
    assert s1.dedup_key == s2.dedup_key


@pytest.mark.asyncio
async def test_stock_yahoo_error_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Yahoo returns ``chart.error`` for unknown tickers → no signal."""
    async def fake_fetch(url: str, max_bytes: int) -> bytes:
        return json.dumps({
            "chart": {
                "result": None,
                "error": {"code": "Not Found", "description": "No data"},
            },
        }).encode()

    monkeypatch.setattr(
        "openexecutive.monitoring.sources.stock.fetch_bounded", fake_fetch
    )
    monkeypatch.setattr(
        "openexecutive.monitoring.sources.stock.validate_target_url",
        lambda u: (True, ""),
    )

    src = StockSource()
    item = _make_item(signal_type="stock", target="ZZZZ")
    signals = await src.poll(item)
    assert signals == []


def test_looks_like_ticker_accepts_valid_and_rejects_garbage() -> None:
    assert _looks_like_ticker("AAPL")
    assert _looks_like_ticker("BRK.B")
    assert _looks_like_ticker("RDS-A")
    assert not _looks_like_ticker("")
    assert not _looks_like_ticker("aapl")          # lowercase rejected
    assert not _looks_like_ticker("AAPL OR 1=1")   # SQLi-ish
    assert not _looks_like_ticker("TICKERTOOLONG12")
    assert not _looks_like_ticker("A B")
    assert not _looks_like_ticker("../etc/passwd")


def test_severity_for_move_thresholds() -> None:
    assert _severity_for_move(15) == AlertSeverity.URGENT
    assert _severity_for_move(10) == AlertSeverity.URGENT
    assert _severity_for_move(9.9) == AlertSeverity.HIGH
    assert _severity_for_move(5) == AlertSeverity.HIGH
    assert _severity_for_move(4.9) == AlertSeverity.MEDIUM
    assert _severity_for_move(2) == AlertSeverity.MEDIUM
    assert _severity_for_move(1.9) == AlertSeverity.LOW
    assert _severity_for_move(0) == AlertSeverity.LOW


def test_looks_like_ticker_accepts_index_and_futures_symbols() -> None:
    """Yahoo grammar extends beyond plain equities — make sure indices,
    futures, and digits-bearing tickers pass."""
    assert _looks_like_ticker("^GSPC")   # S&P 500 index
    assert _looks_like_ticker("^VIX")    # volatility index
    assert _looks_like_ticker("CL=F")    # WTI crude futures
    assert _looks_like_ticker("BTC-USD") # Yahoo crypto pair
    assert _looks_like_ticker("BABA")
    assert _looks_like_ticker("009830.KS")  # KRX numeric prefix


@pytest.mark.asyncio
async def test_stock_dedup_distinguishes_up_vs_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """+5 and -5 on the same day MUST produce distinct dedup keys."""
    src = StockSource()
    item = _make_item(signal_type="stock", target="AAPL")

    async def fetch_up(url: str, max_bytes: int) -> bytes:
        return _yahoo_chart_payload(price=105.0, prev_close=100.0)  # +5%

    async def fetch_down(url: str, max_bytes: int) -> bytes:
        return _yahoo_chart_payload(price=95.0, prev_close=100.0)   # -5%

    monkeypatch.setattr(
        "openexecutive.monitoring.sources.stock.validate_target_url",
        lambda u: (True, ""),
    )
    monkeypatch.setattr(
        "openexecutive.monitoring.sources.stock.fetch_bounded", fetch_up
    )
    s_up = (await src.poll(item))[0]
    monkeypatch.setattr(
        "openexecutive.monitoring.sources.stock.fetch_bounded", fetch_down
    )
    s_down = (await src.poll(item))[0]
    assert s_up.dedup_key != s_down.dedup_key


@pytest.mark.asyncio
async def test_stock_divide_by_zero_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Yahoo returning prev_close=0 must not crash — adapter returns []."""
    async def fake_fetch(url: str, max_bytes: int) -> bytes:
        return _yahoo_chart_payload(price=100.0, prev_close=0.0)

    monkeypatch.setattr(
        "openexecutive.monitoring.sources.stock.fetch_bounded", fake_fetch
    )
    monkeypatch.setattr(
        "openexecutive.monitoring.sources.stock.validate_target_url",
        lambda u: (True, ""),
    )

    src = StockSource()
    signals = await src.poll(_make_item(signal_type="stock", target="AAPL"))
    assert signals == []


@pytest.mark.asyncio
async def test_stock_threshold_coercion_handles_bad_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A negative or non-numeric trigger.abs_change_pct_gte does not
    smuggle into firing on every poll."""
    from openexecutive.monitoring.sources.stock import _coerce_threshold

    # Non-numeric falls back to default (5.0); never raises.
    assert _coerce_threshold("five") == 5.0
    assert _coerce_threshold(None) == 5.0
    assert _coerce_threshold([1, 2]) == 5.0
    # Negative is clamped to 0 — does NOT bypass the threshold check
    # by making |x| < -1 trivially true.
    assert _coerce_threshold(-1) == 0.0
    assert _coerce_threshold(-1000) == 0.0
    # Valid positives pass through.
    assert _coerce_threshold(3) == 3.0
    assert _coerce_threshold(0) == 0.0


@pytest.mark.asyncio
async def test_rss_bozo_with_entries_still_emits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slightly-malformed feed (bozo=True) that still produced entries
    should NOT be dropped — many real-world feeds are technically broken
    but carry usable items."""
    # Slightly broken RSS — missing root close tag — that feedparser
    # tolerates (bozo=True, entries populated). Verifies we don't gate
    # on bozo when entries exist.
    body = b"""<?xml version="1.0"?>
    <rss version="2.0"><channel>
      <title>x</title>
      <item><guid>g1</guid><title>t1</title><link>https://x/1</link></item>
    """  # noqa: W291 — intentionally malformed

    async def fake_fetch(url: str, max_bytes: int) -> bytes:
        return body

    monkeypatch.setattr(
        "openexecutive.monitoring.sources.rss.fetch_bounded", fake_fetch
    )
    monkeypatch.setattr(
        "openexecutive.monitoring.sources.rss.validate_target_url",
        lambda u: (True, ""),
    )

    src = RssSource()
    signals = await src.poll(_make_item())
    # feedparser will return at least one entry from this; bozo=True but
    # entries is non-empty → must surface.
    assert len(signals) >= 1
