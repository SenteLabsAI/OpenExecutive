"""``update_reply_card``: hand back what the inbox's work turn did.

Offered only on that turn (``Session.reply_card_work``, started by
``delegation.card_work``), with Act as me's tools, so it is checked the same
way (``_writer``: the pinned person and their mailbox). It changes the reply
waiting on the person's own card, never sends it: the reply still goes from
its card (Send, or on its own when Approvals lets it).
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

UPDATE_REPLY_CARD = "update_reply_card"

UPDATE_REPLY_CARD_TOOL: dict[str, Any] = {
    "name": UPDATE_REPLY_CARD,
    "description": (
        "Put what you did on the reply card this turn is working on: the reply "
        "rewritten to deliver it, the files it should carry, and one line to the "
        "person saying what you did. Call it once, at the end. It changes the "
        "reply waiting on their card and sends nothing. With only `note`, the "
        "reply stays as it was (use that when the ask is theirs to do)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "note": {
                "type": "string",
                "description": "One short sentence to the person: what you did, or what is theirs to do.",
            },
            "body": {
                "type": "string",
                "description": "The whole reply in their voice, without a signature block. Omit to keep it.",
            },
            "artifact_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Artifacts to attach (ids draft_artifact returned), rendered as files.",
            },
            "files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Files from the email's own thread to attach, by handle (mail:<message_id>#<n>).",
            },
        },
        "required": ["note"],
    },
}

REPLY_CARD_TOOLS: list[dict[str, Any]] = [UPDATE_REPLY_CARD_TOOL]


def offered(session: Any) -> bool:
    """Whether ``session`` is the inbox's work turn on a reply card."""
    card = getattr(session, "reply_card_work", None)
    return isinstance(card, int) and not isinstance(card, bool) and card > 0


def _error(message: str) -> str:
    return json.dumps({"error": message})


def _made_since(created_at: str, since: datetime | None) -> bool:
    """Whether an artifact made at ``created_at`` was made on this turn."""
    if since is None:
        return False
    try:
        made = datetime.fromisoformat(str(created_at).replace(" ", "T").replace("Z", "+00:00"))
    except ValueError:
        return False
    if made.tzinfo is None:
        made = made.replace(tzinfo=UTC)
    return made >= since - timedelta(seconds=5)


async def _artifact_files(ids: list[str], since: datetime | None) -> list[tuple[str, str, bytes]] | str:
    from openexecutive.orchestrator.artifact_records import (
        ArtifactNotFound,
        MalformedArtifactId,
        current_viewer,
        load_artifact,
        render_artifact_file,
    )

    viewer = current_viewer()
    out: list[tuple[str, str, bytes]] = []
    for artifact_id in dict.fromkeys(i.strip() for i in ids if i.strip()):
        try:
            rec = await asyncio.to_thread(load_artifact, artifact_id, viewer=viewer)
            if rec.kind != "draft" or not _made_since(rec.created_at, since):
                return f"{artifact_id}: only what draft_artifact made on this turn can go with the reply"
            file = await asyncio.to_thread(render_artifact_file, rec, None)
        except (MalformedArtifactId, ArtifactNotFound) as exc:
            return f"{artifact_id}: {exc}"
        except Exception:
            logger.exception("update_reply_card: couldn't render %s", artifact_id)
            return f"{artifact_id}: it couldn't be rendered"
        out.append((file.filename, file.mime, file.content))
    return out


async def _outside_the_thread(handles: list[str], mailbox: Any, thread_id: str) -> str:
    """Why a mailbox file can't go with this reply, or "". Only the files of
    the email's own thread can: a reply never carries other mail of theirs."""
    from openexecutive.delegation.gmail import GmailError
    from openexecutive.orchestrator import file_refs

    for handle in handles:
        try:
            ref = file_refs.parse(handle)
        except file_refs.RefError as exc:
            return str(exc)
        if ref.kind != "mail":
            continue
        try:
            message = await mailbox.get_message(ref.value)
        except GmailError:
            return f"{ref}: couldn't read it from their mailbox"
        if not thread_id or getattr(message, "thread_id", None) != thread_id:
            return f"{ref}: only files from the email's own thread can go with this reply"
    return ""


def _strings(value: Any) -> list[str]:
    return [str(v) for v in value if isinstance(v, str)] if isinstance(value, list) else []


async def handle_update_reply_card(tool_input: dict[str, Any]) -> str:
    from openexecutive.delegation import card_work
    from openexecutive.delegation.reply_send import SendRefused
    from openexecutive.orchestrator import file_refs
    from openexecutive.orchestrator.delegation_tools import _writer
    from openexecutive.orchestrator.schedule_tools import current_session

    session = current_session.get()
    if not offered(session):
        return _error(f"{UPDATE_REPLY_CARD} is not available on this turn. Do not retry.")
    card_id = int(session.reply_card_work)
    writer = _writer(UPDATE_REPLY_CARD)
    if isinstance(writer, str):
        return writer
    files: list[tuple[str, str, bytes]] = []
    artifact_ids = _strings(tool_input.get("artifact_ids"))
    if artifact_ids:
        rendered = await _artifact_files(artifact_ids, card_work.started_at(card_id))
        if isinstance(rendered, str):
            return _error(f"Nothing was changed: {rendered}.")
        files += rendered
    handles = _strings(tool_input.get("files"))
    if handles:
        refused = await _outside_the_thread(handles, writer.mailbox, card_work.thread_of(card_id))
        if refused:
            return _error(f"Nothing was changed: {refused}.")
        try:
            resolved = await file_refs.resolve(handles, max_bytes=card_work.MAX_BYTES)
        except file_refs.RefError as exc:
            return _error(f"Nothing was changed: {exc}.")
        files += [(f.name, f.mime, f.data) for f in resolved.items]
    body = tool_input.get("body")
    try:
        await card_work.apply(
            card_id, writer.person, body=body if isinstance(body, str) else "",
            note=str(tool_input.get("note") or ""), files=files, gmail=writer.mailbox,
        )
    except SendRefused as refused:
        return _error(f"Nothing was changed: {refused.message}")
    card_work.keep(card_work.finished(writer.person, card_id, gmail=writer.mailbox))
    return json.dumps({
        "status": "ok",
        "card": card_id,
        "files": [name for name, _, _ in files],
        "detail": "The reply on their card now carries this. It goes from the card; you are done.",
    })


REPLY_CARD_TOOL_HANDLERS: dict[str, Any] = {UPDATE_REPLY_CARD: handle_update_reply_card}
