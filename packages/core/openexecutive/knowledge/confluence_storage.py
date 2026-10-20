"""Confluence storage format (XHTML with ``ac:`` / ``ri:`` elements) → Markdown.

Used by ``knowledge.confluence_sync`` to turn a page body into text worth
retrieving. Stdlib ``html.parser`` only, like ``orchestrator.artifact_formats``.

Macros: the code and noformat macros become fenced blocks; info, note,
warning and tip keep their body behind a bold label; any other macro keeps its
rich-text body (panel, expand, section, column, ...). Parameters and plain-text
bodies of other macros are dropped, so dynamic macros (toc, children, Jira,
include) leave nothing behind: their content is not in the page.
"""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser

_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.DOTALL)
_WS_RE = re.compile(r"\s+")
_BLANKS_RE = re.compile(r"\n{3,}")

_CODE_MACROS = {"code", "noformat"}
_CALLOUTS = {"info": "Info", "note": "Note", "warning": "Warning", "tip": "Tip"}
# Elements whose whole subtree carries nothing a reader would want.
_SKIP = {
    "script",
    "style",
    "ac:parameter",
    "ac:image",
    "ac:emoticon",
    "ac:placeholder",
    "ac:task-id",
    "ac:adf-attribute",
    "ac:adf-fallback",
}
_VOID = {"br", "img", "hr", "col", "input", "meta", "link", "wbr"}
_LIST_LINE_RE = re.compile(r"\s*(?:- |\d+\. )")
_INLINE_MARKS = {"strong": "**", "b": "**", "em": "_", "i": "_", "s": "~~", "del": "~~"}


