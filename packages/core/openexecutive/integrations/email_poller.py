"""MCP-based inbound mailbox polling loop.

Polls the Executive's own mailbox through the configured workspace backend
(`integrations.workspace`: Gmail via workspace-mcp, or Outlook via
ms-365-mcp-server — `EMAIL_PROVIDER`). Each unread message is rendered into
one backend-neutral text (`workspace.mail.render_for_executive`) and handed
to the Executive, which decides what to do: reply, fetch attachments, create
an alert, or ignore. The `--- REPLY ---` block at the end of that text names
the exact reply tool and threading ids for the backend in use, so the persona
stays backend-neutral (and cacheable).

No reply logic, no attachment logic, no alert logic lives here — all of that is the
Executive's responsibility via its tool access. The backend argument shapes
live in `workspace/google.py` and `workspace/microsoft.py`.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any

from openexecutive.config import get_settings
from openexecutive.integrations.workspace.google import _parse_recipients
from openexecutive.integrations.workspace.mail import (
    MailProvider,
    MessageRef,
    render_for_executive,
)
from openexecutive.integrations.workspace.registry import (
    get_mail_provider,
    provider_server_missing,
)

if TYPE_CHECKING:
    from openexecutive.orchestrator.mcp_gateway import MCPGateway

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = get_settings().email_poll_interval_seconds

# Prevents reprocessing the same message within a run (cleared on restart).
_processed_ids: set[str] = set()

_SKIP_SENDERS = ("noreply", "no-reply", "mailer-daemon", "postmaster", "do-not-reply")


def _mail_provider() -> MailProvider:
    """The configured mail backend, resolved through this module's `get_settings`
    (tests patch it with a stub; a stub without `email_provider` → google)."""
    return get_mail_provider(get_settings())


# Section markers in the rendered inbound text (`workspace.mail.render_for_
# executive`, which mirrors get_gmail_message_content's layout): header
# lines, then the body, then an optional numbered attachment list whose lines
# read `1. <filename> (<mime type>, <size> KB)`, then the `--- REPLY ---`
# block the poller appends. The attachment list is appended after the body,
# so a marker-looking line inside the body is told apart by position.
_BODY_MARKER = "--- BODY ---"
_ATTACHMENTS_MARKER = "--- ATTACHMENTS ---"
_REPLY_MARKER = "--- REPLY ---"
# What the MCP writes when a message has no text/plain part — not the
# sender's words.
_NO_BODY_PLACEHOLDER = "[No text/plain body found]"
_NO_SUBJECT_PLACEHOLDER = "(no subject)"
# A reply or forward carries the earlier message's subject — often the
# Executive's own ("Re: Approve the Acme renewal") — which would let its words
# pass the extraction and open-loop quote gates as the sender's.
# Covers the common client prefixes (English, German AW/WG, Scandinavian SV,
# Dutch Antw, Italian R), a counter ("Re[2]:"), and tags an MTA prepends
# ("[EXT] Re:"). Each tag is bounded and ends at "]", so this stays linear.
_REPLY_SUBJECT_RE = re.compile(
    r"^(\[[^\]]{0,40}\]\s*)*(re|fwd?|fw|aw|wg|sv|antw|r)(\[\d{1,3}\])?\s*:",
    re.IGNORECASE,
)
# An attachment line is `N. <filename> (<mime>, <size> KB)`, optionally
# followed by ` [in attached message]`. Parsed by splitting from the right
# rather than one regex: the filename is free text an email sender controls,
# and a pattern with adjacent `\s+` / `.+?` groups backtracks cubically on a
# long run of spaces — enough for one inbound email to stall the process.
_ATTACHMENT_INDEX_RE = re.compile(r"\d+\.\s")
_ATTACHMENT_SIZE_RE = re.compile(r"[\d.]+ KB")
_ATTACHMENT_NESTED_SUFFIX = " [in attached message]"
# Longer lines are not the MCP's; skipping them bounds the work per line.
_ATTACHMENT_LINE_MAX_CHARS = 512
# A reply attribution ("On <date>, <name> <addr> wrote:"). One on a single
# line is trusted: what follows is the older message, quoted with ">" (then
# skipped line by line, so text the sender wrote below it survives) or not
# (then the scan ends there). Gmail wraps a long one over several lines; that
# shape is only trusted when ">" lines follow, so the sender's own
# "On Monday I'll ..." above a "... wrote:" line is never mistaken for one.
_ATTRIBUTION_RE = re.compile(r"^On\s.+wrote:\s*$")
_ATTRIBUTION_TAIL_RE = re.compile(r"wrote:\s*$")
_ATTRIBUTION_MAX_LINES = 4
# Where everything below is an older message rather than the sender's text.
_ORIGINAL_MESSAGE_RE = re.compile(r"^-{2,}\s*Original Message\s*-{2,}", re.IGNORECASE)
_FORWARDED_RE = re.compile(r"^-{2,}\s*Forwarded message\s*-{2,}", re.IGNORECASE)
_OUTLOOK_RULE_RE = re.compile(r"^_{10,}\s*$")
_OUTLOOK_HEADER_RE = re.compile(r"^From:\s")
_OUTLOOK_SENT_RE = re.compile(r"^(Sent|Date):\s")
# How far below an Outlook-style "From:" line its "Sent:" line may sit.
_OUTLOOK_HEADER_SPAN = 4


def _strip_reply_block(raw: str) -> str:
    """Drop the trailing ``--- REPLY ---`` block `render_for_executive` appends.

    That block names the reply tool and threading ids — the poller's words, not
    the sender's — and it is always the LAST such line: a forged copy inside a
    body is quoted (``> --- REPLY ---``) by `workspace.mail._neutralize_markers`
    before this runs, so it never matches.
    """
    lines = raw.splitlines()
    stripped = [ln.strip() for ln in lines]
    if _REPLY_MARKER not in stripped:
        return raw
    end = len(stripped) - 1 - stripped[::-1].index(_REPLY_MARKER)
    return "\n".join(lines[:end])


def _split_gmail_content(raw: str) -> tuple[list[str], list[str], list[str]]:
    """Split get_gmail_message_content text into (header, body, attachment) lines.

    Without a ``--- BODY ---`` marker, the body is everything after the
    first blank line, like an RFC 822 message. The attachment list starts at
    the LAST ``--- ATTACHMENTS ---`` line, since the MCP appends it after the
    body and the body itself may contain that text.
    """
    lines = raw.splitlines()
    stripped = [ln.strip() for ln in lines]
    if _BODY_MARKER in stripped:
        start = stripped.index(_BODY_MARKER)
        header, rest = lines[:start], lines[start + 1:]
    else:
        blank = next((i for i, ln in enumerate(stripped) if not ln), len(lines))
        header, rest = lines[:blank], lines[blank + 1:]
    rest_stripped = [ln.strip() for ln in rest]
    if _ATTACHMENTS_MARKER in rest_stripped:
        end = len(rest_stripped) - 1 - rest_stripped[::-1].index(_ATTACHMENTS_MARKER)
        return header, rest[:end], rest[end + 1:]
    return header, rest, []


def _quote_follows(body: list[str], j: int) -> bool:
    """Whether the next non-blank line after ``body[j]`` is a ">" quote."""
    k = j + 1
    # Indexing, not slicing: a body of many attribution-like lines must not
    # make this quadratic.
    while k < len(body) and not body[k].strip():
        k += 1
    return k < len(body) and body[k].strip().startswith(">")


def _attachment_name(line: str) -> str | None:
    """The filename in one attachment-list line, or None if it isn't one."""
    text = line.strip()
    if len(text) > _ATTACHMENT_LINE_MAX_CHARS:
        return None
    index = _ATTACHMENT_INDEX_RE.match(text)
    if index is None:
        return None
    text = text[index.end():].removesuffix(_ATTACHMENT_NESTED_SUFFIX)
    if not text.endswith(")"):
        return None
    name, sep, meta = text[:-1].rpartition(" (")
    _mime, comma, size = meta.rpartition(", ")
    if not (sep and comma and _ATTACHMENT_SIZE_RE.fullmatch(size)):
        return None
    return name.strip() or None


