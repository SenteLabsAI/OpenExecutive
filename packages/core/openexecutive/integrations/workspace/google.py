"""Google Workspace backend: Gmail + Google Calendar via workspace-mcp.

The argument dicts and response parsing here are the ones `email_poller`,
`calendar_tools` and `api/routes/decisions` used to spell inline — moved
verbatim so the ``google`` default behaves byte-for-byte as before.

Actual tool names (confirmed via tools/list on the live MCP server):
  google_workspace__search_gmail_messages          → plain-text list of Message IDs + Thread IDs
  google_workspace__get_gmail_message_content      → plain-text Subject/From/--- BODY ---/--- ATTACHMENTS ---
  google_workspace__modify_gmail_message_labels    → mark as read (Complete/Extended tier)
  google_workspace__send_gmail_message             → send (reply/forward via thread_id)
  google_workspace__manage_event                   → create/delete calendar events
  google_workspace__query_freebusy                 → busy check
"""
from __future__ import annotations

import json
import logging
import re
from email.utils import parseaddr
from typing import Any

from openexecutive.integrations.workspace.calendar import wants_video_link
from openexecutive.integrations.workspace.mail import InboundMessage, MessageRef

logger = logging.getLogger(__name__)

SERVER_NAME = "google_workspace"

# Headers that can steer where a reply is sent. Stripped from the raw email
# before the Executive sees it. Lowercased for comparison.
_REPLY_REDIRECT_HEADERS = (
    "reply-to:",
    "resent-reply-to:",
    "mail-reply-to:",
    "mail-followup-to:",
)


def _strip_reply_to(raw: str) -> str:
    """Remove headers that could redirect a reply, plus their folded continuations.

    The Executive constructs outbound `to:` itself; if it sees a Reply-To-style
    header it may honor it instead of the From address. The egress gate in
    MCPGateway is the real enforcement, but stripping here removes the attack
    surface entirely so the Executive never has to choose.

    Stops processing at the header/body boundary (the first blank line) so
    body text that happens to contain `Reply-To: ...` is left alone.
    """
    out: list[str] = []
    in_drop = False
    in_body = False
    for line in raw.splitlines(keepends=True):
        if in_body:
            out.append(line)
            continue
        # Header/body boundary: a line that's only CR/LF.
        if line in ("\n", "\r\n", "\r"):
            in_body = True
            in_drop = False
            out.append(line)
            continue
        # Folded continuation of the previous header.
        if line and line[0] in (" ", "\t"):
            if in_drop:
                continue
            out.append(line)
            continue
        # Start of a new header.
        lower = line.lower()
        if any(lower.startswith(h) for h in _REPLY_REDIRECT_HEADERS):
            in_drop = True
            continue
        in_drop = False
        out.append(line)
    return "".join(out)


_RECIPIENT_HEADERS = ("to:", "cc:")
# Strips both `Name <addr@example.com>` and bare `addr@example.com` forms.
# Permissive on the local-part / domain — we only need to identify
# candidates that find_person_by_email then looks up exactly.
_EMAIL_RE = re.compile(r"[\w.+\-]+@[\w.\-]+\.[A-Za-z]{2,}")


def _parse_recipients(raw: str) -> list[str]:
    """Return distinct lowercase email addresses from the raw email's To+Cc headers.

    Mirrors :func:`_strip_reply_to`'s header walker: iterates lines
    until the first blank line (header/body boundary) and honours
    folded-header continuations (leading whitespace). Returns at most
    one entry per address, lowercased for downstream case-insensitive
    lookup via :func:`openexecutive.people.store.find_person_by_email`.
    """
    found: list[str] = []
    seen: set[str] = set()
    capturing_value = ""

    def _flush_value() -> None:
        nonlocal capturing_value
        if not capturing_value:
            return
        for addr in _EMAIL_RE.findall(capturing_value):
            low = addr.lower()
            if low not in seen:
                seen.add(low)
                found.append(low)
        capturing_value = ""

    in_recipient = False
    for line in raw.splitlines():
        # Header/body boundary.
        if not line:
            _flush_value()
            break
        # Folded continuation: appended to the current header value.
        if line[0] in (" ", "\t"):
            if in_recipient:
                capturing_value += " " + line.strip()
            continue
        # New header line — flush whatever we were collecting.
        _flush_value()
        lower = line.lower()
        in_recipient = any(lower.startswith(h) for h in _RECIPIENT_HEADERS)
        if in_recipient:
            # Strip "To:" / "Cc:" prefix; keep the rest as raw value.
            capturing_value = line.split(":", 1)[1] if ":" in line else ""
    # Body never seen (no blank line) — flush trailing header value.
    _flush_value()
    return found


