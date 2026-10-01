"""knowledge.pdf_reader — reading scanned / image-only PDFs.

The model is stubbed (no network): `_model_provider` is patched to a fake
with `messages_create`. OCR runs for real in one test (skipped when RapidOCR
is not installed) and is stubbed elsewhere so the rest stay fast.

The parsers run in-process here (``isolated.enabled = False``) so the
monkeypatches below reach them; ``test_isolated.py`` covers the same reads
through the real child process.
"""
from __future__ import annotations

import io
from types import SimpleNamespace
from typing import Any

import pytest
from pypdf import PdfReader, PdfWriter

from openexecutive.knowledge import isolated, pdf_reader
from openexecutive.knowledge.pdf_reader import PdfReadResult, read_pdf_text


@pytest.fixture(autouse=True)
def _provider_reading_on(monkeypatch):
    """Provider reading is opt-in (off by default); most tests here exercise
    that path, so they turn it on. The default is pinned by its own test."""
    monkeypatch.setenv("PDF_PROVIDER_READING", "true")


@pytest.fixture(autouse=True)
def _parse_in_process(monkeypatch):
    monkeypatch.setattr(isolated, "enabled", False)


@pytest.fixture(autouse=True)
def _fresh_cache():
    pdf_reader.clear_cache()
    pdf_reader.reset_inbound_budget()
    yield
    pdf_reader.clear_cache()
    pdf_reader.reset_inbound_budget()


# ── PDF builders ─────────────────────────────────────────────────────────────


def _blank_pdf(pages: int = 1) -> bytes:
    """Pages with no text layer — what a scan looks like to pypdf."""
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _text_pdf(text: str) -> bytes:
    """A one-page PDF with a real text layer (Helvetica, one line)."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(
        b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
        % (len(objects) + 1, xref)
    )
    return out.getvalue()


def _image_pdf(lines: list[str]) -> bytes:
    """An image-only PDF with ``lines`` drawn on it, like a scanned page."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (1240, 1754), "white")
    draw = ImageDraw.Draw(img)
    try:
        font: Any = ImageFont.truetype("DejaVuSans.ttf", 44)
    except OSError:
        font = ImageFont.load_default(size=44)
    for i, line in enumerate(lines):
        draw.text((100, 150 + i * 110), line, fill="black", font=font)
    out = io.BytesIO()
    img.save(out, format="PDF", resolution=150)
    return out.getvalue()


# ── Fakes ────────────────────────────────────────────────────────────────────


