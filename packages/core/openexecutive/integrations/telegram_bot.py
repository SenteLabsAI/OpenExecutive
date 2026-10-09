from __future__ import annotations

import asyncio
import hmac
import logging
import re
from typing import Any

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from openexecutive.config import get_settings
from openexecutive.orchestrator.people_tools import audit_rows_on_senders_turn

logger = logging.getLogger(__name__)
router = APIRouter()

_TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
# Telegram's 4096 limit is in UTF-16 code units; emoji count double.
# Use 2000 chars as a conservative safe limit.
_MAX_MSG_LEN = 2000

# Module-level client — reused across requests to avoid per-call TLS handshakes.
_http_client: httpx.AsyncClient | None = None

# Per-chat lock so two messages from the same chat serialize — without this,
# concurrent handlers both load stale history and write interleaved turns.
_chat_locks: dict[int, asyncio.Lock] = {}


def _chat_lock(chat_id: int) -> asyncio.Lock:
    lock = _chat_locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        _chat_locks[chat_id] = lock
    return lock


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=30)
    return _http_client


def _tg_url(token: str, method: str) -> str:
    return _TELEGRAM_API.format(token=token, method=method)


def _split_message(text: str) -> list[str]:
    """Split a long response into ≤_MAX_MSG_LEN-char chunks on paragraph boundaries."""
    if len(text) <= _MAX_MSG_LEN:
        return [text]
    chunks: list[str] = []
    while text:
        if len(text) <= _MAX_MSG_LEN:
            chunks.append(text)
            break
        split_at = text.rfind("\n\n", 0, _MAX_MSG_LEN)
        if split_at <= 0:
            split_at = text.rfind("\n", 0, _MAX_MSG_LEN)
        if split_at <= 0:
            split_at = _MAX_MSG_LEN
        chunk = text[:split_at].strip()
        if chunk:
            chunks.append(chunk)
        remainder = text[split_at:].strip()
        if remainder == text:
            # No progress — force a hard split to avoid infinite loop.
            chunks.append(text[:_MAX_MSG_LEN])
            text = text[_MAX_MSG_LEN:]
        else:
            text = remainder
    return [c for c in chunks if c]


async def send_message(token: str, chat_id: int, text: str) -> str | None:
    """Send one or more messages to a Telegram chat, splitting if needed.

    Returns the Telegram message_id of the last chunk sent (best-effort —
    ``None`` if no chunk delivered or the response lacks one), so callers can
    link an outbound DM to a later reply. Existing callers ignore the return."""
    client = _get_http_client()
    last_message_id: str | None = None
    for chunk in _split_message(text):
        if not chunk:
            continue
        resp = await client.post(
            _tg_url(token, "sendMessage"),
            # No link previews: Telegram's server would fetch a link in the
            # text on its own, and a reply can quote mail a sender wrote to
            # carry data out in one.
            json={"chat_id": chat_id, "text": chunk, "link_preview_options": {"is_disabled": True}},
        )
        if resp.is_error:
            logger.error(
                "Telegram sendMessage failed: %s %s", resp.status_code, resp.text
            )
            continue
        try:
            mid = resp.json().get("result", {}).get("message_id")
            if mid is not None:
                last_message_id = str(mid)
        except (ValueError, TypeError, AttributeError):
            pass
    return last_message_id


async def _get_telegram_file_bytes(token: str, file_id: str) -> tuple[str, bytes]:
    """Resolve a Telegram file_id to a download URL and fetch the bytes.

    Returns ``(file_path_on_tg_servers, data)``.  Raises on any error so the
    caller can skip the attachment and log it.
    """
    from openexecutive.integrations.attachments import download_bytes

    client = _get_http_client()
    resp = await client.get(_tg_url(token, f"getFile?file_id={file_id}"))
    resp.raise_for_status()
    result = resp.json().get("result", {})
    file_path = result.get("file_path", "")
    if not file_path:
        raise ValueError(f"Telegram getFile returned no file_path for file_id={file_id}")

    url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    data = await download_bytes(url)
    return file_path, data


def _find_telegram_sender(chat_id: str) -> object:
    from openexecutive.people.store import find_person_by_telegram_chat_id

    return find_person_by_telegram_chat_id(chat_id)


