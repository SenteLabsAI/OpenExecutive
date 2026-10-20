"""Helpers for text interpolated inside a tagged block of a model prompt."""
from __future__ import annotations

import re
import unicodedata

# Control and format characters: newlines are the caller's, and a format
# character (a zero-width space, a word joiner) could hide inside a tag.
_DROPPED = ("Cc", "Cf")
# Unicode's other default-ignorable code points, which render as nothing but
# are neither control nor format characters: the combining grapheme joiner,
# the Hangul fillers, the Khmer inherent vowels, the Mongolian and other
# variation selectors, and the unassigned ranges set aside for such marks.
_IGNORABLE = re.compile(
    "[͏ᅟᅠ឴឵᠋-᠏ㅤ︀-️ﾠ￰-￸"
    "\U0001bca0-\U0001bca3\U0001d173-\U0001d17a\U000e0000-\U000e0fff]"
)


def plain(text: str) -> str:
    """Untrusted text as a model will read it, before anything is matched
    against it: compatibility forms folded (NFKC: a full-width ＜ is <, a
    ligature its letters), and control, format and other invisible
    characters dropped, but line breaks and tabs kept. Letters from another
    script that only look like Latin ones stay (NFKC doesn't fold them):
    where that matters, ``no_tags`` makes a tag impossible whatever it
    spells."""
    folded = _IGNORABLE.sub("", unicodedata.normalize("NFKC", text))
    return "".join(ch for ch in folded if ch in "\n\t" or unicodedata.category(ch) not in _DROPPED)


def no_tags(text: str) -> str:
    """``text`` unable to open or close any tag at all: every ``<`` becomes
    ``‹``. For untrusted text a model reads as data, where a tag spelled
    with look-alike letters would otherwise pass for one of the prompt's."""
    return text.replace("<", "‹")


def scrub_block_line(line: str, close_tag: str) -> str:
    """One line of untrusted text as it may appear inside a ``<tag>`` block.

    It is made ``plain`` first (so no hidden character survives into the
    checks), control characters go (the caller handles newlines), and
    ``close_tag`` in any spelling (any case, spaces inside it) is defanged, so
    the text cannot end the block early."""
    cleaned = "".join(ch for ch in plain(line) if unicodedata.category(ch) not in _DROPPED)
    name = close_tag.strip().lstrip("<").lstrip("/").rstrip(">").strip()
    closing = re.compile(rf"<\s*/\s*{re.escape(name)}\s*>", re.IGNORECASE)
    return closing.sub(close_tag.replace("</", "<\\/", 1), cleaned).strip()