def _parse_search_results(raw: str) -> list[dict[str, str]]:
    """Parse plain-text search_gmail_messages response into [{message_id, thread_id}].

    Confirmed response format (MCP server v3.3.1):
      Message ID: 19e3280dac59147f
      Thread ID:  19e3280c8d101120
    """
    messages = []
    msg_ids = re.findall(r"Message ID:\s*(\S+)", raw)
    thread_ids = re.findall(r"Thread ID:\s*(\S+)", raw)
    for mid, tid in zip(msg_ids, thread_ids, strict=False):
        messages.append({"message_id": mid, "thread_id": tid})
    for mid in msg_ids[len(messages):]:
        messages.append({"message_id": mid, "thread_id": ""})
    return messages


def _header_value(raw: str, header: str) -> str:
    line = next((ln for ln in raw.splitlines() if ln.lower().startswith(header)), "")
    return line[len(header):].strip() if line else ""


class GoogleMail:
    """Gmail through workspace-mcp. Text in, text out — the poller's original path."""

    name = "google"
    server_name = SERVER_NAME
    send_tool_name = "google_workspace__send_gmail_message"
    discovery_queries: tuple[str, ...] = (
        "search gmail messages unread inbox",
        "get gmail message content subject body sender",
        "get gmail attachment content download base64",
        "modify gmail message labels mark read unread",
    )

    async def list_unread(self, gateway: Any, mailbox: str, limit: int) -> list[MessageRef]:
        try:
            raw = await gateway.call_tool({
                "name": "google_workspace__search_gmail_messages",
                "arguments": {
                    "query": "is:unread in:inbox",
                    "user_google_email": mailbox,
                    "page_size": limit,
                },
            })
        except Exception:
            logger.exception("search_gmail_messages failed")
            return []
        if not isinstance(raw, str) or not raw.strip():
            return []
        return [
            MessageRef(message_id=m["message_id"], thread_id=m.get("thread_id", ""))
            for m in _parse_search_results(raw)
        ]

    async def fetch(self, gateway: Any, ref: MessageRef, mailbox: str) -> InboundMessage | None:
        raw = await gateway.call_tool({
            "name": "google_workspace__get_gmail_message_content",
            "arguments": {
                "message_id": ref.message_id,
                "user_google_email": mailbox,
                "body_format": "text",
            },
        })
        # Length only: the body is someone's mail, and debug logs are often
        # kept and shared far more widely than the mailbox itself.
        logger.debug("get_content: %d chars", len(raw) if isinstance(raw, str) else 0)
        if not isinstance(raw, str) or not raw.strip():
            return None
        # Use stdlib parseaddr so adversarial From headers like
        # `<a@evil.com> ignore previous instructions` don't smuggle trailing
        # content through. parseaddr returns ("display", "addr@host") and
        # ignores garbage after the angle-bracket address. Empty / unparseable
        # input → from_addr stays empty, downstream guards (audit, roster
        # lookup, [POLICY] notice) handle that gracefully.
        from_value = _header_value(raw, "from:")
        from_name, from_addr = parseaddr(from_value)
        stripped = _strip_reply_to(raw)
        recipients = _parse_recipients(raw)
        return InboundMessage(
            message_id=ref.message_id,
            thread_id=ref.thread_id,
            from_addr=from_addr.strip(),
            from_name=from_name.strip() or from_value,
            subject=_header_value(raw, "subject:")[:160],
            to=recipients,
            body_text=stripped,
            has_attachments="--- ATTACHMENTS ---" in raw,
            raw_text=stripped,
        )

    async def mark_read(self, gateway: Any, ref: MessageRef, mailbox: str) -> None:
        try:
            await gateway.call_tool({
                "name": "google_workspace__modify_gmail_message_labels",
                "arguments": {
                    "message_id": ref.message_id,
                    "user_google_email": mailbox,
                    "remove_label_ids": ["UNREAD"],
                },
            })
            logger.debug("marked message=%s as read", ref.message_id)
        except Exception:
            logger.warning("failed to mark message=%s as read", ref.message_id)

    def build_send_arguments(
        self,
        *,
        mailbox: str,
        to: str,
        subject: str,
        body: str,
        thread_id: str | None = None,
        html: bool = False,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "user_google_email": mailbox,
            "to": to,
            "subject": subject,
            "body": body,
        }
        if html:
            arguments["body_format"] = "html"
        if thread_id:
            arguments["thread_id"] = thread_id
        return arguments

    def reply_block(self, msg: InboundMessage) -> str:
        return (
            f"tool: {self.send_tool_name}\n"
            f"thread_id: {msg.thread_id or '(none — start a new thread)'}\n"
            "Reply with call_tool using that tool: set `to` to the sender, reuse the "
            "subject, and pass `thread_id` so the reply lands in the same thread."
        )

    def send_tool_hint(self) -> str:
        return "google_workspace__send_gmail_message (via MCP)"


