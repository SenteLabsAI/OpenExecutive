"""Microsoft 365 backend: Outlook mail + calendar via ms-365-mcp-server.

Tool names and argument shapes track @softeria/ms-365-mcp-server 0.154.2
(pinned in docker/Dockerfile) as registered over MCP with
``--preset mail,calendar``; re-verify on a bump. Notably:

* path parameters are camelCase (``messageId``, ``eventId``, ``mailFolderId``),
* OData query options drop the ``$`` (``filter``, ``top``, ``select``),
* request bodies sit under ``body``, and Graph *action* parameters keep their
  OpenAPI casing (``body.Message`` / ``body.SaveToSentItems`` for sendMail,
  ``body.Comment`` for reply),
* a success is the Graph JSON as text (``{"value": [...]}`` for collections,
  ``{"success": true}`` for a 202/204), a failure is ``{"error": "..."}``.

Graph returns HTML bodies (the ``Prefer`` header selecting text cannot be set
through the tool), so bodies go through `_html.html_to_text`.
"""
from __future__ import annotations

import json
import logging
import math
import re
from datetime import UTC, datetime
from typing import Any

from openexecutive.integrations.workspace._html import html_to_text
from openexecutive.integrations.workspace.calendar import wants_video_link
from openexecutive.integrations.workspace.mail import InboundMessage, MessageRef

logger = logging.getLogger(__name__)

SERVER_NAME = "microsoft_365"
_PREFIX = "microsoft_365__"

_LIST_SELECT = "id,conversationId,subject,from,receivedDateTime"
_UNREAD_FILTER = "receivedDateTime ge 1970-01-01T00:00:00Z and isRead eq false"
_FETCH_SELECT = (
    "id,conversationId,subject,from,toRecipients,ccRecipients,body,"
    "hasAttachments,receivedDateTime"
)
_ATTACHMENT_SELECT = "id,name,contentType,size,isInline"
# Attachment names are sender-controlled; keep one line per file short enough
# for email_poller._attachment_name (which caps a line at 512 chars).
_ATTACHMENT_NAME_MAX = 200
# The MIME type is sender-controlled too and sits inside the `(mime, size KB)`
# tail that parser splits on " (" and ", ": keep only media-type characters.
_MIME_CHARS_RE = re.compile(r"[^A-Za-z0-9.+/_-]")
_MIME_MAX = 100
# showAs values that mean the slot is taken. `free` and `workingElsewhere`
# are not conflicts.
_BUSY_STATES = frozenset({"busy", "oof", "tentative"})


def _parse_json(raw: Any) -> Any:
    """The tool result as JSON, or ``None`` when it is not JSON."""
    if isinstance(raw, (dict, list)):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _error_of(parsed: Any) -> str | None:
    if isinstance(parsed, dict) and "error" in parsed:
        err = parsed["error"]
        return err if isinstance(err, str) else json.dumps(err)
    return None


def _items(parsed: Any) -> list[Any]:
    """A Graph collection: ``{"value": [...]}`` or a bare list."""
    if isinstance(parsed, dict) and isinstance(parsed.get("value"), list):
        return parsed["value"]
    if isinstance(parsed, list):
        return parsed
    return []


def _address_of(recipient: Any) -> tuple[str, str]:
    """``(address, name)`` from a Graph ``recipient`` / ``emailAddress`` object."""
    if not isinstance(recipient, dict):
        return "", ""
    email_obj = recipient.get("emailAddress", recipient)
    if not isinstance(email_obj, dict):
        return "", ""
    addr = email_obj.get("address")
    name = email_obj.get("name")
    return (addr.strip() if isinstance(addr, str) else ""), (name.strip() if isinstance(name, str) else "")


def _addresses(recipients: Any) -> list[str]:
    out: list[str] = []
    for r in recipients if isinstance(recipients, list) else []:
        addr, _ = _address_of(r)
        if addr and addr.lower() not in out:
            out.append(addr.lower())
    return out