def _attribution_end(body: list[str], i: int) -> tuple[int, bool] | None:
    """If ``body[i]`` opens a reply attribution: the index of its last line
    and whether ">" quote lines follow it."""
    text = body[i].strip()
    if not text.startswith("On "):
        return None
    if _ATTRIBUTION_RE.match(text):
        return i, _quote_follows(body, i)
    for j in range(i + 1, min(i + _ATTRIBUTION_MAX_LINES, len(body))):
        line = body[j].strip()
        # A wrapped attribution is one unbroken run of lines: a blank, a quote
        # or another "On ..." line means body[i] was the sender's own text
        # ("On it, will send Friday.") and any real attribution is later.
        if not line or line.startswith((">", "On ")):
            return None
        if _ATTRIBUTION_TAIL_RE.search(line):
            return (j, True) if _quote_follows(body, j) else None
    return None


def _new_text_lines(body: list[str]) -> tuple[list[str], bool]:
    """The sender's own lines, without quoted replies, stopping where an
    older message is appended below. Returns (lines, whether a forwarded
    message was cut off).

    A reply attribution followed by ">" lines is skipped rather than ending
    the scan, so text the sender wrote below a quote (bottom-posting or an
    interleaved reply) is kept.
    """
    kept: list[str] = []
    i = 0
    while i < len(body):
        text = body[i].strip()
        if _FORWARDED_RE.match(text):
            return kept, True
        if _ORIGINAL_MESSAGE_RE.match(text) or _OUTLOOK_RULE_RE.match(text):
            break
        if _OUTLOOK_HEADER_RE.match(text) and any(
            _OUTLOOK_SENT_RE.match(b.strip())
            for b in body[i + 1:i + 1 + _OUTLOOK_HEADER_SPAN]
        ):
            break
        attribution = _attribution_end(body, i)
        if attribution is not None:
            end, quoted = attribution
            if not quoted:
                break
            i = end + 1
            continue
        if not text.startswith(">") and text != _NO_BODY_PLACEHOLDER:
            kept.append(body[i].rstrip())
        i += 1
    return kept, False


