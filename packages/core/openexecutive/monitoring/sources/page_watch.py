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
    PAGE_WATCH_FETCH_LEGACY_XCRAWL,
    SOURCE_KIND_PAGE_WATCH,
    Signal,
    WatchlistItem,
)
from openexecutive.monitoring.sources._http import (
    FetchOverflowError,
    fetch_bounded,
    validate_target_url,
)
from openexecutive.utils.html_tags import strip_tags

logger = logging.getLogger(__name__)

# Pages change slowly; 6h is polite and sufficient. Per-row cadence can tighten.
_DEFAULT_POLL_MINUTES = 360

# The legacy fetch marker (config_json["fetch"] == "xcrawl") lives in
# monitoring.models; see PAGE_WATCH_FETCH_KEY / PAGE_WATCH_FETCH_LEGACY_XCRAWL.

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
# JS/CSS never counts as "visible text". Removed by a linear scan
# (_strip_blocks), not a lazy `.*?` regex: on a page full of unterminated
# openers such a regex is quadratic, and a page the server sends is the
# attacker's to shape. The remaining tags go the same way, through
# utils.html_tags.strip_tags rather than `<[^>]+>`, which rescans to the end
# of the page from every "<" that nothing closes.
_DROP_BLOCK_TAGS = ("script", "style", "noscript")
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

        # A row still tagged with the legacy xcrawl marker has a baseline
        # captured as scraped markdown, which never hashes equal to the
        # httpx text of the same page. Re-capture it silently and clear the
        # marker so the switch of fetcher is not reported as a page change.
        # Only a stored row can have its marker cleared, so an unsaved item
        # (no id) is compared normally rather than re-baselined every poll.
        legacy_baseline = bool(item.id) and (
            item.config_json.get(PAGE_WATCH_FETCH_KEY) == PAGE_WATCH_FETCH_LEGACY_XCRAWL
        )
        if prior is None or legacy_baseline:
            # First observation — record the baseline, do not alert.
            store.upsert_page_watch_state(item.slug, content_hash, snapshot, db_path=db_path)
            if legacy_baseline:
                self._clear_legacy_marker(item, db_path=db_path)
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
        return html_to_text(body)

    @staticmethod
    def _clear_legacy_marker(item: WatchlistItem, *, db_path: Path | None) -> None:
        """Drop the legacy ``fetch: xcrawl`` marker from the row's config so
        the next poll compares against the freshly captured baseline. The
        key is removed in place (no read-modify-write of the whole column),
        so a concurrent edit to another config key is kept. Best-effort: a
        failed update only means the baseline is re-captured once more."""
        if not item.id:
            return
        try:
            changed = store.remove_watchlist_config_key(
                item.id, PAGE_WATCH_FETCH_KEY, db_path=db_path,
            )
        except Exception:  # noqa: BLE001 — never let housekeeping sink a poll
            changed = 0
        if not changed:
            logger.warning(
                "page_watch: could not clear legacy fetch marker on %s", item.slug,
            )

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


def html_to_text(body: bytes) -> str:
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
    markup = _strip_blocks(markup)
    markup = strip_tags(markup, repl=" ")
    text = html.unescape(markup)
    # Full normalized text — NOT truncated. The fetch is already byte-capped
    # (~2MB), and hashing the whole thing means detection has no blind spot;
    # snapshot/diff length is bounded separately at the call site.
    return _WS_RE.sub(" ", text).strip()


def _strip_blocks(markup: str) -> str:
    """Drop HTML comments and <script>/<style>/<noscript> blocks (content
    included) in one pass that is linear in the page length. An unterminated
    comment or block runs to the end of the page: a truncated script is not
    visible text either."""
    lower = markup.lower()
    n = len(markup)
    out: list[str] = []
    i = 0
    while i < n:
        j = lower.find("<", i)
        if j == -1:
            out.append(markup[i:])
            break
        out.append(markup[i:j])
        if lower.startswith("<!--", j):
            end = lower.find("-->", j + 4)
            i = n if end == -1 else end + 3
            out.append(" ")
            continue
        tag = next(
            (
                t for t in _DROP_BLOCK_TAGS
                if lower.startswith("<" + t, j)
                and (j + 1 + len(t) >= n or not (lower[j + 1 + len(t)].isalnum() or lower[j + 1 + len(t)] == "_"))
            ),
            None,
        )
        if tag is None:
            out.append("<")
            i = j + 1
            continue
        close = lower.find("</" + tag, j)
        if close == -1:
            i = n
        else:
            gt = lower.find(">", close)
            i = n if gt == -1 else gt + 1
        out.append(" ")
    return "".join(out)


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