def _recipient(addr: str) -> dict[str, Any]:
    return {"emailAddress": {"address": addr}}


def _graph_datetime(iso: str) -> str:
    """ISO 8601 (any offset) → Graph's ``dateTime`` for ``timeZone: "UTC"``."""
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")


class MicrosoftMail:
    """Outlook mail through ms-365-mcp-server."""

    name = "microsoft"
    server_name = SERVER_NAME
    send_tool_name = _PREFIX + "send-mail"
    discovery_queries: tuple[str, ...] = (
        "list outlook mail folder messages unread inbox",
        "get outlook mail message subject body sender",
        "list mail message attachments download bytes",
        "update mail message mark as read",
        "reply to mail message outlook",
        "send mail outlook",
    )

    async def list_unread(self, gateway: Any, mailbox: str, limit: int) -> list[MessageRef]:
        try:
            raw = await gateway.call_tool({
                "name": _PREFIX + "list-mail-folder-messages",
                "arguments": {
                    "mailFolderId": "inbox",
                    # Graph requires every $orderby property to appear in
                    # $filter first (else "InefficientFilter"), hence the
                    # always-true receivedDateTime clause ahead of isRead.
                    "filter": _UNREAD_FILTER,
                    "orderby": "receivedDateTime desc",
                    "top": limit,
                    "select": _LIST_SELECT,
                },
            })
        except Exception:
            logger.exception("list-mail-folder-messages failed")
            return []
        parsed = _parse_json(raw)
        err = _error_of(parsed)
        if err is not None:
            logger.warning("list-mail-folder-messages error: %s", err[:300])
            return []
        refs: list[MessageRef] = []
        for item in _items(parsed):
            if not isinstance(item, dict):
                continue
            mid = item.get("id")
            if not isinstance(mid, str) or not mid:
                continue
            tid = item.get("conversationId")
            refs.append(MessageRef(message_id=mid, thread_id=tid if isinstance(tid, str) else ""))
        return refs

    async def fetch(self, gateway: Any, ref: MessageRef, mailbox: str) -> InboundMessage | None:
        raw = await gateway.call_tool({
            "name": _PREFIX + "get-mail-message",
            "arguments": {"messageId": ref.message_id, "select": _FETCH_SELECT},
        })
        parsed = _parse_json(raw)
        err = _error_of(parsed)
        if err is not None:
            logger.warning("get-mail-message %s error: %s", ref.message_id, err[:300])
            return None
        if not isinstance(parsed, dict):
            return None
        from_addr, from_name = _address_of(parsed.get("from"))
        body = parsed.get("body")
        content = body.get("content") if isinstance(body, dict) else None
        content_type = body.get("contentType") if isinstance(body, dict) else None
        text = content if isinstance(content, str) else ""
        if isinstance(content_type, str) and content_type.lower() == "html":
            text = html_to_text(text)
        has_attachments = bool(parsed.get("hasAttachments"))
        attachments = (
            await self._attachment_lines(gateway, ref.message_id) if has_attachments else []
        )
        thread_id = parsed.get("conversationId")
        subject = parsed.get("subject")
        return InboundMessage(
            message_id=ref.message_id,
            thread_id=thread_id if isinstance(thread_id, str) else ref.thread_id,
            from_addr=from_addr,
            from_name=from_name,
            subject=(subject if isinstance(subject, str) else "")[:160],
            to=_addresses(parsed.get("toRecipients")),
            cc=_addresses(parsed.get("ccRecipients")),
            body_text=text,
            has_attachments=has_attachments,
            attachments=attachments,
        )

    async def _attachment_lines(self, gateway: Any, message_id: str) -> list[str]:
        """The `--- ATTACHMENTS ---` lines for a message that has attachments:
        one `N. <name> (<contentType>, <size> KB)` line per file — the exact
        shape workspace-mcp prints for Gmail, which `email_poller.
        _attachment_name` parses for peer memory — plus the fetch hint. Inline
        parts (signature images) are left out. Best-effort: a failed listing
        leaves only the hint, so the Executive can still list them itself.
        """
        hint = (
            f"Fetch one with {_PREFIX}download-bytes; list them with "
            f"{_PREFIX}list-mail-attachments (messageId={message_id})."
        )
        try:
            raw = await gateway.call_tool({
                "name": _PREFIX + "list-mail-attachments",
                "arguments": {"messageId": message_id, "select": _ATTACHMENT_SELECT},
            })
        except Exception:
            logger.warning("list-mail-attachments %s failed", message_id, exc_info=True)
            return [hint]
        parsed = _parse_json(raw)
        items = parsed.get("value") if isinstance(parsed, dict) else None
        if _error_of(parsed) is not None or not isinstance(items, list):
            return [hint]
        lines: list[str] = []
        for item in items:
            if not isinstance(item, dict) or item.get("isInline"):
                continue
            name = item.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            name = " ".join(name.split())[:_ATTACHMENT_NAME_MAX]
            mime = _MIME_CHARS_RE.sub("", item.get("contentType") or "")[:_MIME_MAX]
            size = item.get("size")
            kb = (
                size / 1024
                if isinstance(size, int | float) and math.isfinite(size) and size >= 0
                else 0.0
            )
            lines.append(
                f"{len(lines) + 1}. {name} ({mime or 'application/octet-stream'}, {kb:.1f} KB)"
            )
        return [*lines, hint]

    async def mark_read(self, gateway: Any, ref: MessageRef, mailbox: str) -> None:
        try:
            raw = await gateway.call_tool({
                "name": _PREFIX + "update-mail-message",
                "arguments": {"messageId": ref.message_id, "body": {"isRead": True}},
            })
            err = _error_of(_parse_json(raw))
            if err is not None:
                logger.warning("failed to mark message=%s as read: %s", ref.message_id, err[:300])
            else:
                logger.debug("marked message=%s as read", ref.message_id)
        except Exception:
            logger.warning("failed to mark message=%s as read", ref.message_id)

    def build_send_arguments(
        self, *, mailbox: str, to: str, subject: str, body: str, thread_id: str | None = None,
    ) -> dict[str, Any]:
        # `mailbox` is implicit (the signed-in account) and Graph's sendMail has
        # no thread parameter — replies thread through reply-mail-message, and
        # a fresh sendMail threads on the client side by subject.
        return {
            "body": {
                "Message": {
                    "subject": subject,
                    "body": {"contentType": "Text", "content": body},
                    "toRecipients": [_recipient(to)],
                },
                "SaveToSentItems": True,
            },
        }

    def reply_block(self, msg: InboundMessage) -> str:
        return (
            f"tool: {_PREFIX}reply-mail-message\n"
            f"messageId: {msg.message_id}\n"
            "Reply with call_tool using that tool and arguments "
            f'{{"messageId": "{msg.message_id}", "body": {{"Comment": "<your reply>"}}}} — '
            "it goes to the sender in the same thread; do not add recipients. "
            f"Use {self.send_tool_name} only for a brand-new message."
        )

    def send_tool_hint(self) -> str:
        return (
            f"{self.send_tool_name} (via MCP; put the address in "
            "body.Message.toRecipients — Outlook threads by subject, so reuse the "
            "original subject prefixed 'Re:')"
        )