# The rows this handler writes before the turn binds its session (the inbound
# row, the knowledge retrieval, alert triage) are private when the principal
# sent the message from a verified chat and they name one of their contacts.
@audit_rows_on_senders_turn(
    "telegram",
    sender_ref=lambda args: str(args["chat_id"]),
    find_sender=_find_telegram_sender,
)
async def _process_and_reply(
    message_text: str,
    sender_name: str,
    chat_id: int,
    message_id: int,
    token: str,
    attachment_file_ids: list[tuple[str, str, str]] | None = None,
) -> None:
    """Handle one inbound Telegram message.

    ``attachment_file_ids`` is a list of ``(file_id, filename, content_type)``
    tuples for any files or photos attached to the message.
    """
    # Per-conversation session id — stamped on every audit row (inbound,
    # chat_turn, specialist_consult, tool_invocation) so a single request can
    # be followed end-to-end in /audit. Must match the value used when the
    # Session is constructed below. The chat is one stream, `telegram:{chat_id}`,
    # cut into conversations after a quiet gap or a /new (memory.conversations).
    from openexecutive.integrations.conversations import conversation_for

    session_id = await asyncio.to_thread(conversation_for, f"telegram:{chat_id}")

    from openexecutive.audit import log_event as audit_log
    audit_log(
        "integration_inbound",
        f"Inbound telegram from {sender_name} (chat_id={chat_id}): {message_text[:160]}",
        actor="telegram",
        session_id=session_id,
        details={
            "channel": "telegram",
            "chat_id": chat_id,
            "message_id": message_id,
            "sender": sender_name,
            "text_len": len(message_text),
        },
    )
    # WaitForHuman inbound resolver — check BEFORE alert triage.
    from openexecutive.people.store import find_person_by_telegram_chat_id
    from openexecutive.workflows.inbound_resolver import resolve_and_acknowledge

    async def _send_ack(text: str) -> None:
        await send_message(token, chat_id, text)

    _tg_person = find_person_by_telegram_chat_id(str(chat_id))
    if (
        _tg_person is not None
        and _tg_person.id is not None
        and await resolve_and_acknowledge(
            channel="telegram",
            channel_ref=str(chat_id),
            person_id=_tg_person.id,
            text=message_text,
            send=_send_ack,
            message_id=str(message_id),
            in_reply_to="",
            session_ids=[session_id],
        )
    ):
        return

    # Fork into alert triage pipeline (fire-and-forget, same pattern as other integrations).
    try:
        from openexecutive.alerts.models import AlertEvent
        from openexecutive.alerts.pipeline import schedule_evaluation

        schedule_evaluation(
            AlertEvent(
                source="telegram",
                external_id=str(message_id),
                body=message_text,
                user=sender_name,
            )
        )
    except Exception:
        logger.exception("Telegram: failed to schedule alert evaluation")

    # Process file / photo attachments before entering the chat lock so that
    # slow downloads don't hold the lock while the chat serialises.
    att_image_blocks: list[dict] = []
    if attachment_file_ids:
        try:
            from openexecutive.integrations.attachments import build_attachment_output

            for file_id, filename, content_type in attachment_file_ids:
                try:
                    _file_path, data = await _get_telegram_file_bytes(token, file_id)
                    # build_attachment_output works on bytes directly — we
                    # don't need AttachmentItem/process_attachments here since
                    # Telegram requires a separate getFile API call rather than
                    # a direct URL download.
                    att_text, img_blocks = await build_attachment_output(
                        filename, data, content_type
                    )
                    if att_text:
                        message_text = (
                            f"{att_text}\n\n{message_text}" if message_text else att_text
                        )
                    att_image_blocks.extend(img_blocks)
                except Exception:
                    logger.exception(
                        "Telegram: failed to download/process attachment file_id=%s", file_id
                    )
        except Exception:
            logger.exception("Telegram: attachment processing setup failed")

    from openexecutive.integrations.channel_context import (
        attach_briefing_context,
        build_channel_context_block,
    )
    from openexecutive.knowledge.retriever import retrieve
    from openexecutive.memory.episodic import format_for_prompt
    from openexecutive.memory.session_store import (
        create_session,
        load_messages,
        save_message,
        update_session_timestamp,
    )
    from openexecutive.onboarding.profile_builder import load_or_create_profile
    from openexecutive.orchestrator.executive import Executive
    from openexecutive.orchestrator.mcp_gateway import get_active_gateway
    from openexecutive.orchestrator.session import Session

    response: str | None = None
    async with _chat_lock(chat_id):
        # Again under the lock: a /new queued behind an earlier turn may have
        # started the next conversation since this message was picked up.
        session_id = await asyncio.to_thread(conversation_for, f"telegram:{chat_id}")
        try:
            profile = load_or_create_profile()
            # See the note in slack_bot: lets an approval gate raised in
            # this turn be answered by a reply in this same conversation.
            session = Session(
                session_id=session_id,
                company_profile=profile if not profile.is_empty() else None,
                origin_channel="telegram",
                origin_channel_ref=str(chat_id),
            )
            history = load_messages(session_id)
            if history:
                session.conversation_history = history
            # Record this channel_ref as user-initiated so the Executive may
            # schedule follow-ups back to this chat.
            session.seen_channel_refs.add(("telegram", str(chat_id)))
            retrieved_context = retrieve(query=message_text)
            episodic_context = format_for_prompt(session_id=session_id)

            # Look up the OE Person record so Honcho can key per-person
            # memory off Person.id (shared across channels). No match →
            # person_id stays None and the Honcho layer no-ops.
            from openexecutive.people.store import find_person_by_telegram_chat_id
            person = find_person_by_telegram_chat_id(str(chat_id))
            person_id = person.id if person else None
            # Bound here rather than at construction because the id is only
            # resolved now; an approval gate raised later in this turn reads it.
            session.caller_person_id = person_id

            # Hydrate with the context of any recent outbound DM oe sent this
            # chat, so a reply oe solicited from another session lands with its
            # backstory. Injected into the model's copy only — message_text is
            # persisted to history below and must stay free of the one-shot
            # block. One-shot consumed inside the helper.
            #
            # Gated to 1:1 private chats (positive chat_id; Telegram groups and
            # channels are negative) so private outbound context can never be
            # pulled into a group — mirrors the Discord is_dm / Slack mode=="dm"
            # guards.
            chat_user_message = message_text
            if chat_id > 0:
                from openexecutive.integrations.inbound_hydration import (
                    hydrate_user_message,
                )

                chat_user_message = hydrate_user_message(
                    channel="telegram",
                    channel_ref=str(chat_id),
                    user_message=message_text,
                )

            briefing_context = await asyncio.to_thread(
                attach_briefing_context,
                session,
                # Telegram private chats are 1:1; a group chat is not.
                is_dm=(str(chat_id).lstrip("-").isdigit() and not str(chat_id).startswith("-")),
                person=person,
            )

            response = await Executive(mcp_gateway=get_active_gateway()).chat(
                user_message=chat_user_message,
                session=session,
                retrieved_context=retrieved_context,
                episodic_context=episodic_context,
                attachment_blocks=att_image_blocks or None,
                briefing_context=briefing_context,
                channel_context_block=build_channel_context_block("telegram"),
                person_id=person_id,
            )
            await send_message(token, chat_id, response)
        except Exception:
            logger.exception("Telegram: handler error for message %s", message_id)
            try:
                await send_message(
                    token,
                    chat_id,
                    "I encountered an error processing your request. Please try again.",
                )
            except Exception:
                logger.exception("Telegram: also failed to send error reply")
            return

        # Persist only after a successful reply. Failures here must not trigger
        # a user-facing error — the user already got their answer.
        #
        # Channel sessions are owned by the resolved sender if mapped to a
        # Person, otherwise fall back to the principal so unrostered channel
        # threads still appear in the operator's sidebar (instead of becoming
        # invisible NULL-owner rows).
        from openexecutive.people.store import find_principal_person
        session_owner_id = person_id
        if session_owner_id is None:
            principal = find_principal_person()
            session_owner_id = principal.id if principal is not None else None
        try:
            create_session(
                session_id,
                f"Telegram {sender_name}",
                session.created_at.isoformat(),
                caller_person_id=session_owner_id,
            )
            save_message(session_id, "user", message_text, sender_person_id=person_id)
            save_message(session_id, "assistant", response)
            update_session_timestamp(session_id)
        except Exception:
            logger.exception(
                "Telegram: failed to persist turn for session %s", session_id
            )
        else:
            from openexecutive.integrations.conversations import title_after_turn

            title_after_turn(session_id, message_text, response)


