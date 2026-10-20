"""Read an inbound email's document attachments into its turn.

Chat, Discord, Telegram, Slack and Google Chat hand the Executive the text of
a document someone sends. Email used to hand over only the attachment list,
so reading one took a tool call — and on a turn private to the principal
(mail from a contact, or mail the principal forwarded) that call is refused:
``get_gmail_attachment_content`` is not in ``PRIVATE_TURN_MCP_TOOLS``.

``read_email_attachments`` closes that gap outside the model loop. For each
readable attachment of *this* message it calls ``get_gmail_attachment_content``
through the gateway, which (workspace-mcp in stdio mode, as it runs
co-located here) saves the file and answers ``📎 Saved to: <path>``. The path
must resolve inside the tool download folders (``tool_catalog.
resolve_readable_file``, the check ``oe__read_file`` uses) before it is read
— with a scanned PDF converted by ``knowledge.pdf_reader``. Only this email's
own attachments are read, so no tool is widened for the turn.

Images (PNG, JPEG, GIF, WebP, typed by their bytes) come back as vision
blocks for the turn, as they do in chat. A few at most, and none under ``MIN_IMAGE_BYTES``: mail
carries signature logos and tracking pixels as attachments too, and those
would only spend tokens. One over ``MAX_IMAGE_BYTES`` is skipped with a
note, because the model API refuses a larger image and would fail the turn.

Audio attachments (a voice note, a recorded call) are transcribed, up to
``MAX_AUDIO``, and their words appended like a document's: a file's words,
not the sender's.

Everything is best-effort: a failure becomes a one-line note in the turn, and
the Executive can still use ``read_document`` on a file it downloads itself.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from openexecutive.orchestrator.mcp_gateway import MCPGateway

logger = logging.getLogger(__name__)

# Per email: bounds the downloads (and OCR / model work) one message can cause.
MAX_ATTACHMENTS = 5
MAX_BYTES = 20 * 1024 * 1024
# Text inlined per attachment; the rest is one read_document call away.
MAX_CHARS_PER_ATTACHMENT = 15_000

_READABLE_SUFFIXES = frozenset({".pdf", ".docx", ".xlsx", ".xlsm", ".csv", ".md", ".txt"})
# Images the model can see, by suffix (the saved file has the sender's).
IMAGE_MEDIA_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp",
}
MAX_IMAGES = 3
# Voice notes and recordings, transcribed (providers.transcription).
MAX_AUDIO = 2
# Below this an image is a logo, an icon or a tracking pixel.
MIN_IMAGE_BYTES = 10 * 1024
# The model API takes an image up to 5 MB once base64'd (4/3 larger).
MAX_IMAGE_BYTES = 3 * 1024 * 1024
_ATTACHMENT_TOOL = "google_workspace__get_gmail_attachment_content"
# workspace-mcp's stdio answer names the saved file on its own line. In HTTP
# mode it gives a download URL instead, which this does not follow. Anchored
# to the start of a line, and the LAST one is taken: the answer also echoes
# the sender-chosen attachment name on an earlier "Filename:" line, which may
# itself contain "Saved to: /…" to point the poller at another saved file.
_SAVED_TO_RE = re.compile(r"^\s*(?:\U0001F4CE\s*)?Saved to:\s*(/.+?)\s*$", re.MULTILINE)
# The saved file's own name, which workspace-mcp sanitizes and makes unique;
# the path above must end in it.
_SAVED_NAME_RE = re.compile(r"^\s*Saved filename:\s*(.+?)\s*$", re.MULTILINE)


@dataclass
class EmailAttachments:
    """What one email's attachments add to its turn: document text to append
    to the message, and image blocks to pass as ``attachment_blocks``."""

    text: str = ""
    image_blocks: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class EmailAttachmentRef:
    """One entry of get_gmail_message_content's ``--- ATTACHMENTS ---`` list."""

    name: str
    attachment_id: str
    size_bytes: int


def _saved_path(answer: object) -> str | None:
    """The path workspace-mcp saved the attachment to, or None.

    Only a ``Saved to:`` line that starts its line counts, the last one wins,
    and its file name must match the ``Saved filename:`` line — both lines
    are written by workspace-mcp after the sender-chosen name it echoes."""
    if not isinstance(answer, str):
        return None
    paths = _SAVED_TO_RE.findall(answer)
    names = _SAVED_NAME_RE.findall(answer)
    if not paths or not names:
        return None
    path = paths[-1]
    return path if Path(path).name == names[-1] else None


async def _download(
    gateway: MCPGateway,
    message_id: str,
    user_email: str,
    ref: EmailAttachmentRef,
    suffixes: frozenset[str] | None = None,
) -> Path | str:
    """Save one attachment through the gateway: its path inside the tool
    download folders, or the note for the turn when that fails."""
    from openexecutive.workflows.tool_catalog import resolve_readable_file

    try:
        answer = await gateway.call_tool({
            "name": _ATTACHMENT_TOOL,
            "arguments": {
                "message_id": message_id,
                "attachment_id": ref.attachment_id,
                "user_google_email": user_email,
            },
        })
    except Exception as exc:
        logger.warning("email attachment download failed (%s)", type(exc).__name__)
        return f"(Could not download {ref.name})"

    saved = _saved_path(answer)
    if saved is None:
        return f"(Could not download {ref.name})"
    resolved = resolve_readable_file(saved, suffixes=suffixes)
    if isinstance(resolved, str):
        logger.warning("email attachment not read: %s", resolved)
        return f"(Could not read {ref.name})"
    return resolved


