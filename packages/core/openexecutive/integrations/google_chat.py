from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from openexecutive.config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter()


_GOOGLE_CHAT_ISSUER = "chat@system.gserviceaccount.com"


def verify_google_jwt(token: str, project_number: str) -> Mapping[str, Any]:
    """Verify a Bearer JWT from a Google Chat webhook.

    Google Chat sets the JWT audience to the GCP project number (numeric string)
    and iss to chat@system.gserviceaccount.com.
    Raises google.auth.exceptions.TransportError or ValueError on failure.
    """
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    claims = id_token.verify_token(token, google_requests.Request(), audience=project_number)
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


async def _process_and_reply(
    message_text: str,
    sender_name: str,
    space_name: str,
    thread_name: str,
    message_name: str,
    service_account_file: str | None,
    service_account_email: str | None,
) -> None:
    if not space_name:
        logger.error("Google Chat: missing space_name for message %s, cannot reply", message_name)
        return

    # Deterministic per-thread session id so every audit row from this inbound
    # (chat_turn, specialist_consult, tool_invocation) shares a grouping key
    # with the integration_inbound row.
    session_id = f"google_chat:{space_name}:{thread_name}"
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
            "text_len": len(message_text),
        },
    )

    # Fork into alert triage pipeline (fire-and-forget, same pattern as Slack/email).
    try:
        from openexecutive.alerts.models import AlertEvent
        from openexecutive.alerts.pipeline import schedule_evaluation

        schedule_evaluation(
            AlertEvent(
                source="google_chat",
                external_id=message_name,
                body=message_text,
                user=sender_name,
            )
        )
    except Exception:
        logger.exception("Google Chat: failed to schedule alert evaluation")

    try:
        from openexecutive.integrations.channel_context import (
            build_channel_context_block,
        )
        from openexecutive.knowledge.retriever import retrieve
        from openexecutive.memory.episodic import format_for_prompt
        from openexecutive.onboarding.profile_builder import load_or_create_profile
        from openexecutive.orchestrator.executive import Executive
        from openexecutive.orchestrator.mcp_gateway import get_active_gateway
        from openexecutive.orchestrator.session import Session

        profile = load_or_create_profile()
        session = Session(
            session_id=session_id,
            company_profile=profile if not profile.is_empty() else None,
            # Names the surface for logging and the `<channel>` block. This
            # adapter has no roster gate (the webhook authenticates Google,
            # not the sender) and never populates trusted_alert_ids, so
            # `ack_alert` refuses here — matching what the channel block
            # already tells the model about this surface.
            origin_channel="google_chat",
        )
        retrieved_context = retrieve(query=message_text)
        episodic_context = format_for_prompt()

        response = await Executive(mcp_gateway=get_active_gateway()).chat(
            user_message=message_text,
            session=session,
            retrieved_context=retrieved_context,
            episodic_context=episodic_context,
            channel_context_block=build_channel_context_block("google_chat"),
        )
        await asyncio.to_thread(
            send_reply, space_name, thread_name, response, service_account_file, service_account_email
        )
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

    if event_type != "MESSAGE":
        return {}

    message = body.get("message", {})
    raw_text = message.get("text", "")
    # Strip structured @mention tokens (format: <users/USER_ID>).
    cleaned = re.sub(r"<users/\d+>", "", raw_text).strip()
    if not cleaned:
        return {}

    background_tasks.add_task(
        _process_and_reply,
        message_text=cleaned,
        sender_name=body.get("user", {}).get("displayName", "Unknown"),
        space_name=body.get("space", {}).get("name", ""),
        thread_name=message.get("thread", {}).get("name", ""),
        message_name=message.get("name", ""),
        service_account_file=settings.google_chat_service_account_file,
        service_account_email=settings.google_chat_service_account_email,
    )
    return {}
