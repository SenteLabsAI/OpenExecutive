"""Insert-time validation + normalization of watchlist targets.

The watchlist is largely populated by an LLM (the ``executive_research``
workflow) proposing ``rss`` feed URLs it never verified — historically
the great majority were dead (404 / 403 / not-actually-a-feed), producing
a watchlist full of rows that fetch nothing and a permanently-empty
briefing.

This module validates an ``rss`` target at insert time: it must fetch and
parse as a feed with entries. A target the server *answers* but that
isn't a feed is either:

  - converted to a ``page_watch`` when the page the feed check already
    fetched yields readable text (a news or pricing page with no feed), or
  - rejected (:class:`WatchlistTargetError`) when it does not,

so a dead ``rss`` row never reaches the scan loop. A transient / network
blip passes the target through unchanged rather than blocking a
legitimate add on a hiccup. Every non-``rss`` type returns unchanged with
no network I/O.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Literal
from urllib.parse import urlsplit

import feedparser
import httpx

from openexecutive.config import get_settings
from openexecutive.monitoring.models import (
    SOURCE_KIND_PAGE_WATCH,
    SOURCE_KIND_RSS,
)
from openexecutive.monitoring.sources._http import (
    FetchOverflowError,
    fetch_bounded,
    validate_target_url,
)
from openexecutive.monitoring.sources.page_watch import html_to_text

logger = logging.getLogger(__name__)

# A non-feed page becomes a page_watch only when its visible text is at least
# this long. Rules out empty shells, bare error strings and binary bodies that
# happen to decode to a few characters; a real article, pricing or news page
# clears it easily.
_MIN_PAGE_TEXT_CHARS = 200
# Only the head of the body is reduced for that check: 200 visible
# characters never need more, and the insert path runs on the request
# thread, so the work is bounded whatever the server sends.
_READABILITY_SCAN_BYTES = 262_144


# Sites that show a signed-out visitor a sign-in wall instead of the content,
# so a page watch there sees the same wall on every poll and never changes.
_SIGN_IN_WALL_HOSTS = (
    "linkedin.com", "facebook.com", "instagram.com", "x.com", "twitter.com",
    "tiktok.com", "glassdoor.com", "threads.net",
)
# Document files: a fixed report or deck whose bytes never change, and whose
# "text" is binary to the page reader.
_DOCUMENT_SUFFIXES = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip")
_MONTHS = (
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
)
_DATE_IN_PATH_RES = (
    re.compile(r"/20\d\d/\d{1,2}(/|$)"),            # /2026/10/...
    re.compile(r"20\d\d-\d{1,2}-\d{1,2}"),           # 2026-10-15
    re.compile(r"(?:^|[/-])(?:" + "|".join(_MONTHS) + r")-\d{1,2}-20\d\d"),  # october-15-2026
)
# A last path segment of this many hyphen-separated words is an article's
# title slug ("the-great-tightening-how-senior-housing-is-entering"), not a
# section of a site.
_ARTICLE_SLUG_MIN_WORDS = 6


def page_watch_target_problem(url: str) -> str:
    """Why ``url`` would make a page watch that never reports anything, or
    ``""`` when it looks like a page that changes as news is added.

    No network: judged from the URL alone, for the research workflow, which
    must watch a site's news or insights page (or its feed), not one article,
    one document or a profile behind a sign-in wall."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return "not a valid URL"
    host = (parts.hostname or "").lower()
    path = parts.path.lower().rstrip("/")
    if any(host == h or host.endswith("." + h) for h in _SIGN_IN_WALL_HOSTS):
        return f"{host} shows a sign-in wall to the watcher, so the page never changes"
    if path.endswith(_DOCUMENT_SUFFIXES):
        return "a document file is a fixed copy that never changes"
    if any(rx.search(path) for rx in _DATE_IN_PATH_RES):
        return "a dated single article never changes once published"
    last = path.rsplit("/", 1)[-1]
    if len([w for w in last.split("-") if w]) >= _ARTICLE_SLUG_MIN_WORDS:
        return "a single article never changes once published"
    return ""


