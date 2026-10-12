"""Read-only operating-data feeds and the check-in context builder.

``build_operating_context`` is the single entry point used by the department
check-in workflow: it fetches the feeds mapped to a department (in parallel,
each bounded by a timeout) and renders an ``<operating_data>`` block. A seat
that sees "not connected" in that block is instructed (see
``prompts/partner_prompts.py``) to flag the gap rather than estimate.
"""
from __future__ import annotations

import asyncio
import logging

from openexecutive.feeds.base import Feed, FeedResult
from openexecutive.feeds.billing_feed import BillingFeed
from openexecutive.feeds.linear_feed import LinearFeed
from openexecutive.feeds.stripe_feed import StripeFeed

logger = logging.getLogger(__name__)

__all__ = [
    "FEEDS_BY_DEPARTMENT",
    "BillingFeed",
    "Feed",
    "FeedResult",
    "LinearFeed",
    "StripeFeed",
    "build_operating_context",
]

# Which feeds each department's check-in sees. Strategy sees all of them: the
# CEO review needs the whole picture.
FEEDS_BY_DEPARTMENT: dict[str, tuple[type[Feed], ...]] = {
    "finance": (BillingFeed, StripeFeed),
    "operations": (LinearFeed, BillingFeed),
    "product": (LinearFeed,),
    "strategy": (BillingFeed, StripeFeed, LinearFeed),
}


async def _fetch_one(feed: Feed, timeout: float) -> FeedResult:
    try:
        return await asyncio.wait_for(feed.fetch(), timeout=timeout)
    except TimeoutError:
        return FeedResult(
            name=feed.name,
            status="error",
            text=f"Timed out after {timeout:.0f}s. {feed.gap_hint}",
            fetched_at="",
        )


async def build_operating_context(department_slug: str) -> str:
    """Render the ``<operating_data>`` block for a department ('' if none mapped)."""
    feed_types = FEEDS_BY_DEPARTMENT.get(department_slug)
    if not feed_types:
        return ""

    from openexecutive.config import get_settings

    timeout = float(get_settings().feed_timeout_seconds) + 5.0
    results = await asyncio.gather(*(_fetch_one(ft(), timeout) for ft in feed_types))
    body = "\n\n".join(r.render() for r in results)
    return (
        "<operating_data>\n"
        "Live, read-only figures. Treat as the only source of numbers; a feed that is "
        "not connected or errored is a data gap to flag, not something to estimate.\n\n"
        f"{body}\n"
        "</operating_data>"
    )
