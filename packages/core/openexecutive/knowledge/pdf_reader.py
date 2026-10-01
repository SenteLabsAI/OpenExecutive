"""Read the text of any PDF, including scanned and image-only ones.

pypdf returns only a PDF's saved text layer. A scan, a phone photo saved as
PDF, or a document "printed" to PDF as images has none, so every reader that
relied on pypdf alone got ``""`` back and the Executive could only say it
could not read the file. ``read_pdf_text`` tries three readers in order:

1. **Text layer** (pypdf) — free and exact; used whenever it yields real text.
2. **The deployment's own model, through its provider's PDF support**
   (opt-in: ``PDF_PROVIDER_READING=true``; off by default, so scanned PDFs
   stay on the server unless an operator turns this on) — the
   pages go out as an Anthropic ``document`` block to ``PDF_VISION_MODEL``
   (default: ``DEFAULT_MODEL``), and each provider carries it its own way:
   Anthropic reads it natively; OpenRouter gets an OpenAI ``file`` part plus
   its ``file-parser`` plugin (``native`` for a model that reads files, else
   ``PDF_OPENROUTER_ENGINE``, default ``mistral-ocr``); a local server gets
   the ``file`` part only with ``LOCAL_PDF_INPUT`` (e.g. OpenAI's own API).
   ``providers.registry.pdf_input_supported`` decides; the translation lives
   in ``providers/translator.py`` and ``openrouter_provider.py``.
   The destination is logged once per process when this step is on.
3. **Local OCR** — pages rendered with pypdfium2 and read by RapidOCR (ONNX,
   models bundled in the wheel), offline, with no key and no per-page cost:
   the default reader while step 2 is off, for a local model without PDF
   input, and the fallback whenever step 2 fails or refuses.

Callers get a ``PdfReadResult`` and never an exception: an unreadable PDF
comes back as ``method="none"`` with a ``note`` saying why, which callers
show to the model in place of the old bare "could not extract any text".

Steps 1 and 3 parse the file in a short-lived child process
(``knowledge.isolated``), so the memory a large PDF takes to parse goes back
to the OS when it is done instead of staying with the API process. A caller
holding the file on disk passes its ``Path``: the child reads it, and this
process only loads the bytes if they have to go to the model (step 2).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import logging
import math
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from openexecutive.knowledge.isolated import WorkerStopped, run_isolated

logger = logging.getLogger(__name__)

Method = Literal["text_layer", "model", "ocr", "none"]

# Below this many non-whitespace characters per page, the text layer is taken
# to be missing: a scanned deck often carries only page numbers or a footer.
_MIN_CHARS_PER_PAGE = 20
_VISION_CONCURRENCY = 3
_VISION_MAX_TOKENS = 16_000
# Marks where a single page's transcription ran into the output limit.
_CUT_OFF_MARK = "[transcription cut off here]"
# Hard ceiling on a PDF's page count, checked before any page is parsed. The
# page caps below bound conversion only; without this a crafted file of tens
# of thousands of tiny pages would have every page's text extracted first.
_MAX_PDF_PAGES = 2000
_CACHE_SIZE = 32
# ~144 dpi for a Letter/A4 page: enough for body text.
_OCR_RENDER_SCALE = 2.0
# Pixel budget per rendered page. A page's size is whatever its MediaBox
# says, so a crafted 20000pt-square page would otherwise render to a
# multi-gigabyte bitmap; a bigger page is rendered at a lower scale instead.
_OCR_MAX_PAGE_PIXELS = 25_000_000
# OCR is CPU-bound and runs in a child process: at most this many documents
# at once, each stopping (with what it has) after the time budget.
_OCR_CONCURRENCY = 2
_OCR_TIME_BUDGET_S = 240.0
# Wall-clock limits on the child processes, past which they are killed. OCR
# gets its time budget plus room to start Python and load the model; the text
# layer gets enough for a legitimate _MAX_PDF_PAGES-page file on one slow CPU.
_TEXT_LAYER_TIMEOUT_S = 300.0
_OCR_PROCESS_SLACK_S = 120.0
# Pages converted (model or OCR) for files that arrive on their own through
# a channel — as opposed to one the Executive or the signed-in user asks to
# read — are metered per rolling hour, so no sender can run up unbounded model
# spend or CPU by sending scans. See PDF_INBOUND_MAX_PAGES / _PAGES_PER_HOUR.
_INBOUND_WINDOW_S = 3600.0

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
        return self.method in ("model", "ocr")


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

class PdfTooLarge(ValueError):
    def __init__(self, pages: int) -> None:
        super().__init__(f"{pages} pages")
        self.pages = pages


def _text_layer(source: bytes | Path, max_pages: int | None = None) -> tuple[str, int]:
    """(joined page text, page count) from the PDF's own text layer.

    Raises ``PdfTooLarge`` past ``max_pages`` (default ``_MAX_PDF_PAGES``),
    before any page's text is extracted. ``read_pdf_text`` runs this in a
    child process, which is why the ceiling is an argument: the child does
    not see this process's module state."""
    from pypdf import PdfReader

    limit = _MAX_PDF_PAGES if max_pages is None else max_pages
    reader = PdfReader(io.BytesIO(source) if isinstance(source, bytes) else str(source))
    count = len(reader.pages)
    if count > limit:
        raise PdfTooLarge(count)
    pages = []
    for page in reader.pages:
        text = page.extract_text()
        if text:
            pages.append(text.strip())
    return "\n\n".join(pages), len(reader.pages)


