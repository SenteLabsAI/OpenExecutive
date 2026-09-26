"""knowledge.pdf_reader — reading scanned / image-only PDFs.

The model is stubbed (no network): `_vision_provider` is patched to a fake
with `messages_create`. OCR runs for real in one test (skipped when RapidOCR
is not installed) and is stubbed elsewhere so the rest stay fast.
"""
from __future__ import annotations

import io
from types import SimpleNamespace
from typing import Any

import pytest
from pypdf import PdfReader, PdfWriter

from openexecutive.knowledge import pdf_reader
from openexecutive.knowledge.pdf_reader import PdfReadResult, read_pdf_text


@pytest.fixture(autouse=True)
def _fresh_cache():
    pdf_reader.clear_cache()
    yield
    pdf_reader.clear_cache()


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
                 error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.reply = reply
        self.stop_reason = stop_reason
        self.error = error

    async def messages_create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        prompt = kwargs["messages"][0]["content"][1]["text"]
        pages = prompt.rsplit("These are pages ", 1)[1].split(" of ")[0]
        text = f"{self.reply} [{pages}]" if self.reply else ""
        return SimpleNamespace(
            stop_reason=self.stop_reason,
            content=[SimpleNamespace(type="text", text=text)],
        )


def _use_provider(monkeypatch: pytest.MonkeyPatch, provider: Any) -> None:
    monkeypatch.setattr(pdf_reader, "_vision_provider", lambda model: provider)


def _stub_ocr(monkeypatch: pytest.MonkeyPatch, text: str = "--- page 1 ---\nOCR TEXT") -> list:
    calls: list = []

    def fake(data: bytes, max_pages: int) -> str:
        calls.append(max_pages)
        return text

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

    assert result == PdfReadResult("Revenue 4.2M [1 to 2]", "vision", 2)
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

    assert result.method == "vision"
    assert result.text == "part [1 to 20]\n\npart [21 to 40]\n\npart [41 to 45]"
    assert sorted(_pages_in(c) for c in provider.calls) == [5, 20, 20]


async def test_pages_past_the_cap_are_skipped_with_a_note(monkeypatch):
    monkeypatch.setenv("PDF_VISION_MAX_PAGES", "10")
    provider = _FakeProvider()
    _use_provider(monkeypatch, provider)

    result = await read_pdf_text(_blank_pdf(25), filename="scan.pdf")

    assert result.method == "vision"
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


def test_openrouter_and_missing_keys_get_no_vision_provider(monkeypatch):
    """The OpenRouter / local translator drops `document` blocks, so only the
    Anthropic-direct provider may be handed the PDF."""
    from openexecutive.providers import registry
    from openexecutive.providers.anthropic_provider import AnthropicProvider

    registry._reset_for_tests()
    try:
        assert isinstance(pdf_reader._vision_provider("claude-sonnet-5"), AnthropicProvider)

        registry._reset_for_tests()
        monkeypatch.setenv("OPENROUTER_ENABLED", "true")
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        assert pdf_reader._vision_provider("claude-sonnet-5") is None
        assert pdf_reader._vision_provider("openai/gpt-6-astra") is None
    finally:
        registry._reset_for_tests()


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
    assert (await read_pdf_text(data)).method == "vision"


# ── Real OCR ─────────────────────────────────────────────────────────────────


def test_ocr_reads_an_image_only_pdf():
    pytest.importorskip("rapidocr_onnxruntime")
    pytest.importorskip("pypdfium2")
    data = _image_pdf(["Board meeting notes", "Approve the hiring plan"])
    assert PdfReader(io.BytesIO(data)).pages[0].extract_text() == ""

    text = pdf_reader._ocr_pdf(data, max_pages=5)

    assert text.startswith("--- page 1 ---")
    assert "Board meeting notes" in text
    assert "Approve the hiring plan" in text
