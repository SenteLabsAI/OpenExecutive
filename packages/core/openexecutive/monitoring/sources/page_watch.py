"""Generic web-page change-detection adapter.

Watches an arbitrary web page that publishes no feed — a competitor's pricing
page, a careers page, a leadership/about page, a terms-of-service page — and
emits a Signal when its visible text changes. This is the second half of the
"watch arbitrary things" capability (the `query` adapter covers searchable
developments; this covers a *specific page* you want to know changed).

How it works (STATEFUL — the first stateful adapter):
  1. Fetch the page (bounded, SSRF-guarded) and reduce it to normalized
     visible text (strip script/style, drop tags, unescape entities, collapse
     whitespace). This deliberately ignores markup/layout churn so only
     content changes register.
  2. Hash the normalized text.
  3. Compare to the last-seen hash in the `page_watch_state` table:
       - unseen (first poll)  → store baseline, emit NOTHING (no spurious alert)
       - unchanged            → emit nothing
       - changed              → emit ONE Signal with a short diff summary,
                                 then update the stored baseline.

Watchlist row shape:
  - ``signal_type``: ``"page_watch"``
  - ``target``: the page URL (public http/https).
  - ``config_json``: optional ``{"label": "Acme pricing"}``.
  - ``trigger_json``: optional ``{"keywords": [...]}`` — only surface a change
    when the added/changed text contains a keyword (same idea as rss).

Severity hint is LOW (arbitrary-page diffs are noisy); capture-time enrichment
scores relevance and the watchlist's severity_floor filters. Dedup key folds
slug + the new content hash, so a repeated identical change is idempotent even
if the state row is lost.
"""
from __future__ import annotations

import difflib
import hashlib
import html
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

import httpx

from openexecutive.alerts.models import AlertSeverity
from openexecutive.config import get_settings
from openexecutive.monitoring import store
from openexecutive.monitoring.models import (
    PAGE_WATCH_FETCH_KEY,
    PAGE_WATCH_FETCH_XCRAWL,
    SOURCE_KIND_PAGE_WATCH,
    Signal,
    WatchlistItem,
)
from openexecutive.monitoring.sources._http import (
    FetchOverflowError,
    fetch_bounded,
    validate_target_url,
)

logger = logging.getLogger(__name__)

# Pages change slowly; 6h is polite and sufficient. Per-row cadence can tighten.
_DEFAULT_POLL_MINUTES = 360

# The fetch-source sentinel (config_json["fetch"] == "xcrawl") lives in
# monitoring.models so the insert-time validator and this adapter share one
# definition; see PAGE_WATCH_FETCH_KEY / PAGE_WATCH_FETCH_XCRAWL.

# Cap the text we SNAPSHOT (store) and DIFF — NOT what we hash. Detection
# hashes the full normalized text so a change anywhere on the page registers;
# the snapshot/diff are only for the human-readable summary, so bounding them
# keeps storage + difflib cost in check without creating a detection blind spot.
_MAX_TEXT_CHARS = 100_000
# Hard cap on words fed to difflib.SequenceMatcher (O(N·M), autojunk off) — a
# high-entropy page that mutates every poll otherwise burns CPU per tick.
_MAX_DIFF_WORDS = 8_000
# Bounds on the change summary written into the alert / persisted payload.
_SUMMARY_SNIPPET_CHARS = 160
_ADDED_TEXT_CHARS = 1_000

# <script>/<style>/<noscript> blocks: drop content, not just tags, so inline
# JS/CSS never counts as "visible text".
_DROP_BLOCKS_RE = re.compile(r"(?is)<(script|style|noscript)\b.*?</\1>")
_COMMENT_RE = re.compile(r"(?s)<!--.*?-->")
_TAG_RE = re.compile(r"(?s)<[^>]+>")
_WS_RE = re.compile(r"\s+")


