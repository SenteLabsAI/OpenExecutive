"""The HTML part of an email written as someone, for the tables in it.

The ghostwriter writes plain text, and may lay figures out as a Markdown pipe
table (``| Property | Amount |`` then ``|---|---|``). Mail clients show that as
text, so a draft with a table gets an HTML alternative next to its plain text
(``multipart/alternative``): the same words, with the table as a real table.
A draft with no table stays plain text, exactly as before.

The words come partly from other people's email (the thread being answered),
so the renderer is set up like ``utils.markdown_email``'s: raw HTML off (markup
shows as text), images off, and links off too — a URL stays visible text,
which ``ghostwriter.lint`` has already checked, and the mail client links it.
Line breaks are kept, since a plain-text email's single line breaks (a
sign-off, an address) mean something. Styles are inline, because most mail
clients drop ``<style>`` blocks.
"""
from __future__ import annotations

import html
import re

from markdown_it import MarkdownIt
from markdown_it.token import Token

_RENDERER = (
    MarkdownIt("commonmark", {"html": False, "breaks": True})
    .enable("table")
    .disable(["image", "link", "autolink"])
)

# A table's separator row: pipes and dashes, optional colons for alignment,
# one or more dashes a cell.
_SEPARATOR = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")

_STYLES = {
    "table": "border-collapse:collapse;margin:8px 0",
    "th": "border:1px solid #d0d7de;padding:6px 10px;background:#f6f8fa;text-align:left",
    "td": "border:1px solid #d0d7de;padding:6px 10px",
}

_WRAPPER = '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.45">{}</div>'


def _lines_with_tables(text: str) -> tuple[list[str], bool]:
    """``text``'s lines with a blank line before each table's header row (a
    table can't interrupt a paragraph), and whether it has a table."""
    lines = text.split("\n")
    out: list[str] = []
    found = False
    for i, line in enumerate(lines):
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        header = "|" in line and "|" in nxt and bool(_SEPARATOR.match(nxt))
        if header:
            found = True
            if out and out[-1].strip():
                out.append("")
        out.append(line)
    return out, found


def has_table(text: str) -> bool:
    """Whether ``text`` holds a Markdown pipe table."""
    return _lines_with_tables(text or "")[1]


def _style(tokens: list[Token]) -> None:
    for token in tokens:
        tag = token.tag if token.nesting == 1 else ""
        if tag in _STYLES:
            # Keep the column's alignment, which markdown-it sets as a style.
            align = str(token.attrGet("style") or "")
            token.attrSet("style", f"{_STYLES[tag]};{align}" if align else _STYLES[tag])


def body_html(body: str, *, quoted: str = "") -> str | None:
    """``body`` as the HTML part of an email when it holds a table, else
    None (the email stays plain text). ``quoted`` (a forwarded message,
    under the note) goes below it as escaped text, never rendered."""
    lines, found = _lines_with_tables(body or "")
    if not found:
        return None
    env: dict[str, object] = {}
    tokens = _RENDERER.parse("\n".join(lines), env)
    _style(tokens)
    rendered = str(_RENDERER.renderer.render(tokens, _RENDERER.options, env))
    if quoted:
        rendered += "<br>" + html.escape(quoted).replace("\n", "<br>\n")
    return _WRAPPER.format(rendered)
