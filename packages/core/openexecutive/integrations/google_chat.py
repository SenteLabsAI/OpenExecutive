from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from openexecutive.config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter()


_GOOGLE_CHAT_ISSUER = "chat@system.gserviceaccount.com"
_GOOGLE_CHAT_CERTS_URL = f"https://www.googleapis.com/service_accounts/v1/metadata/x509/{_GOOGLE_CHAT_ISSUER}"


def verify_google_jwt(token: str, project_number: str) -> Mapping[str, Any]:
    """Verify a Bearer JWT from a Google Chat webhook.

    Google Chat sets the JWT audience to the GCP project number (numeric string)
    and iss to chat@system.gserviceaccount.com.
    Raises google.auth.exceptions.TransportError or ValueError on failure.
    """
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    # Chat signs with its own service account's keys, not Google's OAuth
    # ones, which ``verify_token`` checks by default.
    claims = id_token.verify_token(
        token, google_requests.Request(), audience=project_number, certs_url=_GOOGLE_CHAT_CERTS_URL
    )
    if claims.get("iss") != _GOOGLE_CHAT_ISSUER:
        raise ValueError(f"Unexpected issuer: {claims.get('iss')!r}")
    return claims


_CHAT_SCOPES = ["https://www.googleapis.com/auth/chat.bot"]


def _build_credentials(
    service_account_file: str | None,
    service_account_email: str | None,
) -> Any:
    """Return Google credentials for the Chat REST API.

    Auth method is selected by which env vars are set:

    1. Key file  — GOOGLE_CHAT_SERVICE_ACCOUNT_FILE is set.
       Standard service-account JSON key.  Blocked by the org policy
       ``iam.disableServiceAccountKeyCreation``.

    2. Impersonation — GOOGLE_CHAT_SERVICE_ACCOUNT_EMAIL is set (no key file).
       Uses Application Default Credentials (ADC) to impersonate the service
       account.  Works when the org policy blocks key creation.
       Local setup: ``gcloud auth application-default login``, then grant your
       user account ``roles/iam.serviceAccountTokenCreator`` on the SA.

    3. ADC direct — neither var is set.
       Uses the ambient credential (metadata server on Cloud Run / GCE / GKE,
       or whatever ``gcloud auth application-default login`` produced locally).
       The executing identity must already be the Chat bot SA or have chat.bot
       scope.
    """
    if service_account_file:
        from google.oauth2 import service_account as sa_module

        return sa_module.Credentials.from_service_account_file(
            service_account_file, scopes=_CHAT_SCOPES
        )

    import google.auth

    if service_account_email:
        # Impersonation requires the source credential to call the IAM Credentials
        # API, which needs cloud-platform scope — NOT chat.bot scope.
        from google.auth import impersonated_credentials

        base_creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        return impersonated_credentials.Credentials(
            source_credentials=base_creds,
            target_principal=service_account_email,
            target_scopes=_CHAT_SCOPES,
            lifetime=3600,
        )

    # ADC direct: works on Cloud Run / GCE / GKE where the compute identity
    # is already the Chat bot service account.
    base_creds, _ = google.auth.default(scopes=_CHAT_SCOPES)
    return base_creds


def send_reply(
    space_name: str,
    thread_name: str,
    text: str,
    service_account_file: str | None,
    service_account_email: str | None = None,
) -> None:
    """Post a reply to a Google Chat thread via the REST API.

    This is a synchronous function; call it via asyncio.to_thread() from async contexts.
    thread_name may be empty for DMs — in that case no thread key is sent.
    """
    from googleapiclient.discovery import build as google_build

    creds = _build_credentials(service_account_file, service_account_email)
    service = google_build("chat", "v1", credentials=creds, cache_discovery=False)
    body: dict[str, Any] = {"text": text}
    if thread_name:
        body["thread"] = {"name": thread_name}
    service.spaces().messages().create(
        parent=space_name,
        body=body,
        messageReplyOption="REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD",
    ).execute()


# A card's button runs this function (``onClick.action.function``); its tap
# comes back as a CARD_CLICKED event naming it, with the button's data
# (``channel_cards.callback_data``) under ``_CARD_PARAM``.
_CARD_FUNCTION = "oe_card"
_CARD_PARAM = "card"
# What a card's message says while its tap is being decided.
_WORKING = "Working on it…"
# What it says when a tap couldn't be confirmed (``verify_tap``).
# How long the webhook waits on Chat to confirm a tap (Chat waits 30s).
_TAP_CHECK_S = 20.0
_UNCONFIRMED = "That tap couldn't be checked, so nothing was done. The card is still waiting on Today."