class PageWatchSource:
    kind: str = SOURCE_KIND_PAGE_WATCH
    default_poll_interval_minutes: int = _DEFAULT_POLL_MINUTES
    # Keeps its own first-observation baseline in page_watch_state.
    seed_on_first_poll: bool = False

    async def poll(
        self, item: WatchlistItem, *, db_path: Path | None = None
    ) -> list[Signal]:
        if not item.target:
            logger.warning("page_watch: watchlist %r has empty target — skipping", item.slug)
            return []

        ok, reason = validate_target_url(item.target)
        if not ok:
            logger.warning("page_watch: rejecting %r target — %s", item.slug, reason)
            return []

        # Two fetch paths. Default is the keyless httpx fetcher. Rows whose
        # config_json carries ``{"fetch": "xcrawl"}`` route through xcrawl's
        # scrape API instead — for pages the keyless fetcher can't read
        # (JS-only SPAs, soft bot blocks) that publish no RSS feed. xcrawl
        # returns clean markdown, so it skips the HTML→text reduction.
        if item.config_json.get(PAGE_WATCH_FETCH_KEY) == PAGE_WATCH_FETCH_XCRAWL:
            text = await self._fetch_text_xcrawl(item)
        else:
            text = await self._fetch_text_httpx(item)
        if text is None:
            # Fetch failed (already logged at the fetch site) — drop this tick.
            return []
        if not text:
            # An empty extraction (e.g. a JS-only shell or a fetch that returned
            # no markup) is not a meaningful baseline — skip rather than store
            # an empty hash that would later "change" into real content.
            logger.debug("page_watch: %s produced no extractable text", item.slug)
            return []

        # Hash the FULL normalized text so a change anywhere on the page is
        # detected; only the snapshot we store + diff is length-capped.
        content_hash = hashlib.sha256(text.encode()).hexdigest()
        snapshot = text[:_MAX_TEXT_CHARS]
        prior = store.get_page_watch_state(item.slug, db_path=db_path)

        if prior is None:
            # First observation — record the baseline, do not alert.
            store.upsert_page_watch_state(item.slug, content_hash, snapshot, db_path=db_path)
            logger.debug("page_watch: %s baseline captured", item.slug)
            return []

        if prior.get("content_hash") == content_hash:
            return []  # unchanged

        # Changed — summarise (one diff pass), advance the baseline, emit one Signal.
        old_snapshot = prior.get("text_snapshot") or ""
        pct, added = _diff(old_snapshot, snapshot)
        # NOTE: at-most-once. We advance the stored baseline here, before the
        # pipeline inserts the Signal. If the process dies between this write
        # and the insert, this one change is not re-emitted (the next poll sees
        # the new hash as current). Acceptable for a monitoring poller; the
        # alternative (advance-after-insert) needs insertion feedback the
        # adapter doesn't have. The dedup_key still prevents double-emit.
        store.upsert_page_watch_state(item.slug, content_hash, snapshot, db_path=db_path)

        snippet = added[:_SUMMARY_SNIPPET_CHARS].strip()
        summary_detail = f"~{pct}% of text differs" + (f"; new: {snippet}" if snippet else "")
        label = item.config_json.get("label") or _host_label(item.target)
        return [Signal(
            watchlist_id=item.id or 0,
            source_kind=self.kind,
            source_external_id=content_hash[:32],
            captured_at=datetime.now(UTC).isoformat(),
            normalized_summary=f"[{label}] page changed — {summary_detail}"[:500],
            raw_payload={
                "label": label,
                "target_url": item.target,
                "content_hash": content_hash,
                "change_summary": summary_detail,
                "added_text": added[:_ADDED_TEXT_CHARS],
            },
            provenance_url=item.target,
            severity_hint=AlertSeverity.LOW,
            dedup_key=_make_dedup_key(item.slug, content_hash),
        )]

    async def _fetch_text_httpx(self, item: WatchlistItem) -> str | None:
        """Fetch via the keyless bounded httpx fetcher → normalized text.

        Returns the normalized visible text, ``""`` when the page yields no
        extractable text, or ``None`` when the fetch itself failed.
        """
        max_bytes = get_settings().external_monitor_max_fetch_bytes
        try:
            body = await fetch_bounded(item.target, max_bytes)
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            logger.warning(
                "page_watch: fetch failed for %s (%s): %s",
                item.slug, item.target, exc,
            )
            return None
        except FetchOverflowError:
            logger.warning(
                "page_watch: %s exceeded byte cap — dropping tick", item.target,
            )
            return None
        return _html_to_text(body)

    async def _fetch_text_xcrawl(self, item: WatchlistItem) -> str | None:
        """Fetch via xcrawl scrape (markdown) → normalized text, or ``None``.

        Returns ``None`` when xcrawl is disabled or the scrape failed /
        returned nothing. xcrawl markdown is already clean text, so we only
        collapse whitespace (matching ``_html_to_text``'s output) so the
        content hash is stable across trivial reformatting.
        """
        from openexecutive.integrations import xcrawl_client

        markdown = await xcrawl_client.scrape(item.target)
        if markdown is None:
            # Disabled xcrawl is an expected config state, not a fault — a
            # row tagged fetch=xcrawl on a deployment with XCRAWL_ENABLED=false
            # would otherwise log a WARNING every poll tick. Demote that case
            # to debug; a genuine scrape failure (xcrawl on, call failed) was
            # already logged inside xcrawl_client.scrape, so debug here too.
            if not get_settings().xcrawl_enabled:
                logger.debug(
                    "page_watch: %s tagged fetch=xcrawl but xcrawl disabled",
                    item.slug,
                )
            else:
                logger.debug(
                    "page_watch: xcrawl scrape yielded nothing for %s (%s)",
                    item.slug, item.target,
                )
            return None
        # `or None` (not "") so that markdown which normalizes to empty — e.g.
        # truncated down to a whitespace-only prefix — is treated as "no fetch
        # this tick" (drop, keep prior baseline), NOT as an empty extraction
        # that would store a blank baseline and later "change" into content.
        return _WS_RE.sub(" ", markdown).strip() or None

    def matches_trigger(self, signal: Signal, item: WatchlistItem) -> bool:
        """Optional keyword filter — matched against the change summary + added text."""
        keywords = item.trigger_json.get("keywords") or []
        if not keywords:
            return True
        haystack = (
            signal.normalized_summary
            + " "
            + (signal.raw_payload.get("added_text") or "")
        ).lower()
        return any(str(kw).lower() in haystack for kw in keywords)