async def _start_fresh_and_reply(chat_id: int, token: str) -> None:
    """Answer /new: start a new conversation in this chat and say so."""
    from openexecutive.integrations.conversations import start_fresh
    from openexecutive.people.store import find_person_by_telegram_chat_id

    async with _chat_lock(chat_id):
        person = await asyncio.to_thread(find_person_by_telegram_chat_id, str(chat_id))
        reply = await asyncio.to_thread(
            start_fresh, f"telegram:{chat_id}", person.id if person is not None else None
        )
    try:
        await send_message(token, chat_id, reply)
    except Exception:
        logger.exception("Telegram: failed to confirm /new for chat %s", chat_id)


# Known bot commands that should be stripped before passing to the Executive.
_COMMAND_RE = re.compile(r"^/(start|help|ask)(?:@\w+)?\s*", re.IGNORECASE)


def _message_content(message: dict[str, Any]) -> tuple[str, list[tuple[str, str, str]]]:
    """A Telegram message's text (known bot commands stripped) and its
    attachments as ``(file_id, filename, content_type)``. Downloads happen
    later, in ``_process_and_reply``, so the webhook stays fast."""
    text: str = message.get("text", "") or message.get("caption", "")
    text = text.strip()
    # Only strip known bot commands (/start, /help, /ask), not arbitrary slash-prefixed content.
    text = _COMMAND_RE.sub("", text).strip()

    attachment_file_ids: list[tuple[str, str, str]] = []

    # Single document (any file type).
    doc = message.get("document")
    if doc:
        file_id = doc.get("file_id", "")
        filename = doc.get("file_name") or f"file_{file_id}"
        content_type = doc.get("mime_type") or ""
        if file_id:
            attachment_file_ids.append((file_id, filename, content_type))

    # Photos — Telegram sends an array of sizes; pick the largest.
    photos = message.get("photo")
    if photos and isinstance(photos, list) and photos:
        largest = max(photos, key=lambda p: p.get("file_size", 0))
        file_id = largest.get("file_id", "")
        if file_id:
            attachment_file_ids.append((file_id, f"photo_{file_id}.jpg", "image/jpeg"))
    return text, attachment_file_ids


