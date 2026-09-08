"""Generic RSS / Atom feed adapter.

Wraps ``feedparser`` so the watchlist can monitor any feed — competitor
changelogs, layoffs.fyi, USPTO patent feeds, regulatory filings, blog
RSS, etc. — without each one needing its own adapter.

Watchlist row shape:

  - ``signal_type``: ``"rss"``
  - ``target``: the full feed URL
  - ``config_json``: optional ``{"feed_label": "Acme Changelog"}`` — used
    as a prefix in the normalized summary. Falls back to a host-derived
    label when missing. Optional ``"max_future_skew_hours"`` (0 .. 8760)
    overrides ``EXTERNAL_MONITOR_MAX_FUTURE_SKEW_HOURS`` for this feed (0 =
    off, for calendars / maintenance windows that legitimately date entries
    ahead); an out-of-range value is ignored with a warning.
  - ``trigger_json``: optional ``{"keywords": ["pricing", "launch"]}``
    — when present, only entries whose title OR summary contain at least
    one keyword surface. Otherwise every new entry surfaces.

Freshness: each entry's ``<pubDate>`` / ``<updated>`` is parsed into
``Signal.published_at`` so the pipeline can tell *when it happened* apart
from *when we first saw it* (``captured_at``). Two guards follow from
that (issue #80 — a January article was surfacing as September news):
the first poll of a row records the feed's existing entries as a
baseline without promoting them, and entries older than
``EXTERNAL_MONITOR_MAX_SIGNAL_AGE_DAYS`` are recorded but never promoted
(entries dated implausibly far in the future are deferred until the date
passes).

Severity hint defaults to ``LOW`` — RSS is by far the noisiest source,
so a low default lets the watchlist's ``severity_floor`` filter
aggressively. Operators expecting HIGH-severity feeds (e.g. SEC EDGAR
8-K) should override via the per-row severity_floor + ceiling — PR-C
will extend the triage prompt to act on the source_kind tag for finer
calibration.

Design note: ``feedparser`` is a single pure-Python dep (~500KB, only
optional dep is sgmllib3k for SGML-tolerant feeds). It handles Atom 1.0,
RSS 0.9 → 2.0, RDF, and most of the real-world quirks our hand-rolled
parser in vendor_status would choke on (e.g. layoffs.fyi's RSS is
slightly malformed).
"""
from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import httpx

from openexecutive.alerts.models import AlertSeverity
from openexecutive.config import get_settings
from openexecutive.monitoring.models import SOURCE_KIND_RSS, Signal, WatchlistItem
from openexecutive.monitoring.sources._http import (
    FetchOverflowError,
    fetch_bounded,
    strip_url_query,
    validate_target_url,
)
from openexecutive.monitoring.sources.base import (
    collapse_whitespace,
    feed_entry_published_at,
    future_skew_for,
)

logger = logging.getLogger(__name__)

# feedparser is happy to walk a 50k-entry feed; we cap to keep one bad
# row from owning a whole tick. Matches the vendor_status ceiling.
_MAX_ENTRIES_PER_FEED = 100