def _card_buttons(decision_id: int, labels: tuple[str, ...]) -> list[dict[str, Any]]:
    """A card's Approve and Skip (and, on a card that can teach, the "on its
    own next time") buttons, as one Chat ``cardsV2`` card."""
    from openexecutive.integrations import channel_cards

    choices = (channel_cards.APPROVE, channel_cards.SKIP, channel_cards.ALLOW)
    buttons = [
        {
            "text": label,
            "onClick": {"action": {
                "function": _CARD_FUNCTION,
                "parameters": [{"key": _CARD_PARAM, "value": channel_cards.callback_data(decision_id, choice)}],
            }},
        }
        for label, choice in zip(labels, choices, strict=False)
    ]
    return [{"cardId": f"oe-card-{decision_id}", "card": {"sections": [{"widgets": [{"buttonList": {"buttons": buttons}}]}]}}]


def card_safe(text: str) -> str:
    """A card's words with Chat's link markup made inert. A card can quote
    what someone else wrote (a meeting title from an email), and Chat turns
    ``<https://…|words>`` into a link that shows only the words, so an
    approval card could hide where it points."""
    return text.replace("<", "\u2039")


def send_card(
    space_name: str,
    thread_name: str,
    decision_id: int,
    text: str,
    labels: tuple[str, ...],
    service_account_file: str | None,
    service_account_email: str | None = None,
) -> None:
    """Post a card the reply left (``channel_cards``) as a message: its words,
    then its buttons. Synchronous; call it via asyncio.to_thread()."""
    from googleapiclient.discovery import build as google_build

    creds = _build_credentials(service_account_file, service_account_email)
    service = google_build("chat", "v1", credentials=creds, cache_discovery=False)
    body: dict[str, Any] = {"text": card_safe(text), "cardsV2": _card_buttons(decision_id, labels)}
    if thread_name:
        body["thread"] = {"name": thread_name}
    service.spaces().messages().create(
        parent=space_name,
        body=body,
        messageReplyOption="REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD",
    ).execute()


def update_card(
    message_name: str,
    text: str,
    service_account_file: str | None,
    service_account_email: str | None = None,
) -> None:
    """Rewrite a card's message once its tap is decided: what it said with
    the outcome under it, and no buttons. The app may edit only its own
    messages. Synchronous; call it via asyncio.to_thread()."""
    from googleapiclient.discovery import build as google_build

    creds = _build_credentials(service_account_file, service_account_email)
    service = google_build("chat", "v1", credentials=creds, cache_discovery=False)
    service.spaces().messages().patch(
        name=message_name, updateMask="text,cardsV2", body={"text": card_safe(text), "cardsV2": []},
    ).execute()


def _obj(body: dict[str, Any], key: str) -> dict[str, Any]:
    value = body.get(key)
    return value if isinstance(value, dict) else {}


def _tapped(body: dict[str, Any]) -> str | None:
    """The button data of a CARD_CLICKED event on one of our cards, or None."""
    common = _obj(body, "common")
    action = _obj(body, "action")
    function = common.get("invokedFunction") or action.get("actionMethodName")
    if function != _CARD_FUNCTION:
        return None
    params = common.get("parameters")
    if isinstance(params, dict) and isinstance(params.get(_CARD_PARAM), str):
        return str(params[_CARD_PARAM])
    for item in action.get("parameters") or []:
        if isinstance(item, dict) and item.get("key") == _CARD_PARAM and isinstance(item.get("value"), str):
            return str(item["value"])
    return None


def _card_shown(body: dict[str, Any]) -> str:
    """What the tapped card's message says, without a line a tap added."""
    message = _obj(body, "message")
    return _strip_working(str(message.get("text") or ""))


def _strip_working(text: str) -> str:
    return text.removesuffix(f"\n\n{_WORKING}")


