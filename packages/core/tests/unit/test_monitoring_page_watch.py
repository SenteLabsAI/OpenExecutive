"""Unit tests for the page_watch change-detection adapter (P2)."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from openexecutive.alerts.models import AlertSeverity
from openexecutive.alerts.store import initialize_db as initialize_alerts_db
from openexecutive.memory.episodic import initialize_db as initialize_episodic_db
from openexecutive.monitoring import store as ms
from openexecutive.monitoring.models import WatchlistItem
from openexecutive.monitoring.sources import list_registered_kinds
from openexecutive.monitoring.sources.page_watch import (
    PageWatchSource,
    _diff,
    html_to_text,
)


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "test_page_watch.db"
    monkeypatch.setattr("openexecutive.memory.episodic.DB_PATH", db_path)
    monkeypatch.setattr("openexecutive.alerts.store.DB_PATH", db_path)
    initialize_episodic_db(db_path)
    initialize_alerts_db(db_path)
    ms.initialize_db(db_path)
    return db_path


def _make_item(
    *, slug: str = "page-acme", target: str = "https://acme.example/pricing",
    config: dict | None = None, trigger: dict | None = None,
) -> WatchlistItem:
    return WatchlistItem(
        id=1, slug=slug, signal_type="page_watch", target=target,
        config_json=config or {}, trigger_json=trigger or {},
    )


class _Page:
    """Mutable body holder so a test can change the 'served' page between polls."""

    def __init__(self, body: bytes) -> None:
        self.body = body


def _install(monkeypatch: pytest.MonkeyPatch, page: _Page) -> None:
    async def fake_fetch(url: str, max_bytes: int, **kwargs: object) -> bytes:
        return page.body

    monkeypatch.setattr("openexecutive.monitoring.sources.page_watch.fetch_bounded", fake_fetch)
    monkeypatch.setattr(
        "openexecutive.monitoring.sources.page_watch.validate_target_url",
        lambda u: (True, ""),
    )


# --------------------------------------------------------------------- #
# Normalization
# --------------------------------------------------------------------- #


def test_html_to_text_strips_markup_and_scripts() -> None:
    body = b"""
    <html><head><style>.x{color:red}</style>
    <script>var a = 1 < 2;</script></head>
    <body><!-- comment --><h1>Pricing</h1>
    <p>Pro&nbsp;plan is &pound;30/mo</p></body></html>
    """
    text = html_to_text(body)
    assert "Pricing" in text
    assert "£30/mo" in text  # &nbsp; and &pound; entities unescaped
    # script/style content must not survive
    assert "color:red" not in text
    assert "var a" not in text
    # collapsed whitespace (no double spaces)
    assert "  " not in text


def test_diff_reports_percent_and_added_text() -> None:
    pct, added = _diff("the price is ten", "the price is twenty now")
    assert "twenty" in added
    assert 0 < pct <= 100


# --------------------------------------------------------------------- #
# poll() lifecycle
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_first_poll_baselines_without_alert(
    db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _Page(b"<html><body><h1>Pro plan $30/mo</h1></body></html>")
    _install(monkeypatch, page)
    src = PageWatchSource()
    item = _make_item()

    signals = await src.poll(item, db_path=db)
    assert signals == []  # baseline, no alert

    state = ms.get_page_watch_state("page-acme", db_path=db)
    assert state is not None
    assert state["content_hash"]


@pytest.mark.asyncio
async def test_unchanged_poll_emits_nothing(
    db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _Page(b"<html><body>same content</body></html>")
    _install(monkeypatch, page)
    src = PageWatchSource()
    item = _make_item()
    await src.poll(item, db_path=db)        # baseline
    assert await src.poll(item, db_path=db) == []  # unchanged


@pytest.mark.asyncio
async def test_changed_poll_emits_signal_and_updates_state(
    db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _Page(b"<html><body><h1>Pro plan $30/mo</h1></body></html>")
    _install(monkeypatch, page)
    src = PageWatchSource()
    item = _make_item(config={"label": "Acme pricing"})

    await src.poll(item, db_path=db)  # baseline
    state1 = ms.get_page_watch_state("page-acme", db_path=db)

    # Price change.
    page.body = b"<html><body><h1>Pro plan $45/mo</h1></body></html>"
    signals = await src.poll(item, db_path=db)

    assert len(signals) == 1
    sig = signals[0]
    assert sig.severity_hint == AlertSeverity.LOW
    assert sig.provenance_url == "https://acme.example/pricing"
    assert sig.normalized_summary.startswith("[Acme pricing] page changed —")
    assert "45" in sig.raw_payload["added_text"]
    assert sig.dedup_key.startswith("page_watch:")

    # State advanced to the new hash.
    state2 = ms.get_page_watch_state("page-acme", db_path=db)
    assert state2["content_hash"] != state1["content_hash"]

    # Re-polling the now-current page is quiet again.
    assert await src.poll(item, db_path=db) == []


@pytest.mark.asyncio
async def test_change_beyond_snapshot_cap_is_still_detected(
    db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Detection hashes the FULL text, so a change past the snapshot cap fires."""
    filler = "word " * 60_000  # ~300k chars of normalized text, > _MAX_TEXT_CHARS
    page = _Page(f"<body>{filler}END_A</body>".encode())
    _install(monkeypatch, page)
    src = PageWatchSource()
    item = _make_item()
    await src.poll(item, db_path=db)  # baseline

    # The only change is at the very end, well beyond the 100k snapshot cap.
    page.body = f"<body>{filler}END_B</body>".encode()
    signals = await src.poll(item, db_path=db)
    assert len(signals) == 1  # full-text hash caught it


