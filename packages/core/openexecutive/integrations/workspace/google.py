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

    For a Calendar event object (as JSON), the
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


# workspace-mcp answers ``manage_event`` in plain text ("Successfully created
# event 'X' for a@b.com. Link: https://www.google.com/calendar/event?eid=...
# Google Meet: https://meet.google.com/..."), and a failure as the error's
# text. Only the gateway's own refusals are JSON.
_CREATED_PREFIX = "successfully created event"
_DELETED_PREFIX = "successfully deleted event"
_EVENT_LINK_RE = re.compile(r"Link:\s*(\S+)")
_MEET_LINK_RE = re.compile(r"Google Meet:\s*(https://\S+)")
_EVENT_LINK_HOSTS = frozenset({"www.google.com", "calendar.google.com"})
_MEET_HOST = "meet.google.com"


def _json_object(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _event_id_from_link(link: str, calendar_owner: str | None) -> str | None:
    """The event id inside a Calendar ``htmlLink``: its ``eid`` parameter is
    base64url of ``"<event id> <calendar id>"``. Only a Google Calendar link,
    and only an event on ``calendar_owner``'s calendar when that is known."""
    import base64
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(link)
    if parsed.scheme != "https" or parsed.netloc.lower() not in _EVENT_LINK_HOSTS:
        return None
    eid = (parse_qs(parsed.query).get("eid") or [""])[0]
    if not eid:
        return None
    try:
        decoded = base64.urlsafe_b64decode(eid + "=" * (-len(eid) % 4)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    event_id, _, calendar_id = decoded.partition(" ")
    event_id = event_id.strip()
    if calendar_owner and calendar_id.strip().lower() != calendar_owner.strip().lower():
        return None
    return event_id or None


def _after_echoed_title(text: str, title: str | None) -> str:
    """The reply with the event's own title cut off. workspace-mcp echoes the
    title before the link, and the title is the model's text: a title that
    spells out its own "Link: …" must not be read as the event's."""
    for spelling in dict.fromkeys((title, (title or "").strip())):
        if spelling:
            marker = f"'{spelling}' for "
            at = text.find(marker)
            if at != -1:
                return text[at + len(marker):]
    # Unknown or reworded title: the real link and Meet link come last.
    return text


def _last(pattern: re.Pattern[str], text: str) -> str | None:
    found = pattern.findall(text)
    return found[-1] if found else None


def _parse_created_event(
    raw: Any, *, title: str | None = None, calendar_owner: str | None = None,
) -> dict[str, Any]:
    """``{"event_id", "raw"[, "meet_link"]}`` for a created event, else
    ``{"error"}``. A confirmed create is a success even when no id can be read
    from it: reporting it failed would invite a second, duplicate booking."""
    from urllib.parse import urlparse

    result = _json_object(raw)
    if result is not None:
        if "error" in result:
            return {"error": result["error"]}
        event_id = result.get("id") or result.get("event_id") or result.get("eventId")
        out: dict[str, Any] = {"event_id": event_id, "raw": result}
        meet_link = _extract_meet_link(result)
        if meet_link:
            out["meet_link"] = meet_link
        return out
    text = raw if isinstance(raw, str) else str(raw)
    if not text.strip().lower().startswith(_CREATED_PREFIX):
        return {"error": f"unexpected manage_event response: {text.strip()[:200]}"}
    rest = _after_echoed_title(text, title)
    link = _last(_EVENT_LINK_RE, rest)
    event_id = _event_id_from_link(link, calendar_owner) if link else None
    if event_id is None:
        logger.warning("calendar: created an event but read no id from its link")
    out = {"event_id": event_id, "raw": {"text": text}}
    meet = _last(_MEET_LINK_RE, rest)
    if meet and urlparse(meet).netloc.lower() == _MEET_HOST:
        out["meet_link"] = meet
    return out


def _parse_deleted_event(raw: Any) -> dict[str, Any]:
    result = _json_object(raw)
    if result is not None:
        return result
    text = raw if isinstance(raw, str) else str(raw)
    if text.strip().lower().startswith(_DELETED_PREFIX):
        return {"ok": True}
    return {"error": f"unexpected manage_event response: {text.strip()[:200]}"}


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
            return _parse_created_event(
                raw,
                title=payload["title"],
                calendar_owner=getattr(settings, "exec_email_address", None),
            )
        except Exception as exc:
            logger.exception("calendar: manage_event create failed")
            return {"error": str(exc)}

    async def delete_event(self, gateway: Any, external_event_id: str) -> dict[str, Any]:
        from openexecutive.orchestrator.mcp_gateway import calendar_cancel_grant

        try:
            # A meeting the Executive booked, attendees roster-checked when
            # booked: the gateway lets this delete mail its cancellation.
            with calendar_cancel_grant(external_event_id):
                raw = await gateway.call_tool({
                    "name": "google_workspace__manage_event",
                    "arguments": {
                        "action": "delete",
                        "event_id": external_event_id,
                        "send_updates": "all",
                    },
                })
            return _parse_deleted_event(raw)
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
