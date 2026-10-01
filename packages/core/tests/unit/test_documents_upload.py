"""Unit tests for POST/DELETE /documents.

Regression guards, in the order they were found:

1. `domain` was declared as a bare default (`domain: str = "general"`), which
   FastAPI parses as a query parameter. The UI sends it as a form field, so it
   was silently dropped and every upload landed under "general".

2. (#113) The handler staged the upload in a `tempfile.NamedTemporaryFile` and
   passed *that* path to `ingest_file`, which derives chunk metadata AND the
   chunk id from it. The id is an MD5 of a freshly random path, so the id-keyed
   upsert never collided: re-uploads appended a whole duplicate chunk set, and
   the stored `filename` was `tmpXXXXXXXX.md`, which
   `DELETE /documents/{filename}` could never match — it unlinked the on-disk
   copy, returned 200, and left every chunk in the store forever.

3. (#114) `domain` was an unvalidated free string, so `domain=finanace`
   returned 200 and indexed the document where no specialist filters, making it
   permanently unretrievable with no error and no way to notice.
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import documents

from ._fake_store import FakeStore as _CapturingStore


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("VECTOR_STORE_PATH", str(tmp_path / "chroma"))
    monkeypatch.setenv("COMPANY_PROFILE_PATH", str(tmp_path / "company" / "profile.yaml"))
    # Don't fan out to the real proactive-alerts pipeline during the test.
    monkeypatch.setattr(
        "openexecutive.alerts.pipeline.schedule_evaluation",
        lambda *a, **k: None,
    )

    app = FastAPI()
    app.include_router(documents.router)
    app.state.store = _CapturingStore()
    return TestClient(app)


def _upload(client: TestClient, **data: str) -> Any:
    files = {"file": ("plan.md", io.BytesIO(b"# Plan\nGrow revenue 30%."), "text/markdown")}
    return client.post("/documents", files=files, data=data)


def test_domain_from_form_field_is_honored(client: TestClient) -> None:
    resp = _upload(client, domain="finance")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["domain"] == "finance"
    assert body["chunks_indexed"] >= 1

    store: _CapturingStore = client.app.state.store  # type: ignore[attr-defined]
    assert store.added, "expected at least one indexed chunk"
    assert all(m["domain"] == "finance" for m in store.added)


def test_domain_defaults_to_general_when_omitted(client: TestClient) -> None:
    resp = _upload(client)

    assert resp.status_code == 200, resp.text
    assert resp.json()["domain"] == "general"
    store: _CapturingStore = client.app.state.store  # type: ignore[attr-defined]
    assert all(m["domain"] == "general" for m in store.added)


def test_get_document_returns_extracted_text(client: TestClient) -> None:
    # Upload writes the original file to disk; the viewer reads it back.
    assert _upload(client).status_code == 200

    resp = client.get("/documents/plan.md")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["filename"] == "plan.md"
    assert "Grow revenue 30%." in body["content"]


def test_scanned_pdf_upload_is_indexed_and_previewed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PDF with no text layer used to index 0 chunks (while reporting
    "indexed") and preview as "no extractable text". Its converted text is
    now what gets indexed and shown (knowledge.pdf_reader, stubbed)."""
    from openexecutive.knowledge import pdf_reader

    async def fake_read(data: bytes, *, filename: str = "", inbound: bool = False) -> pdf_reader.PdfReadResult:
        return pdf_reader.PdfReadResult("Signed lease: rent 12,000 per month.", "ocr", 2)

    monkeypatch.setattr(pdf_reader, "read_pdf_text", fake_read)
    files = {"file": ("lease.pdf", io.BytesIO(b"%PDF-scan"), "application/pdf")}

    resp = client.post("/documents", files=files)

    assert resp.status_code == 200, resp.text
    assert resp.json()["chunks_indexed"] >= 1
    store: _CapturingStore = client.app.state.store  # type: ignore[attr-defined]
    assert any(m["filename"] == "lease.pdf" for m in store.rows.values())

    preview = client.get("/documents/lease.pdf")
    assert "rent 12,000 per month" in preview.json()["content"]