def verify_tap(
    space_name: str,
    message_name: str,
    data: str,
    user_name: str,
    service_account_file: str | None,
    service_account_email: str | None = None,
) -> str | None:
    """Whether a tap's event matches what Chat itself says, read as the app:
    the tapped message is the app's own, in that space, carrying this very
    button; the space is a DM between the app and one person alone; and that
    person is the event's user. The webhook's token proves Google issued it,
    not what the body says, so the tap counts only when Chat agrees.
    Returns the card message's words as Chat has them, or None for a no.
    Synchronous; call it via asyncio.to_thread(). Never raises: any doubt is
    a no."""
    import json

    from googleapiclient.discovery import build as google_build

    if not (space_name and user_name and message_name.startswith(f"{space_name}/messages/")):
        return None
    try:
        creds = _build_credentials(service_account_file, service_account_email)
        service = google_build("chat", "v1", credentials=creds, cache_discovery=False)
        message = service.spaces().messages().get(name=message_name).execute()
        sender = message.get("sender") if isinstance(message.get("sender"), dict) else {}
        if sender.get("type") != "BOT" or json.dumps(data) not in json.dumps(message.get("cardsV2") or []):
            return None
        space = service.spaces().get(name=space_name).execute()
        if space.get("singleUserBotDm") is not True:
            return None
        members = service.spaces().members().list(parent=space_name).execute()
        humans = [
            str((m.get("member") or {}).get("name") or "")
            for m in members.get("memberships") or []
            if isinstance(m, dict) and (m.get("member") or {}).get("type") == "HUMAN"
        ]
        return _strip_working(str(message.get("text") or "")) if humans == [user_name] else None
    except Exception:
        logger.warning("Google Chat: couldn't check a tap with Chat; it is refused", exc_info=True)
        return None


async def _confirm_tap(
    data: str,
    space_name: str,
    message_name: str,
    sender_email: str,
    user_name: str,
    service_account_file: str | None,
    service_account_email: str | None,
) -> tuple[Any, str] | None:
    """Who tapped, and the card's words as Chat has them, once Chat confirms
    the tap (``verify_tap``); None, audited, for a no. Asked before the
    buttons come off, since the check looks for the tapped button on the
    message."""
    from openexecutive.audit import log_event as audit_log
    from openexecutive.integrations import channel_cards
    from openexecutive.people.store import find_person_by_email

    parsed = channel_cards.parse_callback(data)
    if parsed is None:
        return None
    decision_id = parsed[0]
    try:
        person = await asyncio.to_thread(find_person_by_email, sender_email) if sender_email else None
    except Exception:
        logger.exception("Google Chat: couldn't look up who tapped card %d", decision_id)
        person = None
    confirmed = None
    if person is not None and person.id is not None:
        confirmed = await asyncio.to_thread(
            verify_tap, space_name, message_name, data, user_name, service_account_file, service_account_email,
        )
    if person is not None and person.id is not None and confirmed is not None:
        return person, confirmed
    logger.warning("Google Chat: a tap on card %d was refused (%s)", decision_id,
                   "not on People" if person is None or person.id is None else "Chat didn't confirm it")
    await asyncio.to_thread(
        audit_log,
        "integration_inbound",
        f"Rejected: Google Chat tap on card {decision_id}",
        actor="google_chat",
        details={"channel": "google_chat", "decision_id": decision_id, "rejected": True,
                 "person_id": getattr(person, "id", None), "space_name": space_name,
                 "message_name": message_name, "google_user": user_name},
    )
    return None


async def _decide_tap(
    data: str,
    person: Any,
    shown: str,
    space_name: str,
    message_name: str,
    user_name: str,
    service_account_file: str | None,
    service_account_email: str | None,
) -> None:
    """Decide a tap Chat confirmed (``_confirm_tap``): approve or skip the
    card for ``person``, whose DM with the app it is, then rewrite the card
    from its confirmed words (``shown``) with the outcome under them."""
    from openexecutive.audit import log_event as audit_log
    from openexecutive.integrations import channel_cards

    parsed = channel_cards.parse_callback(data)
    if parsed is None:
        return
    decision_id, approve = parsed
    allow = channel_cards.tapped_allow(data)
    where = {"space_name": space_name, "message_name": message_name, "google_user": user_name}
    line = await channel_cards.resolve(decision_id, person, approve=approve, allow=allow)
    choice = "allow" if allow else "approve" if approve else "skip"
    await asyncio.to_thread(
        audit_log,
        "integration_inbound",
        f"Google Chat card {decision_id}: tapped {choice.capitalize()}: {line[:120]}",
        actor="google_chat",
        details={"channel": "google_chat", "decision_id": decision_id, "choice": choice,
                 "outcome": channel_cards.outcome_of(line), "person_id": person.id, **where},
    )
    if line == channel_cards.BUSY:
        return
    try:
        await asyncio.to_thread(
            update_card, message_name, f"{shown}\n\n{line}" if shown else line,
            service_account_file, service_account_email,
        )
    except Exception:
        logger.exception("Google Chat: couldn't update card %d after the tap", decision_id)