# --------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------- #


def _html_to_text(body: bytes) -> str:
    """Reduce an HTML page to normalized, comparable visible text.

    Markup-insensitive on purpose: strips <script>/<style>/<noscript> blocks
    and comments, removes remaining tags, unescapes entities, and collapses
    all whitespace — so a re-minified or re-ordered-attribute page that reads
    the same does NOT register as a change.
    """
    try:
        markup = body.decode("utf-8", errors="replace")
    except Exception:
        return ""
    markup = _DROP_BLOCKS_RE.sub(" ", markup)
    markup = _COMMENT_RE.sub(" ", markup)
    markup = _TAG_RE.sub(" ", markup)
    text = html.unescape(markup)
    # Full normalized text — NOT truncated. The fetch is already byte-capped
    # (~2MB), and hashing the whole thing means detection has no blind spot;
    # snapshot/diff length is bounded separately at the call site.
    return _WS_RE.sub(" ", text).strip()


def _diff(old: str, new: str) -> tuple[int, str]:
    """Return (percent-changed, added-text) in a single diff pass.

    Word lists are capped at ``_MAX_DIFF_WORDS`` before the (O(N·M),
    autojunk-off) SequenceMatcher runs, so a high-entropy page can't make the
    summary computation pathological. The summary is best-effort over the
    capped prefix; change *detection* (the content hash) is unaffected.
    """
    old_words = old.split()[:_MAX_DIFF_WORDS]
    new_words = new.split()[:_MAX_DIFF_WORDS]
    sm = difflib.SequenceMatcher(None, old_words, new_words, autojunk=False)
    pct = int(round((1.0 - sm.ratio()) * 100))
    chunks: list[str] = []
    for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
        if tag in ("insert", "replace"):
            chunk = " ".join(new_words[j1:j2])
            if chunk:
                chunks.append(chunk)
    return pct, " … ".join(chunks)


def _host_label(url: str) -> str:
    from urllib.parse import urlparse

    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return "page"
    parts = [p for p in host.split(".") if p and p not in {"www"}]
    return parts[0] if parts else host or "page"


def _make_dedup_key(slug: str, content_hash: str) -> str:
    """Fold slug + UTC date + content hash, mirroring the stock adapter.

    Including the date means a page that returns to a previously-seen state on
    a LATER day re-alerts (a genuine new change event), while the same change
    seen twice in one day collapses — without the date, an oscillating page
    (A→B→A→B) would have its repeat transitions silently deduped forever.
    """
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    payload = f"{slug}\x00{today}\x00{content_hash}".encode()
    digest = hashlib.sha256(payload).hexdigest()[:32]
    return f"page_watch:{digest}"
