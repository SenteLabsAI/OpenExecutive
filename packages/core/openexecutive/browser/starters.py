"""The "Try one" pills on chat's card after someone connects their browser.

Once the Open Executive extension is connected, the next new chat shows a
card (``packages/ui/src/components/browser/BrowserWelcomeCard.tsx``) with up
to three things to ask. A tap puts the words in the box; nothing is sent
until they send it.

Each one opens a fresh page, so it works whatever tabs the person has open:
while they are in chat, the tab they are on is Open Executive itself, so an
ask about "the page I'm on" would find nothing to read. They come from the
company profile (its website, name and first competitor), and a pill whose
part of the profile is empty isn't offered; with nothing in the profile the
pills are general ones that need no company at all.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from urllib.parse import urlsplit

from openexecutive.memory.company_profile import CompanyProfile

logger = logging.getLogger(__name__)

MAX = 3
# A name longer than this reads badly on a pill: that pill is left out.
MAX_NAME = 40

GENERAL = (
    "Find today's top business news and sum it up",
    "Open Hacker News and sum up the top five stories",
    "Find three well-reviewed CRMs for a small team and compare their prices",
)

_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


def _host(website: str) -> str:
    """The site's address as people say it ("harborlane.com"), or ""."""
    raw = (website or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw if "://" in raw else f"https://{raw}")
        if parts.scheme not in ("http", "https"):
            return ""
        host = (parts.hostname or "").lower()
    except ValueError:
        return ""
    host = host.removeprefix("www.")
    return host if _HOST_RE.match(host) and len(host) <= 60 else ""


def _name(value: str) -> str:
    """A company name fit for a pill, or ""."""
    shown = "".join(ch for ch in (value or "") if unicodedata.category(ch) not in ("Cc", "Cf"))
    name = " ".join(shown.split())
    if not name or len(name) > MAX_NAME:
        return ""
    return name


def _possessive(name: str) -> str:
    return f"{name}'" if name.endswith("s") else f"{name}'s"


def starters(profile: CompanyProfile) -> list[str]:
    """Up to three asks that each open a page in their browser."""
    host = _host(profile.website)
    name = _name(profile.name)
    rival = next(
        (
            n
            for n in (_name(c) for c in profile.competitive_landscape.primary_competitors)
            if n and n.casefold() != name.casefold()
        ),
        "",
    )
    out: list[str] = []
    if host:
        out.append(f"Open {host} and tell me what you'd fix on the home page")
    if rival:
        out.append(
            f"Compare {_possessive(rival)} pricing page with ours"
            if host
            else f"Find {_possessive(rival)} pricing page and sum up their plans"
        )
    if name:
        out.append(f"Find this week's news about {name} and sum it up")
    for general in GENERAL:
        if len(out) >= MAX:
            break
        out.append(general)
    return out[:MAX]


def for_company() -> list[str]:
    """The pills for this company, from its saved profile."""
    try:
        from openexecutive.config import get_settings

        profile = CompanyProfile.load_from_yaml(get_settings().company_profile_path)
    except Exception:
        logger.info("browser_starters: no company profile to read")
        profile = CompanyProfile()
    return starters(profile)