def _join_url(event: dict[str, Any]) -> str | None:
    online = event.get("onlineMeeting")
    if isinstance(online, dict):
        url = online.get("joinUrl")
        if isinstance(url, str) and url:
            return url
    url = event.get("onlineMeetingUrl")
    return url if isinstance(url, str) and url else None


class MicrosoftCalendar:
    """Outlook calendar through ms-365-mcp-server."""

    name = "microsoft"
    server_name = SERVER_NAME
    video_link_label = "Microsoft Teams"

    async def create_event(self, gateway: Any, payload: dict[str, Any]) -> dict[str, Any]:
        from openexecutive.config import get_settings

        settings = get_settings()
        try:
            start = _graph_datetime(str(payload["start"]))
            end = _graph_datetime(str(payload["end"]))
        except (KeyError, ValueError, TypeError) as exc:
            return {"error": f"invalid start/end: {exc}"}
        online = wants_video_link(payload, getattr(settings, "calendar_meet_links_enabled", True))
        body: dict[str, Any] = {
            "subject": payload["title"],
            "start": {"dateTime": start, "timeZone": "UTC"},
            "end": {"dateTime": end, "timeZone": "UTC"},
            "attendees": [
                {**_recipient(addr), "type": "required"}
                for addr in payload.get("attendee_emails", [])
            ],
            "isOnlineMeeting": online,
        }
        if online:
            body["onlineMeetingProvider"] = "teamsForBusiness"
        if payload.get("description"):
            body["body"] = {"contentType": "Text", "content": payload["description"]}

        try:
            raw = await gateway.call_tool({
                "name": _PREFIX + "create-calendar-event",
                "arguments": {"body": body},
            })
            parsed = _parse_json(raw)
            err = _error_of(parsed)
            if err is not None:
                return {"error": err}
            if not isinstance(parsed, dict):
                return {"error": f"unexpected create-calendar-event response: {str(raw)[:200]}"}
            event_id = parsed.get("id")
            meet_link = _join_url(parsed)
            if online and not meet_link and isinstance(event_id, str):
                # Graph can return the event before the Teams meeting is
                # attached; one re-read is cheap and usually has the link.
                meet_link = await self._refetch_join_url(gateway, event_id)
            out: dict[str, Any] = {"event_id": event_id, "raw": parsed}
            if meet_link:
                out["meet_link"] = meet_link
            return out
        except Exception as exc:
            logger.exception("calendar: create-calendar-event failed")
            return {"error": str(exc)}

    async def _refetch_join_url(self, gateway: Any, event_id: str) -> str | None:
        try:
            raw = await gateway.call_tool({
                "name": _PREFIX + "get-calendar-event",
                "arguments": {"eventId": event_id, "select": "id,onlineMeeting,onlineMeetingUrl"},
            })
            parsed = _parse_json(raw)
            return _join_url(parsed) if isinstance(parsed, dict) else None
        except Exception:
            logger.debug("calendar: join-url re-read failed", exc_info=True)
            return None

    async def delete_event(self, gateway: Any, external_event_id: str) -> dict[str, Any]:
        try:
            raw = await gateway.call_tool({
                "name": _PREFIX + "delete-calendar-event",
                "arguments": {"eventId": external_event_id},
            })
            parsed = _parse_json(raw)
            err = _error_of(parsed)
            if err is not None:
                return {"error": err}
            return parsed if isinstance(parsed, dict) else {"ok": True}
        except Exception as exc:
            logger.exception("calendar: delete-calendar-event failed")
            return {"error": str(exc)}

    async def has_conflicts(
        self, gateway: Any, start_iso: str, end_iso: str, attendee_emails: list[str],
    ) -> bool | None:
        # Narrower than Google's multi-calendar freebusy: only the Executive's
        # own calendar is readable with delegated Calendars.ReadWrite, so this
        # answers "is the organizer busy?", not "is every attendee free?".
        try:
            raw = await gateway.call_tool({
                "name": _PREFIX + "get-calendar-view",
                "arguments": {
                    "startDateTime": start_iso,
                    "endDateTime": end_iso,
                    "select": "id,subject,showAs",
                    "top": 50,
                },
            })
            parsed = _parse_json(raw)
            if not isinstance(parsed, dict) or _error_of(parsed) is not None:
                return None
            if not isinstance(parsed.get("value"), list):
                # A shape we do not understand is "unknown", never "free".
                return None
            for item in _items(parsed):
                if isinstance(item, dict) and str(item.get("showAs", "")).lower() in _BUSY_STATES:
                    return True
            return False
        except Exception:
            logger.debug("calendar: calendar-view check skipped", exc_info=True)
            return None