def _is_thin(text: str, pages: int) -> bool:
    visible = sum(1 for c in text if not c.isspace())
    return visible < _MIN_CHARS_PER_PAGE * max(pages, 1)


# ── The deployment's model (a document block, through any provider) ──────────

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


def _pdf_model() -> str:
    """The model that reads scanned pages: PDF_VISION_MODEL, else the model
    the deployment already runs on."""
    from openexecutive.config import get_settings

    settings = get_settings()
    return settings.pdf_vision_model or settings.default_model


_announced: set[tuple[str, str]] = set()


def _announce_destination(model: str, provider: Any) -> None:
    """Log once per process where scanned PDFs are sent, so an operator can
    see the data leave: which provider, and on OpenRouter which parser."""
    from openexecutive.providers.openrouter_provider import OpenRouterProvider

    where = type(provider).__name__
    if isinstance(provider, OpenRouterProvider):
        from openexecutive.providers.openrouter_provider import _pdf_engine
        from openexecutive.providers.registry import openrouter_slug_for_claude

        slug = openrouter_slug_for_claude(model) or model
        where = f"OpenRouter (file-parser engine {_pdf_engine(slug)})"
    key = (model, where)
    if key in _announced:
        return
    _announced.add(key)
    logger.info(
        "pdf_reader: scanned PDFs are sent to %s via %s "
        "(PDF_PROVIDER_READING is on; off keeps them on this server)",
        model, where,
    )


def _model_provider(model: str) -> Any | None:
    """The provider to send ``model`` a PDF through, or None when that model
    cannot receive one (a local server without LOCAL_PDF_INPUT) or has no
    reachable provider at all (``get_provider`` reports that by raising)."""
    from openexecutive.providers import get_provider
    from openexecutive.providers.registry import pdf_input_supported

    try:
        if not pdf_input_supported(model):
            return None
        return get_provider(model)
    except Exception:
        return None


async def _model_slice(
    provider: Any, model: str, data: bytes, start: int, end: int
) -> tuple[str, bool]:
    """Transcribe pages [start, end): ``(text, cut off at max_tokens)``."""
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
    return text, getattr(response, "stop_reason", None) == "max_tokens"


async def _transcribe(
    request: Any, start: int, end: int
) -> tuple[str, bool]:
    """Transcribe pages [start, end) with ``request(start, end)``, splitting a
    slice whose answer ran into the output limit in half until each part
    fits. A single page that still does not fit keeps what was written, with
    a marker. Returns ``(text, whether any page was cut off)``."""
    text, cut_off = await request(start, end)
    if not cut_off:
        return text, False
    if end - start == 1:
        return f"{text}\n{_CUT_OFF_MARK}", True
    mid = (start + end) // 2
    (first, cut_a), (second, cut_b) = await asyncio.gather(
        _transcribe(request, start, mid), _transcribe(request, mid, end)
    )
    return f"{first}\n\n{second}", cut_a or cut_b


