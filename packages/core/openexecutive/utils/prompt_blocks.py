"""Helpers for text interpolated inside a tagged block of a model prompt."""
from __future__ import annotations

import unicodedata


def scrub_block_line(line: str, close_tag: str) -> str:
    """One line of untrusted text as it may appear inside a ``<tag>`` block.

    Control and format characters go (the caller handles newlines), and a
    literal ``close_tag`` is defanged so the text cannot end the block early."""
    cleaned = "".join(ch for ch in line if unicodedata.category(ch) not in ("Cc", "Cf"))
    return cleaned.replace(close_tag, close_tag.replace("</", "<\\/", 1)).strip()