async def _take_tap(body: dict[str, Any], background_tasks: BackgroundTasks, settings: Any) -> dict[str, Any]:
    """A CARD_CLICKED event: a tap on a card's button. It counts only in a DM
    between the app and the tapper alone (``singleUserBotDm``), from a human
    on People, as a Slack or Discord tap counts only in the tapper's own DM;
    ``verify_tap`` checks that with Chat itself before the answer, since the
    token isn't bound to the body and the check needs the button still on
    the message, and ``decisions.resolve_in_chat`` decides whether the card
    is theirs.
    The card shows it is being worked on at once, without its buttons, since
    deciding can take longer than Chat waits for an answer, and is rewritten
    when it is done. A tap Chat doesn't confirm decides nothing, and the card
    says so (only a fixed line: an unconfirmed tap's event may not be this
    card's)."""
    data = _tapped(body)
    if data is None:
        return {}
    user = _obj(body, "user")
    space = _obj(body, "space")
    message = _obj(body, "message")
    shown = _card_shown(body)
    if not _is_one_on_one(space) or user.get("type") != "HUMAN":
        return {"actionResponse": {"type": "UPDATE_MESSAGE"}, "text": shown, "cardsV2": []}
    space_name = str(space.get("name") or "")
    message_name = str(message.get("name") or "")
    user_name = str(user.get("name") or "")
    sa_file = settings.google_chat_service_account_file
    sa_email = settings.google_chat_service_account_email
    try:
        # Inside Chat's wait for the answer: a check that outlasts it is a no,
        # so a late yes can't decide a card whose tapper saw an error.
        confirmed = await asyncio.wait_for(_confirm_tap(
            data, space_name, message_name, str(user.get("email") or ""), user_name, sa_file, sa_email,
        ), timeout=_TAP_CHECK_S)
    except TimeoutError:
        logger.warning("Google Chat: checking a tap took too long; it is refused")
        confirmed = None
    if confirmed is None:
        return {"actionResponse": {"type": "UPDATE_MESSAGE"}, "text": _UNCONFIRMED, "cardsV2": []}
    person, words = confirmed
    background_tasks.add_task(
        _decide_tap,
        data=data,
        person=person,
        shown=words,
        space_name=space_name,
        message_name=message_name,
        user_name=user_name,
        service_account_file=sa_file,
        service_account_email=sa_email,
    )
    return {
        "actionResponse": {"type": "UPDATE_MESSAGE"},
        "text": f"{words}\n\n{_WORKING}" if words else _WORKING,
        "cardsV2": [],
    }


# Per message: bounds the downloads (and OCR / model work) one message can
# cause. Files are read only for a sender on the People roster (matched by
# the Google account email the event carries): the webhook authenticates
# Google, not the sender, so anyone in a space the bot is in can post.
_MAX_ATTACHMENTS = 5
_MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
_DOWNLOAD_CHUNK_BYTES = 1024 * 1024


def _message_attachments(message: dict) -> list[dict]:
    """The attachments on a Chat MESSAGE event, capped per message."""
    raw = message.get("attachment")
    if not isinstance(raw, list):
        return []
    return [a for a in raw if isinstance(a, dict)][:_MAX_ATTACHMENTS]


def _attachment_name(attachment: dict) -> str:
    return str(attachment.get("contentName") or "file")


def _sender_on_roster(email: str) -> bool:
    """Whether a Chat sender is a team member (not just a contact), by the
    Google account email on the event. No email — a bot, or an event without
    one — is not."""
    if not email:
        return False
    try:
        from openexecutive.people.store import find_person_by_email

        return find_person_by_email(email) is not None
    except Exception:
        logger.exception("Google Chat: roster lookup failed")
        return False


