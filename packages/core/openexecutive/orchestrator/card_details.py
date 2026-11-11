"""What an Approve card shows of the step it holds, in plain words.

A held step keeps the exact call (``payload["input"]``) so Approve does just
that. The card shows the person what it does instead: a plain label for each
field, a file's kind and size in place of its bytes, a link in place of a
Drive id, and none of the plumbing (the Executive's own account, a checksum,
a mail thread id). Every place a card is drawn reads this: the web card on
Today and in the chat (``department_gate.hold``, ``api.routes.decisions``) and
the card in a chat app (``integrations.channel_cards``).
"""
from __future__ import annotations

import json
import re
from typing import Any

# A served payload keeps a field's value up to this long; longer (a file's
# bytes) is left out of what the page downloads. Approve reads the stored row.
SERVED_MAX = 20_000

# Who a Google Workspace call runs as: always the Executive's own account,
# which the gateway checks (``_check_acting_account``). For any other app it
# may pick the mailbox, so it is shown.
_GOOGLE_ACCOUNT = frozenset({"user_google_email", "user_email", "account", "account_id", "acting_account"})
# Plumbing: checksums, a file's type (shown with its size), and the ids a
# reply or a draft threads on (``_REFS``, shown only when nothing else is).
_HIDDEN = frozenset({
    "base64_sha256", "sha256", "content_sha256", "checksum",
    "content_mime_type", "mime_type", "mimetype", "content_type",
    "thread_id", "message_id", "in_reply_to", "references", "draft_id", "idempotency_key",
    "request_id", "supports_all_drives",
})
_REFS = frozenset({"thread_id", "message_id", "in_reply_to", "references", "draft_id"})
# Shown only when not the default: an event on another calendar, a file in
# a shared drive.
_SHOWN_UNLESS_DEFAULT: dict[str, frozenset[str]] = {
    "calendar_id": frozenset({"primary"}), "drive_id": frozenset({"root", "my-drive"}),
}

_LABELS: dict[str, str] = {
    "to": "To", "cc": "Cc", "bcc": "Bcc", "subject": "Subject",
    "body": "Message", "text": "Message", "message": "Message", "html_body": "Message",
    "file_name": "File name", "filename": "File name", "name": "Name", "title": "Title",
    "folder_id": "Folder", "parent_folder_id": "Folder", "parent_id": "Folder", "parents": "Folder",
    "file_id": "File", "document_id": "Document", "spreadsheet_id": "Spreadsheet",
    "presentation_id": "Presentation",
    "start": "Start", "end": "End", "start_time": "Start", "end_time": "End",
    "attendees": "Guests", "description": "Description", "location": "Where",
    "attachments": "Attachments", "content": "Content",
}

_FOLDER_KEYS = frozenset({"folder_id", "parent_folder_id", "parent_id", "parents"})
_FILE_KEYS = frozenset({"file_id", "document_id", "spreadsheet_id", "presentation_id"})

_KINDS: dict[str, str] = {
    "pdf": "PDF", "doc": "Word", "docx": "Word", "xls": "Excel", "xlsx": "Excel", "csv": "CSV",
    "ppt": "PowerPoint", "pptx": "PowerPoint", "png": "Image", "jpg": "Image", "jpeg": "Image",
    "gif": "Image", "webp": "Image", "txt": "Text", "zip": "Zip", "mp3": "Audio", "m4a": "Audio",
    "wav": "Audio", "mp4": "Video", "mov": "Video",
}
_MIME_KINDS: dict[str, str] = {
    "application/pdf": "PDF", "text/csv": "CSV", "text/plain": "Text", "application/zip": "Zip",
}

_BASE64 = re.compile(r"[A-Za-z0-9+/_\-\r\n]+={0,2}")
_DRIVE_ID = re.compile(r"^[A-Za-z0-9_\-]{10,}$")


def _is_bytes(key: str, value: Any) -> bool:
    """Whether ``value`` is a file's bytes (base64), not words to read."""
    # Only what reads as base64 all through: words under a key like "data"
    # are shown, since Approve sends them.
    if not isinstance(value, str) or len(value) < 64 or not _BASE64.fullmatch(value):
        return False
    if "base64" in key.lower() or key.lower() in {"data", "file_data", "file_content", "bytes", "blob"}:
        return True
    return len(value) > 1000


def _size(value: str) -> str:
    raw = len(value.replace("\n", "").replace("\r", "")) * 3 // 4
    if raw >= 1024 * 1024:
        return f"{raw / (1024 * 1024):.1f} MB"
    if raw >= 1024:
        return f"{round(raw / 1024)} KB"
    return f"{raw} bytes"


