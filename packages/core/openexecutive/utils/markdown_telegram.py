"""Markdown replies as Telegram message HTML.

The Executive writes Markdown, and Telegram shows a plain-text message as
typed: asterisks, hashes and backticks included. Telegram's own formatting
takes a small HTML subset (``parse_mode=HTML``), so a reply is parsed with
markdown-it-py and written out in that subset:

- bold, italic, strikethrough, inline code, code blocks and quotes map to
  Telegram's tags; headings become bold lines; list items get a
  bullet or their number, since Telegram has no list tags;
- every piece of text is escaped, and raw HTML in the reply stays text
  (markdown-it's ``html`` is off), so quoted mail or web text can't add
  markup of its own;
- no ``<a>`` tag is ever written. A link shows as its text followed by its
  target in brackets, as plain text (Telegram turns a visible web address
  into a link itself). A reply can quote mail or a web page, and a tag would
  let that text hide where a link goes (``[Your bank](https://evil…)``) or
  make a ``tg://`` or ``ton://`` address one tap away (add a proxy, start a
  bot, a wallet transfer), which plain text never did. Images are off too.
"""
from __future__ import annotations

import html
import re

from markdown_it import MarkdownIt
from markdown_it.token import Token

_PARSER = MarkdownIt("commonmark", {"html": False}).disable("image").enable("strikethrough")

_INLINE_TAGS = {
    "strong_open": "<b>",
    "strong_close": "</b>",
    "em_open": "<i>",
    "em_close": "</i>",
    "s_open": "<s>",
    "s_close": "</s>",
}


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def _inline(children: list[Token]) -> str:
    out: list[str] = []
    # Targets of the links currently open, and where each one's text began.
    links: list[tuple[str, int]] = []
    for tok in children:
        if tok.type == "text":
            out.append(_esc(tok.content))
        elif tok.type in ("softbreak", "hardbreak"):
            out.append("\n")
        elif tok.type == "code_inline":
            out.append(f"<code>{_esc(tok.content)}</code>")
        elif tok.type == "link_open":
            links.append((str(tok.attrGet("href") or ""), len(out)))
        elif tok.type == "link_close":
            href, start = links.pop() if links else ("", len(out))
            shown = "".join(out[start:])
            if href and _esc(href) != shown and f"mailto:{shown}" != _esc(href):
                out.append(f" ({_esc(href)})")
        elif tok.type in _INLINE_TAGS:
            out.append(_INLINE_TAGS[tok.type])
        else:
            out.append(_esc(tok.content))
    return "".join(out)


def markdown_to_telegram_html(text: str) -> str:
    """``text`` (Markdown) as the body of a ``parse_mode=HTML`` message."""
    out: list[str] = []
    # One entry per open list: the next item number, or None for bullets.
    lists: list[int | None] = []
    for tok in _PARSER.parse(text):
        kind = tok.type
        if kind == "heading_open":
            out.append("<b>")
        elif kind == "heading_close":
            out.append("</b>\n\n")
        elif kind == "paragraph_close":
            out.append("\n" if tok.hidden else "\n\n")
        elif kind == "inline":
            out.append(_inline(tok.children or []))
        elif kind == "bullet_list_open":
            lists.append(None)
        elif kind == "ordered_list_open":
            lists.append(int(tok.attrGet("start") or 1))
        elif kind in ("bullet_list_close", "ordered_list_close"):
            lists.pop()
            if not lists:
                out.append("\n")
        elif kind == "list_item_open":
            indent = "  " * (len(lists) - 1)
            current = lists[-1] if lists else None
            if current is None:
                out.append(f"{indent}• ")
            else:
                out.append(f"{indent}{current}. ")
                lists[-1] = current + 1
        elif kind == "blockquote_open":
            out.append("<blockquote>")
        elif kind == "blockquote_close":
            out.append("</blockquote>\n\n")
        elif kind in ("fence", "code_block"):
            out.append(f"<pre>{_esc(tok.content.rstrip(chr(10)))}</pre>\n\n")
        elif kind == "hr":
            out.append("———\n\n")
    rendered = "".join(out)
    # A quote's last paragraph leaves its break inside the tag.
    rendered = re.sub(r"\s+</blockquote>", "</blockquote>", rendered)
    # Collapse runs of blank lines between blocks, but not inside code,
    # which is shown exactly as written.
    parts = re.split(r"(<pre>.*?</pre>)", rendered, flags=re.S)
    return "".join(
        part if part.startswith("<pre>") else re.sub(r"\n{3,}", "\n\n", part) for part in parts
    ).strip()