def download_attachment(
    resource_name: str,
    service_account_file: str | None,
    service_account_email: str | None = None,
    max_bytes: int = _MAX_ATTACHMENT_BYTES,
) -> bytes:
    """Download an uploaded Chat attachment's bytes (``media.download``, the
    ``chat.bot`` scope the replies already use). Raises ``ValueError`` past
    ``max_bytes``, checked per chunk so an oversized file is never buffered
    whole. Synchronous; call it via asyncio.to_thread()."""
    import io

    from googleapiclient.discovery import build as google_build
    from googleapiclient.http import MediaIoBaseDownload

    creds = _build_credentials(service_account_file, service_account_email)
    service = google_build("chat", "v1", credentials=creds, cache_discovery=False)
    request = service.media().download_media(resourceName=resource_name)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request, chunksize=_DOWNLOAD_CHUNK_BYTES)
    done = False
    while not done:
        _status, done = downloader.next_chunk()
        if buf.tell() > max_bytes:
            raise ValueError("attachment too large")
    return buf.getvalue()


async def _read_attachments(
    attachments: list[dict],
    service_account_file: str | None,
    service_account_email: str | None,
) -> tuple[str, list[dict]]:
    """Read a message's attachments the way the other chat channels do:
    documents to text (a scanned PDF converted), images to vision blocks.

    A Drive file shared into the message has no bytes here — it is left to
    the Drive tools, with its id so the Executive can fetch it."""
    from openexecutive.integrations.attachments import build_attachment_output

    parts: list[str] = []
    blocks: list[dict] = []
    limit_mb = _MAX_ATTACHMENT_BYTES // (1024 * 1024)
    for att in attachments:
        name = _attachment_name(att)
        drive = att.get("driveDataRef")
        data_ref = att.get("attachmentDataRef")
        if isinstance(drive, dict) and drive.get("driveFileId"):
            parts.append(
                f"(Attached {name}: a Google Drive file, id {drive['driveFileId']}"
                " — read it with the Drive tools)"
            )
            continue
        resource = data_ref.get("resourceName") if isinstance(data_ref, dict) else None
        if not resource:
            parts.append(f"(Could not download {name})")
            continue
        try:
            data = await asyncio.to_thread(
                download_attachment, str(resource), service_account_file, service_account_email
            )
        except ValueError:
            parts.append(f"(Skipped {name}: file too large — limit {limit_mb} MB)")
            continue
        except Exception:
            logger.exception("Google Chat: attachment download failed for %s", name)
            parts.append(f"(Could not download {name})")
            continue
        try:
            text, image_blocks = await build_attachment_output(
                name, data, str(att.get("contentType") or "")
            )
        except Exception:
            logger.exception("Google Chat: attachment processing failed for %s", name)
            parts.append(f"(Could not process {name})")
            continue
        if text:
            parts.append(text)
        blocks.extend(image_blocks)
    return "\n\n".join(parts), blocks


def _is_direct(space: object) -> bool:
    """The event's space is a DM with the app (old ``type`` or new ``spaceType``)."""
    if not isinstance(space, dict):
        return False
    return (
        space.get("type") == "DM"
        or space.get("spaceType") == "DIRECT_MESSAGE"
        or space.get("singleUserBotDm") is True
    )


def _is_one_on_one(space: object) -> bool:
    """The event's space is a DM between the app and one person alone
    (``singleUserBotDm``). A ``DIRECT_MESSAGE`` can also be two people's DM
    the app was added to, which the other person reads too."""
    return isinstance(space, dict) and space.get("singleUserBotDm") is True


def _load_history(session_id: str) -> list[dict[str, Any]]:
    from openexecutive.memory.session_store import load_messages

    try:
        return load_messages(session_id)
    except Exception:
        logger.exception("Google Chat: couldn't load the conversation %s", session_id)
        return []


def _save_turn(
    session_id: str, sender_name: str, sender_person: Any, session: Any, text: str, response: str
) -> None:
    """Keep the exchange in the conversation's history. A failure here must
    not cost the person their answer, so it is only logged."""
    from openexecutive.memory.session_store import (
        create_session,
        save_message,
        update_session_timestamp,
    )

    person_id = sender_person.id if sender_person is not None else None
    try:
        create_session(
            session_id, f"Google Chat {sender_name}", session.created_at.isoformat(),
            caller_person_id=person_id,
        )
        save_message(session_id, "user", text, sender_person_id=person_id)
        save_message(session_id, "assistant", response)
        update_session_timestamp(session_id)
    except Exception:
        logger.exception("Google Chat: failed to keep the turn for %s", session_id)


