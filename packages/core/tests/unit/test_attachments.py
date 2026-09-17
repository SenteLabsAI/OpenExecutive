"""Unit tests for the shared attachments module.

All HTTP calls and filesystem operations are mocked — no network or disk I/O.
"""
from __future__ import annotations

import asyncio
import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openexecutive.integrations.attachments import (
    AttachmentItem,
    _MAX_EXTRACTED_CHARS,
    build_attachment_output,
    download_bytes,
    process_attachments,
)


# --------------------------------------------------------------------------- #
# download_bytes
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_download_bytes_returns_content():
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.headers = {}
    mock_resp.content = b"hello world"

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient", return_value=mock_client):
        data = await download_bytes("https://example.com/file.pdf")

    assert data == b"hello world"


@pytest.mark.asyncio
async def test_download_bytes_raises_on_content_length_exceeded():
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.headers = {"content-length": str(30 * 1024 * 1024)}  # 30 MB > 20 MB limit
    mock_resp.content = b"x"

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(ValueError, match="too large"):
            await download_bytes("https://example.com/big.pdf")


@pytest.mark.asyncio
async def test_download_bytes_raises_when_actual_content_exceeds_limit():
    """Content-Length header absent but actual payload is oversized."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.headers = {}
    mock_resp.content = b"x" * (21 * 1024 * 1024)  # 21 MB

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(ValueError, match="too large"):
            await download_bytes("https://example.com/big.pdf")


# --------------------------------------------------------------------------- #
# build_attachment_output — image routing
# --------------------------------------------------------------------------- #

def test_build_attachment_output_png_returns_image_block():
    data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20  # fake PNG header
    extra_text, image_blocks = build_attachment_output("chart.png", data, "image/png")

    assert extra_text == ""
    assert len(image_blocks) == 1
    block = image_blocks[0]
    assert block["type"] == "image"
    assert block["source"]["type"] == "base64"
    assert block["source"]["media_type"] == "image/png"
    assert block["source"]["data"] == base64.standard_b64encode(data).decode()


def test_build_attachment_output_jpeg_normalises_jpg_mime():
    """'image/jpg' (non-standard) must be normalised to 'image/jpeg'."""
    data = b"\xff\xd8\xff"  # JPEG magic bytes
    extra_text, image_blocks = build_attachment_output("photo.jpg", data, "image/jpg")

    assert extra_text == ""
    assert image_blocks[0]["source"]["media_type"] == "image/jpeg"


def test_build_attachment_output_image_no_content_type_infers_from_suffix():
    data = b"GIF89a"
    extra_text, image_blocks = build_attachment_output("anim.gif", data, "")

    assert extra_text == ""
    assert image_blocks[0]["source"]["media_type"] == "image/gif"


# --------------------------------------------------------------------------- #
# build_attachment_output — text document routing
# --------------------------------------------------------------------------- #

def test_build_attachment_output_pdf_extracts_text_and_schedules_ingest():
    extracted = "Quarterly revenue grew 23%."
    with (
        patch(
            "openexecutive.integrations.attachments._extract_text",
            return_value=extracted,
        ),
        patch("openexecutive.integrations.attachments._schedule_ingest") as mock_ingest,
    ):
        extra_text, image_blocks = build_attachment_output(
            "report.pdf", b"%PDF-fake", "application/pdf"
        )

    assert image_blocks == []
    assert "[Attached: report.pdf]" in extra_text
    assert "Quarterly revenue grew 23%." in extra_text
    mock_ingest.assert_called_once()


def test_build_attachment_output_truncates_long_text():
    # 10x the limit so the label overhead is negligible relative to the total.
    long_text = "word " * (_MAX_EXTRACTED_CHARS * 2)
    with (
        patch(
            "openexecutive.integrations.attachments._extract_text",
            return_value=long_text,
        ),
        patch("openexecutive.integrations.attachments._schedule_ingest"),
    ):
        extra_text, _ = build_attachment_output("doc.txt", b"...", "text/plain")

    assert "truncated" in extra_text.lower()
    # Total extra_text is label + capped text; must be much smaller than input.
    assert len(extra_text) < len(long_text) // 2


def test_build_attachment_output_empty_extraction_returns_notice():
    with patch(
        "openexecutive.integrations.attachments._extract_text",
        return_value="   ",
    ):
        extra_text, image_blocks = build_attachment_output("empty.pdf", b"", "application/pdf")

    assert image_blocks == []
    assert "could not extract" in extra_text.lower()


# --------------------------------------------------------------------------- #
# build_attachment_output — unsupported type
# --------------------------------------------------------------------------- #

def test_build_attachment_output_unsupported_type_returns_notice():
    extra_text, image_blocks = build_attachment_output(
        "model.xlsx", b"PK...", "application/vnd.ms-excel"
    )

    assert image_blocks == []
    assert "unsupported type" in extra_text.lower()
    assert "model.xlsx" in extra_text


# --------------------------------------------------------------------------- #
# process_attachments
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_process_attachments_skips_oversized_item():
    items = [
        AttachmentItem(
            url="https://example.com/big.pdf",
            filename="big.pdf",
            content_type="application/pdf",
            size=25 * 1024 * 1024,  # 25 MB > 20 MB limit
        )
    ]
    extra_text, image_blocks = await process_attachments(items)

    assert "too large" in extra_text.lower() or "skipped" in extra_text.lower()
    assert image_blocks == []


@pytest.mark.asyncio
async def test_process_attachments_skips_failed_download_and_continues():
    """A download error on item 1 must not stop item 2 from processing."""
    items = [
        AttachmentItem(url="https://fail.example.com/a.pdf", filename="a.pdf", content_type="application/pdf"),
        AttachmentItem(url="https://ok.example.com/b.png", filename="b.png", content_type="image/png"),
    ]

    async def _fake_download(url: str, headers=None, max_bytes=None):
        if "fail" in url:
            raise ConnectionError("network down")
        return b"\x89PNG\r\n\x1a\n" + b"\x00" * 20

    with patch("openexecutive.integrations.attachments.download_bytes", side_effect=_fake_download):
        extra_text, image_blocks = await process_attachments(items)

    # Item 1 failed — note in text
    assert "a.pdf" in extra_text
    # Item 2 succeeded — got an image block
    assert len(image_blocks) == 1
    assert image_blocks[0]["source"]["media_type"] == "image/png"


@pytest.mark.asyncio
async def test_process_attachments_concatenates_multiple_texts():
    items = [
        AttachmentItem(url="https://example.com/a.txt", filename="a.txt", content_type="text/plain"),
        AttachmentItem(url="https://example.com/b.txt", filename="b.txt", content_type="text/plain"),
    ]

    async def _fake_download(url: str, headers=None, max_bytes=None):
        return b"content from " + url.encode().split(b"/")[-1]

    with (
        patch("openexecutive.integrations.attachments.download_bytes", side_effect=_fake_download),
        patch(
            "openexecutive.integrations.attachments._extract_text",
            side_effect=lambda data, filename: data.decode(),
        ),
        patch("openexecutive.integrations.attachments._schedule_ingest"),
    ):
        extra_text, image_blocks = await process_attachments(items)

    assert "a.txt" in extra_text
    assert "b.txt" in extra_text
    assert image_blocks == []


# ── #113/#114 at the attachment ingest path ───────────────────────────────


def _ingest_call(filename: str, data: bytes = b"# Notes\nRevenue grew."):
    """Run `_schedule_ingest` to completion and return the `ingest_file` call."""
    return _ingest_and_store_call(filename, data)[0]


def _ingest_and_store_call(filename: str, data: bytes = b"# Notes\nRevenue grew."):
    """As `_ingest_call`, plus the `ChromaDBStore(...)` construction call.

    The store constructor matters on its own: it is how the ingest picks
    which database to write to, and passing no argument silently sends the
    chunks somewhere nothing reads.
    """
    from openexecutive.integrations import attachments as att
    from openexecutive.knowledge.store import ChromaDBStore

    mock_ingest = AsyncMock(return_value=3)
    mock_store_cls = MagicMock()
    # Mock the constructor, not the collection names: those are data the code
    # under test reads, and a MagicMock attribute would make the assertion
    # compare two mocks and pass regardless of what the code actually chose.
    mock_store_cls.ATTACHMENT_COLLECTION = ChromaDBStore.ATTACHMENT_COLLECTION
    mock_store_cls.COMPANY_COLLECTION = ChromaDBStore.COMPANY_COLLECTION
    with (
        patch("openexecutive.knowledge.loader.ingest_file", mock_ingest),
        patch("openexecutive.knowledge.store.ChromaDBStore", mock_store_cls),
    ):

        async def _drive() -> None:
            att._schedule_ingest(data, filename)
            # `_schedule_ingest` fires a background task; let it run.
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        asyncio.run(_drive())

    mock_ingest.assert_called_once()
    return mock_ingest.call_args, mock_store_cls.call_args


def test_attachment_is_indexed_under_its_real_name_not_the_temp_path():
    """The bug: the staging temp path was passed straight to `ingest_file`, so
    every re-send duplicated and no chunk could ever be deleted."""
    _, kwargs = _ingest_call("board-deck.md")

    assert kwargs["source_name"] == "attachment:board-deck.md"


def test_attachment_name_cannot_collide_with_a_company_document():
    """`source_name` is the chunk-id namespace, and an attachment name is
    chosen by whoever sent the message — without the prefix an emailed
    `strategy-2026.md` would upsert over the curated document of that name."""
    _, kwargs = _ingest_call("strategy-2026.md")

    assert kwargs["source_name"] != "strategy-2026.md"
    assert kwargs["source_name"].startswith("attachment:")


def test_attachment_name_is_stripped_to_a_bare_name():
    _, kwargs = _ingest_call("../../etc/passwd.md")

    assert kwargs["source_name"] == "attachment:passwd.md"


def test_attachment_is_indexed_into_the_isolated_collection():
    """The isolation this path depends on, pinned.

    Attachments used to go to COMPANY_COLLECTION, kept out of retrieval by a
    domain outside the specialist set. That never worked: `query` builds a
    `where` clause only when a domain filter is truthy, and an
    Executive-level `retrieve` passes none — so the chunks matched and came
    back under "From your company documents". The collection is the boundary.
    """
    from openexecutive.knowledge.store import ChromaDBStore

    _, kwargs = _ingest_call("board-deck.md")

    assert kwargs["collection"] == ChromaDBStore.ATTACHMENT_COLLECTION
    assert kwargs["collection"] != ChromaDBStore.COMPANY_COLLECTION


def test_attachment_rows_carry_the_removal_tag():
    """`type` is what makes these rows deletable by a `where` clause — Chroma
    matches exact values only, so without it a wipe means a full scan."""
    _, kwargs = _ingest_call("board-deck.md")

    assert kwargs["extra_metadata"]["type"] == "attachment"


def test_attachment_domain_is_not_the_general_catch_all():
    """Belt-and-braces behind the collection.

    Nothing queries the attachment collection, so this domain is never read
    today. It is chosen so that if anything ever does, the default stays
    "not retrieved": `general` would be the worst possible value, because
    `retriever._with_general` fans it out to every specialist.
    """
    from openexecutive.knowledge.loader import GENERAL_DOMAIN, UPLOAD_DOMAINS

    _, kwargs = _ingest_call("board-deck.md")

    assert kwargs["domain"] not in UPLOAD_DOMAINS
    assert kwargs["domain"] != GENERAL_DOMAIN


def test_attachment_ingest_uses_the_configured_vector_store():
    """The bug this pins: `ChromaDBStore()` defaults to a RELATIVE
    "./chroma_db" resolved against the process CWD, so with
    VECTOR_STORE_PATH set — every container deployment — attachments were
    written to a different database than the one the API reads."""
    from openexecutive.config import get_settings

    _, store_call = _ingest_and_store_call("board-deck.md")

    assert store_call is not None, "ChromaDBStore was never constructed"
    assert store_call.kwargs.get("persist_directory") == get_settings().vector_store_path
