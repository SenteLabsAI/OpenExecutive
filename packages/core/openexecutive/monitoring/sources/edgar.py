"""SEC EDGAR filings adapter.

Watches a public company's recent SEC filings — 8-K (material events),
10-K / 10-Q (annual / quarterly reports), Form 4 (insider transactions),
etc. — the highest-signal keyless structured source in the roadmap. A new
8-K is a company *telling the SEC something material just happened*; paired
with capture-time enrichment ("why this matters to us") it is far higher
signal than a news RSS feed.

Watchlist row shape:

  - ``signal_type``: ``"edgar"``
  - ``target``: a ticker symbol (e.g. ``"AAPL"``) OR a CIK (``"320193"`` /
    ``"CIK0000320193"``). EDGAR's ``CIK=`` query param resolves a ticker to
    its CIK server-side, so we do not ship a ticker→CIK mapping table.
  - ``config_json``: optional ``{"label": "Apple"}`` — prefix in the summary.
  - ``trigger_json``: optional ``{"forms": ["8-K", "10-K"]}`` — only these
    form types surface. Defaults to ``{8-K, 10-K, 10-Q}`` (the material set;
    Form 4 insider noise is opt-in). Amendments (``8-K/A``) match their base
    form.

Data source: SEC's ``browse-edgar`` Atom feed
(``/cgi-bin/browse-edgar?action=getcompany&CIK=…&output=atom``). Parsed with
``feedparser`` (already a dependency). Each entry carries the form type (Atom
``category``), the filing-index URL (clickable provenance), and the accession
number (in the entry id) — a globally-unique natural dedup key, the cleanest
of any source here.

SEC requires a descriptive User-Agent identifying the caller; we send
``settings.edgar_user_agent`` (operators should set a real contact via
``EDGAR_USER_AGENT``). Read-only, bounded fetch, SSRF-guarded like every
adapter.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import feedparser
import httpx

from openexecutive.alerts.models import AlertSeverity
from openexecutive.config import get_settings
from openexecutive.monitoring.models import SOURCE_KIND_EDGAR, Signal, WatchlistItem
from openexecutive.monitoring.sources._http import (
    FetchOverflowError,
    fetch_bounded,
    validate_target_url,
)
from openexecutive.monitoring.sources.base import (
    feed_entry_published_at,
    future_skew_for,
)

logger = logging.getLogger(__name__)

# Filings are not high-frequency; hourly is a polite, sufficient cadence.
_DEFAULT_POLL_MINUTES = 60

# browse-edgar's CIK param accepts a ticker OR a CIK and resolves server-side.
# One constant drives BOTH the feed's count= param (how many recent filings
# SEC returns) and our per-poll output cap, so they can't drift apart.
_EDGAR_ATOM_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
    "&CIK={ident}&type=&dateb=&owner=include&count={count}&output=atom"
)
_MAX_FILINGS_PER_POLL = 40

# Default form filter — the material set. Form 4 (insider trades) and the
# long tail are opt-in via trigger_json.forms.
_DEFAULT_FORMS = frozenset({"8-K", "10-K", "10-Q"})

# Pull the accession number out of the Atom entry id, e.g.
# "urn:tag:sec.gov,2008:accession-number=0000320193-24-000123". Anchored to
# EDGAR's exact 10-2-6 digit shape so a malformed id can't yield a truncated
# or over-greedy capture (which would produce an unstable dedup key); a
# non-matching id falls back to the filing link in _accession().
_ACCESSION_RE = re.compile(r"accession-number=(\d{10}-\d{2}-\d{6})")

# Max length of a ticker/CIK target. CIK is ≤10 digits ("CIK"+10 = 13);
# tickers are short. 20 is generous headroom while still keeping arbitrary
# strings out of the EDGAR URL (with urlencoding + the SSRF guard).
_MAX_IDENT_LEN = 20
_IDENT_RE = re.compile(rf"^[A-Za-z0-9.\-]{{1,{_MAX_IDENT_LEN}}}$")


class EdgarSource:
    kind: str = SOURCE_KIND_EDGAR
    default_poll_interval_minutes: int = _DEFAULT_POLL_MINUTES
    # The Atom feed lists the last N filings on every poll; the first poll
    # is a baseline so a new watch doesn't replay past filings as news.
    seed_on_first_poll: bool = True

    async def poll(
        self, item: WatchlistItem, *, db_path: Path | None = None
    ) -> list[Signal]:
        ident = (item.target or "").strip()
        if not ident or not _IDENT_RE.match(ident):
            logger.warning(
                "edgar: watchlist %r has bad ticker/CIK %r — skipping",
                item.slug, item.target,
            )
            return []

        url = _EDGAR_ATOM_URL.format(
            ident=quote(ident, safe=""), count=_MAX_FILINGS_PER_POLL
        )
        ok, reason = validate_target_url(url)
        if not ok:
            logger.warning("edgar: rejecting target url %r — %s", url, reason)
            return []

        settings = get_settings()
        try:
            body = await fetch_bounded(
                url,
                settings.external_monitor_max_fetch_bytes,
                user_agent=settings.edgar_user_agent,
            )
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            logger.warning("edgar: fetch failed for %s (%s): %s", item.slug, ident, exc)
            return []
        except FetchOverflowError:
            logger.warning("edgar: feed for %s exceeded byte cap — dropping tick", ident)
            return []

        parsed = feedparser.parse(body)
        if parsed.bozo and not parsed.entries:
            logger.warning(
                "edgar: feed for %s could not be parsed: %s",
                ident, parsed.bozo_exception,
            )
            return []

        allowed = _allowed_forms(item.trigger_json)
        label = item.config_json.get("label") or ident.upper()
        skew = future_skew_for(item)
        signals: list[Signal] = []
        # Filter first, then cap the OUTPUT — capping the raw entry list before
        # filtering would let a run of excluded forms (e.g. many Form 4s) at the
        # front starve the budget and hide a matching filing behind them.
        for entry in parsed.entries:
            if len(signals) >= _MAX_FILINGS_PER_POLL:
                break
            signal = _entry_to_signal(entry, item, label, allowed, skew)
            if signal is not None:
                signals.append(signal)
        return signals

    def matches_trigger(self, signal: Signal, item: WatchlistItem) -> bool:
        # Form filtering is applied in poll() (mirrors stock's threshold);
        # keep this permissive so the pipeline doesn't double-filter.
        return True


# --------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------- #


def _allowed_forms(trigger_json: dict) -> frozenset[str]:
    """Resolve the form filter from trigger_json, defaulting to the material set."""
    forms = trigger_json.get("forms")
    if isinstance(forms, list) and forms:
        cleaned = {str(f).strip().upper() for f in forms if str(f).strip()}
        if cleaned:
            return frozenset(cleaned)
    return _DEFAULT_FORMS


def _entry_form(entry: dict) -> str:
    """Best-effort form type from an Atom entry (category term, else title)."""
    tags = entry.get("tags") or []
    for tag in tags:
        term = (tag.get("term") or "").strip()
        if term:
            return term.upper()
    # Fallback: titles look like "8-K - APPLE INC ..." or just "8-K".
    title = (entry.get("title") or "").strip()
    if not title:
        return ""
    head = title.split(" - ")[0].split()
    return head[0].upper() if head else ""


def _form_matches(form: str, allowed: frozenset[str]) -> bool:
    """An entry matches if its form (or its amendment base, '8-K/A'→'8-K') is allowed."""
    if not form:
        return False
    if form in allowed:
        return True
    base = form.split("/")[0]
    return base in allowed


def _accession(entry: dict) -> str:
    """Globally-unique accession number from the entry id, else the link."""
    raw_id = str(entry.get("id") or "")
    m = _ACCESSION_RE.search(raw_id)
    if m:
        return m.group(1)
    link = (entry.get("link") or "").strip()
    return link or raw_id


def _entry_to_signal(
    entry: dict, item: WatchlistItem, label: str, allowed: frozenset[str],
    skew: timedelta,
) -> Signal | None:
    form = _entry_form(entry)
    if not _form_matches(form, allowed):
        return None

    accession = _accession(entry)
    if not accession:
        # No stable id → no reliable dedup → skip rather than risk replays.
        return None

    link = (entry.get("link") or "").strip()
    if not link:
        # provenance_url is mandatory; an EDGAR entry without a filing-index
        # link is unusable.
        return None
    ok, _reason = validate_target_url(link)
    if not ok:
        logger.warning("edgar: dropping %s filing — bad link %r", item.slug, link)
        return None

    filed = (entry.get("updated") or entry.get("published") or "").strip()
    summary = f"[{label}] {form} filed{(' ' + filed[:10]) if filed else ''}"
    return Signal(
        watchlist_id=item.id or 0,
        source_kind=SOURCE_KIND_EDGAR,
        source_external_id=accession[:500],
        captured_at=datetime.now(UTC).isoformat(),
        published_at=feed_entry_published_at(entry, skew=skew),
        normalized_summary=summary[:500],
        raw_payload={
            "label": label,
            "ident": item.target,
            "form": form,
            "accession": accession,
            "filed": filed,
            "link": link,
            "title": (entry.get("title") or "")[:300],
        },
        provenance_url=link,
        severity_hint=_severity_for_form(form),
        dedup_key=_make_dedup_key(item.slug, accession),
    )


def _severity_for_form(form: str) -> AlertSeverity:
    base = form.split("/")[0]
    if base == "8-K":
        # Material-event report — the "something just happened" form.
        return AlertSeverity.HIGH
    if base in ("10-K", "10-Q"):
        return AlertSeverity.MEDIUM
    return AlertSeverity.LOW


def _make_dedup_key(slug: str, accession: str) -> str:
    payload = f"{slug}\x00{accession}".encode()
    digest = hashlib.sha256(payload).hexdigest()[:32]
    return f"edgar:{digest}"