async def _send_cards(
    space_name: str,
    thread_name: str,
    cards: list[int],
    service_account_file: str | None,
    service_account_email: str | None,
) -> None:
    """Post the cards a reply left, each as its own message under the reply."""
    from openexecutive.integrations import channel_cards

    for decision_id in cards[: channel_cards.MAX_CARDS]:
        try:
            text = await asyncio.to_thread(channel_cards.card_text, decision_id)
            if not text:
                continue
            labels = await asyncio.to_thread(channel_cards.card_buttons, decision_id)
            await asyncio.to_thread(
                send_card, space_name, thread_name, decision_id, text, labels,
                service_account_file, service_account_email,
            )
        except Exception:
            logger.exception("Google Chat: couldn't send card %d", decision_id)


async def _process_and_reply(
    message_text: str,
    sender_name: str,
    space_name: str,
    thread_name: str,
    message_name: str,
    service_account_file: str | None,
    service_account_email: str | None,
    attachments: list[dict] | None = None,
    sender_email: str = "",
    reply_context: str = "",
    direct: bool = False,
    one_on_one: bool = False,
) -> None:
    """One Chat message's turn. ``reply_context`` is the message it quotes
    (``integrations.reply_context``), put in front of the words. ``direct``:
    a DM with the app, which is one conversation, whatever thread each
    message lands in. ``one_on_one``: a DM between the app and the sender
    alone (``singleUserBotDm``), the only conversation private to them."""
    if not space_name:
        logger.error("Google Chat: missing space_name for message %s, cannot reply", message_name)
        return

    # Deterministic per-thread session id so every audit row from this inbound
    # (chat_turn, specialist_consult, tool_invocation) shares a grouping key
    # with the integration_inbound row.
    # A DM is one conversation (each message there can start its own thread);
    # in a space, each thread is one.
    session_id = f"google_chat:{space_name}" if direct else f"google_chat:{space_name}:{thread_name}"
    # The sender's People row, by the Google account email on the event (the
    # JWT proves the event is Google's, so the email is Google's word). None
    # for someone who isn't on People: their turn is unverified, and the
    # department gate holds their sends and edits for approval.
    sender_person = None
    if sender_email:
        try:
            from openexecutive.people.store import find_person_by_email

            sender_person = find_person_by_email(sender_email)
        except Exception:
            logger.exception("Google Chat: failed to look up the sender on People")

    # The rows written before the turn binds its session (the inbound row,
    # alert triage, retrieval) follow the sender's turn, as on the other chat
    # channels (``people_tools.audit_rows_on_senders_turn``): a row naming one
    # of the principal's contacts is theirs alone on their own turn, and a
    # teammate's rows are that teammate's.
    from openexecutive.audit.context import principal_turn_rows, rows_for_person
    from openexecutive.orchestrator.people_tools import sent_by_principal, sent_by_teammate

    principal, teammate = False, None
    if sender_person is not None:
        try:
            principal = sent_by_principal("google_chat", space_name, sender_person, speaker_verified=True)
            teammate = sent_by_teammate("google_chat", space_name, sender_person, speaker_verified=True)
        except Exception:
            logger.exception("Google Chat: couldn't tell whose turn this is; rows follow the ordinary rule")
    with (
        principal_turn_rows(principal),
        rows_for_person(teammate) if teammate is not None else contextlib.nullcontext(),
    ):
        await _answer(
            message_text=message_text, sender_name=sender_name, space_name=space_name,
            thread_name=thread_name, message_name=message_name,
            service_account_file=service_account_file, service_account_email=service_account_email,
            attachments=attachments, sender_email=sender_email, reply_context=reply_context,
            one_on_one=one_on_one, session_id=session_id, sender_person=sender_person,
        )


