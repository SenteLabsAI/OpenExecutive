"""Shared intake-attachment handling for multipart draft endpoints.

Extracted verbatim from ``api.routes.clients`` so conversational onboarding
(``POST /onboard/interview/start``) accepts the same decks, one-pagers, and
briefs that client engagement intake already does, with identical validation,
caps, and text extraction. Behaviour is unchanged — this is a move, not a
rewrite.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import HTTPException, UploadFile

# Intake attachments accepted alongside pasted notes on the multipart draft endpoints.
# Extensions whose text knowledge.loader.extract_text_from_file can read (legacy
# binary .xls is excluded — openpyxl reads only .xlsx/.xlsm).
INTAKE_ALLOWED_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".doc",
    ".xlsx",
    ".xlsm",
    ".csv",
    ".md",
    ".txt",
}
_INTAKE_MAX_BYTES_PER_FILE = 25 * 1024 * 1024  # 25 MB
_INTAKE_MAX_FILES = 8
# Per-file cap on the text fed into the LLM draft (bounds cost/context); the
# fuller extracted text (up to _INTAKE_MAX_DOC_CHARS) is still stored as a doc.
_INTAKE_GEN_CHARS_PER_FILE = 15_000
_INTAKE_MAX_DOC_CHARS = 40_000


def _extract_intake_upload(filename: str, content: bytes) -> tuple[str, str]:
    """Validate one intake upload and return ``(safe_filename, extracted_text)``.

    Raises ``HTTPException(400)`` for a bad name/extension and ``413`` when the
    file exceeds the per-file size cap. The text is extracted by the same
    ``knowledge.loader`` path the /documents upload uses, via a temp file.
    """
    from openexecutive.knowledge.loader import extract_text_from_file

    safe = Path(filename or "").name
    if not safe or safe.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid filename")
    ext = Path(safe).suffix.lower()
    if ext not in INTAKE_ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported file type: {ext}. "
                f"Allowed: {', '.join(sorted(INTAKE_ALLOWED_EXTENSIONS))}"
            ),
        )
    if len(content) > _INTAKE_MAX_BYTES_PER_FILE:
        raise HTTPException(
            status_code=413,
            detail=(
                f"{safe}: file too large "
                f"(limit {_INTAKE_MAX_BYTES_PER_FILE // (1024 * 1024)} MB)"
            ),
        )

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    try:
        text = extract_text_from_file(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)
    return safe, text.strip()


async def _gather_intake_attachments(
    uploads: list[UploadFile],
) -> list[tuple[str, str]]:
    """Read, validate, and extract text from intake uploads.

    Enforces the per-request file-count cap, then returns ``(safe_filename,
    extracted_text)`` for each attachment that yielded text. Files with no
    extractable text (e.g. a scanned, image-only PDF) are dropped silently;
    per-file type/size validation (and its 400/413 errors) lives in
    ``_extract_intake_upload``.
    """
    if len(uploads) > _INTAKE_MAX_FILES:
        raise HTTPException(
            status_code=400, detail=f"Too many files (limit {_INTAKE_MAX_FILES})"
        )
    extracted: list[tuple[str, str]] = []
    for upload in uploads:
        if not upload.filename:
            continue
        # Read at most cap+1 bytes so an oversized part is rejected by
        # _extract_intake_upload (413) without buffering the whole stream.
        content = await upload.read(_INTAKE_MAX_BYTES_PER_FILE + 1)
        if not content:
            continue
        safe, text = _extract_intake_upload(upload.filename, content)
        if text:
            extracted.append((safe, text))
    return extracted
