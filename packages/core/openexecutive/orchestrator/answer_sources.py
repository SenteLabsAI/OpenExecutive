"""What an answer looked at, and which part of it had to be left out.

Collected once per Executive turn and sent after the reply as a ``sources``
stream event. The web chat shows it under the answer and saves it with the
message (``chat_messages.sources``); every other channel ignores the event.

- **Sources** are the documents and web pages this turn's knowledge searches
  and web searches returned: what the answer *looked at*, not a claim about
  which sentence came from where. Knowledge retrieval records them through
  ``retrieve(record_source=...)``; the Executive records the web pages its
  searches returned and its reply cites.
- **Unavailable areas** are parts of the analysis (finance, legal, ...) whose
  specialist failed or said nothing, so the reply went ahead without them.
  They are named by area, never by specialist: the user only ever meets one
  Executive (CLAUDE.md — the agent architecture is never exposed).
"""
from __future__ import annotations

import logging
import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

SourceKind = Literal["company", "knowledge", "notion", "research", "document", "web"]

# Enough to show what an answer drew on without burying it. Web results get
# their own ceiling so a few searches can't crowd out the company's documents.
MAX_SOURCES = 12
_MAX_WEB_SOURCES = 6
# Of one web search's results, how many count as looked at besides the pages
# the reply actually cites (a search returns around ten).
_RESULTS_PER_SEARCH = 3
_MAX_TITLE_CHARS = 120
_MAX_URL_CHARS = 2048
# An in-app page, e.g. /artifacts/alert%3A12 — no scheme, no host, nothing a
# browser could read as another site ("//host", "/\\host").
_IN_APP_PATH_RE = re.compile(r"/[A-Za-z0-9_%~/-]*")

# The part of the analysis each specialist covers, in the user's words. The
# web chat lists them as "Some of the analysis is missing (finance, legal)".
_AREAS: dict[str, str] = {
    "cso": "strategy",
    "cfo": "finance",
    "chro": "people and hiring",
    "gc": "legal",
    "coo": "operations",
    "cmo": "marketing",
    "cpo": "product",
    "board_comms": "board and investors",
    "triage": "priorities",
}
_UNKNOWN_AREA = "one area"


def area_for(specialist: str) -> str:
    return _AREAS.get(specialist, _UNKNOWN_AREA)


def title_from_filename(filename: str) -> str:
    """``board_composition_and_governance.md`` → ``Board composition and governance``."""
    stem = filename.rsplit("/", 1)[-1]
    if stem.lower().endswith(".md"):
        stem = stem[:-3]
    words = " ".join(stem.replace("_", " ").replace("-", " ").split())
    return words[:1].upper() + words[1:] if words else filename


def safe_link(url: str | None) -> str | None:
    """An http(s) URL or an in-app path, else ``None`` — nothing else is ever
    rendered as a link."""
    if not url:
        return None
    url = url.strip()
    if not url or len(url) > _MAX_URL_CHARS:
        return None
    if url.startswith("/"):
        return url if _IN_APP_PATH_RE.fullmatch(url) and not url.startswith("//") else None
    parts = urlsplit(url)
    if parts.scheme in ("http", "https") and parts.netloc:
        return url
    return None


def _clean_title(title: object) -> str:
    text = " ".join(str(title or "").split())
    return text if len(text) <= _MAX_TITLE_CHARS else text[: _MAX_TITLE_CHARS - 1] + "…"


@dataclass(frozen=True)
class AnswerSource:
    kind: SourceKind
    title: str
    url: str | None = None


class TurnSources:
    """One turn's sources and missing areas.

    Thread-safe: knowledge retrieval runs in worker threads
    (``asyncio.to_thread``) and records from there.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sources: dict[tuple[str, str], AnswerSource] = {}
        self._unavailable: list[str] = []

    def add(self, kind: SourceKind, title: object, url: str | None = None) -> None:
        """Record one source. Duplicates, and anything past the caps, are
        dropped quietly; a title-less source is ignored."""
        clean = _clean_title(title)
        if not clean:
            return
        link = safe_link(url)
        key = (kind, (link or clean).lower())
        with self._lock:
            if key in self._sources or len(self._sources) >= MAX_SOURCES:
                return
            if kind == "web" and sum(s.kind == "web" for s in self._sources.values()) >= _MAX_WEB_SOURCES:
                return
            self._sources[key] = AnswerSource(kind, clean, link)

    def mark_unavailable(self, specialist: str) -> None:
        area = area_for(specialist)
        with self._lock:
            if area not in self._unavailable:
                self._unavailable.append(area)

    def is_empty(self) -> bool:
        with self._lock:
            return not self._sources and not self._unavailable

    def payload(self) -> dict[str, Any]:
        """``{"sources": [{kind, title, url}], "unavailable": [area]}`` — the
        shape stored in ``chat_messages.sources``."""
        with self._lock:
            return {
                "sources": [
                    {"kind": s.kind, "title": s.title, "url": s.url}
                    for s in self._sources.values()
                ],
                "unavailable": list(self._unavailable),
            }

    def event(self, session_id: str) -> dict[str, Any]:
        return {"type": "sources", "session_id": session_id, **self.payload()}


def _web_title(title: object, url: str) -> str:
    return str(title or "") or urlsplit(url).hostname or url


def record_web_sources(sources: TurnSources, content: Iterable[Any]) -> None:
    """Record the web pages in one model response: the pages its text cites
    first, then the first few results of each web search it ran. Never
    raises — a label under the answer must not cost the answer."""
    try:
        blocks = list(content)
        for block in blocks:
            if getattr(block, "type", None) != "text":
                continue
            for citation in getattr(block, "citations", None) or []:
                url = getattr(citation, "url", None)
                if isinstance(url, str) and url:
                    sources.add("web", _web_title(getattr(citation, "title", None), url), url)
        for block in blocks:
            if getattr(block, "type", None) != "web_search_tool_result":
                continue
            results = getattr(block, "content", None)
            if not isinstance(results, list):  # a WebSearchToolResultError
                continue
            for result in results[:_RESULTS_PER_SEARCH]:
                url = getattr(result, "url", None)
                if isinstance(url, str) and url:
                    sources.add("web", _web_title(getattr(result, "title", None), url), url)
    except Exception:
        logger.warning("recording web sources failed", exc_info=True)
