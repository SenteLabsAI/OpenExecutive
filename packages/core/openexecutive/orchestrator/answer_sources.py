"""What an answer looked at, and which part of it had to be left out.

The web chat route (``api/routes/chat.py``) owns one ``TurnSources`` per turn.
It sends it after the reply as a ``sources`` stream event, stopped and
timed-out replies included, and saves it with the message
(``chat_messages.sources``). No other channel collects it.

- **Sources** are what the answer *looked at*: the documents its knowledge
  searches returned and the web pages its searches returned. They are not a
  claim about which sentence came from where. Knowledge retrieval records
  them through ``retrieve(record_source=...)``. That covers the Executive's
  own search and each specialist that answered; a specialist that failed
  never passed its documents on, so they are left out. The Executive records
  the pages its reply cites and a few results of each web search. Cited
  pages are always kept ahead of the rest.
- **Unavailable areas** are parts of the analysis (finance, legal, ...) whose
  specialist failed or said nothing every time it was asked this turn, so the
  reply went ahead without them. They are named by area, never by
  specialist: the user only ever meets one Executive (CLAUDE.md — the agent
  architecture is never exposed).
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

# Enough to show what an answer drew on without burying it. Web pages get
# their own ceiling so a few searches can't crowd out the company's documents.
MAX_SOURCES = 12
_MAX_WEB_SOURCES = 6
# Of one web search's results, how many count as looked at besides the pages
# the reply actually cites (a search returns around ten).
_RESULTS_PER_SEARCH = 3
_MAX_TITLE_CHARS = 120
_MAX_URL_CHARS = 2048
# The one in-app page a source links to: an earlier document, e.g.
# /artifacts/alert%3A12. One segment of plain characters, so nothing a browser
# could read as another site ("//host", "/\\host") or another page ("/a/../b").
# packages/ui/src/lib/answerSources.ts holds the same pattern (a test checks).
_IN_APP_PATH_RE = re.compile(r"/artifacts/[A-Za-z0-9_%~-]+")

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


def safe_link(url: str | None, *, in_app: bool = True) -> str | None:
    """An http(s) URL or, unless ``in_app`` is false, an in-app document
    path; else ``None``. Nothing else is ever rendered as a link."""
    if not url:
        return None
    url = url.strip()
    if not url or len(url) > _MAX_URL_CHARS:
        return None
    if url.startswith("/"):
        return url if in_app and _IN_APP_PATH_RE.fullmatch(url) else None
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
        # Kept apart so the caps in `payload` can favour the pages the reply
        # cites, whenever in the turn they are recorded. Each list is capped
        # at what `payload` could ever show from it.
        self._cited: list[AnswerSource] = []
        self._documents: list[AnswerSource] = []
        self._results: list[AnswerSource] = []
        self._failed: list[str] = []
        self._answered: set[str] = set()

    def add(
        self, kind: SourceKind, title: object, url: str | None = None, *, cited: bool = False
    ) -> None:
        """Record one source. Duplicates, and anything past the caps, are
        dropped quietly; a title-less source is ignored. ``cited`` marks a web
        page the reply cites."""
        clean = _clean_title(title)
        if not clean:
            return
        is_web = kind == "web"
        source = AnswerSource(kind, clean, safe_link(url, in_app=not is_web))
        if not is_web:
            bucket, limit = self._documents, MAX_SOURCES
        elif cited:
            bucket, limit = self._cited, _MAX_WEB_SOURCES
        else:
            bucket, limit = self._results, _MAX_WEB_SOURCES
        with self._lock:
            if len(bucket) < limit and all(_key(s) != _key(source) for s in bucket):
                bucket.append(source)

    def mark_unavailable(self, specialist: str) -> None:
        """A call to ``specialist`` failed or came back empty."""
        with self._lock:
            if specialist not in self._failed:
                self._failed.append(specialist)

    def mark_answered(self, specialist: str) -> None:
        """A call to ``specialist`` answered, so its area is in the reply even
        if another call to it failed."""
        with self._lock:
            self._answered.add(specialist)

    def is_empty(self) -> bool:
        payload = self.payload()
        return not payload["sources"] and not payload["unavailable"]

    def payload(self) -> dict[str, Any]:
        """``{"sources": [{kind, title, url}], "unavailable": [area]}`` — the
        shape stored in ``chat_messages.sources``.

        Cited pages come first, then documents, then other search results,
        until ``MAX_SOURCES`` (web pages: ``_MAX_WEB_SOURCES``)."""
        with self._lock:
            chosen: list[AnswerSource] = []
            seen: set[tuple[str, str]] = set()
            web = 0
            for source in (*self._cited, *self._documents, *self._results):
                if len(chosen) == MAX_SOURCES:
                    break
                key = _key(source)
                is_web = source.kind == "web"
                if key in seen or (is_web and web == _MAX_WEB_SOURCES):
                    continue
                seen.add(key)
                if is_web:
                    web += 1
                chosen.append(source)
            unavailable: list[str] = []
            for specialist in self._failed:
                area = area_for(specialist)
                if specialist not in self._answered and area not in unavailable:
                    unavailable.append(area)
            return {
                "sources": [{"kind": s.kind, "title": s.title, "url": s.url} for s in chosen],
                "unavailable": unavailable,
            }

    def event(self, session_id: str) -> dict[str, Any]:
        return {"type": "sources", "session_id": session_id, **self.payload()}


def _key(source: AnswerSource) -> tuple[str, str]:
    return (source.kind, (source.url or source.title).lower())


def _web_title(title: object, url: str) -> str:
    return str(title or "") or urlsplit(url).hostname or url


def record_web_sources(sources: TurnSources, content: Iterable[Any]) -> None:
    """Record the web pages in one model response: the pages its text cites,
    and the first few results of each web search it ran. Never raises — a
    label under the answer must not cost the answer."""
    try:
        blocks = list(content)
        for block in blocks:
            if getattr(block, "type", None) != "text":
                continue
            for citation in getattr(block, "citations", None) or []:
                url = getattr(citation, "url", None)
                if isinstance(url, str) and url:
                    sources.add(
                        "web", _web_title(getattr(citation, "title", None), url), url, cited=True
                    )
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
