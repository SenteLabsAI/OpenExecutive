"""A plain word search in SQLite, shared by the lists that offer one.

``words_clause`` matches rows where every word typed appears (any order;
any case for A-Z, SQLite's ``LIKE``; other letters as typed) in at least one
of the given columns. Words are ``LIKE``-escaped, so a
``%`` or ``_`` someone typed matches itself instead of anything.
"""
from __future__ import annotations

# Longer than anyone types into a search box; keeps the query cheap.
MAX_SEARCH_CHARS = 200
MAX_SEARCH_WORDS = 8


def clean_search(text: str | None) -> str:
    """What to search for: trimmed, inner whitespace folded, cut to
    ``MAX_SEARCH_CHARS``. Empty means no search."""
    if not text:
        return ""
    return " ".join(text.split())[:MAX_SEARCH_CHARS]


def _escape(word: str) -> str:
    return word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def words_clause(columns: tuple[str, ...], text: str) -> tuple[str, list[str]]:
    """SQL (``AND ...``, with a trailing space) and its parameters keeping
    only rows where each word of ``text`` is in one of ``columns``. Empty
    ``text`` is no clause. ``columns`` are SQL expressions from the caller's
    code, never from the request."""
    words = clean_search(text).split()[:MAX_SEARCH_WORDS]
    if not words:
        return "", []
    parts: list[str] = []
    params: list[str] = []
    for word in words:
        pattern = f"%{_escape(word)}%"
        parts.append(
            "(" + " OR ".join(f"{c} LIKE ? ESCAPE '\\'" for c in columns) + ")"
        )
        params.extend([pattern] * len(columns))
    return "AND " + " AND ".join(parts) + " ", params
