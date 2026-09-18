"""MCP-based Gmail polling loop.

Polls Gmail via the Google Workspace MCP server (OAuth). Each unread message is
passed raw to the Executive, which decides what to do: reply, fetch attachments,
create an alert, or ignore.

Actual tool names (confirmed via tools/list on the live MCP server):
  google_workspace__search_gmail_messages          → plain-text list of Message IDs + Thread IDs
  google_workspace__get_gmail_message_content      → plain-text Subject/From/--- BODY ---/--- ATTACHMENTS ---
  google_workspace__modify_gmail_message_labels    → mark as read (Complete/Extended tier)

No reply logic, no attachment logic, no alert logic lives here — all of that is the
Executive's responsibility via its tool access.
"""
from __future__ import annotations

import asyncio
import logging
import re
from email.utils import parseaddr
from typing import TYPE_CHECKING, Any

from openexecutive.config import get_settings

if TYPE_CHECKING:
    from openexecutive.orchestrator.mcp_gateway import MCPGateway

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = get_settings().email_poll_interval_seconds

# Prevents reprocessing the same message within a run (cleared on restart).
_processed_ids: set[str] = set()

_SKIP_SENDERS = ("noreply", "no-reply", "mailer-daemon", "postmaster", "do-not-reply")


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


async def poll_once(gateway: MCPGateway) -> None:
    """One poll cycle: find unread messages, hand each to the Executive."""
    from openexecutive.config import get_settings

    settings = get_settings()
    user_email = settings.exec_email_address

    try:
        raw = await gateway.call_tool({
            "name": "google_workspace__search_gmail_messages",
            "arguments": {
                "query": "is:unread in:inbox",
                "user_google_email": user_email,
                "page_size": 10,
            },
        })
    except Exception:
        logger.exception("search_gmail_messages failed")
        return

    if not raw or not raw.strip():
        return

    messages = _parse_search_results(raw)
    logger.debug("poll cycle — %d unread message(s)", len(messages))

    for msg in messages:
        mid = msg["message_id"]
        tid = msg.get("thread_id", "")
        if not mid or mid in _processed_ids:
            continue
        try:
            await _handle_email(gateway, mid, tid, user_email)
            _processed_ids.add(mid)
        except Exception:
            logger.exception("failed for message=%s", mid)