@pytest.mark.parametrize("name", ["budget.xlsx", "budget.xlsm"])
def test_an_excel_upload_is_indexed_and_previewed(client: TestClient, name: str) -> None:
    """(#316) Excel workbooks were refused as an unsupported type even though
    the loader reads them."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Budget"
    ws.append(["Line", "Amount"])
    ws.append(["Marketing", 45000])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    files = {"file": (name, buf, "application/octet-stream")}

    resp = client.post("/documents", files=files, data={"domain": "finance"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["chunks_indexed"] >= 1
    store: _CapturingStore = client.app.state.store  # type: ignore[attr-defined]
    assert any(m["filename"] == name for m in store.rows.values())

    preview = client.get(f"/documents/{name}")
    assert preview.status_code == 200, preview.text
    assert "Marketing\t45000" in preview.json()["content"]


def test_a_csv_upload_is_indexed(client: TestClient) -> None:
    files = {"file": ("pipeline.csv", io.BytesIO(b"deal,value\nAcme,120000\n"), "text/csv")}

    resp = client.post("/documents", files=files)

    assert resp.status_code == 200, resp.text
    assert resp.json()["chunks_indexed"] >= 1


def test_an_unsupported_type_lists_the_allowed_ones_in_order(client: TestClient) -> None:
    files = {"file": ("old.xls", io.BytesIO(b"\xd0\xcf\x11\xe0"), "application/vnd.ms-excel")}

    resp = client.post("/documents", files=files)

    assert resp.status_code == 400
    assert resp.json()["detail"] == (
        "Unsupported file type: .xls. "
        "Allowed: .csv, .doc, .docx, .md, .pdf, .txt, .xlsm, .xlsx"
    )


def test_get_document_missing_returns_404(client: TestClient) -> None:
    resp = client.get("/documents/does_not_exist.md")
    assert resp.status_code == 404


def test_get_document_rejects_dotfile(client: TestClient) -> None:
    # The filename guard rejects dotfiles / non-bare names so a crafted path
    # can't escape the docs directory. (URL-encoded `../` is additionally
    # collapsed by path normalization before it ever reaches the handler.)
    resp = client.get("/documents/.env")
    assert resp.status_code == 400


# ── #113: stable, filename-derived document identity ──────────────────────


def test_reupload_upserts_instead_of_duplicating(client: TestClient) -> None:
    """The bug: each upload staged to a fresh temp path, so chunk ids (an MD5
    of that path) never collided and the upsert always inserted."""
    store: _CapturingStore = client.app.state.store  # type: ignore[attr-defined]

    assert _upload(client).status_code == 200
    after_first = set(store.rows)
    assert after_first, "expected at least one indexed chunk"

    assert _upload(client).status_code == 200

    assert set(store.rows) == after_first, "re-upload must reuse the same chunk ids"
    assert len(store.rows) == len(after_first), "re-upload must not grow the collection"


def test_indexed_under_real_filename_not_temp_path(client: TestClient) -> None:
    assert _upload(client).status_code == 200
    store: _CapturingStore = client.app.state.store  # type: ignore[attr-defined]

    assert store.filenames() == {"plan.md"}
    assert {m["source"] for m in store.added} == {"plan.md"}
    # The specific failure mode: identity taken from the staging file.
    assert not any(m["filename"].startswith("tmp") for m in store.added)


def test_delete_removes_chunks_from_the_index(client: TestClient) -> None:
    """The bug: DELETE matched on `filename`, which held the temp name, so it
    removed nothing, unlinked the on-disk copy and still returned 200 —
    leaving the chunks permanently unreachable."""
    assert _upload(client).status_code == 200
    store: _CapturingStore = client.app.state.store  # type: ignore[attr-defined]
    assert store.rows

    resp = client.delete("/documents/plan.md")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted": "plan.md"}
    assert store.rows == {}, "DELETE must drop the document's chunks, not just the file"


def test_delete_leaves_other_documents_indexed(client: TestClient) -> None:
    files = {"file": ("other.md", io.BytesIO(b"# Other\nUnrelated content."), "text/markdown")}
    assert client.post("/documents", files=files, data={"domain": "hr"}).status_code == 200
    assert _upload(client).status_code == 200

    assert client.delete("/documents/plan.md").status_code == 200

    store: _CapturingStore = client.app.state.store  # type: ignore[attr-defined]
    assert store.filenames() == {"other.md"}


# ── #114: domain validation ───────────────────────────────────────────────


def test_unknown_domain_is_rejected(client: TestClient) -> None:
    """A typo used to return 200 and index the document where no specialist
    filters — silently unretrievable, with nothing to reveal it."""
    resp = _upload(client, domain="finanace")

    assert resp.status_code == 400
    assert "finanace" in resp.json()["detail"]
    store: _CapturingStore = client.app.state.store  # type: ignore[attr-defined]
    assert store.rows == {}, "a rejected upload must not be indexed"


def test_rejected_domain_does_not_write_the_file(client: TestClient, tmp_path: Path) -> None:
    assert _upload(client, domain="nonsense").status_code == 400
    assert not (tmp_path / "company" / "docs" / "plan.md").exists()


@pytest.mark.parametrize(
    "domain",
    ["strategy", "finance", "hr", "legal", "operations", "marketing", "board", "product", "general"],
)
def test_every_known_domain_is_accepted(client: TestClient, domain: str) -> None:
    resp = _upload(client, domain=domain)

    assert resp.status_code == 200, resp.text
    assert resp.json()["domain"] == domain


# ── Streaming the upload to disk ──────────────────────────────────────────


@pytest.fixture
def staging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Stage uploads in a directory of their own, so a test can see that no
    partial copy is left behind."""
    import tempfile

    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(staging))
    return staging