async def _read_with_model(data: bytes, pages: int) -> tuple[str, bool] | None:
    """Transcribe up to ``pages`` pages with the deployment's model: ``(text,
    whether a page was cut off)``, or None if unavailable or failed."""
    from openexecutive.config import get_settings

    settings = get_settings()
    model = _pdf_model()
    provider = _model_provider(model)
    if provider is None:
        return None
    _announce_destination(model, provider)

    step = settings.pdf_vision_pages_per_call
    bounds = [(s, min(s + step, pages)) for s in range(0, pages, step)]
    gate = asyncio.Semaphore(_VISION_CONCURRENCY)

    async def request(start: int, end: int) -> tuple[str, bool]:
        # The gate covers one request, not a split's recursion, so halves of
        # a cut-off slice queue like any other request.
        async with gate:
            return await _model_slice(provider, model, data, start, end)

    try:
        parts = await asyncio.gather(*(_transcribe(request, s, e) for s, e in bounds))
    except Exception as exc:
        logger.warning("pdf_reader: model transcription failed (%s)", type(exc).__name__)
        return None
    return "\n\n".join(t for t, _cut in parts), any(cut for _t, cut in parts)


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


def _render_scale(width_pt: float, height_pt: float) -> float:
    """The render scale for a page, lowered so it stays within the pixel budget."""
    area = max(width_pt, 1.0) * max(height_pt, 1.0)
    if area * _OCR_RENDER_SCALE**2 <= _OCR_MAX_PAGE_PIXELS:
        return _OCR_RENDER_SCALE
    return math.sqrt(_OCR_MAX_PAGE_PIXELS / area)


def _ocr_pdf(source: bytes | Path, max_pages: int) -> tuple[str, int]:
    """OCR up to ``max_pages`` pages: ``(text, pages read)``. Stops early at
    the time budget. Blocking, and loads the OCR model: ``read_pdf_text`` runs
    it through ``_ocr_isolated``."""
    import pypdfium2 as pdfium

    engine = _get_ocr_engine()
    deadline = time.monotonic() + _OCR_TIME_BUDGET_S
    doc = pdfium.PdfDocument(source if isinstance(source, bytes) else str(source))
    try:
        parts: list[str] = []
        read = 0
        for i in range(min(len(doc), max_pages)):
            if time.monotonic() > deadline:
                break
            page = doc[i]
            try:
                width, height = page.get_size()
                image = page.render(scale=_render_scale(width, height)).to_pil()
            finally:
                page.close()
            text = _ocr_page_text(engine, image).strip()
            read += 1
            if text:
                parts.append(f"--- page {i + 1} ---\n{text}")
        return "\n\n".join(parts), read
    finally:
        doc.close()


_ocr_slots = threading.BoundedSemaphore(_OCR_CONCURRENCY)


def _ocr_isolated(source: bytes | Path, max_pages: int) -> tuple[str, int]:
    """``_ocr_pdf`` in a child process, at most ``_OCR_CONCURRENCY`` at once.
    The model and the rendered pages leave with the child. Blocking — run in
    a thread."""
    text, read = run_isolated(
        _ocr_pdf, source, max_pages,
        timeout=_OCR_TIME_BUDGET_S + _OCR_PROCESS_SLACK_S,
        reraise=(OcrUnavailable,),
        slots=_ocr_slots,
    )
    return text, read


# ── Inbound page budget ──────────────────────────────────────────────────────

_inbound_spent: list[tuple[float, int]] = []
_inbound_lock = threading.Lock()


def _take_inbound_pages(wanted: int, per_hour: int) -> int:
    """Reserve up to ``wanted`` pages from the rolling hourly budget and
    return how many were granted (0 when it is spent)."""
    now = time.monotonic()
    with _inbound_lock:
        _inbound_spent[:] = [(t, n) for t, n in _inbound_spent if now - t < _INBOUND_WINDOW_S]
        left = per_hour - sum(n for _t, n in _inbound_spent)
        granted = max(0, min(wanted, left))
        if granted:
            _inbound_spent.append((now, granted))
        return granted


def reset_inbound_budget() -> None:
    """Forget pages spent. Tests call this between cases."""
    with _inbound_lock:
        _inbound_spent.clear()


# ── Public entry point ───────────────────────────────────────────────────────

def _digest(source: bytes | Path) -> str:
    """sha256 of the PDF, read from disk in pieces when given a path."""
    if isinstance(source, bytes):
        return hashlib.sha256(source).hexdigest()
    digest = hashlib.sha256()
    with source.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


async def _cache_if_unchanged(
    key: str, result: PdfReadResult, source: bytes | Path, digest: str
) -> None:
    """Cache ``result`` under ``key``, unless ``source`` is a file that changed
    while it was read: the key is the hash of what was there first, and the
    text may be of what replaced it."""
    if isinstance(source, Path):
        try:
            if await asyncio.to_thread(_digest, source) != digest:
                return
        except OSError:
            return
    _cache_put(key, result)