def _email_memory_text(raw: str) -> str:
    """What peer memory should record as the sender's own words for an email.

    The Executive's turn carries the whole message — the "You have an
    inbound email" framing, any [POLICY] notice, every header (including
    the Executive's own address in To:) and the quoted chain, which often
    holds the Executive's earlier email. Recorded under the sender's peer,
    Honcho reads all of that as the sender speaking and concludes the sender
    *is* the Executive ("received an email from <sender>", "is associated
    with <exec address>"). So memory gets only the sender's new text, the
    attachment filenames and the subject — unless it is a reply's or
    forward's, which is the earlier message's subject, not the sender's.
    """
    header, body, attachments = _split_gmail_content(_strip_reply_block(raw))
    subject = next(
        (ln.split(":", 1)[1].strip() for ln in header if ln.lower().startswith("subject:")),
        "",
    )
    new_lines, forwarded = _new_text_lines(body)
    new_text = "\n".join(new_lines).strip()
    names = [name for name in map(_attachment_name, attachments) if name]

    parts: list[str] = []
    if subject and subject != _NO_SUBJECT_PLACEHOLDER and not _REPLY_SUBJECT_RE.match(subject):
        parts.append(f"Subject: {subject}")
    if new_text:
        parts.append(new_text)
    if forwarded:
        parts.append("[Forwarded an earlier message]")
    if names:
        # Not "[Attached: …]": that line marks inlined document text, and the
        # open-loop pass skips any turn carrying it (see open_loops).
        parts.append(f"(Attached files: {', '.join(names)})")
    return "\n\n".join(parts)


async def poll_once(gateway: MCPGateway, provider: MailProvider | None = None) -> None:
    """One poll cycle: find unread messages, hand each to the Executive."""
    settings = get_settings()
    user_email = settings.exec_email_address
    provider = provider or _mail_provider()

    refs = await provider.list_unread(gateway, user_email, 10)
    logger.debug("poll cycle — %d unread message(s)", len(refs))

    for ref in refs:
        mid = ref.message_id
        if not mid or mid in _processed_ids:
            continue
        try:
            await _handle_email(gateway, mid, ref.thread_id, user_email, provider=provider)
            _processed_ids.add(mid)
        except Exception:
            logger.exception("failed for message=%s", mid)