class WatchlistTargetError(Exception):
    """A watchlist target failed insert-time validation (definitively bad)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def validate_and_normalize_target(
    signal_type: str, target: str, config: dict[str, Any],
) -> tuple[str, str, dict[str, Any]]:
    """Validate ``target`` for ``signal_type``; may convert the signal type.

    Returns the ``(signal_type, target, config)`` to insert — converted to
    ``page_watch`` when a non-feed ``rss`` target is a readable page. Raises
    :class:`WatchlistTargetError` when an ``rss`` target is definitively
    bad and can't be salvaged. Only ``rss`` is validated; every other type
    is returned unchanged with no network I/O.
    """
    if signal_type != SOURCE_KIND_RSS:
        return signal_type, target, config

    verdict, body = await _feed_check(target)
    if verdict == "feed":
        return signal_type, target, config
    if verdict == "unknown":
        # Transient fetch failure — inserting as-is beats rejecting a feed
        # that's merely unreachable right now; the scan loop retries.
        logger.info(
            "watchlist validate: %r unreachable now — inserting rss unverified",
            target,
        )
        return signal_type, target, config
    if verdict == "blocked":
        # SSRF-rejected / unparseable / non-public. Reject outright.
        raise WatchlistTargetError(
            f"{target!r} is not a fetchable public URL "
            "(blocked by the SSRF guard or unparseable)."
        )

    # verdict == "not_feed": a public server answered, but it is not a feed.
    # The body the feed check already fetched (same SSRF guard, same byte
    # cap the page_watch adapter uses) decides whether the page is worth
    # watching: readable text → page_watch; nothing readable → reject.
    if body is not None and _is_readable_page(body):
        new_config = dict(config)
        # page_watch reads config["label"]; the rss row used "feed_label".
        label = new_config.pop("feed_label", None)
        if label and "label" not in new_config:
            new_config["label"] = label
        logger.info(
            "watchlist validate: %r is not a feed but is a readable page — "
            "converting rss → page_watch", target,
        )
        return SOURCE_KIND_PAGE_WATCH, target, new_config

    raise WatchlistTargetError(
        f"{target!r} is not a valid RSS/Atom feed and yields no readable "
        "page text. Fix the feed URL, or add it as signal_type='page_watch'."
    )


def _is_readable_page(body: bytes) -> bool:
    """Whether a fetched body is a text page with enough visible content to
    watch. A NUL byte in the head marks a binary body (PDF, image, archive);
    the text threshold rejects shells and one-line error pages."""
    if b"\x00" in body[:4096]:
        return False
    return len(html_to_text(body[:_READABILITY_SCAN_BYTES])) >= _MIN_PAGE_TEXT_CHARS


async def _feed_check(
    target: str,
) -> tuple[Literal["feed", "not_feed", "blocked", "unknown"], bytes | None]:
    """Classify ``target``; also return the fetched body when there is one.

    ``feed`` — feedparser recognized it as a feed.
    ``not_feed`` — a public server answered, but it isn't a feed (the body
    is returned so the caller can decide whether it is a readable page;
    ``None`` when the server answered with an error or an oversized body).
    ``blocked`` — SSRF-rejected / unparseable / non-public URL (never fetched).
    ``unknown`` — transient fetch failure; caller should not penalize it.
    """
    ok, _reason = validate_target_url(target)
    if not ok:
        return "blocked", None
    max_bytes = get_settings().external_monitor_max_fetch_bytes
    try:
        body = await fetch_bounded(target, max_bytes)
    except FetchOverflowError:
        return "not_feed", None  # too big to be a sane feed or page
    except httpx.HTTPStatusError:
        return "not_feed", None  # server answered 4xx/5xx → definitively bad
    except (httpx.HTTPError, httpx.InvalidURL):
        return "unknown", None   # connection / timeout — transient
    # feedparser sets `version` (e.g. "rss20", "atom10") for anything it
    # recognizes as a feed — INCLUDING a valid but currently-empty feed. Use
    # it rather than `.entries` so a brand-new / quiet feed isn't misread as a
    # non-feed page and wrongly converted or rejected. Use dict `.get` (not
    # attribute access): an empty body yields a FeedParserDict with no
    # `version` key at all, and `parsed.version` would AttributeError.
    parsed = feedparser.parse(body)
    return ("feed", body) if parsed.get("version") else ("not_feed", body)


__all__ = [
    "WatchlistTargetError",
    "page_watch_target_problem",
    "validate_and_normalize_target",
]