async def _hold_unknown_sender(
    *,
    message_text: str,
    sender_name: str,
    chat_id: int,
    message_id: int,
    token: str,
    attachment_file_ids: list[tuple[str, str, str]],
) -> None:
    """Hold a private-chat message from someone off the roster for the
    principal to confirm (``integrations.roster_intake``) and tell them it
    arrived. One of the principal's contacts gets nothing: contacts have no
    chat access."""
    from openexecutive.integrations import roster_intake
    from openexecutive.people.store import find_person_by_telegram_chat_id

    if await asyncio.to_thread(
        find_person_by_telegram_chat_id, str(chat_id), include_contacts=True
    ):
        return

    async def _ack(text: str) -> None:
        await send_message(token, chat_id, text)

    await roster_intake.intake(
        "telegram", str(chat_id),
        external_id=str(message_id),
        payload={
            "message_text": message_text,
            "sender_name": sender_name,
            "chat_id": chat_id,
            "message_id": message_id,
            "attachment_file_ids": [list(a) for a in attachment_file_ids],
        },
        preview=message_text,
        display_name=sender_name if not sender_name.startswith("chat:") else "",
        send_ack=_ack,
    )


async def _replay_held(message: Any, _request: Any) -> bool:
    """Replay a message held while its chat was off the roster."""
    token = get_settings().telegram_bot_token
    payload = message.payload
    if not token or not payload.get("chat_id"):
        return False
    attachments = [
        (str(a[0]), str(a[1]), str(a[2]))
        for a in payload.get("attachment_file_ids") or []
        if isinstance(a, (list, tuple)) and len(a) == 3
    ]
    await _process_and_reply(
        message_text=str(payload.get("message_text") or ""),
        sender_name=str(payload.get("sender_name") or ""),
        chat_id=int(payload["chat_id"]),
        message_id=int(payload.get("message_id") or 0),
        token=token,
        attachment_file_ids=attachments or None,
    )
    return True