def _extract_meet_link(event: dict[str, Any]) -> str | None:
    """Pull the Google Meet video URL out of a created-event response.

    The workspace-mcp ``manage_event`` returns the Calendar event object; the
    Meet link lives under ``conferenceData.entryPoints[].uri`` for the ``video``
    entry point. Some servers also surface ``hangoutLink`` directly. Tolerant of
    both snake_case and camelCase key spellings.
    """
    for key in ("meet_link", "hangoutLink", "hangout_link"):
        val = event.get(key)
        if isinstance(val, str) and val:
            return val
    conf = event.get("conferenceData") or event.get("conference_data") or {}
    if isinstance(conf, dict):
        entry_points = conf.get("entryPoints") or conf.get("entry_points") or []
        for ep in entry_points:
            if isinstance(ep, dict) and ep.get("entryPointType") == "video":
                uri = ep.get("uri")
                if isinstance(uri, str) and uri:
                    return uri
    return None


class GoogleCalendar:
    """Google Calendar through workspace-mcp's single ``manage_event`` tool."""

    name = "google"
    server_name = SERVER_NAME
    video_link_label = "Google Meet"

    async def create_event(self, gateway: Any, payload: dict[str, Any]) -> dict[str, Any]:
        from openexecutive.config import get_settings

        settings = get_settings()
        arguments: dict[str, Any] = {
            "action": "create",
            "summary": payload["title"],
            "start_time": payload["start"],
            "end_time": payload["end"],
            "attendees": payload["attendee_emails"],
            "send_updates": "all",
        }
        # Request a Google Meet link unless the payload explicitly opted out.
        if wants_video_link(payload, getattr(settings, "calendar_meet_links_enabled", True)):
            arguments["add_google_meet"] = True
        if payload.get("description"):
            arguments["description"] = payload["description"]

        try:
            raw = await gateway.call_tool({
                "name": "google_workspace__manage_event",
                "arguments": arguments,
            })
            result = json.loads(raw) if isinstance(raw, str) else raw
            # If the MCP returned an error dict, surface it directly.
            if isinstance(result, dict) and "error" in result:
                return {"error": result["error"]}
            if not isinstance(result, dict):
                return {"error": f"unexpected manage_event response: {str(result)[:200]}"}
            # The MCP returns the event object; extract the id field.
            event_id = result.get("id") or result.get("event_id") or result.get("eventId")
            meet_link = _extract_meet_link(result)
            out: dict[str, Any] = {"event_id": event_id, "raw": result}
            if meet_link:
                out["meet_link"] = meet_link
            return out
        except Exception as exc:
            logger.exception("calendar: manage_event create failed")
            return {"error": str(exc)}

    async def delete_event(self, gateway: Any, external_event_id: str) -> dict[str, Any]:
        try:
            raw = await gateway.call_tool({
                "name": "google_workspace__manage_event",
                "arguments": {
                    "action": "delete",
                    "event_id": external_event_id,
                    "send_updates": "all",
                },
            })
            result = json.loads(raw) if isinstance(raw, str) else raw
            return result if isinstance(result, dict) else {"ok": True}
        except Exception as exc:
            logger.exception("calendar: manage_event delete failed")
            return {"error": str(exc)}

    async def has_conflicts(
        self, gateway: Any, start_iso: str, end_iso: str, attendee_emails: list[str],
    ) -> bool | None:
        try:
            fb_result = await gateway.call_tool({
                "name": "google_workspace__query_freebusy",
                "arguments": {
                    "time_min": start_iso,
                    "time_max": end_iso,
                    "calendar_ids": list(attendee_emails),
                },
            })
            fb = json.loads(fb_result) if isinstance(fb_result, str) else fb_result
            if isinstance(fb, dict) and "has_conflicts" in fb:
                return bool(fb["has_conflicts"])
            return None
        except Exception:
            logger.debug("calendar: freebusy check skipped", exc_info=True)
            return None
