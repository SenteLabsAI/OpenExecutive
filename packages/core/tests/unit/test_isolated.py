"""knowledge.isolated — document parsing in a short-lived child process.

These run the real child (``python -m openexecutive.knowledge.isolated``):
the reads must come back exactly as they do in-process, and a child that
fails, dies or hangs must surface as an error the readers already handle,
never as a hung or crashed API process.
"""
from __future__ import annotations

import io
import logging
import math
import os
import time
import tracemalloc
from pathlib import Path

import pytest

from openexecutive.knowledge import isolated, pdf_reader
from openexecutive.knowledge.isolated import IsolatedError, ParserBusy, WorkerStopped, run_isolated
from openexecutive.knowledge.loader import _parse_file, extract_text_from_file

from ._isolated_helpers import echo, flood_stderr_and_die
from .test_pdf_reader import _blank_pdf, _image_pdf, _text_pdf


@pytest.fixture(autouse=True)
def _fresh_cache():
    pdf_reader.clear_cache()
    pdf_reader.reset_inbound_budget()
    yield
    pdf_reader.clear_cache()
    pdf_reader.reset_inbound_budget()


# ── The mechanism ────────────────────────────────────────────────────────────


def test_a_result_comes_back_from_the_child() -> None:
    assert run_isolated(math.sqrt, 16.0, timeout=60) == 4.0


def test_the_child_does_not_inherit_the_deployments_keys(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")

    assert run_isolated(os.getenv, "ANTHROPIC_API_KEY", timeout=60) is None


def test_a_named_exception_is_raised_again_as_itself() -> None:
    with pytest.raises(ValueError, match="math domain error"):
        run_isolated(math.sqrt, -1.0, timeout=60, reraise=(ValueError,))


def test_any_other_exception_is_an_isolated_error() -> None:
    with pytest.raises(IsolatedError, match="ValueError: math domain error") as info:
        run_isolated(math.sqrt, -1.0, timeout=60)
    assert not isinstance(info.value, WorkerStopped)
    # What callers log: the type, never the message (it can quote the file).
    assert info.value.kind == "ValueError"


def test_a_child_that_dies_without_answering_is_reported() -> None:
    with pytest.raises(WorkerStopped, match="exit code 3"):
        run_isolated(os._exit, 3, timeout=60)


def test_a_child_past_its_timeout_is_killed() -> None:
    started = time.monotonic()
    with pytest.raises(WorkerStopped, match="timed out"):
        run_isolated(time.sleep, 30, timeout=1)
    assert time.monotonic() - started < 20


def test_non_ascii_text_comes_back_intact() -> None:
    text = "café — 30 % growth, 日本"
    assert run_isolated(echo, text, timeout=60) == text


def test_lone_surrogates_come_back_as_they_did_in_process() -> None:
    """pypdf decodes some character maps with surrogatepass, so extracted
    text can hold a lone surrogate; strict UTF-8 would lose the whole file."""
    assert run_isolated(echo, "page\ud800text", timeout=60) == "page\ud800text"


def test_an_answer_past_the_limit_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(isolated, "_MAX_ANSWER_BYTES", 100)

    with pytest.raises(WorkerStopped, match="past the limit"):
        run_isolated(echo, "x" * 200, timeout=60)


def test_a_flood_on_stderr_is_never_held_in_memory(monkeypatch) -> None:
    """Review finding: stderr used to be captured whole. A child that writes
    40 MB to it must cost this process the logged tail, not 40 MB."""
    from unittest.mock import MagicMock

    log = MagicMock()
    monkeypatch.setattr(isolated, "logger", log)
    tracemalloc.start()
    try:
        with pytest.raises(WorkerStopped, match="exit code 5"):
            run_isolated(flood_stderr_and_die, 40, timeout=60)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < 2 * 1024 * 1024
    (call,) = log.warning.call_args_list
    tail = call.args[-1]
    assert tail == "x" * isolated._MAX_STDERR_LOGGED


def _log_flood_pdf(pages: int = 5, fonts: int = 300, name_len: int = 4000) -> bytes:
    """A 9 KB PDF that makes pypdf log ~6 MB: every page shares a font dict
    whose fonts all carry a 4000-character /Encoding name, and pypdf logs
    that name as an error for each font on each page."""
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, StreamObject

    writer = PdfWriter()
    font = writer._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
        NameObject("/Encoding"): NameObject("/" + "A" * name_len),
    }))
    font_dict = writer._add_object(
        DictionaryObject({NameObject(f"/F{i}"): font for i in range(fonts)})
    )
    for _ in range(pages):
        page = writer.add_blank_page(612, 792)
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): font_dict})
        content = StreamObject()
        content.set_data(b"BT /F0 12 Tf 72 720 Td (Quarterly plan) Tj ET")
        page[NameObject("/Contents")] = writer._add_object(content)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def test_a_pdf_that_floods_the_parser_log_costs_this_process_nothing() -> None:
    data = _log_flood_pdf()
    log = io.StringIO()
    handler = logging.StreamHandler(log)
    logging.getLogger("pypdf").addHandler(handler)
    try:
        in_process = pdf_reader._text_layer(data)
    finally:
        logging.getLogger("pypdf").removeHandler(handler)
    assert len(log.getvalue()) > 5_000_000, "the fixture must actually flood the log"

    tracemalloc.start()
    try:
        isolated_result = run_isolated(pdf_reader._text_layer, data, timeout=120)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert tuple(isolated_result) == in_process
    assert peak < 2 * 1024 * 1024


