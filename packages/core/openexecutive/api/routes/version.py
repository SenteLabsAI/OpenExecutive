"""GET /version: the running version, and whether a newer release is out.

The latest release comes from GitHub's public releases API for this repo,
cached in-process so a busy Settings page costs one call every few hours.
Only the tag name is read from GitHub's answer; the release link is built
here from that tag, so nothing GitHub returns reaches the UI as a URL.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

import httpx
from fastapi import APIRouter, Request

from openexecutive.api.models import VersionResponse

logger = logging.getLogger(__name__)

router = APIRouter()

RELEASES_REPO = "SenteLabsAI/OpenExecutive"
_LATEST_RELEASE_URL = f"https://api.github.com/repos/{RELEASES_REPO}/releases/latest"
_RELEASE_PAGE_URL = f"https://github.com/{RELEASES_REPO}/releases/tag/v{{version}}"
_TIMEOUT_S = 5.0
# A good answer is kept for hours; a failed one is retried sooner, but not on
# every page load, so an install without outbound access stays quiet.
_SUCCESS_TTL_S = 6 * 60 * 60
_FAILURE_TTL_S = 60 * 60

_SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


@dataclass
class _Cached:
    latest: str | None
    fetched_at: float
    ok: bool


# Two requests landing together on a stale cache may both ask GitHub; that is
# harmless, and cheaper to live with than a lock tied to one event loop.
_cache: _Cached | None = None


def parse_version(value: str) -> tuple[int, int, int] | None:
    """`X.Y.Z` or `vX.Y.Z` as a comparable tuple; None for anything else."""
    m = _SEMVER_RE.match(value.strip())
    if m is None:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


async def _fetch_latest() -> str | None:
    """The latest release's version (no leading v), or None if unknown."""
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        resp = await client.get(
            _LATEST_RELEASE_URL,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "OpenExecutive-update-check",
            },
        )
    resp.raise_for_status()
    tag = resp.json().get("tag_name")
    if not isinstance(tag, str):
        return None
    parsed = parse_version(tag)
    if parsed is None:
        return None
    return "{}.{}.{}".format(*parsed)


async def latest_release() -> str | None:
    """The cached latest release version, refreshed when its TTL runs out."""
    global _cache
    now = time.monotonic()
    if _cache is not None:
        ttl = _SUCCESS_TTL_S if _cache.ok else _FAILURE_TTL_S
        if now - _cache.fetched_at < ttl:
            return _cache.latest
    try:
        latest = await _fetch_latest()
        _cache = _Cached(latest=latest, fetched_at=now, ok=latest is not None)
    except (httpx.HTTPError, ValueError) as exc:
        logger.info("update check failed: %s", type(exc).__name__)
        # Keep the last good answer through an outage rather than dropping
        # the notice.
        latest = _cache.latest if _cache is not None else None
        _cache = _Cached(latest=latest, fetched_at=now, ok=False)
    return _cache.latest


def _reset_cache() -> None:
    """Forget the cached release (tests)."""
    global _cache
    _cache = None


@router.get("/version", response_model=VersionResponse)
async def get_version(request: Request) -> VersionResponse:
    from openexecutive.config import get_settings

    current = request.app.version
    if not get_settings().update_check_enabled:
        return VersionResponse(current=current, check_enabled=False)

    latest = await latest_release()
    current_parsed = parse_version(current)
    latest_parsed = parse_version(latest) if latest else None
    update_available = (
        current_parsed is not None and latest_parsed is not None and latest_parsed > current_parsed
    )
    return VersionResponse(
        current=current,
        latest=latest,
        update_available=update_available,
        release_url=_RELEASE_PAGE_URL.format(version=latest) if latest else None,
        check_enabled=True,
    )
