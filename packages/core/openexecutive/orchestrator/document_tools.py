"""Chat tool that reads a document file — including a scanned PDF.

Attachments sent in chat, Discord, Telegram, Slack, Google Chat and inbound
email are read for the Executive before the turn starts. ``read_document``
covers the rest: a file another tool saved to disk (a Gmail attachment from
``get_gmail_attachment_content``, a Drive file from
``get_drive_file_download_url``) or a company document by name. A PDF with no
text layer is converted by ``knowledge.pdf_reader`` — Claude when the
deployment reaches it directly, local OCR otherwise — instead of coming back
empty.

Reads are confined to the tool download folders (``tool_catalog.
resolve_readable_file``, the same check ``oe__read_file`` uses) and the
company documents folder. The tool is withheld on turns private to the
principal (``schedule_tools.PRIVATE_TURN_WITHHELD_TOOLS``): those come from a
contact's email, whose own attachments the poller has already read, and must
not steer the Executive into the company's documents. The schema is static,
so the cached tool prefix never moves.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_PAGES_RE = re.compile(r"^\s*(\d+)\s*(?:-\s*(\d+))?\s*$")

READ_DOCUMENT_TOOL: dict[str, Any] = {
    "name": "read_document",
    "description": (
        "Read the full text of a PDF, Word, Excel, CSV, Markdown or text file. "
        "Scanned or image-only PDFs are converted to text, so use this whenever "
        "a PDF came back empty, 'could not extract any text', or 'may be "
        "scanned/image-only'. Pass `path` for a file another tool saved to disk "
        "— e.g. the 'Saved to:' path from get_gmail_attachment_content, or from "
        "get_drive_file_download_url for a Drive PDF whose content could not be "
        "read — or `filename` for a company document. For a long PDF, pass "
        "`pages` (e.g. '1-20') to read part of it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute path of a file another tool downloaded.",
            },
            "filename": {
                "type": "string",
                "description": "Name of a company document, e.g. 'board-deck-q3.pdf'.",
            },
            "pages": {
                "type": "string",
                "description": "PDF page range to read, e.g. '5' or '1-20'. Default: all.",
            },
        },
    },
}

DOCUMENT_TOOLS: list[dict[str, Any]] = [READ_DOCUMENT_TOOL]


def _err(message: str) -> str:
    return json.dumps({"error": message})


def _company_docs_dir() -> Path:
    from openexecutive.config import get_settings

    return (get_settings().company_profile_path.parent / "docs").resolve()


def _parse_pages(raw: str) -> tuple[int, int] | str:
    """1-based inclusive ``pages`` → 0-based ``(start, end)``, or an error."""
    m = _PAGES_RE.match(raw)
    if not m:
        return "pages must look like '5' or '1-20'"
    first = int(m.group(1))
    last = int(m.group(2) or first)
    if first < 1 or last < first:
        return "pages must look like '5' or '1-20'"
    return first - 1, last


async def _read_pages(path: Path, first: int, last: int) -> tuple[Any, str]:
    """Read pages ``[first, last)`` of a PDF. Returns ``(result, range_label)``."""
    from pypdf import PdfReader

    from openexecutive.knowledge.pdf_reader import read_pdf_text, slice_pdf

    data = await asyncio.to_thread(path.read_bytes)

    def _count() -> int:
        import io

        return len(PdfReader(io.BytesIO(data)).pages)

    total = await asyncio.to_thread(_count)
    if first >= total:
        raise ValueError(f"that PDF has only {total} pages")
    last = min(last, total)
    part = await asyncio.to_thread(slice_pdf, data, first, last)
    result = await read_pdf_text(part, filename=path.name)
    return result, f"pages {first + 1}-{last} of {total}"


async def handle_read_document(tool_input: dict[str, Any]) -> str:
    from openexecutive.config import get_settings
    from openexecutive.knowledge.loader import read_document_text
    from openexecutive.workflows.tool_catalog import resolve_readable_file

    raw_path = str(tool_input.get("path") or "").strip()
    filename = str(tool_input.get("filename") or "").strip()
    if not raw_path and not filename:
        return _err("pass `path` (a downloaded file) or `filename` (a company document)")

    docs_dir = _company_docs_dir()
    if filename and not raw_path:
        safe = Path(filename).name
        if not safe or safe.startswith(".") or safe != filename:
            return _err("filename must be a bare document name")
        raw_path = str(docs_dir / safe)

    resolved = resolve_readable_file(raw_path, extra_dirs=[docs_dir])
    if isinstance(resolved, str):
        return _err(resolved)

    pages_raw = str(tool_input.get("pages") or "").strip()
    scope = ""
    try:
        if pages_raw and resolved.suffix.lower() == ".pdf":
            bounds = _parse_pages(pages_raw)
            if isinstance(bounds, str):
                return _err(bounds)
            result, scope = await _read_pages(resolved, *bounds)
        else:
            result = await read_document_text(resolved)
    except ValueError as exc:
        return _err(str(exc))
    except Exception as exc:
        logger.warning("read_document: extraction failed (%s)", type(exc).__name__)
        return _err("could not read that file")

    if not result.text.strip():
        return _err(result.note or "no text could be read from that file")

    header = [f"[{resolved.name}"]
    if scope:
        header.append(f", {scope}")
    if result.converted:
        header.append(", converted from scanned pages")
    if result.note:
        header.append(f" — {result.note}")
    header.append("]")

    text = result.text
    cap = get_settings().tool_result_max_chars
    if len(text) > cap:
        text = text[:cap] + "\n…[truncated — pass `pages` to read the rest]"
    return "".join(header) + "\n" + text


DOCUMENT_TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[str]]] = {
    "read_document": handle_read_document,
}
