"""The HTML part of an email written as someone, for the tables in it.

The ghostwriter writes plain text, and may lay figures out as a Markdown pipe
table (``| Property | Amount |`` then ``|---|---|``). Mail clients show that as
text, so a draft with a table gets an HTML alternative next to its plain text
(``multipart/alternative``): the same words, with the table as a real table.
A draft with no table stays plain text, exactly as before.

Only the tables are Markdown. Every other line goes into the HTML exactly as
written (escaped, line breaks kept), so the HTML says what the plain text the
person approved says. The table renderer is markdown-it's "zero" preset with
just the table rule: no raw HTML, emphasis, links, entities or escapes, so a
cell reads as written too. The words come partly from other people's email,
and none of them can add markup. Styles are inline, because most mail
clients drop ``<style>`` blocks.
"""
from __future__ import annotations

import html
import re

from markdown_it import MarkdownIt
from markdown_it.token import Token

# Only the table rule: the "zero" preset leaves inline text alone (no
# emphasis, links, entities or escapes), so a cell reads as written.
_TABLE = MarkdownIt("zero").enable("table")

# A table's separator row: pipes and dashes, optional colons for alignment,
# one or more dashes a cell.
_SEPARATOR = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")

_STYLES = {
    "table": "border-collapse:collapse;margin:8px 0",
    "th": "border:1px solid #d0d7de;padding:6px 10px;background:#f6f8fa;text-align:left",
    "td": "border:1px solid #d0d7de;padding:6px 10px",
}

_STYLE = "font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.45"
_WRAPPER = '<div style="' + _STYLE + '">{}</div>'
# What marks the HTML as ours once a mail service has stored it: services
# reformat markup (Graph adds a document around it), so the test is the
# wrapper's style with spacing and case ignored.
_MARK = "line-height:1.45"
_WHITESPACE = re.compile(r"\s+")

# The line a forwarded message starts under (``delegation_tools._forward_header``).
_FORWARDED = re.compile(r"^-{2,}\s*Forwarded message\s*-{2,}\s*$", re.IGNORECASE | re.MULTILINE)


def _cells(row: str) -> int:
    """How many cells a table row has (as markdown-it counts them: the
    outer pipes are optional)."""
    inner = row.strip()
    inner = inner[1:] if inner.startswith("|") else inner
    inner = inner[:-1] if inner.endswith("|") else inner
    return inner.count("|") + 1


def _blocks(text: str) -> list[tuple[bool, list[str]]]:
    """``text``'s lines in runs, each marked whether it is a table (a
    header row, its separator row, then the rows with a pipe under them)."""
    lines = text.split("\n")
    blocks: list[tuple[bool, list[str]]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if "|" in line and "|" in nxt and _SEPARATOR.match(nxt) and _cells(line) == _cells(nxt):
            end = i + 2
            while end < len(lines) and "|" in lines[end] and lines[end].strip():
                end += 1
            blocks.append((True, lines[i:end]))
            i = end
            continue
        if blocks and not blocks[-1][0]:
            blocks[-1][1].append(line)
        else:
            blocks.append((False, [line]))
        i += 1
    return blocks


def _text_html(lines: list[str]) -> str:
    """Lines as they were written: escaped, line breaks kept."""
    return "<br>\n".join(html.escape(line) for line in lines)


def has_table(text: str) -> bool:
    """Whether ``text`` holds a Markdown pipe table."""
    return any(table for table, _ in _blocks(text or ""))


def _style(tokens: list[Token]) -> None:
    for token in tokens:
        tag = token.tag if token.nesting == 1 else ""
        if tag in _STYLES:
            # Keep the column's alignment, which markdown-it sets as a style.
            align = str(token.attrGet("style") or "")
            token.attrSet("style", f"{_STYLES[tag]};{align}" if align else _STYLES[tag])


def is_ours(markup: str) -> bool:
    """Whether ``markup`` (an email's stored HTML) is what ``body_html``
    wrote, rather than formatting someone made in their mail app."""
    flat = _WHITESPACE.sub("", (markup or "").lower())
    return _MARK in flat and "<table" in flat


def split_forward(text: str) -> tuple[str, str]:
    """``text`` as the note and the forwarded message under it (from its
    "Forwarded message" line), or the whole text and ""."""
    match = _FORWARDED.search(text or "")
    if match is None:
        return text, ""
    return text[: match.start()].rstrip("\n"), text[match.start():]


def body_html(body: str, *, quoted: str = "") -> str | None:
    """``body`` as the HTML part of an email when it holds a table, else
    None (the email stays plain text). ``quoted`` (a forwarded message,
    under the note) goes below it as escaped text, never rendered; without
    it, a forwarded message in ``body`` itself is split off the same way."""
    if not quoted:
        body, quoted = split_forward(body or "")
    blocks = _blocks(body or "")
    if not any(table for table, _ in blocks):
        return None
    # Only the tables are Markdown; every other line goes as written.
    parts: list[str] = []
    for table, lines in blocks:
        if not table:
            parts.append(_text_html(lines))
            continue
        env: dict[str, object] = {}
        tokens = _TABLE.parse("\n".join(lines), env)
        _style(tokens)
        parts.append(str(_TABLE.renderer.render(tokens, _TABLE.options, env)))
    rendered = "".join(parts)
    if quoted:
        rendered += "<br>" + html.escape(quoted).replace("\n", "<br>\n")
    return _WRAPPER.format(rendered)