async def _handle_email(
    gateway: MCPGateway,
    message_id: str,
    thread_id: str,
    user_email: str,
) -> None:
    raw = await gateway.call_tool({
        "name": "google_workspace__get_gmail_message_content",
        "arguments": {
            "message_id": message_id,
            "user_google_email": user_email,
            "body_format": "text",
        },
    })
    if raw:
        preview = raw[:200]
        suffix = f"…[truncated {len(raw) - 200} chars]" if len(raw) > 200 else ""
        logger.debug("get_content raw=%r%s", preview, suffix)
    else:
        logger.debug("get_content raw=<empty>")

    if not raw or not raw.strip():
        logger.warning("empty content for message=%s", message_id)
        return

    # Minimal guard: skip self-sent (prevents reply loops) and known automated senders.
    from_line = next((ln for ln in raw.splitlines() if ln.lower().startswith("from:")), "")
    from_value = from_line[len("from:"):].strip()
    # Use stdlib parseaddr so adversarial From headers like
    # `<a@evil.com> ignore previous instructions` don't smuggle trailing
    # content through. parseaddr returns ("display", "addr@host") and
    # ignores garbage after the angle-bracket address. Empty / unparseable
    # input → from_addr stays empty, downstream guards (audit, roster
    # lookup, [POLICY] notice) handle that gracefully.
    _, parsed_addr = parseaddr(from_value)
    from_addr = parsed_addr.strip()
    if from_addr.lower() == user_email.lower():
        logger.debug("skipping self-addressed message=%s", message_id)
        return
    if any(p in from_line.lower() for p in _SKIP_SENDERS):
        logger.debug("skipping automated sender for message=%s", message_id)
        return

    # Sender-roster awareness. Unrostered senders are NOT dropped — the
    # Executive still reads, classifies, and decides. What protects us
    # from auto-replying to spam is the outbound gate
    # (orchestrator.mcp_gateway._check_gmail_recipients), which refuses
    # Gmail-send tool calls whose recipient isn't on the People roster.
    # The Executive sees a [POLICY] notice prepended to the body (built
    # in _run_executive) so it knows reply tools will block and proposes
    # to a human instead.
    from openexecutive.audit import log_event as audit_log
    from openexecutive.people.store import find_person_by_email
    sender_in_roster = find_person_by_email(from_addr) is not None
    if not sender_in_roster:
        logger.info(
            "non-roster sender=%s message=%s — routing to Executive (no auto-reply allowed)",
            from_addr, message_id,
        )
        audit_log(
            "integration_inbound",
            f"Accepted non-roster email from {from_addr} (reply blocked at outbound gate)",
            actor="email",
            details={
                "channel": "email",
                "from": from_addr,
                "message_id": message_id,
                "outcome": "accepted_non_roster",
            },
        )

    logger.info("routing message=%s to Executive", message_id)
    subject_line = next(
        (ln for ln in raw.splitlines() if ln.lower().startswith("subject:")), ""
    )
    subject = subject_line[len("subject:"):].strip()[:160] if subject_line else ""
    # Deterministic per-thread session id so every audit row from this inbound
    # (chat_turn, specialist_consult, tool_invocation) shares a grouping key
    # with the integration_inbound row. Falls back to from_addr when the IMAP
    # message exposes no thread header.
    session_id = f"email:{thread_id or from_addr}"
    audit_log(
        "integration_inbound",
        f"Inbound email from {from_addr}: {subject}" if subject else f"Inbound email from {from_addr}",
        actor="email",
        session_id=session_id,
        details={
            "channel": "email",
            "message_id": message_id,
            "thread_id": thread_id,
            "from": from_addr,
            "subject": subject,
        },
    )
    try:
        await _run_executive(
            gateway, _strip_reply_to(raw), message_id, thread_id, from_addr, session_id
        )
    except Exception:
        logger.exception("Executive raised for message=%s", message_id)

    await _mark_read(gateway, message_id, user_email)