async def _answer(
    *,
    message_text: str,
    sender_name: str,
    space_name: str,
    thread_name: str,
    message_name: str,
    service_account_file: str | None,
    service_account_email: str | None,
    attachments: list[dict] | None,
    sender_email: str,
    reply_context: str,
    one_on_one: bool,
    session_id: str,
    sender_person: Any,
) -> None:
    """The rest of ``_process_and_reply``, once the sender is known."""
    from openexecutive.audit import log_event as audit_log
    audit_log(
        "integration_inbound",
        f"Inbound google_chat from {sender_name} (space={space_name}): {message_text[:160]}",
        actor="google_chat",
        session_id=session_id,
        details={
            "channel": "google_chat",
            "space_name": space_name,
            "thread_name": thread_name,
            "message_name": message_name,
            "sender": sender_name,
            # Who it is by People, not by display name: the brief tells the
            # owner's own chat from a namesake's by this (briefing.live_signals).
            "sender_person_id": sender_person.id if sender_person is not None else None,
            "text_len": len(message_text),
        },
    )

    # Fork into alert triage pipeline (fire-and-forget, same pattern as Slack/email).
    try:
        from openexecutive.alerts.pipeline import schedule_chat_message_evaluation

        schedule_chat_message_evaluation(
            source="google_chat",
            external_id=message_name,
            body=message_text,
            sender=sender_person,
            fallback_name=sender_name,
        )
    except Exception:
        logger.exception("Google Chat: failed to schedule alert evaluation")

    try:
        from openexecutive.integrations.channel_context import (
            attach_briefing_context,
            build_channel_context_block,
        )
        from openexecutive.knowledge.retriever import chat_builtin_results, retrieve
        from openexecutive.memory.episodic import format_for_prompt
        from openexecutive.onboarding.profile_builder import load_or_create_profile
        from openexecutive.orchestrator.executive import Executive
        from openexecutive.orchestrator.mcp_gateway import get_active_gateway
        from openexecutive.orchestrator.session import Session

        profile = load_or_create_profile()
        session = Session(
            session_id=session_id,
            company_profile=profile if not profile.is_empty() else None,
            # Names the surface for logging and the `<channel>` block.
            origin_channel="google_chat",
            origin_channel_ref=space_name,
        )
        if sender_person is not None:
            # A sender on People is signed in here, as on Slack: Google
            # signed the event (the JWT checked above), and its `user.email`
            # is the Google account that sent it, never anything the message
            # says. So the turn is theirs, verified, in a space or a DM, and
            # their Approvals level decides what runs. Anyone else stays
            # unverified, with no caller, and the department gate holds what
            # they ask to send or change.
            session.caller_person_id = sender_person.id
            session.speaker_verified = True
            # A DM between the app and the sender alone is read by them
            # alone, like a Slack DM, so what is private to them (Act as me,
            # Apps, recalled notes) may be offered there. Anything else never:
            # a space, or a DM between two people the app was added to.
            session.private_chat = one_on_one
        # Anywhere but that DM, others write into the conversation this turn
        # reads, and unlike Slack's channels not only people on People
        # (Google Chat turns no one away). So it is a shared chat, as a web
        # chat shared with teammates is: the principal-only tools and what
        # else a shared chat withholds stay off, whoever speaks.
        session.shared_chat = not one_on_one
        # And anyone may have written last, so the turn is held as one that
        # read outside words: under the Approvals level, its actions wait on
        # a card rather than run from words someone else planted.
        session.others_spoke_last = not one_on_one
        # The conversation so far, so a follow-up ("add a few more bullets")
        # has what it follows.
        history = await asyncio.to_thread(_load_history, session_id)
        if history:
            session.conversation_history = history
        retrieved_context = retrieve(
            query=message_text, max_chars=get_settings().knowledge_chat_max_chars, n_builtin=chat_builtin_results()
        )
        episodic_context = format_for_prompt()
        # The open-alert board, in the sender's own DM only (the helper
        # returns "" anywhere else, and for someone who isn't on People).
        briefing_context = await asyncio.to_thread(
            attach_briefing_context, session, is_dm=one_on_one and sender_person is not None, person=sender_person
        )

        own_text = message_text
        # Quoted only for people on the team: the quote is text the sender
        # chose, as an attachment's is.
        reply_context = reply_context if _sender_on_roster(sender_email) else ""
        if reply_context:
            from openexecutive.integrations.reply_context import with_context

            message_text = with_context(reply_context, message_text)
        chat_message = message_text
        image_blocks: list[dict] = []
        if attachments and _sender_on_roster(sender_email):
            att_text, image_blocks = await _read_attachments(
                attachments, service_account_file, service_account_email
            )
            if att_text:
                # Before the words, as the other chat channels inline it.
                chat_message = f"{att_text}\n\n{message_text}"
        elif attachments:
            names = ", ".join(map(_attachment_name, attachments))
            chat_message = (
                f"(Attached files, not read — files are read only from people on "
                f"the team: {names})\n\n{message_text}"
            )

        from openexecutive.integrations.channel_cards import shows_cards

        # A DM between the app and its person alone shows the reply's cards
        # with Approve and Skip buttons (integrations.channel_cards).
        cards_here = shows_cards(session)
        cards: list[int] = []
        response = await Executive(mcp_gateway=get_active_gateway()).chat(
            user_message=chat_message,
            session=session,
            retrieved_context=retrieved_context,
            episodic_context=episodic_context,
            briefing_context=briefing_context,
            channel_context_block=build_channel_context_block("google_chat", cards_here=cards_here),
            cards=cards if cards_here else None,
            person_id=sender_person.id if sender_person is not None else None,
            attachment_blocks=image_blocks or None,
            # The quote is not the person's words: memory keeps their own.
            memory_text=own_text if reply_context else None,
        )
        await asyncio.to_thread(
            _save_turn, session_id, sender_name, sender_person, session, own_text, response
        )
        from openexecutive.orchestrator.disclosure import with_chat_line

        await asyncio.to_thread(
            send_reply, space_name, thread_name, with_chat_line(response, session_id),
            service_account_file, service_account_email,
        )
        if cards:
            await _send_cards(space_name, thread_name, cards, service_account_file, service_account_email)
    except Exception:
        logger.exception("Google Chat: handler error for message %s", message_name)
        try:
            await asyncio.to_thread(
                send_reply,
                space_name,
                thread_name,
                "I encountered an error processing your request. Please try again.",
                service_account_file,
                service_account_email,
            )
        except Exception:
            logger.exception("Google Chat: also failed to send error reply")


