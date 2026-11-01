"""Text extraction for company-doc / intake ingestion.

Covers the formats added for client-intake attachments (xlsx + csv) and the
``extract_text_from_file`` dispatcher's behavior on an unsupported suffix.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from openexecutive.knowledge.loader import (
    LegacyWordDocument,
    extract_text_from_docx,
    extract_text_from_file,
    extract_text_from_xlsx,
)


def test_extract_text_from_xlsx(tmp_path: Path) -> None:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Financials"
    ws.append(["Metric", "Value"])
    ws.append([None, None])  # all-empty row is skipped
    ws.append(["ARR", 1200000])
    # A fully-blank second sheet must contribute no heading.
    wb.create_sheet("Empty")
    path = tmp_path / "sheet.xlsx"
    wb.save(str(path))

    text = extract_text_from_xlsx(path)
    assert "## Financials" in text
    assert "Metric\tValue" in text
    assert "ARR\t1200000" in text
    # Blank rows and blank sheets contribute nothing.
    assert "## Empty" not in text
    assert "\n\n" not in text

    # Routed through the dispatcher identically.
    assert extract_text_from_file(path) == text


def test_extract_text_from_xlsx_respects_char_cap(tmp_path: Path) -> None:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    for i in range(100):
        ws.append([f"row{i}", "x" * 50])
    path = tmp_path / "big.xlsx"
    wb.save(str(path))

    capped = extract_text_from_xlsx(path, max_chars=200)
    assert len(capped) < 1000  # stopped well before flattening all 100 rows


def test_a_sheet_that_only_claims_a_huge_size_is_read_quickly(tmp_path: Path) -> None:
    """(#316) Two cells at A1 and XFD1048576 make a ~5 KB file that openpyxl
    walks as a billion empty cells. The row and column caps stop it."""
    import time

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws["A1"] = "kept"
    ws["XFD1048576"] = "out of range"
    path = tmp_path / "sparse.xlsx"
    wb.save(str(path))
    assert path.stat().st_size < 20_000

    start = time.monotonic()
    text = extract_text_from_xlsx(path)
    assert time.monotonic() - start < 20
    assert "kept" in text
    assert "out of range" not in text


def test_blank_rows_count_toward_the_xlsx_row_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openpyxl import Workbook

    from openexecutive.knowledge import loader

    monkeypatch.setattr(loader, "_XLSX_MAX_ROWS", 10)
    wb = Workbook()
    ws = wb.active
    ws["A1"] = "first"
    ws["A50"] = "past the cap"
    path = tmp_path / "gaps.xlsx"
    wb.save(str(path))

    text = extract_text_from_xlsx(path)
    assert "first" in text
    assert "past the cap" not in text


def test_empty_sheets_count_toward_the_xlsx_row_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openpyxl import Workbook

    from openexecutive.knowledge import loader

    monkeypatch.setattr(loader, "_XLSX_MAX_ROWS", 5)
    wb = Workbook()
    for i in range(10):
        wb.create_sheet(f"Empty{i}")
    wb.create_sheet("Last")["A1"] = "past the cap"
    path = tmp_path / "sheets.xlsx"
    wb.save(str(path))

    assert "past the cap" not in extract_text_from_xlsx(path)


def test_a_non_utf8_csv_is_read_not_raised(tmp_path: Path) -> None:
    path = tmp_path / "excel-export.csv"
    path.write_bytes("client,fee\nCafé Noir,1200\n".encode("cp1252"))
    text = extract_text_from_file(path)
    assert "Noir,1200" in text


def test_extract_text_from_csv(tmp_path: Path) -> None:
    path = tmp_path / "roster.csv"
    path.write_text("name,role\nDana Reyes,CEO\n", encoding="utf-8")
    assert "Dana Reyes,CEO" in extract_text_from_file(path)


def test_extract_text_unsupported_suffix(tmp_path: Path) -> None:
    path = tmp_path / "thing.bin"
    path.write_bytes(b"\x00\x01\x02")
    assert extract_text_from_file(path) == ""


async def test_async_extractor_converts_a_scanned_pdf(tmp_path: Path, monkeypatch) -> None:
    """The sync extractor returns "" for a PDF with no text layer; the async
    one — used by uploads, intake, reconcile and the read tools — converts
    it (knowledge.pdf_reader, stubbed) and never raises for a bad PDF."""
    from openexecutive.knowledge import pdf_reader
    from openexecutive.knowledge.loader import (
        extract_text_from_file_async,
        read_document_text,
    )

    path = tmp_path / "scan.pdf"

    async def fake_read(data: Path, *, filename: str = "", inbound: bool = False) -> pdf_reader.PdfReadResult:
        # The path, not the bytes: the reader's child process opens the file.
        assert data == path and filename == "scan.pdf"
        return pdf_reader.PdfReadResult("Minutes: approve the budget.", "ocr", 1)

    monkeypatch.setattr(pdf_reader, "read_pdf_text", fake_read)
    path.write_bytes(b"%PDF-scan")

    assert await extract_text_from_file_async(path) == "Minutes: approve the budget."
    assert (await read_document_text(path)).converted

    csv = tmp_path / "roster.csv"
    csv.write_text("name\nDana\n", encoding="utf-8")
    result = await read_document_text(csv)
    assert result.text == "name\nDana\n" and not result.converted


def test_docx_tables_are_read_in_order(tmp_path: Path) -> None:
    from docx import Document

    doc = Document()
    doc.add_paragraph("Approved vendors:")
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Vendor"
    table.cell(0, 1).text = "Terms"
    table.cell(1, 0).text = "Pinecrest Paper"
    table.cell(1, 1).text = "Net 45"
    doc.add_paragraph("Reviewed yearly.")
    path = tmp_path / "vendors.docx"
    doc.save(path)

    text = extract_text_from_docx(path)
    assert text == "Approved vendors:\n\nVendor | Terms\nPinecrest Paper | Net 45\n\nReviewed yearly."


def test_docx_merged_cells_are_kept_once(tmp_path: Path) -> None:
    from docx import Document

    doc = Document()
    table = doc.add_table(rows=1, cols=3)
    table.cell(0, 0).merge(table.cell(0, 1)).text = "Region"
    table.cell(0, 2).text = "Target"
    path = tmp_path / "merged.docx"
    doc.save(path)

    assert extract_text_from_docx(path) == "Region | Target"


def test_docx_equal_and_empty_cells_keep_their_place(tmp_path: Path) -> None:
    from docx import Document

    doc = Document()
    table = doc.add_table(rows=2, cols=4)
    for i, value in enumerate(["Q1", "0", "0", "12"]):
        table.cell(0, i).text = value
    for i, value in enumerate(["Name", "", "", "Phone"]):
        table.cell(1, i).text = value
    path = tmp_path / "equal.docx"
    doc.save(path)

    assert extract_text_from_docx(path) == "Q1 | 0 | 0 | 12\nName |  |  | Phone"


def test_a_docx_saved_as_doc_is_read(tmp_path: Path) -> None:
    from docx import Document

    doc = Document()
    doc.add_paragraph("Renamed but modern.")
    path = tmp_path / "renamed.doc"
    doc.save(path)

    assert extract_text_from_docx(path) == "Renamed but modern."


def test_an_old_format_doc_raises_by_name(tmp_path: Path) -> None:
    path = tmp_path / "legacy.doc"
    path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 512)
    with pytest.raises(LegacyWordDocument):
        extract_text_from_docx(path)