def test_an_upload_over_the_limit_is_refused_with_413(
    client: TestClient, tmp_path: Path, staging: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Small limit and copy size, so the refusal comes several pieces in.
    monkeypatch.setattr(documents, "MAX_UPLOAD_BYTES", 100)
    monkeypatch.setattr(documents, "_COPY_CHUNK_BYTES", 16)
    files = {"file": ("big.md", io.BytesIO(b"x" * 101), "text/markdown")}

    resp = client.post("/documents", files=files)

    assert resp.status_code == 413
    assert resp.json()["detail"] == "File too large (max 50MB)"
    store: _CapturingStore = client.app.state.store  # type: ignore[attr-defined]
    assert store.rows == {}
    assert not (tmp_path / "company" / "docs" / "big.md").exists()
    assert list(staging.iterdir()) == [], "the partial copy must be removed"


def test_an_upload_at_the_limit_is_stored_byte_for_byte(
    client: TestClient, tmp_path: Path, staging: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(documents, "MAX_UPLOAD_BYTES", 100)
    monkeypatch.setattr(documents, "_COPY_CHUNK_BYTES", 16)
    body = b"# Plan\n" + b"Grow revenue. " * 6 + b"x" * 9
    assert len(body) == 100
    files = {"file": ("plan.md", io.BytesIO(body), "text/markdown")}

    resp = client.post("/documents", files=files)

    assert resp.status_code == 200, resp.text
    assert (tmp_path / "company" / "docs" / "plan.md").read_bytes() == body
    assert list(staging.iterdir()) == [], "the staging copy must be removed"


def test_the_alert_excerpt_is_the_head_of_the_upload(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[Any] = []
    monkeypatch.setattr(
        "openexecutive.alerts.pipeline.schedule_evaluation", events.append
    )
    body = b"# Notes\n" + b"a" * 9000
    files = {"file": ("notes.md", io.BytesIO(body), "text/markdown")}

    assert client.post("/documents", files=files).status_code == 200

    (event,) = events
    assert event.body == body[:8000].decode()


# ── A Word / Excel file its parser could not read ─────────────────────────


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        ("stopped", "it is too large or took too long to read"),
        ("failed", "the file may be damaged, or not the type its name says"),
    ],
)
def test_an_unreadable_upload_is_a_422_not_a_500(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: str, reason: str
) -> None:
    """The parser runs in a child process; one that dies, times out or
    raises must reach the client as a readable 422, with nothing kept."""
    from openexecutive.knowledge import isolated, loader

    def unreadable(path: Path, **_: Any) -> str:
        if error == "stopped":
            raise isolated.WorkerStopped("_parse_file timed out after 300s")
        raise isolated.IsolatedError("PackageNotFoundError: Package not found at '/tmp/x'")

    monkeypatch.setattr(loader, "extract_text_from_file", unreadable)
    files = {"file": ("plan.docx", io.BytesIO(b"PK not really"), "application/octet-stream")}

    resp = client.post("/documents", files=files)

    assert resp.status_code == 422
    assert resp.json()["detail"] == f"Could not read plan.docx: {reason}."
    store: _CapturingStore = client.app.state.store  # type: ignore[attr-defined]
    assert store.rows == {}
    assert not (tmp_path / "company" / "docs" / "plan.docx").exists()


def test_an_unreadable_document_preview_is_a_422(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.knowledge import isolated, loader

    docs = tmp_path / "company" / "docs"
    docs.mkdir(parents=True)
    (docs / "budget.xlsx").write_bytes(b"PK not really")

    def stopped(path: Path, **_: Any) -> str:
        raise isolated.WorkerStopped("_parse_file stopped with exit code -9")

    monkeypatch.setattr(loader, "extract_text_from_file", stopped)

    resp = client.get("/documents/budget.xlsx")

    assert resp.status_code == 422
    assert resp.json()["detail"] == (
        "Could not read budget.xlsx: it is too large or took too long to read."
    )


def test_an_upload_met_by_busy_parsers_is_a_503_to_retry(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.knowledge import isolated, loader

    def busy(path: Path, **_: Any) -> str:
        raise isolated.ParserBusy("_parse_file found no free parser slot")

    monkeypatch.setattr(loader, "extract_text_from_file", busy)
    files = {"file": ("plan.docx", io.BytesIO(b"PK not really"), "application/octet-stream")}

    resp = client.post("/documents", files=files)

    assert resp.status_code == 503
    assert resp.headers["retry-after"] == "30"
    assert not (tmp_path / "company" / "docs" / "plan.docx").exists()


async def test_an_unreadable_intake_upload_is_a_422(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException

    from openexecutive.api.intake_uploads import _extract_intake_upload
    from openexecutive.knowledge import isolated, loader

    def failed(path: Path, **_: Any) -> str:
        raise isolated.IsolatedError("BadZipFile: File is not a zip file", kind="BadZipFile")

    monkeypatch.setattr(loader, "extract_text_from_file", failed)

    with pytest.raises(HTTPException) as info:
        await _extract_intake_upload("notes.docx", b"PK not really")

    assert info.value.status_code == 422
    assert info.value.detail == (
        "Could not read notes.docx: the file may be damaged, or not the type its name says."
    )


def test_a_pdf_met_by_busy_parsers_is_a_503_not_an_empty_index(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review finding: read_pdf_text never raises, so a busy PDF came back
    empty and was stored and reported "indexed" with 0 chunks."""
    from openexecutive.knowledge import pdf_reader

    async def busy(data: Any, *, filename: str = "", inbound: bool = False) -> pdf_reader.PdfReadResult:
        return pdf_reader.PdfReadResult("", "none", 0, "the PDF was not read: busy", busy=True)

    monkeypatch.setattr(pdf_reader, "read_pdf_text", busy)
    files = {"file": ("lease.pdf", io.BytesIO(b"%PDF-1.4"), "application/pdf")}

    resp = client.post("/documents", files=files)

    assert resp.status_code == 503
    store: _CapturingStore = client.app.state.store  # type: ignore[attr-defined]
    assert store.rows == {}
    assert not (tmp_path / "company" / "docs" / "lease.pdf").exists()