@router.post("/webhook/google-chat", status_code=200)
async def google_chat_webhook(request: Request, background_tasks: BackgroundTasks) -> dict:
    settings = get_settings()

    if not settings.google_chat_project_number or not (
        settings.google_chat_service_account_file
        or settings.google_chat_service_account_email
    ):
        raise HTTPException(status_code=503, detail="Google Chat integration not configured")

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")

    try:
        verify_google_jwt(auth.removeprefix("Bearer "), settings.google_chat_project_number)
    except Exception:
        logger.warning("Google Chat JWT verification failed")
        raise HTTPException(status_code=401, detail="Invalid JWT") from None

    body = await request.json()
    event_type = body.get("type", "")

    if event_type == "ADDED_TO_SPACE":
        logger.info("Google Chat bot added to space: %s", body.get("space", {}).get("name"))
        return {"text": "Hello! I'm your AI executive. Mention me anytime with a question."}

    if event_type == "REMOVED_FROM_SPACE":
        logger.info("Google Chat bot removed from space: %s", body.get("space", {}).get("name"))
        return {}

    if event_type == "CARD_CLICKED":
        return await _take_tap(body, background_tasks, settings)

    if event_type != "MESSAGE":
        return {}

    message = body.get("message", {})
    raw_text = message.get("text", "")
    # Strip structured @mention tokens (format: <users/USER_ID>).
    cleaned = re.sub(r"<users/\d+>", "", raw_text).strip()
    attachments = _message_attachments(message)
    if not cleaned and not attachments:
        return {}
    if not cleaned:
        # A file-only message: its names stand in for the words, as the web
        # upload route records one.
        cleaned = f"(Attached files: {', '.join(map(_attachment_name, attachments))})"

    from openexecutive.integrations import reply_context

    background_tasks.add_task(
        _process_and_reply,
        message_text=cleaned,
        sender_name=body.get("user", {}).get("displayName", "Unknown"),
        space_name=body.get("space", {}).get("name", ""),
        thread_name=message.get("thread", {}).get("name", ""),
        message_name=message.get("name", ""),
        reply_context=reply_context.google_chat(
            message,
            reply_context.google_chat_app_id(message),
            # A DM between the app and one person has two in it: the sender and the app.
            two_party=_is_one_on_one(body.get("space", {})),
        ),
        direct=_is_direct(body.get("space", {})),
        one_on_one=_is_one_on_one(body.get("space", {})),
        service_account_file=settings.google_chat_service_account_file,
        service_account_email=settings.google_chat_service_account_email,
        attachments=attachments,
        sender_email=str(body.get("user", {}).get("email") or ""),
    )
    return {}
