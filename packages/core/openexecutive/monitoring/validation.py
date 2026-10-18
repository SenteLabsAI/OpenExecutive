"""Shared validators for watchlist inputs.

Both the Anthropic chat tools in ``orchestrator/watchlist_tools.py`` and
the HTTP routes in ``api/routes/watchlist.py`` accept user-supplied
slugs and severity strings. Keeping the regex and severity set in one
place means the two surfaces can't drift — a slug the chat tool would
reject must be rejected by the HTTP route too.

``is_valid_mode`` / ``is_valid_cadence`` live in ``monitoring/models``
because they sit next to their string constants; import them from
there.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

from openexecutive.alerts.models import AlertSeverity

# Kebab-case ASCII slug. Rejects control characters, RTL overrides,
# whitespace, and anything that could embed Markdown / SQL fragments in
# audit text downstream. Uses ``\Z`` instead of ``$`` so a trailing
# newline doesn't sneak past the validator — ``$`` matches before a
# final \n in Python's default mode, ``\Z`` matches absolute end-of-string.
WATCHLIST_SLUG_RE = re.compile(r"\A[a-z0-9][a-z0-9\-]{0,60}\Z")

VALID_SEVERITY_VALUES: frozenset[str] = frozenset(s.value for s in AlertSeverity)


def is_valid_watchlist_slug(slug: str) -> bool:
    return bool(WATCHLIST_SLUG_RE.match(slug))


def is_valid_severity(value: str) -> bool:
    return value in VALID_SEVERITY_VALUES


def normalize_target(signal_type: str, target: str) -> str:
    """Canonical form of a watch target, so two spellings of one source
    compare equal (the declines memory and the duplicate-source guard key
    on this). Tickers / CIKs upper-case; URLs lower-case scheme + host and
    drop the fragment and a trailing slash. Never raises."""
    raw = (target or "").strip()
    if signal_type in ("stock", "edgar"):
        return raw.upper()
    if signal_type == "query" or "://" not in raw:
        # Free-text targets (standing queries): collapse case + whitespace.
        return " ".join(raw.lower().split())
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw.lower()
    # Scheme, www., a trailing host dot, query string, fragment, path case,
    # percent-encoding, dot segments and a trailing slash are all ways to
    # spell the same source; none of them may defeat a decline or the
    # duplicate-source guard.
    host = (parts.hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    path = _canonical_path(parts.path)
    return urlunsplit(("https", host, path, "", ""))


def _canonical_path(path: str) -> str:
    """Lower-cased, percent-decoded path with dot segments resolved and no
    trailing slash ("" for the root)."""
    segments: list[str] = []
    for seg in unquote(path).split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if segments:
                segments.pop()
            continue
        segments.append(seg.lower())
    return "/" + "/".join(segments) if segments else ""


def registrable_domain(url: str) -> str:
    """Host with a leading ``www.`` stripped (``""`` for non-URL targets).
    Good enough to say "same site" for the duplicate-source guard."""
    if "://" not in (url or ""):
        return ""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


# Slug prefix per signal type. Mirrors packages/ui/src/lib/watchSlug.ts so a
# watch named by the UI and one named by the API read the same.
_SLUG_PREFIX: dict[str, str] = {
    "stock": "stock",
    "rss": "rss",
    "vendor_status": "status",
    "edgar": "edgar",
    "page_watch": "page",
    "query": "search",
}
_SLUG_MAX_LEN = 61


def suggest_watch_slug(signal_type: str, target: str) -> str:
    """A slug from the signal type and target, kebab-cased to
    ``WATCHLIST_SLUG_RE`` (``""`` without a target). Same rule as the UI's
    ``suggestWatchSlug``."""
    t = (target or "").strip().lower()
    if not t:
        return ""
    t = re.sub(r"^[a-z][a-z0-9+.-]*://", "", t)
    t = re.sub(r"^www\.", "", t)
    prefix = _SLUG_PREFIX.get(signal_type) or re.sub(r"[^a-z0-9]+", "-", signal_type.lower())
    body = re.sub(r"[^a-z0-9]+", "-", t)
    slug = re.sub(r"-+", "-", f"{prefix}-{body}").lstrip("-")
    return slug[:_SLUG_MAX_LEN].rstrip("-")


def display_label_config(signal_type: str, display_label: Any) -> dict[str, Any]:
    """Map the user-facing label onto the adapter-specific config key."""
    config: dict[str, Any] = {}
    if display_label:
        if signal_type == "vendor_status":
            config["vendor_label"] = display_label
        elif signal_type == "rss":
            config["feed_label"] = display_label
        elif signal_type == "stock":
            config["display_name"] = display_label
        elif signal_type in ("query", "edgar", "page_watch"):
            config["label"] = display_label
    return config


__all__ = [
    "VALID_SEVERITY_VALUES",
    "WATCHLIST_SLUG_RE",
    "display_label_config",
    "is_valid_severity",
    "is_valid_watchlist_slug",
    "normalize_target",
    "registrable_domain",
    "suggest_watch_slug",
]