@pytest.mark.asyncio
async def test_markup_only_change_does_not_alert(
    db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _Page(b"<html><body><h1>Hello</h1></body></html>")
    _install(monkeypatch, page)
    src = PageWatchSource()
    item = _make_item()
    await src.poll(item, db_path=db)  # baseline

    # Same visible text, different markup / whitespace / attributes.
    page.body = b"<html>\n  <body>\n    <h1 class='t'>Hello</h1>\n  </body>\n</html>"
    assert await src.poll(item, db_path=db) == []


@pytest.mark.asyncio
async def test_keyword_trigger_matches_changed_text_only(
    db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The keyword filter matches the CHANGE (added text), not the whole page."""
    page = _Page(b"<html><body>Plans: Pro and Team</body></html>")
    _install(monkeypatch, page)
    src = PageWatchSource()
    item = _make_item()
    await src.poll(item, db_path=db)  # baseline

    # New text introduces an Enterprise tier.
    page.body = b"<html><body>Plans: Pro and Team and Enterprise</body></html>"
    signals = await src.poll(item, db_path=db)
    assert len(signals) == 1
    assert "Enterprise" in signals[0].raw_payload["added_text"]

    # A keyword in the added text → kept.
    assert src.matches_trigger(
        signals[0], _make_item(trigger={"keywords": ["enterprise"]})
    )
    # A keyword absent from the change → filtered out (even if elsewhere on page).
    assert not src.matches_trigger(
        signals[0], _make_item(trigger={"keywords": ["acquisition"]})
    )
    # No keywords → permissive.
    assert src.matches_trigger(signals[0], _make_item())


@pytest.mark.asyncio
async def test_empty_extraction_skips(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    page = _Page(b"<html><head><script>noop()</script></head><body></body></html>")
    _install(monkeypatch, page)
    src = PageWatchSource()
    item = _make_item()
    assert await src.poll(item, db_path=db) == []
    # No baseline stored for an empty page.
    assert ms.get_page_watch_state("page-acme", db_path=db) is None


@pytest.mark.asyncio
async def test_bad_target_and_fetch_failure_return_empty(
    db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    src = PageWatchSource()
    # Empty target.
    assert await src.poll(_make_item(target=""), db_path=db) == []

    # Fetch failure.
    async def boom(url: str, max_bytes: int, **kwargs: object) -> bytes:
        raise httpx.ConnectError("dns")

    monkeypatch.setattr("openexecutive.monitoring.sources.page_watch.fetch_bounded", boom)
    monkeypatch.setattr(
        "openexecutive.monitoring.sources.page_watch.validate_target_url",
        lambda u: (True, ""),
    )
    assert await src.poll(_make_item(), db_path=db) == []


# --------------------------------------------------------------------- #
# store + registration
# --------------------------------------------------------------------- #


def test_page_watch_state_roundtrip(db: Path) -> None:
    assert ms.get_page_watch_state("s1", db_path=db) is None
    ms.upsert_page_watch_state("s1", "hashA", "text A", db_path=db)
    st = ms.get_page_watch_state("s1", db_path=db)
    assert st is not None and st["content_hash"] == "hashA"
    # Upsert replaces.
    ms.upsert_page_watch_state("s1", "hashB", "text B", db_path=db)
    st2 = ms.get_page_watch_state("s1", db_path=db)
    assert st2["content_hash"] == "hashB"
    assert st2["text_snapshot"] == "text B"


def test_html_to_text_is_linear_on_unterminated_openers() -> None:
    """A page full of unterminated comment / script openers must reduce in
    linear time: the insert-time conversion runs this on whatever a server
    sends, on the request thread."""
    import time

    for body in (b"<!--" * 100_000, b"<script>" * 60_000, b"<style" * 80_000):
        t0 = time.monotonic()
        assert html_to_text(body) == ""
        assert time.monotonic() - t0 < 1.0
    # Block stripping still behaves: content of script/style/noscript and
    # comments is gone, an unrelated tag such as <scripts> is only a tag.
    assert html_to_text(
        b"<p>a</p><script type=x>js()</script>b<STYLE>c</STYLE><!-- x -->d"
        b"<noscript>n</noscript><scripts>e</scripts>"
    ) == "a b d e"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"<" * 200_000, "<" * 200_000),
        (b"<a" * 100_000, "<a" * 100_000),
        (b"<\n" * 100_000, "< " * 99_999 + "<"),
        (b"<div class=x" * 40_000, "<div class=x" * 40_000),
        (b"<p>kept</p>" + b"<" * 200_000, "kept " + "<" * 200_000),
        (b"<>" * 100_000, "<>" * 100_000),
    ],
    ids=["lt", "lt-a", "lt-newline", "div-attr", "tags-then-lt", "empty-pairs"],
)
def test_html_to_text_is_linear_on_unclosed_tags(body: bytes, expected: str) -> None:
    """A page full of "<" that nothing closes must reduce in one pass: the
    poll runs this on the event loop, and the `<[^>]+>` strip it replaced
    rescanned to the end of the page from every such "<" (quadratic — about
    40 minutes of CPU for a 2 MB page). Such a "<" stays text, as it did."""
    import time

    started = time.perf_counter()
    assert html_to_text(body) == expected
    assert time.perf_counter() - started < 2


def test_page_watch_registered() -> None:
    assert "page_watch" in list_registered_kinds()


# --------------------------------------------------------------------- #
# Legacy rows tagged config_json["fetch"] == "xcrawl"
# --------------------------------------------------------------------- #


def _install_httpx(monkeypatch: pytest.MonkeyPatch, holder: SimpleNamespace) -> None:
    async def fake_fetch(url: str, max_bytes: int, **kwargs: object) -> bytes:
        holder.calls += 1
        return holder.body

    monkeypatch.setattr(
        "openexecutive.monitoring.sources.page_watch.validate_target_url",
        lambda u: (True, ""),
    )
    monkeypatch.setattr(
        "openexecutive.monitoring.sources.page_watch.fetch_bounded", fake_fetch,
    )


@pytest.mark.asyncio
async def test_legacy_xcrawl_row_rebaselines_without_signal(
    db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row whose baseline was captured as xcrawl markdown must not report
    the switch to the plain fetcher as a page change: the first plain poll
    re-captures the baseline silently and clears the legacy marker, and
    only a later real change emits."""
    holder = SimpleNamespace(body=b"<html><body>Pro plan is $30/mo</body></html>", calls=0)
    _install_httpx(monkeypatch, holder)
    ms.insert_watchlist_item(
        slug="page-legacy", signal_type="page_watch",
        target="https://example.com/pricing", config={"fetch": "xcrawl", "label": "Pricing"},
        db_path=db,
    )
    item = next(i for i in ms.list_watchlist(db_path=db) if i.slug == "page-legacy")
    # Baseline as xcrawl would have stored it: markdown, a different hash.
    ms.upsert_page_watch_state("page-legacy", "old-markdown-hash", "# Pricing Pro plan is $30/mo", db_path=db)
    src = PageWatchSource()

    assert await src.poll(item, db_path=db) == []            # re-baseline, no signal
    state = ms.get_page_watch_state("page-legacy", db_path=db)
    assert state is not None and state["content_hash"] != "old-markdown-hash"
    refreshed = next(i for i in ms.list_watchlist(db_path=db) if i.slug == "page-legacy")
    assert "fetch" not in refreshed.config_json                # marker cleared
    assert refreshed.config_json.get("label") == "Pricing"    # other config kept

    assert await src.poll(refreshed, db_path=db) == []        # unchanged
    holder.body = b"<html><body>Pro plan is $40/mo</body></html>"
    signals = await src.poll(refreshed, db_path=db)           # real change
    assert len(signals) == 1
    assert "$40/mo" in signals[0].raw_payload["added_text"]
    assert holder.calls == 3


@pytest.mark.asyncio
async def test_legacy_marker_on_unsaved_item_does_not_rebaseline_forever(
    db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An item with no id cannot have its marker cleared, so it must be
    compared normally — otherwise it would re-baseline on every poll and
    never report a change."""
    holder = SimpleNamespace(body=b"<html><body>v1</body></html>", calls=0)
    _install_httpx(monkeypatch, holder)
    item = WatchlistItem(
        id=None, slug="page-noid", signal_type="page_watch",
        target="https://example.com/p", config_json={"fetch": "xcrawl"},
    )
    src = PageWatchSource()
    assert await src.poll(item, db_path=db) == []             # first baseline
    holder.body = b"<html><body>v2</body></html>"
    assert len(await src.poll(item, db_path=db)) == 1         # change reported


def test_remove_watchlist_config_key_keeps_other_keys(db: Path) -> None:
    ms.insert_watchlist_item(
        slug="page-k", signal_type="page_watch", target="https://example.com/k",
        config={"fetch": "xcrawl", "label": "K"}, db_path=db,
    )
    item = next(i for i in ms.list_watchlist(db_path=db) if i.slug == "page-k")
    assert item.id is not None
    assert ms.remove_watchlist_config_key(item.id, "fetch", db_path=db) == 1
    again = next(i for i in ms.list_watchlist(db_path=db) if i.slug == "page-k")
    assert again.config_json == {"label": "K"}
    assert ms.remove_watchlist_config_key(item.id + 1000, "fetch", db_path=db) == 0
    with pytest.raises(ValueError):
        ms.remove_watchlist_config_key(item.id, "$.evil", db_path=db)
