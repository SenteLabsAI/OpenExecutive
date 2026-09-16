"""Shared attachment handling for bot integrations (Discord, Telegram, …).

Each integration calls `process_attachments()` with a list of `AttachmentItem`
dataclasses.  The helper downloads, extracts, and returns:
- ``extra_text`` — extracted text from documents, ready to prepend to the
  user message before passing to ``executive.chat()``.
- ``image_blocks`` — Anthropic vision content blocks for images, ready to
  pass as ``executive.chat(attachment_blocks=...)``.

Text-extractable files are also indexed into ChromaDB (``ingest_file``) as a
background task so they surface in future RAG retrieval sessions.

Supported file types
---------------------
Images:   image/png, image/jpeg, image/gif, image/webp
Docs:     .pdf, .docx, .doc, .txt, .md, .rst, .csv

Limits
------
Images:   20 MB per file (Discord free tier cap; keeps base64 payloads sane)
Docs:     20 MB per file download; extracted text truncated to 40 000 chars
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
# Constants
# ------------------------------------------------------------------ #

_DEFAULT_MAX_BYTES = 20 * 1024 * 1024  # 20 MB
_MAX_EXTRACTED_CHARS = 40_000
_EXTRACTABLE_SUFFIXES = frozenset({".pdf", ".docx", ".doc", ".txt", ".md", ".rst", ".csv"})
_SUPPORTED_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})

# Strong refs so background ingest tasks aren't GC'd before they complete.
_ingest_tasks: set[asyncio.Task[Any]] = set()


# ------------------------------------------------------------------ #
# Data model
# ------------------------------------------------------------------ #

@dataclass
class AttachmentItem:
    """Portable description of one attachment from any integration."""

    url: str
    filename: str
    content_type: str = ""
    size: int = 0
    headers: dict[str, str] = field(default_factory=dict)


# ------------------------------------------------------------------ #
# Download
# ------------------------------------------------------------------ #

async def download_bytes(
    url: str,
    headers: dict[str, str] | None = None,
    max_bytes: int = _DEFAULT_MAX_BYTES,
) -> bytes:
    """Fetch *url* and return the raw bytes.

    Raises ``ValueError`` if the response exceeds *max_bytes* or the
    server signals a size over the limit via Content-Length.
    Raises ``httpx.HTTPStatusError`` on non-2xx responses.

    Security notes
    ~~~~~~~~~~~~~~
    - SSRF: This function is generic; callers are responsible for ensuring the
      URL is from a trusted source. Discord attachment URLs are CDN-hosted
      (cdn.discordapp.com). Telegram file URLs are api.telegram.org. Neither is
      a user-controlled arbitrary URL; an SSRF attack would require compromising
      those CDNs. A future hardening step could add an allowlist for URL
      prefixes or reject RFC-1918 / link-local IPs after DNS resolution.
    - Buffering: ``resp.content`` buffers the full response body before the
      post-hoc size check. A server streaming a response without Content-Length
      could theoretically push more than max_bytes before the check fires.
      TODO: switch to ``client.stream()`` with an incremental byte counter
      for full protection. Current 15s timeout partially mitigates abuse.
    """
    import httpx

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, headers=headers or {}, follow_redirects=True)
        resp.raise_for_status()

        content_length = int(resp.headers.get("content-length", "0") or "0")
        if content_length and content_length > max_bytes:
            raise ValueError(
                f"Attachment too large: server reports {content_length} bytes "
                f"(limit {max_bytes})"
            )

        data = resp.content
        if len(data) > max_bytes:
            raise ValueError(
                f"Attachment too large: {len(data)} bytes received (limit {max_bytes})"
            )
        return data


# ------------------------------------------------------------------ #
# Text extraction + ChromaDB ingest
# ------------------------------------------------------------------ #

def _suffix_from_filename(filename: str) -> str:
    return Path(filename).suffix.lower()


def _extract_text(data: bytes, filename: str) -> str:
    """Write *data* to a temp file and call the shared extractor.

    Returns extracted text (may be empty if extraction fails or yields nothing).
    """
    suffix = _suffix_from_filename(filename)
    if suffix == ".csv":
        # CSV isn't in extract_text_from_file — just decode as UTF-8 text.
        try:
            return data.decode("utf-8", errors="replace")
        except Exception:
            logger.exception("attachments: CSV decode failed for %s", filename)
            return ""

    try:
        from openexecutive.knowledge.loader import extract_text_from_file

        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(data)
            tmp_path = Path(tmp.name)

        try:
            return extract_text_from_file(tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)
    except Exception:
        logger.exception("attachments: text extraction failed for %s", filename)
        return ""


def _schedule_ingest(data: bytes, filename: str) -> None:
    """Fire-and-forget ChromaDB ingest of *data* as a new company document.

    Uses the same strong-ref pattern as ``_thread_rename_tasks`` in
    discord_bot to prevent GC cancellation mid-flight.
    """
    async def _run() -> None:
        suffix = _suffix_from_filename(filename)
        try:
            from openexecutive.knowledge.loader import ingest_file
            from openexecutive.knowledge.store import ChromaDBStore

            store = ChromaDBStore()
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp.write(data)
                tmp_path = Path(tmp.name)

            try:
                # `source_name` is the real attachment name: `tmp_path` is a
                # random staging name, and indexing under it both duplicates
                # on every re-send and leaves chunks no API call can delete.
                #
                # It is PREFIXED, and stripped to a bare name, because an
                # attachment name is chosen by whoever sent the message. The
                # name is the chunk-id namespace, so an unprefixed
                # "strategy-2026.md" arriving by email would upsert straight
                # over the curated company document of that name. The prefix
                # keeps inbound content in its own namespace and makes the
                # provenance visible in the `[filename]` retrieval citation —
                # the same reason Notion and research artifacts are isolated.
                #
                # `domain` stays "company_docs", which is NOT one of the
                # specialist domains — so these chunks match no specialist's
                # domain filter and never reach the Executive's context. That
                # is a known gap, left deliberately: anyone who can attach a
                # file in an integration channel would otherwise be writing
                # into every specialist's RAG context, and these rows have no
                # removal path at all (they are never written to
                # `company/docs/`, so `GET /documents` does not list them and
                # `DELETE /documents/{filename}` 404s before reaching the
                # store). Making them retrievable is a trust-boundary decision
                # that needs its own change, with a delete path alongside it —
                # not a side effect of fixing the chunk-id bug below.
                count = await ingest_file(
                    tmp_path,
                    store,
                    domain="company_docs",
                    source_name=f"attachment:{Path(filename).name}",
                )
                logger.info(
                    "attachments: indexed %d chunks from %s into ChromaDB",
                    count,
                    filename,
                )
            finally:
                tmp_path.unlink(missing_ok=True)
        except Exception:
            logger.exception("attachments: background ingest failed for %s", filename)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — skip silently. This can happen in sync test contexts
        # or CLI usage. ChromaDB ingest is best-effort; the user already has
        # their answer and can re-upload explicitly if needed.
        logger.debug("attachments: no running event loop — skipping ChromaDB ingest for %s", filename)
        return
    task = loop.create_task(_run())
    _ingest_tasks.add(task)
    task.add_done_callback(_ingest_tasks.discard)


# ------------------------------------------------------------------ #
# Per-attachment routing
# ------------------------------------------------------------------ #

def _build_image_block(data: bytes, content_type: str) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": content_type,
            "data": base64.standard_b64encode(data).decode(),
        },
    }


def build_attachment_output(
    filename: str,
    data: bytes,
    content_type: str,
) -> tuple[str, list[dict[str, Any]]]:
    """Route one attachment to the right handler.

    Returns ``(extra_text, image_blocks)``.  Both may be empty — callers
    concatenate results across all attachments.
    """
    # Normalise content_type — some servers omit it or add parameters.
    # All normalization (non-standard aliases, suffix inference) happens once
    # here so downstream code sees a clean, canonical MIME type.
    ct = (content_type.split(";")[0].strip().lower()) if content_type else ""
    if ct == "image/jpg":
        ct = "image/jpeg"  # non-standard alias used by some cameras / servers
    suffix = _suffix_from_filename(filename)
    if not ct and suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        ct = "image/jpeg" if suffix in {".jpg", ".jpeg"} else f"image/{suffix.lstrip('.')}"

    if ct in _SUPPORTED_IMAGE_TYPES:
        return "", [_build_image_block(data, ct)]

    if suffix in _EXTRACTABLE_SUFFIXES:
        text = _extract_text(data, filename)
        if not text.strip():
            return f"(Attached {filename}: could not extract any text)", []

        truncated = False
        if len(text) > _MAX_EXTRACTED_CHARS:
            text = text[:_MAX_EXTRACTED_CHARS]
            truncated = True

        # Collapse control chars / excessive whitespace so the injected block
        # doesn't confuse the model with raw PDF artefacts.
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)

        label = f"[Attached: {filename}]"
        if truncated:
            label += f" (truncated to {_MAX_EXTRACTED_CHARS} chars)"
        extra_text = f"{label}\n{text.strip()}"

        # Security note: extracted text is injected verbatim into the LLM
        # context. A malicious document could contain prompt-injection payloads.
        # This is a systemic risk shared with any RAG system that ingests
        # untrusted content; no currently deployed mitigation exists here.
        # The Executive's system prompt and tool-call gating are the primary
        # defences; treat attachment sources the same as other untrusted inputs.
        _schedule_ingest(data, filename)
        return extra_text, []

    return f"(Could not read {filename}: unsupported type — supported: PDF, DOCX, TXT, MD, PNG, JPG, GIF, WebP)", []


# ------------------------------------------------------------------ #
# Public entry point
# ------------------------------------------------------------------ #

async def process_attachments(
    items: list[AttachmentItem],
    max_bytes: int = _DEFAULT_MAX_BYTES,
) -> tuple[str, list[dict[str, Any]]]:
    """Download and process a list of attachments.

    Returns ``(extra_text, image_blocks)``.  Items that fail to download
    or process are skipped with a log message — a single bad attachment
    must not prevent the user from getting a response.
    """
    all_text_parts: list[str] = []
    all_image_blocks: list[dict[str, Any]] = []

    for item in items:
        if item.size > max_bytes:
            all_text_parts.append(
                f"(Skipped {item.filename}: file too large — "
                f"{item.size // (1024 * 1024)} MB, limit {max_bytes // (1024 * 1024)} MB)"
            )
            continue

        try:
            data = await download_bytes(item.url, headers=item.headers, max_bytes=max_bytes)
        except ValueError as exc:
            all_text_parts.append(f"(Skipped {item.filename}: {exc})")
            continue
        except Exception:
            logger.exception("attachments: download failed for %s", item.filename)
            all_text_parts.append(f"(Could not download {item.filename})")
            continue

        try:
            extra_text, image_blocks = build_attachment_output(item.filename, data, item.content_type)
        except Exception:
            logger.exception("attachments: processing failed for %s", item.filename)
            all_text_parts.append(f"(Could not process {item.filename})")
            continue

        if extra_text:
            all_text_parts.append(extra_text)
        all_image_blocks.extend(image_blocks)

    return "\n\n".join(all_text_parts), all_image_blocks
