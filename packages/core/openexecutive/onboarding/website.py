"""Read a company's website for setup.

Setup's first step takes an optional website. This reads the home page and up
to four more pages on the same site (about, team, products and the like), as
plain text, for the interview's opening turn, the way attached files are read.
Our own server fetches the pages, so it works whatever model is configured,
web search or not.

Every fetch goes through ``monitoring.sources._http.fetch_bounded`` (SSRF
guard with pinned DNS, three redirects, a body cap, a timeout). The whole read
has a time limit; a page that fails is skipped, and a site with nothing
readable returns no pages, so setup carries on from the text box. Nothing is
stored or indexed: the pages live only in the setup transcript. The text is
someone else's words, so callers wrap it as untrusted content.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import httpx

from openexecutive.monitoring.sources._http import (
    FetchOverflowError,
    fetch_bounded,
)
from openexecutive.monitoring.sources.page_watch import html_to_text

logger = logging.getLogger(__name__)

# Pages read besides the home page.
MAX_EXTRA_PAGES = 4
# The whole read, every page included.
TIME_LIMIT_SECONDS = 20.0
# One page's bytes as fetched; text is capped by the caller.
MAX_PAGE_BYTES = 2 * 1024 * 1024
# What was typed: a domain or an address, nothing longer.
MAX_INPUT_CHARS = 300
# A page's label (host and path), as it appears in the setup transcript.
MAX_LABEL_CHARS = 120
# What a path may keep as is: everything else is percent-encoded, so a link
# can't carry markup or prose into a label (e.g. "</untrusted_content>").
_PATH_SAFE = "/-._~%!$&'()*+,;=:@"

# Text shorter than this is a script shell or an empty page, not content.
MIN_PAGE_CHARS = 200

# A read is kept briefly so setup's two calls (understand, then start) read
# the site once. Per process: another worker just reads it again.
CACHE_SECONDS = 600.0
_CACHE_MAX = 32
_cache: OrderedDict[tuple[str, int, int], tuple[float, list[WebsitePage]]] = OrderedDict()

# Link paths or texts worth reading for a company profile, best first.
_WANTED = (
    "about",
    "company",
    "team",
    "people",
    "leadership",
    "product",
    "solution",
    "service",
    "pricing",
    "customer",
)
_SKIPPED_EXTENSIONS = (
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".zip",
    ".mp4", ".mp3", ".css", ".js", ".xml", ".json", ".ico",
)


class WebsiteError(ValueError):
    """What was typed is not a website address. Its text is safe to show."""


@dataclass(frozen=True)
class WebsitePage:
    # Shown in the transcript's label, e.g. "example.com/team".
    label: str
    text: str


def normalize_url(raw: str) -> str:
    """``example.com`` → ``https://example.com/``. Refuses anything that is
    not an http(s) address with a host (credentials and ports included)."""
    value = raw.strip()
    if not value:
        raise WebsiteError("Enter a website address, like example.com.")
    if len(value) > MAX_INPUT_CHARS or any(c.isspace() for c in value):
        raise WebsiteError("That doesn't look like a website address.")
    if "://" not in value:
        value = f"https://{value}"
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise WebsiteError("That doesn't look like a website address.") from exc
    host = (parts.hostname or "").rstrip(".")
    if parts.scheme not in ("http", "https") or "." not in host or parts.username or port:
        raise WebsiteError("That doesn't look like a website address.")
    return urlunsplit((parts.scheme, host, _clean_path(parts.path), quote(parts.query, safe="=&"), ""))


def _clean_path(path: str) -> str:
    return quote(path, safe=_PATH_SAFE) or "/"


def _site(host: str) -> str:
    host = host.lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def label_for(url: str) -> str:
    """``https://www.example.com/team/`` → ``example.com/team``: the host and
    a percent-encoded path, capped. It sits outside the untrusted block in the
    transcript, so nothing from the page may reach it unencoded."""
    parts = urlsplit(url)
    path = _clean_path(parts.path).rstrip("/")
    return f"{_site(parts.hostname or '')}{path}"[:MAX_LABEL_CHARS]


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None


def pick_pages(home_url: str, html: str, limit: int = MAX_EXTRA_PAGES) -> list[str]:
    """Up to ``limit`` same-site pages from the home page's links, the ones
    that read as about, team, products and so on first. ``/about`` when none
    does."""
    parser = _Links()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 — a malformed page just has no links
        logger.debug("website: could not parse the home page's links")
    site = _site(urlsplit(home_url).hostname or "")
    home = label_for(home_url)
    ranked: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    for order, (href, text) in enumerate(parser.links):
        try:
            url = urljoin(home_url, href.strip())
            parts = urlsplit(url)
        except ValueError:
            continue
        if parts.scheme not in ("http", "https") or _site(parts.hostname or "") != site:
            continue
        if parts.path.lower().endswith(_SKIPPED_EXTENSIONS):
            continue
        clean = urlunsplit((parts.scheme, parts.hostname or "", _clean_path(parts.path), "", ""))
        key = label_for(clean)
        if key == home or key in seen:
            continue
        seen.add(key)
        words = f"{parts.path} {text}".lower()
        rank = next((i for i, w in enumerate(_WANTED) if w in words), None)
        if rank is not None:
            ranked.append((rank, order, clean))
    ranked.sort()
    picked = [url for _, _, url in ranked[:limit]]
    if not picked and limit > 0:
        picked = [urljoin(home_url, "/about")]
    return picked


async def _fetch_text(url: str) -> str | None:
    try:
        body = await fetch_bounded(url, MAX_PAGE_BYTES)
    except (httpx.HTTPError, FetchOverflowError, OSError, ValueError) as exc:
        logger.info("website: skipped a page: %s", type(exc).__name__)
        return None
    text = html_to_text(body)
    return text if len(text) >= MIN_PAGE_CHARS else None


async def read_website(
    raw: str, *, max_chars_per_page: int, max_total_chars: int
) -> list[WebsitePage]:
    """The site's readable pages, home first, each and all together capped.
    Empty when nothing could be read in time. Raises ``WebsiteError`` only
    for input that is not an address at all."""
    home_url = normalize_url(raw)
    key = (home_url, max_chars_per_page, max_total_chars)
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None and now - hit[0] < CACHE_SECONDS:
        return list(hit[1])
    pages: list[WebsitePage] = []

    async def _read() -> None:
        try:
            body = await fetch_bounded(home_url, MAX_PAGE_BYTES)
        except (httpx.HTTPError, FetchOverflowError, OSError, ValueError) as exc:
            logger.info("website: home page unreadable: %s", type(exc).__name__)
            return
        text = html_to_text(body)
        if len(text) >= MIN_PAGE_CHARS:
            pages.append(WebsitePage(label_for(home_url), text[:max_chars_per_page]))
        extra = pick_pages(home_url, body.decode("utf-8", errors="replace"))
        results = await asyncio.gather(*(_fetch_text(u) for u in extra))
        for url, page_text in zip(extra, results, strict=True):
            if page_text:
                pages.append(WebsitePage(label_for(url), page_text[:max_chars_per_page]))

    try:
        await asyncio.wait_for(_read(), timeout=TIME_LIMIT_SECONDS)
    except TimeoutError:
        logger.info("website: read stopped at the time limit with %d page(s)", len(pages))
    kept: list[WebsitePage] = []
    left = max_total_chars
    for page in pages:
        if left <= 0:
            break
        kept.append(WebsitePage(page.label, page.text[:left]))
        left -= len(kept[-1].text)
    # An empty read is not kept: the site may be back in a minute.
    if kept:
        _cache[key] = (now, kept)
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
    return kept