async def _read_one(
    gateway: MCPGateway, message_id: str, user_email: str, ref: EmailAttachmentRef
) -> str:
    from openexecutive.integrations.attachments import format_attached_text
    from openexecutive.knowledge.loader import read_document_text

    resolved = await _download(gateway, message_id, user_email, ref)
    if isinstance(resolved, str):
        return resolved
    try:
        result = await read_document_text(resolved, inbound=True)
    except Exception as exc:
        logger.warning("email attachment extraction failed (%s)", type(exc).__name__)
        return f"(Could not read {ref.name})"
    if not result.text.strip():
        return f"(Attached {ref.name}: {result.note or 'could not extract any text'})"
    # Label with the name the sender gave it, not workspace-mcp's saved name.
    return format_attached_text(
        ref.name,
        result.text,
        converted=result.converted,
        note=result.note,
        max_chars=MAX_CHARS_PER_ATTACHMENT,
    )


async def _read_image(
    gateway: MCPGateway, message_id: str, user_email: str, ref: EmailAttachmentRef
) -> dict[str, Any] | str:
    """One image attachment as a vision block, or the note for the turn."""
    from openexecutive.integrations.attachments import build_image_block, sniff_image_type

    resolved = await _download(
        gateway, message_id, user_email, ref, suffixes=frozenset(IMAGE_MEDIA_TYPES)
    )
    if isinstance(resolved, str):
        return resolved
    try:
        data = resolved.read_bytes()
    except OSError as exc:
        logger.warning("email image read failed (%s)", type(exc).__name__)
        return f"(Could not read {ref.name})"
    if len(data) > MAX_IMAGE_BYTES:
        return f"(Skipped {ref.name}: image too large — limit {MAX_IMAGE_BYTES // (1024 * 1024)} MB)"
    # Typed by its bytes, not its name: a mismatch would fail the whole turn.
    media_type = sniff_image_type(data)
    if media_type is None:
        return f"(Could not read {ref.name})"
    return build_image_block(data, media_type)


async def _read_audio(
    gateway: MCPGateway, message_id: str, user_email: str, ref: EmailAttachmentRef
) -> str:
    """One audio attachment's words, as attachment text, or the note."""
    from openexecutive.integrations.attachments import format_attached_text
    from openexecutive.providers import transcription

    resolved = await _download(
        gateway, message_id, user_email, ref,
        suffixes=frozenset(transcription.AUDIO_MEDIA_TYPES),
    )
    if isinstance(resolved, str):
        return resolved
    try:
        data = resolved.read_bytes()
    except OSError as exc:
        logger.warning("email audio read failed (%s)", type(exc).__name__)
        return f"(Could not read {ref.name})"
    try:
        words = await transcription.transcribe(data, ref.name)
    except transcription.TranscriptionError as exc:
        return f"(Could not transcribe {ref.name}: {exc})"
    if not words:
        return f"(Attached {ref.name}: no words in it)"
    return format_attached_text(
        ref.name, words, note="transcribed", max_chars=MAX_CHARS_PER_ATTACHMENT
    )


async def read_email_attachments(
    gateway: MCPGateway,
    message_id: str,
    user_email: str,
    refs: list[EmailAttachmentRef],
) -> EmailAttachments:
    """This email's readable attachments, ready for the turn: the documents'
    text and the images' vision blocks (both empty when it has none)."""
    readable = [r for r in refs if Path(r.name).suffix.lower() in _READABLE_SUFFIXES]
    parts: list[str] = []
    for ref in readable[:MAX_ATTACHMENTS]:
        if ref.size_bytes > MAX_BYTES:
            parts.append(
                f"(Skipped {ref.name}: file too large — limit {MAX_BYTES // (1024 * 1024)} MB)"
            )
            continue
        parts.append(await _read_one(gateway, message_id, user_email, ref))
    extra = len(readable) - MAX_ATTACHMENTS
    if extra > 0:
        parts.append(
            f"(Skipped {extra} more attachment(s); download one with "
            "get_gmail_attachment_content and read it with read_document)"
        )

    images = [
        r for r in refs
        if Path(r.name).suffix.lower() in IMAGE_MEDIA_TYPES and r.size_bytes >= MIN_IMAGE_BYTES
    ]
    blocks: list[dict[str, Any]] = []
    for ref in images[:MAX_IMAGES]:
        if ref.size_bytes > MAX_IMAGE_BYTES:
            parts.append(
                f"(Skipped {ref.name}: image too large — limit {MAX_IMAGE_BYTES // (1024 * 1024)} MB)"
            )
            continue
        image = await _read_image(gateway, message_id, user_email, ref)
        if isinstance(image, str):
            parts.append(image)
        else:
            blocks.append(image)
    extra = len(images) - MAX_IMAGES
    if extra > 0:
        parts.append(f"(Skipped {extra} more image(s))")

    from openexecutive.providers import transcription

    if transcription.available():
        audio = [r for r in refs if Path(r.name).suffix.lower() in transcription.AUDIO_MEDIA_TYPES]
        for ref in audio[:MAX_AUDIO]:
            if ref.size_bytes > transcription.MAX_BYTES:
                parts.append(f"(Skipped {ref.name}: recording too large)")
                continue
            parts.append(await _read_audio(gateway, message_id, user_email, ref))
        extra = len(audio) - MAX_AUDIO
        if extra > 0:
            parts.append(f"(Skipped {extra} more recording(s))")
    return EmailAttachments("\n\n".join(parts), blocks)