def _register_replayer() -> None:
    from openexecutive.integrations.roster_intake import register_replayer

    register_replayer("telegram", _replay_held)


_register_replayer()


@router.post("/webhook/telegram", status_code=200)
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks) -> dict[str, Any]:
    settings = get_settings()

    if not settings.telegram_bot_token:
        raise HTTPException(status_code=503, detail="Telegram integration not configured")

    # Verify the secret token Telegram sends in the header (set when registering the webhook).
    if settings.telegram_webhook_secret:
        if not settings.telegram_webhook_secret_valid:
            # Telegram can never send such a value (setWebhook refuses it), so
            # the only request that could match it is a forged one.
            logger.warning(
                "Telegram: TELEGRAM_WEBHOOK_SECRET is not a value Telegram can send "
                "(1-256 of A-Z a-z 0-9 _ -); refusing every update until it is fixed"
            )
            raise HTTPException(status_code=401, detail="Invalid webhook secret")
        sent = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(sent, settings.telegram_webhook_secret):
            logger.warning("Telegram: webhook secret mismatch")
            raise HTTPException(status_code=401, detail="Invalid webhook secret")

    # Parse body — return 200 on failure so Telegram doesn't retry bad payloads.
    try:
        body = await request.json()
    except Exception:
        logger.warning("Telegram: failed to parse JSON body")
        return {}

    # Only handle regular messages (ignore channel posts, edited messages, etc.)
    message = body.get("message")
    if not message:
        return {}

    # Extract fields safely — malformed payloads return 200 to stop Telegram retries.
    try:
        chat_id: int = message["chat"]["id"]
        message_id: int = message["message_id"]
    except (KeyError, TypeError):
        logger.warning("Telegram: malformed message payload, missing chat.id or message_id")
        return {}

    from_user: dict[str, Any] = message.get("from") or {}
    sender_name = " ".join(
        filter(None, [from_user.get("first_name"), from_user.get("last_name")])
    ) or from_user.get("username") or f"chat:{chat_id}"

    # Roster gate. The Telegram chat must match a non-archived Person
    # with telegram_chat_id set. Manage access via the /people UI; the
    # old TELEGRAM_ALLOWED_CHAT_IDS env var has been removed.
    from openexecutive.audit import log_event as audit_log
    from openexecutive.people.store import find_person_by_telegram_chat_id

    if find_person_by_telegram_chat_id(str(chat_id)) is None:
        logger.warning(
            "Telegram: rejected message from chat_id=%s (not in People roster)",
            chat_id,
        )
        audit_log(
            "integration_inbound",
            f"Rejected: telegram chat_id={chat_id} not in People roster",
            actor="telegram",
            details={
                "channel": "telegram",
                "chat_id": chat_id,
                "outcome": "rejected_unknown_sender",
            },
        )
        # A private chat, on a webhook only Telegram can call: hold the
        # message for the principal to confirm and tell the sender it
        # arrived. A group (everyone in it) or an unverified webhook (anyone
        # could forge the update) stays silent, as before.
        chat = message.get("chat") or {}
        if (
            chat.get("type") == "private"
            and settings.telegram_webhook_secret
            and settings.telegram_webhook_secret_valid
        ):
            text, attachment_file_ids = _message_content(message)
            if text or attachment_file_ids:
                background_tasks.add_task(
                    _hold_unknown_sender,
                    message_text=text,
                    sender_name=sender_name,
                    chat_id=chat_id,
                    message_id=message_id,
                    token=settings.telegram_bot_token,
                    attachment_file_ids=attachment_file_ids,
                )
        return {}

    text, attachment_file_ids = _message_content(message)

    # Require either text or at least one attachment to proceed.
    if not text and not attachment_file_ids:
        return {}

    from openexecutive.integrations.conversations import is_new_conversation_command

    if not attachment_file_ids and is_new_conversation_command(text):
        background_tasks.add_task(
            _start_fresh_and_reply, chat_id=chat_id, token=settings.telegram_bot_token
        )
        return {}

    background_tasks.add_task(
        _process_and_reply,
        message_text=text,
        sender_name=sender_name,
        chat_id=chat_id,
        message_id=message_id,
        token=settings.telegram_bot_token,
        attachment_file_ids=attachment_file_ids or None,
    )
    return {}
