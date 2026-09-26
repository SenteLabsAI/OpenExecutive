"""Read the text of any PDF, including scanned and image-only ones.

pypdf returns only a PDF's saved text layer. A scan, a phone photo saved as
PDF, or a document "printed" to PDF as images has none, so every reader that
relied on pypdf alone got ``""`` back and the Executive could only say it
could not read the file. ``read_pdf_text`` tries three readers in order:

1. **Text layer** (pypdf) — free and exact; used whenever it yields real text.
2. **Claude** — a ``document`` content block, which gives the model every
   page as an image as well as its text. Only when ``PDF_VISION_MODEL``
   resolves to the Anthropic-direct provider: the OpenRouter / local
   translator drops ``document`` blocks, so sending one there would silently
   lose the PDF.
3. **Local OCR** — pages rendered with pypdfium2 and read by RapidOCR (ONNX,
   models bundled in the wheel). Works on every provider, offline, with no
   key and no per-page cost. Also the fallback when step 2 fails or refuses.

Callers get a ``PdfReadResult`` and never an exception: an unreadable PDF
comes back as ``method="none"`` with a ``note`` saying why, which callers
show to the model in place of the old bare "could not extract any text".
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger(__name__)

Method = Literal["text_layer", "vision", "ocr", "none"]

# Below this many non-whitespace characters per page, the text layer is taken
# to be missing: a scanned deck often carries only page numbers or a footer.
_MIN_CHARS_PER_PAGE = 20
_VISION_CONCURRENCY = 3
_VISION_MAX_TOKENS = 16_000
_CACHE_SIZE = 32
# ~144 dpi for a Letter/A4 page: enough for body text, bounded in memory.
_OCR_RENDER_SCALE = 2.0

_TRANSCRIBE_PROMPT = (
    "Transcribe every page of this PDF into Markdown, verbatim. Keep the "
    "original wording, numbers and reading order. Render tables as Markdown "
    "tables and keep headings as headings. Before each page write a line "
    "'--- page N ---' using the page numbers given below. Write [illegible] "
    "for text you cannot read and describe charts or images in one short "
    "bracketed line. Output only the transcription: no preamble, no summary, "
    "no commentary."
)


@dataclass(frozen=True)
class PdfReadResult:
    text: str
    method: Method
    pages: int
    note: str = ""

    @property
    def converted(self) -> bool:
        """True when the text came from reading page images, not a text layer."""
        return self.method in ("vision", "ocr")


# ── Cache ────────────────────────────────────────────────────────────────────

_cache: OrderedDict[str, PdfReadResult] = OrderedDict()
_cache_lock = threading.Lock()


def _cache_get(key: str) -> PdfReadResult | None:
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)
        return hit


def _cache_put(key: str, result: PdfReadResult) -> None:
    with _cache_lock:
        _cache[key] = result
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_SIZE:
            _cache.popitem(last=False)


def clear_cache() -> None:
    """Drop cached conversions. Tests call this between cases."""
    with _cache_lock:
        _cache.clear()


# ── Text layer ───────────────────────────────────────────────────────────────

def _text_layer(data: bytes) -> tuple[str, int]:
    """(joined page text, page count) from the PDF's own text layer."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = []
    for page in reader.pages:
        text = page.extract_text()
        if text:
            pages.append(text.strip())
    return "\n\n".join(pages), len(reader.pages)


def _is_thin(text: str, pages: int) -> bool:
    visible = sum(1 for c in text if not c.isspace())
    return visible < _MIN_CHARS_PER_PAGE * max(pages, 1)


# ── Claude (document blocks) ─────────────────────────────────────────────────

def slice_pdf(data: bytes, start: int, end: int) -> bytes:
    """Pages [start, end) of ``data`` as a standalone PDF."""
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(data))
    writer = PdfWriter()
    for i in range(start, end):
        writer.add_page(reader.pages[i])
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _vision_provider(model: str) -> Any | None:
    """The provider for ``model`` when it is Anthropic-direct, else None.

    Anything else (OpenRouter, a local server, or no API key at all — which
    ``get_provider`` reports by raising) cannot carry a ``document`` block.
    """
    from openexecutive.providers import get_provider
    from openexecutive.providers.anthropic_provider import AnthropicProvider

    try:
        provider = get_provider(model)
    except Exception:
        return None
    return provider if isinstance(provider, AnthropicProvider) else None


async def _vision_slice(
    provider: Any, model: str, data: bytes, start: int, end: int
) -> str:
    chunk = await asyncio.to_thread(slice_pdf, data, start, end)
    response = await provider.messages_create(
        model=model,
        max_tokens=_VISION_MAX_TOKENS,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": base64.standard_b64encode(chunk).decode(),
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            f"{_TRANSCRIBE_PROMPT}\n\nThese are pages "
                            f"{start + 1} to {end} of the original document."
                        ),
                    },
                ],
            }
        ],
    )
    if getattr(response, "stop_reason", None) == "refusal":
        raise RuntimeError("model declined to transcribe the pages")
    text = "".join(
        getattr(b, "text", "") for b in response.content if getattr(b, "type", "") == "text"
    ).strip()
    if not text:
        raise RuntimeError("model returned no transcription")
    return text