def test_out_of_memory_at_spawn_is_reported_not_parsed_in_process(monkeypatch) -> None:
    """Review finding: falling back in-process on ENOMEM / EAGAIN would put
    the parse in the API exactly when memory is shortest."""
    import errno

    def no_memory(*_a: object, **_k: object) -> None:
        raise OSError(errno.ENOMEM, "Cannot allocate memory")

    monkeypatch.setattr(isolated.subprocess, "run", no_memory)

    with pytest.raises(WorkerStopped, match="could not start a parser process"):
        run_isolated(math.sqrt, 9.0, timeout=60)


def test_children_wait_for_a_free_slot(monkeypatch) -> None:
    import threading

    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(isolated, "_child_slots", slots)

    # A slot is given back after each call, so calls in turn all run.
    assert run_isolated(math.sqrt, 4.0, timeout=60) == 2.0
    assert run_isolated(math.sqrt, 9.0, timeout=60) == 3.0

    monkeypatch.setattr(isolated, "_MAX_SLOT_WAIT_S", 0.2)
    slots.acquire()
    try:
        started = time.monotonic()
        with pytest.raises(ParserBusy, match="no free parser slot"):
            run_isolated(math.sqrt, 16.0, timeout=60)
        # Review finding: two slow files must not hold every other parse
        # for its whole timeout; the wait is short, not the caller's 60 s.
        assert time.monotonic() - started < 5
    finally:
        slots.release()


def test_the_wait_for_a_slot_counts_against_the_timeout(monkeypatch) -> None:
    """A caller's limit covers the wait and the parse together, so Drive
    sync's 120 s still kills the child when its wait_for gives up."""
    import threading

    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(isolated, "_child_slots", slots)
    slots.acquire()
    threading.Timer(1.0, slots.release).start()

    started = time.monotonic()
    with pytest.raises(WorkerStopped, match="timed out") as info:
        run_isolated(time.sleep, 30, timeout=3.0)
    assert not isinstance(info.value, ParserBusy)
    assert time.monotonic() - started < 6


def test_the_in_process_fallback_does_not_hold_a_slot(monkeypatch) -> None:
    """An in-process parse cannot be timed out; a hung one must not keep a
    slot the child processes need."""
    import threading

    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(isolated, "_child_slots", slots)
    monkeypatch.setattr(isolated.sys, "executable", "/nonexistent/python")

    def slot_is_free() -> bool:
        if slots.acquire(blocking=False):
            slots.release()
            return True
        return False

    assert run_isolated(slot_is_free, timeout=60) is True


def test_a_caller_with_its_own_gate_does_not_wait_on_the_shared_one(monkeypatch) -> None:
    """OCR brings its own gate, so a long OCR run never holds a slot the
    quick text-layer parses need, and they never hold one OCR needs."""
    import threading

    shared = threading.BoundedSemaphore(1)
    monkeypatch.setattr(isolated, "_child_slots", shared)
    shared.acquire()
    try:
        own = threading.BoundedSemaphore(1)
        assert run_isolated(math.sqrt, 25.0, timeout=60, slots=own) == 5.0
    finally:
        shared.release()


def test_without_a_child_process_it_parses_in_process(monkeypatch) -> None:
    monkeypatch.setattr(isolated.sys, "executable", "/nonexistent/python")

    assert run_isolated(math.sqrt, 9.0, timeout=60) == 3.0


# ── PDFs: same reads as in-process ───────────────────────────────────────────


async def test_a_text_layer_pdf_reads_the_same_as_in_process(tmp_path: Path, monkeypatch) -> None:
    data = _text_pdf("Revenue grew 30 percent year over year in every region we serve")
    path = tmp_path / "report.pdf"
    path.write_bytes(data)

    from_path = await pdf_reader.read_pdf_text(path, filename="report.pdf")
    pdf_reader.clear_cache()
    from_bytes = await pdf_reader.read_pdf_text(data, filename="report.pdf")
    pdf_reader.clear_cache()
    monkeypatch.setattr(isolated, "enabled", False)
    in_process = await pdf_reader.read_pdf_text(data, filename="report.pdf")

    assert from_path == from_bytes == in_process
    assert from_path.method == "text_layer"
    assert "Revenue grew 30 percent" in from_path.text