async def read_pdf_text(
    data: bytes | Path, *, filename: str = "", inbound: bool = False
) -> PdfReadResult:
    """Return the text of a PDF, converting scanned pages when needed.

    ``data`` is the PDF's bytes, or the ``Path`` of a PDF on disk: with a
    path, the parsers read the file themselves and this process never holds
    it whole unless the pages go to the model.

    ``inbound`` marks a file that arrived on its own through a channel (a
    chat, Slack, Google Chat or email attachment) rather than one the
    Executive or the signed-in user asked to read: its conversion gets the
    smaller ``PDF_INBOUND_MAX_PAGES`` cap and draws on the hourly
    ``PDF_INBOUND_PAGES_PER_HOUR`` budget. A text layer is free either way.
    """
    from openexecutive.config import get_settings

    label = filename or "PDF"
    try:
        digest = await asyncio.to_thread(_digest, data)
    except OSError as exc:
        logger.warning("pdf_reader: could not read %s (%s)", label, type(exc).__name__)
        return PdfReadResult("", "none", 0, "the file could not be read")
    key = f"{digest}:{'inbound' if inbound else 'asked'}"
    cached = _cache_get(key)
    if cached is not None:
        return cached

    try:
        text, pages = await asyncio.to_thread(
            run_isolated, _text_layer, data, _MAX_PDF_PAGES,
            timeout=_TEXT_LAYER_TIMEOUT_S, reraise=(PdfTooLarge,),
        )
    except PdfTooLarge as exc:
        return PdfReadResult(
            "", "none", exc.pages,
            f"the PDF has {exc.pages} pages — more than the {_MAX_PDF_PAGES} this reads",
        )
    except WorkerStopped as exc:
        logger.warning("pdf_reader: could not read %s (%s)", label, exc)
        return PdfReadResult(
            "", "none", 0, "the PDF could not be read: it was too large or took too long"
        )
    except Exception as exc:
        logger.warning("pdf_reader: could not open %s (%s)", label, type(exc).__name__)
        return PdfReadResult(
            "", "none", 0, "the file could not be opened as a PDF (it may be damaged or password-protected)"
        )

    if pages and not _is_thin(text, pages):
        result = PdfReadResult(text, "text_layer", pages)
        await _cache_if_unchanged(key, result, data, digest)
        return result

    settings = get_settings()
    limit = min(pages, settings.pdf_vision_max_pages)
    if inbound:
        limit = min(limit, settings.pdf_inbound_max_pages)
        granted = _take_inbound_pages(limit, settings.pdf_inbound_pages_per_hour)
        if limit and not granted:
            return _fallback(
                text, pages,
                "it looks scanned and the hourly budget for converting sent files is used up",
            )
        limit = granted

    transcribed = None
    if limit and settings.pdf_provider_reading:
        pdf = data if isinstance(data, bytes) else await asyncio.to_thread(data.read_bytes)
        transcribed = await _read_with_model(pdf, limit)
        del pdf
    if transcribed:
        transcript, cut_off = transcribed
        note = "; ".join(
            n for n in (
                _pages_note(limit, pages),
                "some pages' transcription was cut off" if cut_off else "",
            ) if n
        )
        result = PdfReadResult(transcript, "model", pages, note)
        await _cache_if_unchanged(key, result, data, digest)
        return result

    if not settings.pdf_ocr_enabled:
        return _fallback(text, pages, "it looks scanned and local OCR is turned off")
    try:
        ocr, read = await asyncio.to_thread(_ocr_isolated, data, limit)
    except OcrUnavailable:
        return _fallback(text, pages, "it looks scanned and OCR is not installed on this server")
    except Exception as exc:
        logger.warning("pdf_reader: OCR failed for %s (%s)", label, type(exc).__name__)
        return _fallback(text, pages, "it looks scanned and OCR could not read it")
    if not ocr.strip():
        return _fallback(text, pages, "it looks scanned and no text could be read from its pages")
    result = PdfReadResult(ocr, "ocr", pages, _pages_note(read, pages))
    await _cache_if_unchanged(key, result, data, digest)
    return result


def _pages_note(read: int, pages: int) -> str:
    return f"only the first {read} of {pages} pages were read" if read < pages else ""


def _fallback(text: str, pages: int, reason: str) -> PdfReadResult:
    """Whatever thin text layer there was, or nothing — never cached, so a
    later call (e.g. after a key is configured) can still convert it."""
    if text.strip():
        return PdfReadResult(text, "text_layer", pages, f"text may be incomplete: {reason}")
    return PdfReadResult("", "none", pages, f"no text could be read: {reason}")
