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

Everything is best-effort: a failure becomes a one-line note in the turn, and
the Executive can still use ``read_document`` on a file it downloads itself.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openexecutive.orchestrator.mcp_gateway import MCPGateway

logger = logging.getLogger(__name__)

# Per email: bounds the downloads (and OCR / model work) one message can cause.
MAX_ATTACHMENTS = 5
MAX_BYTES = 20 * 1024 * 1024
# Text inlined per attachment; the rest is one read_document call away.
MAX_CHARS_PER_ATTACHMENT = 15_000

_READABLE_SUFFIXES = frozenset({".pdf", ".docx", ".xlsx", ".xlsm", ".csv", ".md", ".txt"})
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


async def _read_one(
    gateway: MCPGateway, message_id: str, user_email: str, ref: EmailAttachmentRef
) -> str:
    from openexecutive.integrations.attachments import format_attached_text
    from openexecutive.knowledge.loader import read_document_text
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
    resolved = resolve_readable_file(saved)
    if isinstance(resolved, str):
        logger.warning("email attachment not read: %s", resolved)
        return f"(Could not read {ref.name})"

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


async def read_email_attachments(
    gateway: MCPGateway,
    message_id: str,
    user_email: str,
    refs: list[EmailAttachmentRef],
) -> str:
    """The text of this email's readable attachments, ready to append to the
    turn — ``""`` when it has none."""
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
    return "\n\n".join(parts)