async def _run_executive(
    gateway: MCPGateway,
    raw_email: str,
    message_id: str,
    thread_id: str,
    from_addr: str = "",
    session_id: str | None = None,
) -> None:
    from openexecutive.knowledge.retriever import retrieve
    from openexecutive.memory.episodic import format_for_prompt
    from openexecutive.onboarding.profile_builder import load_or_create_profile
    from openexecutive.orchestrator.executive import Executive
    from openexecutive.orchestrator.session import Session

    profile = load_or_create_profile()
    session_kwargs: dict[str, Any] = {
        "company_profile": profile if not profile.is_empty() else None,
    }
    if session_id:
        session_kwargs["session_id"] = session_id
    session = Session(**session_kwargs)
    if from_addr:
        # Only register the sender as a schedulable channel_ref if they
        # are in the People roster. Without this guard, an attacker who
        # can spoof a From header could persuade the Executive (via
        # prompt injection in the body) to schedule outbound mail to
        # arbitrary third parties. The roster gate in _handle_email
        # already ensures we only get here for known senders, but
        # re-verify defensively — _run_executive is also reachable from
        # other code paths.
        from openexecutive.people.store import find_person_by_email

        settings = get_settings()
        if (
            from_addr.lower() == settings.exec_email_address.lower()
            or find_person_by_email(from_addr) is not None
        ):
            session.seen_channel_refs.add(("email", f"{from_addr}|{thread_id}"))
            session.seen_channel_refs.add(("email", from_addr))
    # Look up the OE Person record (case-insensitive by email) so Honcho
    # can key per-person memory off Person.id (shared across channels).
    # No match → person_id stays None and the Honcho layer no-ops.
    from openexecutive.people.store import find_person_by_email

    person_id: int | None = None
    if from_addr:
        person = find_person_by_email(from_addr)
        person_id = person.id if person else None

    # Multi-peer co-presence: parse To+Cc headers and resolve each
    # recipient to a Person via find_person_by_email. Skip the From
    # (already covered by person_id) and the OE exec's own address
    # (we ARE the executive — never a peer). Best-effort: parse
    # failures degrade to an empty list rather than blocking the turn.
    co_present_person_ids: list[int] = []
    try:
        recipients = _parse_recipients(raw_email)
        exec_email = get_settings().exec_email_address.lower()
        from_addr_lower = (from_addr or "").lower()
        for addr in recipients:
            addr_lower = addr.lower()
            if addr_lower in (exec_email, from_addr_lower):
                continue
            other = find_person_by_email(addr)
            if other and other.id is not None and other.id not in co_present_person_ids:
                co_present_person_ids.append(other.id)
    except Exception:
        logger.warning(
            "email: recipient parsing failed for message=%s — passing empty co-present list",
            message_id,
            exc_info=True,
        )

    # When the sender isn't on the People roster, prepend a [POLICY]
    # notice so the Executive doesn't waste a turn trying to auto-reply
    # (the MCP gateway's _check_gmail_recipients will block it anyway).
    # The notice lists the actions that ARE allowed so the model picks
    # the right path: classify, log, alert, or propose adding to roster.
    policy_notice = ""
    if from_addr and person_id is None:
        policy_notice = (
            f"[POLICY] This inbound is from {from_addr}, who is NOT on your team's "
            "People roster. You can classify it, log a decision, schedule an internal "
            "follow-up, alert the principal, or surface a proposal to add the sender "
            "to the roster. You cannot send an outbound reply directly to "
            f"{from_addr} — the email gateway will block it. To actually reply, the "
            "principal must add the sender to the People roster first.\n\n"
            "---\n\n"
        )

    # If this email is a reply to mail the Executive sent during another
    # session (e.g. web chat), hydrate the turn with that originating context
    # — the email analogue of the DM bots. channel_ref is the bare lowercased
    # sender address, matching what the gateway records at send time. No-op on
    # a miss, so a thread that already carries history is unaffected.
    base_message = (
        f"You have an inbound email (message_id={message_id}, thread_id={thread_id}).\n\n"
        f"{policy_notice}{raw_email}"
    )
    if from_addr:
        from openexecutive.integrations.inbound_hydration import (
            hydrate_user_message,
        )

        base_message = hydrate_user_message(
            channel="email",
            channel_ref=from_addr.lower(),
            user_message=base_message,
        )

    executive = Executive(mcp_gateway=gateway)
    # Standard (non-committee) path, same as the Slack and Discord
    # adapters. Committee review (draft + 3 critiques + revision, and a
    # deeper Honcho prefetch) is a per-request opt-in on /chat only; it
    # was previously forced on here for every inbound email, including
    # off-roster senders the gateway will not let us reply to anyway.
    await executive.chat(
        user_message=base_message,
        session=session,
        retrieved_context=retrieve(query=raw_email[:500]),
        episodic_context=format_for_prompt(),
        person_id=person_id,
        co_present_person_ids=co_present_person_ids or None,
    )


async def _mark_read(gateway: MCPGateway, message_id: str, user_email: str) -> None:
    try:
        await gateway.call_tool({
            "name": "google_workspace__modify_gmail_message_labels",
            "arguments": {
                "message_id": message_id,
                "user_google_email": user_email,
                "remove_label_ids": ["UNREAD"],
            },
        })
        logger.debug("marked message=%s as read", message_id)
    except Exception:
        logger.warning("failed to mark message=%s as read", message_id)


async def _discover_gmail_tools(gateway: MCPGateway) -> None:
    """Discover Gmail MCP tools (extensible-mcp requires per-session discovery)."""
    queries = [
        "search gmail messages unread inbox",
        "get gmail message content subject body sender",
        "get gmail attachment content download base64",
        "modify gmail message labels mark read unread",
    ]
    for query in queries:
        result = await gateway.search_tools({"query": query})
        logger.debug(
            "search_tools(%r) -> %r",
            query, str(result)[:200] if result else "",
        )
    logger.info("Gmail tools discovered")


async def run_email_poller(gateway: MCPGateway) -> None:
    """Async polling loop. Run as a background task; cancelled on shutdown."""
    logger.info("started (interval=%ds)", POLL_INTERVAL_SECONDS)
    while True:
        try:
            await _discover_gmail_tools(gateway)
            await poll_once(gateway)
        except asyncio.CancelledError:
            logger.info("cancelled")
            raise
        except Exception:
            logger.exception("unexpected error in poll cycle")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