class _StorageToMarkdown(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        # Text goes to the top buffer; links, cells, quotes and task statuses
        # push their own and fold back in when they close.
        self._buf: list[list[str]] = [[]]
        self._skip = 0
        self._pre = 0
        self._lists: list[list[str | int]] = []  # [kind, next number]
        self._li = 0
        self._tables: list[list[list[str]]] = []
        self._macros: list[str] = []
        self._plain_body = 0  # inside a non-code macro's plain-text body
        self._links: list[dict[str, str]] = []
        # Set after a callout label so its first paragraph stays on that line.
        self._glue = False

    # -- helpers -------------------------------------------------------------

    def _emit(self, text: str) -> None:
        self._buf[-1].append(text)

    def _push(self) -> None:
        self._buf.append([])

    def _pop(self) -> str:
        return "".join(self._buf.pop()) if len(self._buf) > 1 else ""

    def _block(self) -> None:
        if self._glue:
            self._glue = False
            return
        if not self._li and not self._tables:
            self._emit("\n\n")

    # -- parser hooks --------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skip or tag in _SKIP:
            # Void HTML has no end tag, so it must not deepen the skip.
            if tag not in _VOID:
                self._skip += 1
            return
        a = {k: v or "" for k, v in attrs}
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._emit("\n\n" + "#" * int(tag[1]) + " ")
        elif tag in ("p", "div"):
            self._block()
        elif tag == "br":
            self._emit(" " if self._tables else "\n")
        elif tag in ("ul", "ol", "ac:task-list"):
            self._lists.append(["ol" if tag == "ol" else "ul", 1])
        elif tag in ("li", "ac:task"):
            depth = max(len(self._lists) - 1, 0)
            marker = "- "
            if self._lists and self._lists[-1][0] == "ol" and tag == "li":
                n = int(self._lists[-1][1])
                self._lists[-1][1] = n + 1
                marker = f"{n}. "
            self._emit("\n" + "  " * depth + marker)
            self._li += 1
        elif tag == "ac:task-status":
            self._push()
        elif tag in _INLINE_MARKS:
            self._emit(_INLINE_MARKS[tag])
        elif tag == "code" and not self._pre:
            self._emit("`")
        elif tag == "pre":
            self._emit("\n\n```\n")
            self._pre += 1
        elif tag == "blockquote":
            self._push()
        elif tag == "a":
            self._links.append({"href": a.get("href", "")})
            self._push()
        elif tag == "ac:link":
            self._links.append({"title": ""})
            self._push()
        elif tag in ("ri:page", "ri:blog-post") and self._links:
            self._links[-1]["title"] = a.get("ri:content-title", "")
        elif tag == "ri:attachment" and self._links:
            self._links[-1]["title"] = a.get("ri:filename", "")
        elif tag == "time":
            self._emit(a.get("datetime", ""))
        elif tag == "table":
            self._tables.append([])
        elif tag == "tr" and self._tables:
            self._tables[-1].append([])
        elif tag in ("td", "th") and self._tables:
            self._push()
        elif tag == "ac:structured-macro":
            name = a.get("ac:name", "").lower()
            self._macros.append(name)
            if name in _CODE_MACROS:
                self._emit("\n\n```\n")
                self._pre += 1
            elif name in _CALLOUTS:
                self._block()
                self._emit(f"**{_CALLOUTS[name]}:** ")
                self._glue = True
        elif tag == "ac:plain-text-body" and not (
            self._macros and self._macros[-1] in _CODE_MACROS
        ):
            self._plain_body += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # A self-closing element opens and closes at once; void HTML (br)
        # has no end tag to balance.
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if self._skip:
            self._skip -= 1
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._emit("\n\n")
        elif tag in ("p", "div"):
            self._block()
        elif tag in ("ul", "ol", "ac:task-list"):
            if self._lists:
                self._lists.pop()
            # A nested list ends inside its parent item; the next item opens
            # its own line.
            self._emit("" if self._lists else "\n\n")
        elif tag in ("li", "ac:task"):
            self._li = max(self._li - 1, 0)
        elif tag == "ac:task-status":
            status = self._pop().strip().lower()
            self._emit("[x] " if status == "complete" else "[ ] ")
        elif tag in _INLINE_MARKS:
            self._emit(_INLINE_MARKS[tag])
        elif tag == "code" and not self._pre:
            self._emit("`")
        elif tag == "pre":
            self._pre = max(self._pre - 1, 0)
            self._emit("\n```\n\n")
        elif tag == "blockquote":
            body = _BLANKS_RE.sub("\n\n", self._pop().strip())
            if self._tables:
                self._emit(body)
            else:
                self._emit("\n\n" + "\n".join(f"> {line}".rstrip() for line in body.split("\n")) + "\n\n")
        elif tag == "a":
            text = self._pop().strip()
            href = self._links.pop().get("href", "") if self._links else ""
            if href.startswith(("https://", "http://")) and text and "]" not in text:
                self._emit(f"[{text}]({href.replace(')', '%29').replace(' ', '%20')})")
            else:
                self._emit(text)
        elif tag == "ac:link":
            text = self._pop().strip()
            title = self._links.pop().get("title", "") if self._links else ""
            self._emit(text or title)
        elif tag in ("td", "th") and self._tables:
            cell = " ".join(self._pop().split()).replace("|", "\\|")
            if not self._tables[-1]:
                self._tables[-1].append([])
            self._tables[-1][-1].append(cell)
        elif tag == "table" and self._tables:
            rows = [r for r in self._tables.pop() if r]
            self._emit(_render_table(rows, nested=bool(self._tables)))
        elif tag == "ac:structured-macro":
            name = self._macros.pop() if self._macros else ""
            if name in _CODE_MACROS:
                self._pre = max(self._pre - 1, 0)
                self._emit("\n```\n\n")
            elif name in _CALLOUTS:
                self._block()
        elif tag == "ac:plain-text-body" and self._plain_body:
            self._plain_body -= 1

    def handle_data(self, data: str) -> None:
        if self._skip or self._plain_body:
            return
        if self._glue and data.strip():
            self._glue = False
        if self._pre:
            self._emit(data)
        else:
            self._emit(_WS_RE.sub(" ", data))

    def result(self) -> str:
        while len(self._buf) > 1:  # unclosed elements: keep their text
            text = self._pop()
            self._emit(text)
        raw = "".join(self._buf[0])
        lines = [line.rstrip() for line in raw.split("\n")]
        # A space left at the start of a line by collapsed markup whitespace
        # would read as indentation; keep it only inside fenced blocks.
        out: list[str] = []
        fenced = False
        for line in lines:
            if line.startswith("```"):
                fenced = not fenced
                out.append(line)
            elif fenced or _LIST_LINE_RE.match(line):
                out.append(line)
            else:
                out.append(line.lstrip())
        return _BLANKS_RE.sub("\n\n", "\n".join(out)).strip()


def _render_table(rows: list[list[str]], *, nested: bool) -> str:
    if not rows:
        return ""
    if nested:
        return " ".join(cell for row in rows for cell in row if cell)
    width = max(len(r) for r in rows)
    padded = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(padded[0]) + " |", "|" + " --- |" * width]
    lines += ["| " + " | ".join(r) + " |" for r in padded[1:]]
    return "\n\n" + "\n".join(lines) + "\n\n"


def storage_to_markdown(storage: str) -> str:
    """Markdown for a page's ``body.storage.value``. Never raises on bad markup."""
    if not storage:
        return ""
    # CDATA (code macro bodies) is read as escaped text, so the parser's
    # handling of it, which differs across Python versions, never matters.
    text = _CDATA_RE.sub(lambda m: html.escape(m.group(1), quote=False), storage)
    parser = _StorageToMarkdown()
    try:
        parser.feed(text)
        parser.close()
    except Exception:  # pragma: no cover - html.parser is lenient
        return _WS_RE.sub(" ", re.sub(r"<[^>]+>", " ", text)).strip()
    return parser.result()
