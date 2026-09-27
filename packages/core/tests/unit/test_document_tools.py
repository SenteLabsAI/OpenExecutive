"""orchestrator.document_tools — the Executive's `read_document` tool."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from openexecutive.knowledge import pdf_reader
from openexecutive.orchestrator.document_tools import (
    READ_DOCUMENT_TOOL,
    handle_read_document,
)


@pytest.fixture()
def dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """(download dir, company docs dir), both empty."""
    downloads = tmp_path / "attachments"
    downloads.mkdir()
    company = tmp_path / "company"
    (company / "docs").mkdir(parents=True)
    monkeypatch.setenv("WORKFLOW_FILE_DIRS", str(downloads))
    monkeypatch.setenv("COMPANY_PROFILE_PATH", str(company / "profile.yaml"))
    return downloads, company / "docs"


@pytest.fixture()
def fake_pdf(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    """Stub the converter: every PDF reads as scanned pages converted to
    'PAGE TEXT (<n> bytes)'. Returns the bytes it was handed."""
    seen: list[bytes] = []

    async def fake_read(data: bytes, *, filename: str = "", inbound: bool = False) -> pdf_reader.PdfReadResult:
        seen.append(data)
        return pdf_reader.PdfReadResult(f"PAGE TEXT ({len(data)} bytes)", "ocr", 1)

    monkeypatch.setattr(pdf_reader, "read_pdf_text", fake_read)
    return seen


def _error(out: str) -> str:
    return json.loads(out)["error"]


async def test_reads_a_downloaded_scanned_pdf(dirs, fake_pdf) -> None:
    downloads, _ = dirs
    (downloads / "appraisal.pdf").write_bytes(b"%PDF-scan")

    out = await handle_read_document({"path": str(downloads / "appraisal.pdf")})

    assert out == "[appraisal.pdf, converted from scanned pages]\nPAGE TEXT (9 bytes)"
    assert fake_pdf == [b"%PDF-scan"]


async def test_reads_a_company_document_by_name(dirs) -> None:
    _, docs = dirs
    (docs / "plan.md").write_text("# Plan\nHire two engineers.")

    out = await handle_read_document({"filename": "plan.md"})

    assert out == "[plan.md]\n# Plan\nHire two engineers."


async def test_refuses_files_outside_the_allowed_folders(dirs, tmp_path: Path) -> None:
    downloads, _ = dirs
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET")
    link = downloads / "innocent.txt"
    link.symlink_to(secret)

    for path in (str(secret), str(downloads / ".." / "secret.txt"), str(link)):
        out = await handle_read_document({"path": path})
        assert "outside the folders" in _error(out) and "TOP SECRET" not in out


async def test_a_company_filename_cannot_climb_out_of_the_docs_folder(dirs, tmp_path) -> None:
    (tmp_path / "secret.txt").write_text("TOP SECRET")

    for name in ("../../secret.txt", "../secret.txt", ".env"):
        out = await handle_read_document({"filename": name})
        assert "bare document name" in _error(out) and "TOP SECRET" not in out


async def test_needs_a_path_or_a_filename(dirs) -> None:
    assert "pass `path`" in _error(await handle_read_document({}))


async def test_reads_a_page_range_of_a_pdf(dirs, fake_pdf) -> None:
    import io

    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for _ in range(30):
        writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    downloads, _ = dirs
    (downloads / "deck.pdf").write_bytes(buf.getvalue())

    out = await handle_read_document({"path": str(downloads / "deck.pdf"), "pages": "11-20"})

    assert out.startswith("[deck.pdf, pages 11-20 of 30, converted from scanned pages]")
    (part,) = fake_pdf
    assert len(PdfReader(io.BytesIO(part)).pages) == 10

    past_end = await handle_read_document({"path": str(downloads / "deck.pdf"), "pages": "31"})
    assert _error(past_end) == "that PDF has only 30 pages"
    bad = await handle_read_document({"path": str(downloads / "deck.pdf"), "pages": "ten"})
    assert "pages must look like" in _error(bad)


async def test_an_unreadable_pdf_says_why(dirs, monkeypatch) -> None:
    async def fake_read(data: bytes, *, filename: str = "", inbound: bool = False) -> pdf_reader.PdfReadResult:
        return pdf_reader.PdfReadResult("", "none", 2, "no text could be read: OCR is off")

    monkeypatch.setattr(pdf_reader, "read_pdf_text", fake_read)
    downloads, _ = dirs
    (downloads / "scan.pdf").write_bytes(b"%PDF")

    out = await handle_read_document({"path": str(downloads / "scan.pdf")})

    assert _error(out) == "no text could be read: OCR is off"


async def test_long_text_is_capped(dirs, monkeypatch) -> None:
    monkeypatch.setenv("TOOL_RESULT_MAX_CHARS", "1000")
    downloads, _ = dirs
    (downloads / "long.txt").write_text("x" * 5000)

    out = await handle_read_document({"path": str(downloads / "long.txt")})

    assert "[truncated — pass `pages` to read the rest]" in out
    assert len(out) < 1200


def test_is_registered_on_the_executive_and_withheld_on_private_turns() -> None:
    from openexecutive.orchestrator import executive
    from openexecutive.orchestrator.schedule_tools import (
        PRIVATE_TURN_WITHHELD_TOOLS,
        private_turn_withholds,
    )

    assert READ_DOCUMENT_TOOL in executive._ALL_SKILL_TOOLS
    assert executive._ALL_SKILL_HANDLERS["read_document"] is handle_read_document
    assert "read_document" in PRIVATE_TURN_WITHHELD_TOOLS
    assert private_turn_withholds("read_document", {"path": "/x"})
