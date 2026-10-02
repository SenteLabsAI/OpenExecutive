"""HTML → plain text for Outlook message bodies.

Microsoft Graph returns ``body.content`` as HTML unless a ``Prefer`` header
selects text, and the MCP tool cannot set that header. A real parser (never a
regex): ``<script>``/``<style>``/``<head>`` content is dropped, block-level
tags and ``<br>`` become line breaks, entities are decoded, and runs of
whitespace collapse — the same stance as `monitoring.sources.vendor_status`'s
``_BodyReader``. Lenient by construction: a parse that gives up keeps whatever
text it already collected.
"""
from __future__ import annotations

import logging
import re
from html.parser import HTMLParser

logger = logging.getLogger(__name__)

_SKIP_TAGS = frozenset({"script", "style", "head", "title", "template"})
_BLOCK_TAGS = frozenset({
    "p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "pre", "table", "ul", "ol", "hr", "section", "article",
    "header", "footer", "address",
})
_MULTI_BLANK = re.compile(r"\n{3,}")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_startendtag(self, tag: str, attrs: object) -> None:
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._chunks.append(data)

    def text(self) -> str:
        raw = "".join(self._chunks)
        lines = [" ".join(line.split()) for line in raw.splitlines()]
        return _MULTI_BLANK.sub("\n\n", "\n".join(lines)).strip()


def html_to_text(html: str) -> str:
    """Plain text of an HTML fragment or document; ``""`` for empty input."""
    if not html:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # pragma: no cover - HTMLParser is lenient by design
        logger.debug("workspace: html body parse failed", exc_info=True)
    return parser.text()
