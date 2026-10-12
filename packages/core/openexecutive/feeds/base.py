"""Shared types for read-only operating-data feeds.

A *feed* pulls a small, human-readable snapshot from an external system
(billing export, Stripe, Linear) so a department check-in can reason from
actuals instead of memory. Feeds are strictly read-only, bounded in size, and
fail soft: a missing credential or an upstream error becomes a labelled line in
the snapshot — never an exception in the check-in, and never a made-up number.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import httpx

logger = logging.getLogger(__name__)

FeedStatus = Literal["ok", "not_configured", "error"]

# Hard cap on text a single feed may contribute to a prompt.
MAX_FEED_CHARS = 3000


@dataclass(frozen=True)
class FeedResult:
    """Outcome of one feed fetch."""

    name: str
    status: FeedStatus
    text: str
    fetched_at: str

    def render(self) -> str:
        header = f"## {self.name} — {self.status} (fetched {self.fetched_at} UTC)"
        return f"{header}\n{self.text}".rstrip()


def _now_label() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M")


class Feed(ABC):
    """A read-only data source."""

    #: Display name used in the snapshot header and in audit lines.
    name: str
    #: Short label of what the seat must do when the feed is unavailable.
    gap_hint: str = "Do not estimate these figures; flag the gap and ask for the number."

    @abstractmethod
    def configured(self) -> bool:
        """True when every credential/setting this feed needs is present."""

    @abstractmethod
    def required_config(self) -> str:
        """Human-readable name of the setting(s) that enable this feed."""

    @abstractmethod
    async def _fetch(self, client: httpx.AsyncClient) -> str:
        """Return the snapshot text. May raise; ``fetch`` converts to a result."""

    async def fetch(self, client: httpx.AsyncClient | None = None) -> FeedResult:
        """Fetch a snapshot, never raising."""
        stamp = _now_label()
        if not self.configured():
            return FeedResult(
                name=self.name,
                status="not_configured",
                text=f"Not connected ({self.required_config()} is not set). {self.gap_hint}",
                fetched_at=stamp,
            )
        owns_client = client is None
        http = client or httpx.AsyncClient(timeout=_timeout())
        try:
            text = await self._fetch(http)
        except httpx.HTTPStatusError as exc:
            # Status code only: response bodies and URLs can echo credentials.
            logger.warning("feed %s: upstream HTTP %s", self.name, exc.response.status_code)
            return FeedResult(
                name=self.name,
                status="error",
                text=f"Upstream returned HTTP {exc.response.status_code}. {self.gap_hint}",
                fetched_at=stamp,
            )
        except Exception as exc:  # noqa: BLE001 - feeds must fail soft.
            logger.warning("feed %s: %s", self.name, type(exc).__name__)
            return FeedResult(
                name=self.name,
                status="error",
                text=f"Fetch failed ({type(exc).__name__}). {self.gap_hint}",
                fetched_at=stamp,
            )
        finally:
            if owns_client:
                await http.aclose()
        return FeedResult(
            name=self.name,
            status="ok",
            text=text[:MAX_FEED_CHARS],
            fetched_at=stamp,
        )


def _timeout() -> float:
    from openexecutive.config import get_settings

    return float(get_settings().feed_timeout_seconds)