def _kind(tool_input: dict[str, Any]) -> str:
    for key in ("content_mime_type", "mime_type", "mimetype", "content_type"):
        mime = tool_input.get(key)
        if isinstance(mime, str) and mime:
            mime = mime.split(";")[0].strip().lower()
            if mime in _MIME_KINDS:
                return _MIME_KINDS[mime]
            if mime.startswith("image/"):
                return "Image"
            if "spreadsheet" in mime or "excel" in mime:
                return "Excel"
            if "word" in mime:
                return "Word"
    for key in ("file_name", "filename", "name", "title"):
        name = tool_input.get(key)
        if isinstance(name, str) and "." in name:
            ext = name.rsplit(".", 1)[-1].lower()
            if ext in _KINDS:
                return _KINDS[ext]
    return ""


def _label(key: str) -> str:
    norm = key.strip().lower()
    if norm in _LABELS:
        return _LABELS[norm]
    words = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", key).replace("_", " ").replace("-", " ").split()
    return " ".join(words).capitalize() or key


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(v, (str, int, float)) for v in value):
        return ", ".join(str(v) for v in value)
    return json.dumps(value, ensure_ascii=False, default=str)


def _drive_link(key: str, value: Any) -> str | None:
    ident = value[0] if isinstance(value, list) and len(value) == 1 else value
    if not isinstance(ident, str) or not _DRIVE_ID.match(ident):
        return None
    if key in _FOLDER_KEYS:
        return f"https://drive.google.com/drive/folders/{ident}"
    return f"https://drive.google.com/open?id={ident}"


def plain(tool: str, tool_input: Any) -> list[dict[str, str]]:
    """Each field of the call worth showing, as ``{"field", "value"}`` in
    plain words: never a file's bytes, an internal name or the plumbing."""
    if not isinstance(tool_input, dict):
        return []
    google = isinstance(tool, str) and tool.startswith("google_workspace__")
    out: list[dict[str, str]] = []
    refs: list[dict[str, str]] = []
    seen: set[str] = set()
    for key, value in tool_input.items():
        key = str(key)
        norm = key.strip().lower()
        if norm in _REFS and value not in (None, "", [], {}):
            refs.append({"field": _label(key), "value": _text(value)})
            continue
        if (google and norm in _GOOGLE_ACCOUNT) or norm in _HIDDEN or value is None or value == "" or value == [] or value == {}:
            continue
        if norm in _SHOWN_UNLESS_DEFAULT and str(value).strip().lower() in _SHOWN_UNLESS_DEFAULT[norm]:
            continue
        if _is_bytes(key, value):
            kind = _kind(tool_input)
            field, text = "File", f"{kind + ' file' if kind else 'A file'}, {_size(value)}"
        elif google and (norm in _FOLDER_KEYS or norm in _FILE_KEYS) and _drive_link(norm, value):
            field, text = _label(key), str(_drive_link(norm, value))
        else:
            # Words are shown whole: Approve does exactly what the card shows.
            field, text = _label(key), _text(value)
        if field in seen:
            field = f"{field} ({key.replace('_', ' ')})"
        seen.add(field)
        out.append({"field": field, "value": text})
    # A call that names only what it acts on (send this draft, delete this
    # message) still shows that, never an empty card.
    return out or refs


def held_steps(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Every call a held card runs on Approve: its ``steps`` when one card
    holds several (levels.hold_many), else its one ``input``."""
    steps = payload.get("steps")
    if isinstance(steps, list) and len(steps) > 1 and all(isinstance(s, dict) for s in steps):
        return steps
    tool_input = payload.get("input")
    return [tool_input] if isinstance(tool_input, dict) else []


def plain_steps(tool: str, steps: list[dict[str, Any]]) -> list[dict[str, str]]:
    """``plain`` for each call one card holds, numbered when there are
    several, so the card shows everything Approve does."""
    if len(steps) == 1:
        return plain(tool, steps[0])
    return [
        {"field": f"{n}. {detail['field']}", "value": detail["value"]}
        for n, step in enumerate(steps, 1)
        for detail in plain(tool, step)
    ]


def served(payload: dict[str, Any]) -> dict[str, Any]:
    """A held step's payload as a page reads it: its details in plain words
    (cards held before this read raw), and no field long enough to be a
    file's bytes. Approve never reads this copy."""
    tool_input = payload.get("input")
    if not isinstance(tool_input, dict):
        return payload
    out = dict(payload)
    out["details"] = plain_steps(str(payload.get("tool") or ""), held_steps(payload))
    def short(step: dict[str, Any]) -> dict[str, Any]:
        return {k: ("" if isinstance(v, str) and len(v) > SERVED_MAX else v) for k, v in step.items()}

    out["input"] = short(tool_input)
    if isinstance(payload.get("steps"), list):
        out["steps"] = [short(s) if isinstance(s, dict) else s for s in payload["steps"]]
    return out