async def _handle_email(
    gateway: MCPGateway,
    message_id: str,
    thread_id: str,
    user_email: str,
    provider: MailProvider | None = None,
) -> None:
    provider = provider or _mail_provider()
    msg = await provider.fetch(gateway, MessageRef(message_id, thread_id), user_email)
    if msg is None:
        logger.warning("empty content for message=%s", message_id)
        return
    # The backend derived from_addr with parseaddr (or a structured field), so
    # adversarial From headers like `<a@evil.com> ignore previous instructions`
    # cannot smuggle trailing content into the [POLICY] notice below. Empty /
    # unparseable → from_addr stays empty; downstream guards (audit, roster
    # lookup, [POLICY] notice) handle that gracefully.
    from_addr = msg.from_addr
    thread_id = msg.thread_id or thread_id

    # Minimal guard: skip self-sent (prevents reply loops) and known automated senders.
    if from_addr.lower() == user_email.lower():
        logger.debug("skipping self-addressed message=%s", message_id)
        return
    sender_blob = f"{msg.from_name} {from_addr}".lower()
    if any(p in sender_blob for p in _SKIP_SENDERS):
        logger.debug("skipping automated sender for message=%s", message_id)
        return

    # Sender-roster awareness. Unrostered senders are NOT dropped — the
    # Executive still reads, classifies, and decides. What protects us
    # from auto-replying to spam is the outbound gate
    # (orchestrator.mcp_gateway: _check_gmail_recipients /
    # _check_m365_recipients), which refuses mail-send tool calls whose
    # recipient isn't on the People roster. The Executive sees a [POLICY]
    # notice prepended to the body (built in _run_executive) so it knows
    # reply tools will block and proposes to a human instead.
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
    subject = msg.subject[:160]
    # Deterministic per-thread session id so every audit row from this inbound
    # (chat_turn, specialist_consult, tool_invocation) shares a grouping key
    # with the integration_inbound row. Falls back to from_addr when the
    # backend exposes no thread id.
    session_id = f"email:{thread_id or from_addr}"
    audit_log(
        "integration_inbound",
        f"Inbound email from {from_addr}: {subject}" if subject else f"Inbound email from {from_addr}",
        actor="email",
        session_id=session_id,
        details={
            "channel": "email",
            "provider": provider.name,
            "message_id": message_id,
            "thread_id": thread_id,
            "from": from_addr,
            "subject": subject,
        },
    )
    rendered = render_for_executive(msg, provider.reply_block(msg))
    try:
        await _run_executive(gateway, rendered, message_id, thread_id, from_addr, session_id)
    except Exception:
        logger.exception("Executive raised for message=%s", message_id)

    await _mark_read(gateway, message_id, user_email, provider=provider)


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
        # Only the sender's own words reach peer memory — see _email_memory_text.
        # An unrostered sender has no peer to record into, so skip the parse.
        memory_text=_email_memory_text(raw_email) if person_id is not None else None,
    )


async def _mark_read(
    gateway: MCPGateway, message_id: str, user_email: str, provider: MailProvider | None = None,
) -> None:
    provider = provider or _mail_provider()
    await provider.mark_read(gateway, MessageRef(message_id), user_email)


async def _discover_mail_tools(gateway: MCPGateway, provider: MailProvider) -> None:
    """Discover the backend's mail tools (extensible-mcp requires per-session discovery)."""
    for query in provider.discovery_queries:
        result = await gateway.search_tools({"query": query})
        logger.debug(
            "search_tools(%r) -> %r",
            query, str(result)[:200] if result else "",
        )
    logger.info("%s mail tools discovered", provider.name)


async def run_email_poller(gateway: MCPGateway, provider: MailProvider | None = None) -> None:
    """Async polling loop. Run as a background task; cancelled on shutdown.

    Fail-soft on a misconfigured switch: when the chosen backend's MCP server
    is not in the config, every cycle logs one ERROR and skips instead of
    spraying tool-not-found errors (and the API keeps serving).
    """
    from openexecutive.scheduler.pause import is_paused

    settings = get_settings()
    provider = provider or get_mail_provider(settings)
    logger.info("started (provider=%s, interval=%ds)", provider.name, POLL_INTERVAL_SECONDS)
    config_path = getattr(settings, "mcp_servers_config_path", None)
    holding_for_pause = False
    while True:
        try:
            # Operator pause: leave the inbox untouched. Unread mail stays
            # unread and is processed on the first poll after resume.
            if is_paused():
                if not holding_for_pause:
                    logger.warning("executive paused — not polling the %s mailbox", provider.name)
                    holding_for_pause = True
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                continue
            if holding_for_pause:
                logger.info("executive resumed — polling the %s mailbox again", provider.name)
                holding_for_pause = False
            if config_path is not None and provider_server_missing(provider.server_name, config_path):
                logger.error(
                    "email poller: EMAIL_PROVIDER=%s but MCP server '%s' is not defined in %s "
                    "— skipping this cycle",
                    provider.name, provider.server_name, config_path,
                )
            else:
                await _discover_mail_tools(gateway, provider)
                await poll_once(gateway, provider)
        except asyncio.CancelledError:
            logger.info("cancelled")
            raise
        except Exception:
            logger.exception("unexpected error in poll cycle")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
