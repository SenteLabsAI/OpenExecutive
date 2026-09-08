"""Stock-price adapter using Yahoo Finance's public chart endpoint.

This is the same endpoint yfinance scrapes internally, used here
directly via httpx so we don't pull pandas + numpy through the
dependency tree. It's free, key-less, and returns JSON — at the cost
of being undocumented and breakable on a Yahoo whim. The plan calls
out that v2 can swap to a paid feed (Alpha Vantage / Finnhub) behind
this same ``Source`` interface without touching anything downstream.

Watchlist row shape:

  - ``signal_type``: ``"stock"``
  - ``target``: the ticker symbol (e.g. ``"AAPL"``, ``"NVDA"``).
    Internally fetched via
    ``https://query1.finance.yahoo.com/v8/finance/chart/{TICKER}``.
  - ``config_json``: optional ``{"display_name": "Apple"}`` — used as
    the prefix in the normalized summary. Falls back to the ticker
    itself when missing.
  - ``trigger_json``: ``{"abs_change_pct_gte": 5}`` (the default if
    unset) — fire when ``|current vs previous close| >= N percent``.
    Use ``{"abs_change_pct_gte": 0}`` to fire on every poll (useful
    for testing).

Severity hint scales with the magnitude of the move:
  - ≥ 10% → URGENT
  - ≥ 5%  → HIGH
  - ≥ 2%  → MEDIUM
  - else  → LOW
The watchlist's ``severity_floor`` / ``severity_ceiling`` clamp these
downstream.

Dedup: the dedup_key folds ticker + UTC date + sign + abs-pct-bucket,
so the same stock at the same magnitude on the same day is a no-op
even across multiple polls — but a fresh move tomorrow surfaces.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from openexecutive.alerts.models import AlertSeverity
from openexecutive.config import get_settings
from openexecutive.monitoring.models import (
    SOURCE_KIND_STOCK,
    Signal,
    WatchlistItem,
)
from openexecutive.monitoring.sources._http import (
    FetchOverflowError,
    fetch_bounded,
    validate_target_url,
)

logger = logging.getLogger(__name__)

_YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

# Default trigger when watchlist row leaves trigger_json empty —
# stocks publish constantly, so a sensible-default threshold is much
# more useful than firing on every poll.
_DEFAULT_THRESHOLD_PCT = 5.0


class StockSource:
    kind: str = SOURCE_KIND_STOCK
    # Stock prices update fast but we don't want to be a high-frequency
    # trader's data feed — 15 min is a reasonable middle ground.
    default_poll_interval_minutes: int = 15
    # Point-in-time threshold check — a crossing on the first poll is real.
    seed_on_first_poll: bool = False

    async def poll(
        self, item: WatchlistItem, *, db_path: Path | None = None
    ) -> list[Signal]:
        ticker = (item.target or "").strip().upper()
        if not ticker or not _looks_like_ticker(ticker):
            logger.warning(
                "stock: watchlist %r has bad ticker %r — skipping",
                item.slug, item.target,
            )
            return []

        url = _YAHOO_CHART_URL.format(symbol=ticker)
        # Reuse the SSRF guard — Yahoo's chart endpoint resolves to a
        # public host, but the validator also catches scheme typos.
        ok, reason = validate_target_url(url)
        if not ok:
            logger.warning(
                "stock: rejecting target url %r — %s", url, reason
            )
            return []

        max_bytes = get_settings().external_monitor_max_fetch_bytes
        try:
            body = await fetch_bounded(url, max_bytes)
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            logger.warning(
                "stock: fetch failed for %s (%s): %s",
                item.slug, ticker, exc,
            )
            return []
        except FetchOverflowError:
            logger.warning(
                "stock: response for %s exceeded %d bytes — dropping",
                ticker, max_bytes,
            )
            return []

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            logger.warning(
                "stock: ticker %s — non-JSON response: %s", ticker, exc
            )
            return []

        snapshot = _extract_snapshot(payload, ticker)
        if snapshot is None:
            return []

        price, prev_close = snapshot
        change_pct = ((price - prev_close) / prev_close) * 100.0
        display_name = item.config_json.get("display_name") or ticker

        # No fire when below threshold — emit nothing rather than a
        # suppressed signal, since we are not yet at "interesting" by
        # the watchlist's own definition.
        threshold = _coerce_threshold(item.trigger_json.get("abs_change_pct_gte"))
        if abs(change_pct) < threshold:
            return []

        severity = _severity_for_move(abs(change_pct))
        direction = "up" if change_pct >= 0 else "down"
        summary = (
            f"{display_name} ({ticker}) {direction} {abs(change_pct):.1f}% "
            f"vs prev close (${price:.2f} from ${prev_close:.2f})"
        )

        return [Signal(
            watchlist_id=item.id or 0,
            source_kind=self.kind,
            source_external_id=ticker,
            captured_at=datetime.now(UTC).isoformat(),
            normalized_summary=summary[:500],
            raw_payload={
                "display_name": display_name,
                "ticker": ticker,
                "price": price,
                "previous_close": prev_close,
                "change_pct": round(change_pct, 4),
                "source_endpoint": url,
            },
            # Yahoo's chart page is the most useful clickable for the
            # principal; their long-form URLs include a region query
            # that we strip for safety in the audit log. The user-facing
            # link in the alert body keeps both pieces clean.
            provenance_url=f"https://finance.yahoo.com/quote/{ticker}",
            severity_hint=severity,
            dedup_key=_make_dedup_key(ticker, change_pct),
        )]

    def matches_trigger(self, signal: Signal, item: WatchlistItem) -> bool:
        # Threshold was already applied in poll(). matches_trigger stays
        # a permissive default so the pipeline doesn't double-filter.
        return True


# --------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------- #


def _coerce_threshold(value: Any) -> float:
    """Coerce a watchlist trigger value into a non-negative percent.

    The LLM-driven chat tools pass arbitrary JSON into trigger_json;
    a bad type (``"five"``, ``None``, ``[]``) or a negative value
    (``-1`` would otherwise fire on every poll because ``abs(x) < -1``
    is never true) must not crash the adapter or open a smuggle path.
    """
    try:
        f = float(value) if value is not None else _DEFAULT_THRESHOLD_PCT
    except (TypeError, ValueError):
        return _DEFAULT_THRESHOLD_PCT
    return max(0.0, f)


def _looks_like_ticker(s: str) -> bool:
    """Cheap validity check on the user-supplied ticker.

    Yahoo's symbol grammar covers:
      - Equities  — A-Z, optional ``.`` (``BRK.B``) or ``-`` (``RDS-A``)
      - Indices   — ``^`` prefix (``^GSPC`` S&P 500, ``^VIX``)
      - Futures   — ``=F`` suffix (``CL=F`` crude)
      - Crypto    — ``-USD`` suffix (``BTC-USD``)
    Reject anything else early — keeps Yahoo from echoing arbitrary
    user strings back at us via the URL, and saves a wasted HTTP call.
    """
    if not (1 <= len(s) <= 12):
        return False
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-^=")
    return all(c in allowed for c in s)


def _extract_snapshot(
    payload: dict[str, Any], ticker: str,
) -> tuple[float, float] | None:
    """Return (price, previous_close) from a Yahoo Chart response.

    The Chart API embeds the snapshot in ``chart.result[0].meta``. When
    Yahoo returns no data (typically because the ticker is unknown),
    ``chart.error`` is populated and ``chart.result`` is null — log
    and skip.
    """
    chart = payload.get("chart") or {}
    err = chart.get("error")
    if err:
        logger.warning(
            "stock: ticker %s — Yahoo error: %s", ticker, err
        )
        return None
    results = chart.get("result") or []
    if not results:
        return None
    meta = (results[0] or {}).get("meta") or {}
    price = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    if price is None or prev is None or prev == 0:
        return None
    try:
        return float(price), float(prev)
    except (TypeError, ValueError):
        return None


def _severity_for_move(abs_pct: float) -> AlertSeverity:
    if abs_pct >= 10:
        return AlertSeverity.URGENT
    if abs_pct >= 5:
        return AlertSeverity.HIGH
    if abs_pct >= 2:
        return AlertSeverity.MEDIUM
    return AlertSeverity.LOW


def _make_dedup_key(ticker: str, change_pct: float) -> str:
    """Fold ticker + UTC date + sign + magnitude bucket into a dedup key.

    Repeated polls of the same move on the same day collapse; a fresh
    move tomorrow (or a bigger move today) produces a new key and
    therefore a new signal.
    """
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    sign = "+" if change_pct >= 0 else "-"
    # Bucket by integer percent so a 5.0 and a 5.2% move don't double-fire.
    bucket = int(abs(change_pct))
    payload = f"{ticker}\x00{today}\x00{sign}\x00{bucket}".encode()
    digest = hashlib.sha256(payload).hexdigest()[:32]
    return f"stock:{digest}"