class RssSource:
    kind: str = SOURCE_KIND_RSS
    # RSS publishers update less often than status pages — 30 min is the
    # sweet spot between freshness and politeness toward upstream.
    default_poll_interval_minutes: int = 30
    # A feed returns its whole back-catalogue on every poll, so the first
    # poll is a baseline (see Source.seed_on_first_poll) — otherwise every
    # historical entry would fire as "news" the moment a watch is added.
    seed_on_first_poll: bool = True

    async def poll(
        self, item: WatchlistItem, *, db_path: Path | None = None
    ) -> list[Signal]:
        if not item.target:
            logger.warning(
                "rss: watchlist %r has empty target — skipping", item.slug
            )
            return []

        ok, reason = validate_target_url(item.target)
        if not ok:
            logger.warning(
                "rss: rejecting watchlist %r target — %s", item.slug, reason
            )
            return []

        max_bytes = get_settings().external_monitor_max_fetch_bytes
        try:
            body = await fetch_bounded(item.target, max_bytes)
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            logger.warning(
                "rss: fetch failed for %s (%s): %s",
                item.slug, item.target, exc,
            )
            return []
        except FetchOverflowError:
            logger.warning(
                "rss: feed %s exceeded %d bytes — dropping tick",
                item.target, max_bytes,
            )
            return []

        # feedparser is sync + CPU-bound on large feeds. We don't move it
        # to a thread because the body is already capped at ~2MB and the
        # scan loop tolerates a 50ms hiccup; if PR-C measures otherwise,
        # wrap with asyncio.to_thread.
        parsed = feedparser.parse(body)
        if parsed.bozo and not parsed.entries:
            logger.warning(
                "rss: feed %s could not be parsed: %s",
                item.target, parsed.bozo_exception,
            )
            return []

        feed_label = (
            item.config_json.get("feed_label")
            or _host_label(item.target)
        )
        skew = future_skew_for(item)
        signals: list[Signal] = []
        for entry in parsed.entries[:_MAX_ENTRIES_PER_FEED]:
            entry_id = _entry_id(entry)
            if not entry_id:
                # No stable upstream id → no reliable dedup → skip.
                continue
            title = collapse_whitespace(entry.get("title") or "") or "(untitled)"
            link = strip_url_query((entry.get("link") or "").strip())
            summary = f"[{feed_label}] {title}"
            signals.append(Signal(
                watchlist_id=item.id or 0,
                source_kind=self.kind,
                source_external_id=entry_id[:500],
                captured_at=datetime.now(UTC).isoformat(),
                published_at=feed_entry_published_at(entry, skew=skew),
                normalized_summary=summary[:500],
                raw_payload={
                    "feed_label": feed_label,
                    "target_url": item.target,
                    "entry_id": entry_id,
                    "title": title,
                    "link": link,
                    "published": entry.get("published", "")
                    or entry.get("updated", ""),
                    # Trim the body — some feeds embed the full article.
                    # We persist enough for triage context but no more.
                    "summary": (entry.get("summary") or "")[:1000],
                },
                provenance_url=link or item.target,
                severity_hint=AlertSeverity.LOW,
                dedup_key=_make_dedup_key(item.slug, entry_id),
            ))
        return signals

    def matches_trigger(self, signal: Signal, item: WatchlistItem) -> bool:
        """Optional keyword filter — matched against summary + body."""
        keywords = item.trigger_json.get("keywords") or []
        if not keywords:
            return True
        haystack = (
            signal.normalized_summary
            + " "
            + (signal.raw_payload.get("summary") or "")
        ).lower()
        return any(kw.lower() in haystack for kw in keywords)


# --------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------- #


def _entry_id(entry: dict) -> str:
    """Pick the most stable id available on a feedparser entry.

    feedparser exposes `id` (Atom <id>), `guid` (RSS <guid>), and `link`
    in roughly that order of stability. Some sloppy feeds repeat the
    pubDate as the id; falling back through link gives us at least URL-
    stable dedup. Last resort: hash the title, which silently re-fires
    on minor edits — acceptable because hand-edited blog posts are rare
    and the dedup cooldown filters noise.
    """
    for key in ("id", "guid", "link"):
        v = entry.get(key)
        if v:
            return str(v).strip()
    title = entry.get("title") or ""
    return f"title:{title[:120]}"


def _host_label(url: str) -> str:
    """Best-effort feed name from URL host when config doesn't set one."""
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return "feed"
    parts = [p for p in host.split(".") if p and p not in {"www", "feeds"}]
    return parts[0] if parts else host or "feed"


def _make_dedup_key(slug: str, entry_id: str) -> str:
    payload = f"{slug}\x00{entry_id}".encode()
    digest = hashlib.sha256(payload).hexdigest()[:32]
    return f"rss:{digest}"
