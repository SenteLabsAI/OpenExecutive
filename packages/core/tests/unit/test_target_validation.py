"""Unit tests for insert-time watchlist target validation.

Covers: non-rss passthrough (no network), valid-feed keep, non-feed →
page_watch conversion when the fetched page has readable text, non-feed
reject when it has none, transient-failure keep, and the 4xx → reject path.
All network is mocked.
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest

from openexecutive.monitoring import target_validation as tv
from openexecutive.monitoring.models import PAGE_WATCH_FETCH_KEY
from openexecutive.monitoring.sources import page_watch
from openexecutive.monitoring.sources._http import FetchOverflowError

_FEED = (
    b'<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>'
    b"<item><title>i1</title><link>http://x/1</link><guid>1</guid></item>"
    b"</channel></rss>"
)
# Valid RSS that is recognized as a feed but currently has no <item> entries.
_EMPTY_FEED = (
    b'<?xml version="1.0"?><rss version="2.0"><channel>'
    b"<title>Brand new feed</title></channel></rss>"
)
_NOT_FEED = (
    b"<html><body><h1>Pricing</h1><p>Our Pro plan is now priced per seat, "
    b"with annual billing available for teams of ten or more. Enterprise "
    b"customers get SSO, audit logs, a dedicated success manager and a "
    b"99.9% uptime commitment. Contact sales for volume pricing.</p></body></html>"
)
# Answers, but reduces to no visible text (a JS-only shell).
_EMPTY_SHELL = b"<html><head><script>boot()</script></head><body></body></html>"
# Answers with a one-line page: too little text to be worth watching.
_THIN_PAGE = b"<html><body>Error 404 - not found</body></html>"
# A binary body (PDF magic + NUL bytes) that would decode to garbage.
_BINARY = b"%PDF-1.7\x00\x00" + bytes(range(256)) * 8


def _install(monkeypatch: pytest.MonkeyPatch, *, fetch: Any) -> None:
    monkeypatch.setattr(tv, "validate_target_url", lambda u: (True, ""))
    monkeypatch.setattr(tv, "fetch_bounded", fetch)


def _fetch_returning(body: bytes) -> Any:
    async def _f(url: str, max_bytes: int, **kw: object) -> bytes:
        return body
    return _f


def _fetch_raising(exc: Exception) -> Any:
    async def _f(url: str, max_bytes: int, **kw: object) -> bytes:
        raise exc
    return _f


@pytest.mark.asyncio
async def test_non_rss_passthrough_no_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # fetch_bounded set to explode — a stock add must never touch it.
    async def boom(*a: object, **k: object) -> bytes:
        raise AssertionError("non-rss must not fetch")

    monkeypatch.setattr(tv, "fetch_bounded", boom)
    st, target, config = await tv.validate_and_normalize_target(
        "stock", "AAPL", {"display_name": "Apple"},
    )
    assert (st, target, config) == ("stock", "AAPL", {"display_name": "Apple"})


@pytest.mark.asyncio
async def test_valid_feed_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, fetch=_fetch_returning(_FEED))
    st, target, config = await tv.validate_and_normalize_target(
        "rss", "https://good.example/feed", {"feed_label": "Good"},
    )
    assert st == "rss"
    assert config == {"feed_label": "Good"}


@pytest.mark.asyncio
async def test_non_feed_readable_page_converts_to_page_watch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fetch(url: str, max_bytes: int, **kw: object) -> bytes:
        calls.append(url)
        return _NOT_FEED

    _install(monkeypatch, fetch=fetch)
    st, target, config = await tv.validate_and_normalize_target(
        "rss", "https://news.example/page", {"feed_label": "News"},
    )
    assert st == page_watch.PageWatchSource.kind  # "page_watch"
    # The body the feed check fetched decides the conversion — one fetch.
    assert calls == ["https://news.example/page"]
    # No fetch-source marker is written; the plain fetcher is the only path.
    assert PAGE_WATCH_FETCH_KEY not in config
    # feed_label is remapped to the label key page_watch reads.
    assert config.get("label") == "News"
    assert "feed_label" not in config


@pytest.mark.asyncio
async def test_empty_but_valid_feed_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A recognized feed with zero entries (brand-new / quiet) must be kept as
    # rss, not misread as a non-feed page.
    _install(monkeypatch, fetch=_fetch_returning(_EMPTY_FEED))
    st, _t, config = await tv.validate_and_normalize_target(
        "rss", "https://new.example/feed", {},
    )
    assert st == "rss"


@pytest.mark.asyncio
async def test_empty_body_is_not_feed_no_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # HTTP 200 with a zero-byte body: feedparser yields a dict with no
    # `version` key — must classify as not_feed, not raise AttributeError.
    _install(monkeypatch, fetch=_fetch_returning(b""))
    with pytest.raises(tv.WatchlistTargetError):
        await tv.validate_and_normalize_target("rss", "https://empty.example", {})


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_readability_check_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the head of a body over the cap is reduced for the readability check."""
    calls: list[int] = []

    def fake_html_to_text(body: bytes) -> str:
        calls.append(len(body))
        return "x" * 500

    monkeypatch.setattr(tv, "html_to_text", fake_html_to_text)
    _install(monkeypatch, fetch=_fetch_returning(b"<p>" * (tv._READABILITY_SCAN_BYTES // 3 + 1)))
    st, _target, _config = await tv.validate_and_normalize_target("rss", "https://big.example/x", {})
    assert st == "page_watch"
    assert calls == [tv._READABILITY_SCAN_BYTES]


@pytest.mark.parametrize("body", [_EMPTY_SHELL, _THIN_PAGE, _BINARY], ids=["shell", "thin", "binary"])
async def test_non_feed_without_readable_text_rejected(
    monkeypatch: pytest.MonkeyPatch, body: bytes,
) -> None:
    _install(monkeypatch, fetch=_fetch_returning(body))
    with pytest.raises(tv.WatchlistTargetError) as ei:
        await tv.validate_and_normalize_target(
            "rss", "https://dead.example/x", {},
        )
    assert "not a valid RSS" in str(ei.value)


@pytest.mark.asyncio
async def test_404_status_is_not_feed_then_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exc = httpx.HTTPStatusError(
        "404",
        request=httpx.Request("GET", "https://x.example"),
        response=httpx.Response(404),
    )
    _install(monkeypatch, fetch=_fetch_raising(exc))
    with pytest.raises(tv.WatchlistTargetError):
        await tv.validate_and_normalize_target("rss", "https://x.example", {})


@pytest.mark.asyncio
async def test_transient_failure_keeps_rss_unverified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, fetch=_fetch_raising(httpx.ConnectError("reset")))
    st, target, config = await tv.validate_and_normalize_target(
        "rss", "https://maybe.example/feed", {},
    )
    assert st == "rss"  # not penalized for a transient blip


@pytest.mark.asyncio
async def test_oversized_body_is_not_feed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, fetch=_fetch_raising(FetchOverflowError()))
    # Oversized: no body to inspect, so it cannot become a page_watch either.
    with pytest.raises(tv.WatchlistTargetError):
        await tv.validate_and_normalize_target("rss", "https://big.example", {})


@pytest.mark.asyncio
async def test_ssrf_rejected_target_is_not_feed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tv, "validate_target_url", lambda u: (False, "private IP"))

    async def boom(*a: object, **k: object) -> bytes:
        raise AssertionError("must not fetch an SSRF-rejected target")

    monkeypatch.setattr(tv, "fetch_bounded", boom)
    # Rejected outright as "blocked" — never fetched.
    with pytest.raises(tv.WatchlistTargetError):
        await tv.validate_and_normalize_target("rss", "http://169.254.169.254", {})
