"""Which page_watch targets research may not watch (they never change)."""
from __future__ import annotations

import pytest

from openexecutive.monitoring.target_validation import page_watch_target_problem


@pytest.mark.parametrize("url", [
    "https://www.linkedin.com/company/harbor-lane-coffee/",
    "https://x.com/harborlane",
    "https://cdn.example.com/reports/2Q26-market-report.pdf",
    "https://brokerco.example/economic-update-october-15-2026/",
    "https://news.example/2026/10/harbor-lane-opens-second-store",
    "https://blog.example/posts/2026-10-15-quarterly-results",
    "https://research.example/blog/the-great-cooling-how-coffee-prices-are-entering-a-new-era",
])
def test_pages_that_never_change_are_refused(url: str) -> None:
    assert page_watch_target_problem(url)


@pytest.mark.parametrize("url", [
    "https://news.example/",
    "https://brokerco.example/news",
    "https://www.jll.example/en-us/insights/market-perspectives/seniors-housing",
    "https://acme.example/pricing",
    "https://acme.example/press-releases/",
])
def test_index_pages_pass(url: str) -> None:
    assert page_watch_target_problem(url) == ""
