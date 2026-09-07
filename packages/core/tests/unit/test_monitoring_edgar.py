"""Unit tests for the SEC EDGAR source adapter (P1)."""
from __future__ import annotations

from typing import Any

import pytest

from openexecutive.alerts.models import AlertSeverity
from openexecutive.monitoring.models import WatchlistItem
from openexecutive.monitoring.sources import list_registered_kinds
from openexecutive.monitoring.sources.edgar import (
    EdgarSource,
    _allowed_forms,
    _form_matches,
    _severity_for_form,
)

# --------------------------------------------------------------------- #
# Sample browse-edgar Atom feed (8-K, Form 4, 10-K)
# --------------------------------------------------------------------- #

_SAMPLE_ATOM = b"""<?xml version="1.0" encoding="ISO-8859-1"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Latest Filings - Apple Inc.</title>
  <entry>
    <category scheme="https://www.sec.gov/" label="form type" term="8-K"/>
    <id>urn:tag:sec.gov,2008:accession-number=0000320193-24-000123</id>
    <link rel="alternate" type="text/html"
      href="https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/0000320193-24-000123-index.htm"/>
    <title>8-K - Apple Inc. (Filer)</title>
    <updated>2026-05-28T16:30:00-04:00</updated>
  </entry>
  <entry>
    <category scheme="https://www.sec.gov/" label="form type" term="4"/>
    <id>urn:tag:sec.gov,2008:accession-number=0000320193-24-000124</id>
    <link rel="alternate" type="text/html"
      href="https://www.sec.gov/Archives/edgar/data/320193/000032019324000124/index.htm"/>
    <title>4 - COOK TIMOTHY D (Reporting)</title>
    <updated>2026-05-27T09:00:00-04:00</updated>
  </entry>
  <entry>
    <category scheme="https://www.sec.gov/" label="form type" term="10-K"/>
    <id>urn:tag:sec.gov,2008:accession-number=0000320193-24-000125</id>
    <link rel="alternate" type="text/html"
      href="https://www.sec.gov/Archives/edgar/data/320193/000032019324000125/index.htm"/>
    <title>10-K - Apple Inc.</title>
    <updated>2026-05-26T08:00:00-04:00</updated>
  </entry>
</feed>
"""


def _make_item(
    *,
    slug: str = "edgar-aapl",
    target: str = "AAPL",
    config: dict | None = None,
    trigger: dict | None = None,
) -> WatchlistItem:
    return WatchlistItem(
        id=1,
        slug=slug,
        signal_type="edgar",
        target=target,
        config_json=config or {},
        trigger_json=trigger or {},
    )


# --------------------------------------------------------------------- #
# poll()
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_edgar_emits_signal_per_matching_filing(install_source_feed) -> None:
    captured = install_source_feed("edgar", _SAMPLE_ATOM)
    src = EdgarSource()
    signals = await src.poll(_make_item(config={"label": "Apple"}))

    # Default forms {8-K, 10-K, 10-Q} → 8-K and 10-K match; Form 4 excluded.
    assert len(signals) == 2
    forms = {s.raw_payload["form"] for s in signals}
    assert forms == {"8-K", "10-K"}

    by_form = {s.raw_payload["form"]: s for s in signals}
    assert by_form["8-K"].severity_hint == AlertSeverity.HIGH
    assert by_form["10-K"].severity_hint == AlertSeverity.MEDIUM
    assert by_form["8-K"].raw_payload["accession"] == "0000320193-24-000123"
    assert by_form["8-K"].provenance_url.endswith("-index.htm")
    assert by_form["8-K"].normalized_summary.startswith("[Apple] 8-K filed 2026-05-28")
    assert all(s.dedup_key.startswith("edgar:") for s in signals)
    # <updated>2026-05-28T16:30:00-04:00</updated> → published_at in UTC, so
    # the pipeline's age gate judges the filing date, not the poll time.
    assert by_form["8-K"].published_at == "2026-05-28T20:30:00+00:00"
    assert by_form["8-K"].captured_at != by_form["8-K"].published_at

    # The ticker target reaches the EDGAR URL; the configured UA is sent.
    from openexecutive.config import get_settings

    assert "CIK=AAPL" in captured["url"]
    assert captured["user_agent"] == get_settings().edgar_user_agent


@pytest.mark.asyncio
async def test_edgar_dedup_stable_across_polls(install_source_feed) -> None:
    install_source_feed("edgar", _SAMPLE_ATOM)
    src = EdgarSource()
    item = _make_item()
    first = await src.poll(item)
    second = await src.poll(item)
    assert [s.dedup_key for s in first] == [s.dedup_key for s in second]


@pytest.mark.asyncio
async def test_edgar_forms_filter_override(install_source_feed) -> None:
    install_source_feed("edgar", _SAMPLE_ATOM)
    src = EdgarSource()
    # Opt into insider filings only.
    signals = await src.poll(_make_item(trigger={"forms": ["4"]}))
    assert len(signals) == 1
    assert signals[0].raw_payload["form"] == "4"
    assert signals[0].severity_hint == AlertSeverity.LOW