async def test_a_damaged_pdf_is_reported_through_the_child(tmp_path: Path) -> None:
    path = tmp_path / "bad.pdf"
    path.write_bytes(b"%PDF-1.4 not really a pdf")

    result = await pdf_reader.read_pdf_text(path, filename="bad.pdf")

    assert result.method == "none"
    assert "could not be opened" in result.note


async def test_the_page_ceiling_holds_in_the_child(monkeypatch) -> None:
    """The ceiling is passed to the child, which never sees a patched module."""
    monkeypatch.setattr(pdf_reader, "_MAX_PDF_PAGES", 5)

    result = await pdf_reader.read_pdf_text(_blank_pdf(8), filename="huge.pdf")

    assert result == pdf_reader.PdfReadResult(
        "", "none", 8, "the PDF has 8 pages — more than the 5 this reads"
    )


async def test_a_reader_that_runs_out_of_time_never_raises(monkeypatch) -> None:
    monkeypatch.setattr(pdf_reader, "_TEXT_LAYER_TIMEOUT_S", 0.001)

    result = await pdf_reader.read_pdf_text(_text_pdf("Quarterly plan"), filename="slow.pdf")

    assert result.method == "none"
    assert result.note == "the PDF could not be read: it was too large or took too long"


async def test_a_missing_file_is_reported_not_raised(tmp_path: Path) -> None:
    result = await pdf_reader.read_pdf_text(tmp_path / "gone.pdf", filename="gone.pdf")

    assert result.method == "none"
    assert result.note == "the file could not be read"


async def test_a_scan_is_ocrd_in_the_child(tmp_path: Path, monkeypatch) -> None:
    pytest.importorskip("rapidocr_onnxruntime")
    pytest.importorskip("pypdfium2")
    # test_pdf_reader.py loads the engine in-process on purpose; start clean
    # so the check below sees only what this read did.
    monkeypatch.setattr(pdf_reader, "_ocr_engine", None)
    monkeypatch.setenv("PDF_PROVIDER_READING", "false")
    monkeypatch.setenv("PDF_OCR_ENABLED", "true")
    path = tmp_path / "scan.pdf"
    path.write_bytes(_image_pdf(["Board meeting notes", "Approve the hiring plan"]))

    result = await pdf_reader.read_pdf_text(path, filename="scan.pdf")

    assert result.method == "ocr"
    assert "Board meeting notes" in result.text
    assert "Approve the hiring plan" in result.text
    # The OCR model was loaded in the child, not here.
    assert pdf_reader._ocr_engine is None


# ── Word / Excel ─────────────────────────────────────────────────────────────


def test_a_docx_reads_the_same_as_in_process(tmp_path: Path) -> None:
    from docx import Document

    doc = Document()
    doc.add_paragraph("Hiring plan")
    doc.add_paragraph("")
    doc.add_paragraph("Two engineers in Q3, one designer in Q4.")
    path = tmp_path / "plan.docx"
    doc.save(str(path))

    text = extract_text_from_file(path)

    assert text == _parse_file(path)
    assert text == "Hiring plan\n\nTwo engineers in Q3, one designer in Q4."


def test_a_corrupt_docx_raises_naming_the_parser_error(tmp_path: Path) -> None:
    path = tmp_path / "broken.docx"
    path.write_bytes(b"not a zip archive")

    with pytest.raises(IsolatedError, match="PackageNotFoundError"):
        extract_text_from_file(path)


def test_what_a_parser_prints_does_not_spoil_the_answer() -> None:
    """Output a library writes to stdout goes to stderr, not into the JSON."""
    assert run_isolated(print, "noise from a parser", timeout=60) is None


async def test_a_pdf_met_by_busy_parsers_says_so_and_is_not_cached(monkeypatch) -> None:
    import threading

    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(isolated, "_child_slots", slots)
    monkeypatch.setattr(isolated, "_MAX_SLOT_WAIT_S", 0.1)
    data = _text_pdf("Hire two engineers in the third quarter")

    slots.acquire()
    try:
        busy = await pdf_reader.read_pdf_text(data, filename="plan.pdf")
    finally:
        slots.release()
    read = await pdf_reader.read_pdf_text(data, filename="plan.pdf")

    assert busy.method == "none"
    assert "busy reading other documents" in busy.note
    assert read.method == "text_layer"