class _FakeProvider:
    """Records each request and answers with one text block per call."""

    def __init__(self, *, reply: str = "TRANSCRIBED", stop_reason: str = "end_turn",
                 error: Exception | None = None, fits_pages: int | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.reply = reply
        self.stop_reason = stop_reason
        self.error = error
        # A slice of more pages than this runs into max_tokens.
        self.fits_pages = fits_pages

    async def messages_create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        prompt = kwargs["messages"][0]["content"][1]["text"]
        pages = prompt.rsplit("These are pages ", 1)[1].split(" of ")[0]
        text = f"{self.reply} [{pages}]" if self.reply else ""
        stop_reason = self.stop_reason
        if self.fits_pages is not None and _pages_in(kwargs) > self.fits_pages:
            stop_reason = "max_tokens"
        return SimpleNamespace(
            stop_reason=stop_reason,
            content=[SimpleNamespace(type="text", text=text)],
        )


def _use_provider(monkeypatch: pytest.MonkeyPatch, provider: Any) -> None:
    monkeypatch.setattr(pdf_reader, "_model_provider", lambda model: provider)


def _stub_ocr(monkeypatch: pytest.MonkeyPatch, text: str = "--- page 1 ---\nOCR TEXT") -> list:
    calls: list = []

    def fake(data: bytes, max_pages: int) -> tuple[str, int]:
        calls.append(max_pages)
        return text, max_pages

    monkeypatch.setattr(pdf_reader, "_ocr_pdf", fake)
    return calls


def _pages_in(call: dict[str, Any]) -> int:
    block = call["messages"][0]["content"][0]
    import base64

    return len(PdfReader(io.BytesIO(base64.b64decode(block["source"]["data"]))).pages)


# ── Text layer ───────────────────────────────────────────────────────────────


async def test_pdf_with_a_text_layer_is_read_without_a_model_call(monkeypatch):
    provider = _FakeProvider()
    _use_provider(monkeypatch, provider)
    ocr = _stub_ocr(monkeypatch)
    body = "Quarterly revenue grew twenty three percent against plan this year"

    result = await read_pdf_text(_text_pdf(body), filename="report.pdf")

    assert result.method == "text_layer"
    assert body in result.text
    assert not result.converted
    assert provider.calls == [] and ocr == []


# ── Claude (document blocks) ─────────────────────────────────────────────────


async def test_scanned_pdf_is_transcribed_from_a_document_block(monkeypatch):
    provider = _FakeProvider(reply="Revenue 4.2M")
    _use_provider(monkeypatch, provider)
    ocr = _stub_ocr(monkeypatch)

    result = await read_pdf_text(_blank_pdf(2), filename="scan.pdf")

    assert result == PdfReadResult("Revenue 4.2M [1 to 2]", "model", 2)
    assert result.converted
    assert ocr == []
    (call,) = provider.calls
    document, instruction = call["messages"][0]["content"]
    assert document["type"] == "document"
    assert document["source"]["type"] == "base64"
    assert document["source"]["media_type"] == "application/pdf"
    assert instruction["type"] == "text"
    assert _pages_in(call) == 2


async def test_long_scan_is_sent_in_slices_and_joined_in_order(monkeypatch):
    monkeypatch.setenv("PDF_VISION_PAGES_PER_CALL", "20")
    provider = _FakeProvider(reply="part")
    _use_provider(monkeypatch, provider)

    result = await read_pdf_text(_blank_pdf(45), filename="scan.pdf")

    assert result.method == "model"
    assert result.text == "part [1 to 20]\n\npart [21 to 40]\n\npart [41 to 45]"
    assert sorted(_pages_in(c) for c in provider.calls) == [5, 20, 20]


async def test_pages_past_the_cap_are_skipped_with_a_note(monkeypatch):
    monkeypatch.setenv("PDF_VISION_MAX_PAGES", "10")
    provider = _FakeProvider()
    _use_provider(monkeypatch, provider)

    result = await read_pdf_text(_blank_pdf(25), filename="scan.pdf")

    assert result.method == "model"
    assert result.pages == 25
    assert result.note == "only the first 10 of 25 pages were read"
    assert sum(_pages_in(c) for c in provider.calls) == 10


# ── Non-Claude deployments and failures fall back to OCR ─────────────────────


async def test_without_a_claude_provider_the_pages_are_ocrd(monkeypatch):
    _use_provider(monkeypatch, None)
    ocr = _stub_ocr(monkeypatch)

    result = await read_pdf_text(_blank_pdf(3), filename="scan.pdf")

    assert result == PdfReadResult("--- page 1 ---\nOCR TEXT", "ocr", 3)
    assert ocr == [3]


@pytest.mark.parametrize(
    "provider",
    [
        _FakeProvider(error=RuntimeError("boom")),
        _FakeProvider(stop_reason="refusal"),
        _FakeProvider(reply=""),
    ],
    ids=["error", "refusal", "empty"],
)
async def test_a_failed_transcription_falls_back_to_ocr(monkeypatch, provider):
    _use_provider(monkeypatch, provider)
    _stub_ocr(monkeypatch)

    result = await read_pdf_text(_blank_pdf(), filename="scan.pdf")

    assert result.method == "ocr"
    assert provider.calls


def test_each_deployment_gets_its_own_pdf_reader(monkeypatch):
    """The PDF goes to whatever provider the model routes to — Anthropic,
    OpenRouter, or a local server that says it takes PDFs — and only a model
    with no way to receive one falls to local OCR."""
    from openexecutive.providers import registry
    from openexecutive.providers.anthropic_provider import AnthropicProvider
    from openexecutive.providers.openai_compatible import OpenAICompatibleProvider
    from openexecutive.providers.openrouter_provider import OpenRouterProvider

    registry._reset_for_tests()
    try:
        assert isinstance(pdf_reader._model_provider("claude-sonnet-5"), AnthropicProvider)

        registry._reset_for_tests()
        monkeypatch.setenv("OPENROUTER_ENABLED", "true")
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        assert isinstance(pdf_reader._model_provider("claude-sonnet-5"), OpenRouterProvider)
        assert isinstance(pdf_reader._model_provider("openai/gpt-6-astra"), OpenRouterProvider)

        registry._reset_for_tests()
        monkeypatch.setenv("LOCAL_MODELS_ENABLED", "true")
        monkeypatch.setenv("LOCAL_BASE_URL", "http://localhost:11434/v1")
        monkeypatch.setenv("LOCAL_MODELS", "llama3.3")
        assert pdf_reader._model_provider("llama3.3") is None
        monkeypatch.setenv("LOCAL_PDF_INPUT", "true")
        assert isinstance(pdf_reader._model_provider("llama3.3"), OpenAICompatibleProvider)
    finally:
        registry._reset_for_tests()


def test_the_pdf_model_defaults_to_the_deployments_model(monkeypatch):
    monkeypatch.delenv("PDF_VISION_MODEL", raising=False)
    monkeypatch.setenv("DEFAULT_MODEL", "openai/gpt-6-astra")
    assert pdf_reader._pdf_model() == "openai/gpt-6-astra"
    monkeypatch.setenv("PDF_VISION_MODEL", "claude-haiku-4-5")
    assert pdf_reader._pdf_model() == "claude-haiku-4-5"


async def test_ocr_not_installed_says_so(monkeypatch):
    _use_provider(monkeypatch, None)

    def unavailable() -> Any:
        raise pdf_reader.OcrUnavailable("no module")

    monkeypatch.setattr(pdf_reader, "_get_ocr_engine", unavailable)

    result = await read_pdf_text(_blank_pdf(), filename="scan.pdf")

    assert result.method == "none"
    assert result.text == ""
    assert "OCR is not installed" in result.note


async def test_ocr_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("PDF_OCR_ENABLED", "false")
    _use_provider(monkeypatch, None)
    ocr = _stub_ocr(monkeypatch)

    result = await read_pdf_text(_blank_pdf(), filename="scan.pdf")

    assert result.method == "none"
    assert "turned off" in result.note
    assert ocr == []


async def test_a_damaged_pdf_is_reported_not_raised(monkeypatch):
    _use_provider(monkeypatch, _FakeProvider())

    result = await read_pdf_text(b"%PDF-1.4 not really a pdf", filename="bad.pdf")

    assert result.method == "none"
    assert "could not be opened" in result.note


# ── Cache ────────────────────────────────────────────────────────────────────


async def test_the_same_pdf_is_converted_once(monkeypatch):
    provider = _FakeProvider()
    _use_provider(monkeypatch, provider)
    data = _blank_pdf()

    first = await read_pdf_text(data, filename="a.pdf")
    second = await read_pdf_text(data, filename="a.pdf")

    assert first == second
    assert len(provider.calls) == 1


async def test_an_unreadable_result_is_not_cached(monkeypatch):
    """A failure (no key yet, OCR off) must not stick once it is fixed."""
    monkeypatch.setenv("PDF_OCR_ENABLED", "false")
    _use_provider(monkeypatch, None)
    data = _blank_pdf()
    assert (await read_pdf_text(data)).method == "none"

    _use_provider(monkeypatch, _FakeProvider())
    assert (await read_pdf_text(data)).method == "model"


# ── Real OCR ─────────────────────────────────────────────────────────────────


def test_ocr_reads_an_image_only_pdf():
    pytest.importorskip("rapidocr_onnxruntime")
    pytest.importorskip("pypdfium2")
    data = _image_pdf(["Board meeting notes", "Approve the hiring plan"])
    assert PdfReader(io.BytesIO(data)).pages[0].extract_text() == ""

    text, read = pdf_reader._ocr_pdf(data, max_pages=5)

    assert read == 1
    assert text.startswith("--- page 1 ---")
    assert "Board meeting notes" in text
    assert "Approve the hiring plan" in text


# ── Resource bounds ──────────────────────────────────────────────────────────


def test_a_huge_page_is_rendered_within_the_pixel_budget():
    """A page's size is whatever its MediaBox says; a 20000pt-square page at
    the normal scale would allocate gigabytes."""
    assert pdf_reader._render_scale(612, 792) == pdf_reader._OCR_RENDER_SCALE
    scale = pdf_reader._render_scale(20_000, 20_000)
    assert (20_000 * scale) ** 2 <= pdf_reader._OCR_MAX_PAGE_PIXELS * 1.0001


def test_ocr_renders_a_huge_page_at_the_capped_scale(monkeypatch):
    pytest.importorskip("pypdfium2")
    writer = PdfWriter()
    writer.add_blank_page(width=14_000, height=14_000)
    buf = io.BytesIO()
    writer.write(buf)
    sizes: list[tuple[int, int]] = []

    class Engine:
        def __call__(self, image: Any) -> tuple[None, None]:
            sizes.append(image.shape[:2])
            return None, None

    monkeypatch.setattr(pdf_reader, "_get_ocr_engine", lambda: Engine())

    assert pdf_reader._ocr_pdf(buf.getvalue(), max_pages=1) == ("", 1)
    (height, width), = sizes
    assert height * width <= pdf_reader._OCR_MAX_PAGE_PIXELS * 1.001


def test_ocr_stops_at_its_time_budget(monkeypatch):
    pytest.importorskip("pypdfium2")
    monkeypatch.setattr(pdf_reader, "_OCR_TIME_BUDGET_S", -1.0)
    monkeypatch.setattr(pdf_reader, "_get_ocr_engine", lambda: object())

    assert pdf_reader._ocr_pdf(_blank_pdf(5), max_pages=5) == ("", 0)


async def test_inbound_files_get_the_smaller_page_cap(monkeypatch):
    monkeypatch.setenv("PDF_INBOUND_MAX_PAGES", "4")
    _use_provider(monkeypatch, None)
    ocr = _stub_ocr(monkeypatch)
    data = _blank_pdf(10)

    inbound = await read_pdf_text(data, inbound=True)
    asked = await read_pdf_text(data)

    assert ocr == [4, 10]
    assert inbound.note == "only the first 4 of 10 pages were read"
    assert asked.note == ""


async def test_inbound_conversions_share_an_hourly_page_budget(monkeypatch):
    monkeypatch.setenv("PDF_INBOUND_PAGES_PER_HOUR", "5")
    _use_provider(monkeypatch, None)
    ocr = _stub_ocr(monkeypatch)

    first = await read_pdf_text(_blank_pdf(3), inbound=True)
    second = await read_pdf_text(_blank_pdf(4), inbound=True)
    third = await read_pdf_text(_blank_pdf(2), inbound=True)
    asked = await read_pdf_text(_blank_pdf(2))

    assert ocr == [3, 2, 2]
    assert first.method == second.method == "ocr"
    assert second.note == "only the first 2 of 4 pages were read"
    assert third.method == "none" and "hourly budget" in third.note
    assert asked.method == "ocr"


# ── Review findings: page-tree ceiling, cut-off transcriptions ───────────────


async def test_a_pdf_past_the_page_ceiling_is_refused_before_parsing(monkeypatch):
    """The page caps bound conversion only; the text-layer pass would
    otherwise extract every page of a crafted many-thousand-page file."""
    from pypdf import PageObject

    monkeypatch.setattr(pdf_reader, "_MAX_PDF_PAGES", 5)

    def never(*_a: Any, **_k: Any) -> str:
        raise AssertionError("no page may be parsed past the ceiling")

    monkeypatch.setattr(PageObject, "extract_text", never)
    provider = _FakeProvider()
    _use_provider(monkeypatch, provider)

    result = await read_pdf_text(_blank_pdf(8), filename="huge.pdf")

    assert result == PdfReadResult(
        "", "none", 8, "the PDF has 8 pages — more than the 5 this reads"
    )
    assert provider.calls == []


async def test_a_cut_off_slice_is_split_until_each_part_fits(monkeypatch):
    monkeypatch.setenv("PDF_VISION_PAGES_PER_CALL", "20")
    provider = _FakeProvider(reply="part", fits_pages=5)
    _use_provider(monkeypatch, provider)

    result = await read_pdf_text(_blank_pdf(20), filename="dense.pdf")

    assert result.method == "model"
    assert result.note == ""
    assert result.text == (
        "part [1 to 5]\n\npart [6 to 10]\n\npart [11 to 15]\n\npart [16 to 20]"
    )
    # 20 -> 10 + 10 -> four slices of 5: seven requests in all.
    assert sorted(_pages_in(c) for c in provider.calls) == [5, 5, 5, 5, 10, 10, 20]


async def test_a_single_page_that_never_fits_is_kept_and_flagged(monkeypatch):
    provider = _FakeProvider(reply="dense page", stop_reason="max_tokens")
    _use_provider(monkeypatch, provider)

    result = await read_pdf_text(_blank_pdf(2), filename="dense.pdf")

    assert result.method == "model"
    assert result.text == (
        "dense page [1 to 1]\n[transcription cut off here]\n\n"
        "dense page [2 to 2]\n[transcription cut off here]"
    )
    assert result.note == "some pages' transcription was cut off"


# ── Keeping scanned PDFs on the server ───────────────────────────────────────


async def test_provider_reading_off_keeps_scans_on_this_server(monkeypatch):
    monkeypatch.setenv("PDF_PROVIDER_READING", "false")
    provider = _FakeProvider()
    _use_provider(monkeypatch, provider)
    ocr = _stub_ocr(monkeypatch)

    result = await read_pdf_text(_blank_pdf(2), filename="scan.pdf")

    assert result.method == "ocr"
    assert provider.calls == []
    assert ocr == [2]


async def test_provider_reading_is_off_by_default(monkeypatch):
    """An upgrade must not start sending scanned PDFs off the server."""
    monkeypatch.delenv("PDF_PROVIDER_READING", raising=False)
    provider = _FakeProvider()
    _use_provider(monkeypatch, provider)
    ocr = _stub_ocr(monkeypatch)

    result = await read_pdf_text(_blank_pdf(2), filename="scan.pdf")

    assert result.method == "ocr"
    assert provider.calls == []
    assert ocr == [2]


async def test_where_scans_go_is_logged_once(monkeypatch):
    from unittest.mock import MagicMock

    monkeypatch.setattr(pdf_reader, "_announced", set())
    log = MagicMock()
    monkeypatch.setattr(pdf_reader, "logger", log)
    _use_provider(monkeypatch, _FakeProvider())

    await read_pdf_text(_blank_pdf(1), filename="a.pdf")
    await read_pdf_text(_blank_pdf(2), filename="b.pdf")

    lines = [c.args[0] % c.args[1:] for c in log.info.call_args_list]
    assert len([line for line in lines if "scanned PDFs are sent to" in line]) == 1
    assert "PDF_PROVIDER_READING is on" in lines[0]
