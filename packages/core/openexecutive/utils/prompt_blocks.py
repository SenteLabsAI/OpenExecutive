"""Helpers for text interpolated inside a tagged block of a model prompt."""
from __future__ import annotations

import re
import unicodedata

# Control and format characters: newlines are the caller's, and a format
# character (a zero-width space, a word joiner) could hide inside a tag.
_DROPPED = ("Cc", "Cf")


def plain(text: str) -> str:
    """Untrusted text as a model will read it, before anything is matched
    against it: compatibility forms folded (NFKC: a full-width ＜ is <, a
    ligature its letters) and control and format characters dropped, but
    line breaks and tabs kept."""
    folded = unicodedata.normalize("NFKC", text)
    return "".join(ch for ch in folded if ch in "\n\t" or unicodedata.category(ch) not in _DROPPED)


def defang_tag(text: str, tag: str) -> str:
    """``text`` with every opening or closing ``tag`` (any case, spaces
    inside it) made harmless: ``<`` becomes ``‹``. Match after ``plain``."""
    return re.sub(rf"<(\s*/?\s*{re.escape(tag)}\b)", r"‹\1", text, flags=re.IGNORECASE)


def scrub_block_line(line: str, close_tag: str) -> str:
    """One line of untrusted text as it may appear inside a ``<tag>`` block.

    It is made ``plain`` first (so nothing hidden or look-alike survives into
    the checks), control characters go (the caller handles newlines), and
    ``close_tag`` in any spelling (any case, spaces inside it) is defanged, so
    the text cannot end the block early."""
    cleaned = "".join(ch for ch in plain(line) if unicodedata.category(ch) not in _DROPPED)
    name = close_tag.strip().lstrip("<").lstrip("/").rstrip(">").strip()
    closing = re.compile(rf"<\s*/\s*{re.escape(name)}\s*>", re.IGNORECASE)
    return closing.sub(close_tag.replace("</", "<\\/", 1), cleaned).strip()