@pytest.mark.asyncio
async def test_edgar_amendment_matches_base_form(install_source_feed) -> None:
    amended = _SAMPLE_ATOM.replace(b'term="8-K"', b'term="8-K/A"').replace(
        b"<title>8-K -", b"<title>8-K/A -"
    )
    install_source_feed("edgar", amended)
    src = EdgarSource()
    # Default filter contains "8-K"; the "8-K/A" amendment should match it.
    signals = await src.poll(_make_item())
    forms = {s.raw_payload["form"] for s in signals}
    assert "8-K/A" in forms


@pytest.mark.asyncio
async def test_edgar_filter_then_cap_does_not_starve(install_source_feed) -> None:
    """Excluded forms at the front of the feed must not consume the output cap."""
    # 5 Form-4 entries (excluded by default) followed by one 8-K.
    entries = "".join(
        f"""
  <entry>
    <category label="form type" term="4"/>
    <id>urn:tag:sec.gov,2008:accession-number=000032019{i}-24-00099{i}</id>
    <link href="https://www.sec.gov/Archives/edgar/data/320193/x{i}/index.htm"/>
    <title>4 - INSIDER {i}</title>
    <updated>2026-05-2{i}T09:00:00-04:00</updated>
  </entry>"""
        for i in range(5)
    )
    feed = (
        b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
        + entries.encode()
        + b"""
  <entry>
    <category label="form type" term="8-K"/>
    <id>urn:tag:sec.gov,2008:accession-number=0000320193-24-000200</id>
    <link href="https://www.sec.gov/Archives/edgar/data/320193/y/index.htm"/>
    <title>8-K - Apple Inc.</title>
    <updated>2026-05-29T09:00:00-04:00</updated>
  </entry></feed>"""
    )
    install_source_feed("edgar", feed)
    src = EdgarSource()
    signals = await src.poll(_make_item())  # default forms exclude Form 4
    # The 8-K behind the Form-4 run still surfaces.
    assert len(signals) == 1
    assert signals[0].raw_payload["form"] == "8-K"
    assert signals[0].raw_payload["accession"] == "0000320193-24-000200"


@pytest.mark.asyncio
async def test_edgar_bad_target_skipped(install_source_feed) -> None:
    install_source_feed("edgar", _SAMPLE_ATOM)
    src = EdgarSource()
    assert await src.poll(_make_item(target="")) == []
    assert await src.poll(_make_item(target="../etc/passwd")) == []
    assert await src.poll(_make_item(target="a b c")) == []


@pytest.mark.asyncio
async def test_edgar_fetch_failure_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    async def boom(url: str, max_bytes: int, *, user_agent: str | None = None) -> bytes:
        raise httpx.ConnectError("dns")

    monkeypatch.setattr("openexecutive.monitoring.sources.edgar.fetch_bounded", boom)
    monkeypatch.setattr(
        "openexecutive.monitoring.sources.edgar.validate_target_url",
        lambda u: (True, ""),
    )
    src = EdgarSource()
    assert await src.poll(_make_item()) == []


@pytest.mark.asyncio
async def test_edgar_drops_bad_provenance_link(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_fetch(url: str, max_bytes: int, *, user_agent: str | None = None) -> bytes:
        captured["url"] = url
        return _SAMPLE_ATOM

    monkeypatch.setattr("openexecutive.monitoring.sources.edgar.fetch_bounded", fake_fetch)
    # Guard rejects the 10-K's link only; the feed URL + others pass.
    monkeypatch.setattr(
        "openexecutive.monitoring.sources.edgar.validate_target_url",
        lambda u: (False, "blocked") if "000125" in u else (True, ""),
    )
    src = EdgarSource()
    signals = await src.poll(_make_item())
    # 8-K survives, 10-K dropped (bad link), Form 4 filtered by default forms.
    assert {s.raw_payload["form"] for s in signals} == {"8-K"}


# --------------------------------------------------------------------- #
# Helpers + registration
# --------------------------------------------------------------------- #


def test_allowed_forms_default_and_override() -> None:
    assert _allowed_forms({}) == frozenset({"8-K", "10-K", "10-Q"})
    assert _allowed_forms({"forms": ["8-k", " 10-q "]}) == frozenset({"8-K", "10-Q"})
    # Empty/garbage falls back to the default set.
    assert _allowed_forms({"forms": []}) == frozenset({"8-K", "10-K", "10-Q"})


def test_form_matches_handles_amendments() -> None:
    allowed = frozenset({"8-K", "10-K"})
    assert _form_matches("8-K", allowed)
    assert _form_matches("8-K/A", allowed)  # amendment → base form
    assert not _form_matches("4", allowed)
    assert not _form_matches("", allowed)


def test_severity_for_form() -> None:
    assert _severity_for_form("8-K") == AlertSeverity.HIGH
    assert _severity_for_form("8-K/A") == AlertSeverity.HIGH
    assert _severity_for_form("10-K") == AlertSeverity.MEDIUM
    assert _severity_for_form("10-Q") == AlertSeverity.MEDIUM
    assert _severity_for_form("4") == AlertSeverity.LOW


def test_edgar_is_a_seeding_source() -> None:
    """The Atom feed lists past filings on every poll — first poll must baseline."""
    assert EdgarSource.seed_on_first_poll is True


def test_edgar_registered() -> None:
    assert "edgar" in list_registered_kinds()