async def _read_with_vision(data: bytes, pages: int) -> str | None:
    """Transcribe up to ``pages`` pages with Claude, or None if unavailable."""
    from openexecutive.config import get_settings

    settings = get_settings()
    model = settings.pdf_vision_model
    provider = _vision_provider(model)
    if provider is None:
        return None

    step = settings.pdf_vision_pages_per_call
    bounds = [(s, min(s + step, pages)) for s in range(0, pages, step)]
    gate = asyncio.Semaphore(_VISION_CONCURRENCY)

    async def run(start: int, end: int) -> str:
        async with gate:
            return await _vision_slice(provider, model, data, start, end)

    try:
        parts = await asyncio.gather(*(run(s, e) for s, e in bounds))
    except Exception as exc:
        logger.warning("pdf_reader: vision transcription failed (%s)", type(exc).__name__)
        return None
    return "\n\n".join(parts)


# ── Local OCR ────────────────────────────────────────────────────────────────

_ocr_engine: Any = None
_ocr_lock = threading.Lock()


class OcrUnavailable(RuntimeError):
    pass


def _get_ocr_engine() -> Any:
    global _ocr_engine
    with _ocr_lock:
        if _ocr_engine is None:
            try:
                from rapidocr_onnxruntime import RapidOCR
            except ImportError as exc:  # e.g. Python 3.13+, where it isn't installed
                raise OcrUnavailable(str(exc)) from exc
            _ocr_engine = RapidOCR()
        return _ocr_engine


def _ocr_page_text(engine: Any, image: Any) -> str:
    """OCR one page image into lines, top to bottom then left to right."""
    import numpy as np

    result, _ = engine(np.asarray(image.convert("RGB")))
    if not result:
        return ""
    # Each item is (box, text, score); box is four [x, y] corners starting
    # top-left. A box joins the current row when its vertical centre falls
    # within that row's first box, so words on one visual line stay together.
    items = sorted(result, key=lambda r: (r[0][0][1], r[0][0][0]))
    rows: list[list[tuple[float, str]]] = []
    row_top = row_bottom = 0.0
    for box, text, _score in items:
        top = min(p[1] for p in box)
        bottom = max(p[1] for p in box)
        centre = (top + bottom) / 2
        if rows and row_top <= centre <= row_bottom:
            rows[-1].append((box[0][0], text))
        else:
            rows.append([(box[0][0], text)])
            row_top, row_bottom = top, bottom
    return "\n".join(" ".join(t for _x, t in sorted(row)) for row in rows)


def _ocr_pdf(data: bytes, max_pages: int) -> str:
    """OCR the first ``max_pages`` pages. Blocking — run in a thread."""
    import pypdfium2 as pdfium

    engine = _get_ocr_engine()
    doc = pdfium.PdfDocument(data)
    try:
        parts: list[str] = []
        for i in range(min(len(doc), max_pages)):
            page = doc[i]
            try:
                image = page.render(scale=_OCR_RENDER_SCALE).to_pil()
            finally:
                page.close()
            text = _ocr_page_text(engine, image).strip()
            if text:
                parts.append(f"--- page {i + 1} ---\n{text}")
        return "\n\n".join(parts)
    finally:
        doc.close()


# ── Public entry point ───────────────────────────────────────────────────────

async def read_pdf_text(data: bytes, *, filename: str = "") -> PdfReadResult:
    """Return the text of a PDF, converting scanned pages when needed."""
    from openexecutive.config import get_settings

    key = hashlib.sha256(data).hexdigest()
    cached = _cache_get(key)
    if cached is not None:
        return cached

    label = filename or "PDF"
    try:
        text, pages = await asyncio.to_thread(_text_layer, data)
    except Exception as exc:
        logger.warning("pdf_reader: could not open %s (%s)", label, type(exc).__name__)
        return PdfReadResult(
            "", "none", 0, "the file could not be opened as a PDF (it may be damaged or password-protected)"
        )

    if pages and not _is_thin(text, pages):
        result = PdfReadResult(text, "text_layer", pages)
        _cache_put(key, result)
        return result

    settings = get_settings()
    limit = min(pages, settings.pdf_vision_max_pages)
    skipped = (
        f"only the first {limit} of {pages} pages were read" if pages > limit else ""
    )

    vision = await _read_with_vision(data, limit) if limit else None
    if vision:
        result = PdfReadResult(vision, "vision", pages, skipped)
        _cache_put(key, result)
        return result

    if not settings.pdf_ocr_enabled:
        return _fallback(text, pages, "it looks scanned and scanned-PDF conversion is turned off")
    try:
        ocr = await asyncio.to_thread(_ocr_pdf, data, limit)
    except OcrUnavailable:
        return _fallback(text, pages, "it looks scanned and OCR is not installed on this server")
    except Exception as exc:
        logger.warning("pdf_reader: OCR failed for %s (%s)", label, type(exc).__name__)
        return _fallback(text, pages, "it looks scanned and OCR could not read it")
    if not ocr.strip():
        return _fallback(text, pages, "it looks scanned and no text could be read from its pages")
    result = PdfReadResult(ocr, "ocr", pages, skipped)
    _cache_put(key, result)
    return result


def _fallback(text: str, pages: int, reason: str) -> PdfReadResult:
    """Whatever thin text layer there was, or nothing — never cached, so a
    later call (e.g. after a key is configured) can still convert it."""
    if text.strip():
        return PdfReadResult(text, "text_layer", pages, f"text may be incomplete: {reason}")
    return PdfReadResult("", "none", pages, f"no text could be read: {reason}")
