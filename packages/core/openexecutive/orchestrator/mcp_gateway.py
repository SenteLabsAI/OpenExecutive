from __future__ import annotations

import contextlib
import json
import logging
import os
import re
from collections.abc import Iterator
from email.utils import getaddresses
from pathlib import Path
from typing import Any

from openexecutive.config import get_settings
from openexecutive.security.grimdall_guard import guard_tool_call, is_enabled

logger = logging.getLogger(__name__)

_UVX_CMD = "uvx"
_EXTENSIBLE_MCP_GIT = "git+https://github.com/SenteLabsAI/extensible-mcp"
_EXTENSIBLE_MCP_CMD = "extensible-mcp"

# Env vars forwarded into the extensible-mcp subprocess. The MCP stdio client
# (mcp.client.stdio) does NOT pass our environment through: when
# StdioServerParameters.env is None it gives the child only a fixed safe
# allowlist (HOME, PATH, …) and drops everything else. That silently stripped
# the embedding-cache/offline config, so extensible-mcp's fastembed tool-search
# model (Qdrant/all-MiniLM-L6-v2-onnx, which fastembed resolves from
# "sentence-transformers/all-MiniLM-L6-v2") was re-fetched from the Hugging Face
# Hub on every cold start. Forwarding these — set in the API image, see
# docker/Dockerfile — lets fastembed load the baked cache offline instead.
# Only vars actually present are forwarded, so local/CI behaviour is unchanged
# when they are unset (env stays None → SDK default).
#
# The Google* / WORKSPACE_MCP_* / GWORKSPACE_AUTH_MODE vars are forwarded for the
# co-located google_workspace stdio child: extensible-mcp interpolates the
# `$VAR` placeholders in that server's `env` block (mcp_servers.json) from its
# OWN environment, so the API's Google secrets must reach extensible-mcp here
# first. They carry the workspace-mcp credentials/auth-mode and the credentials
# dir on the /data volume. Absent → not forwarded, so non-Google installs and CI
# are unaffected.
_FORWARDED_ENV_VARS = (
    "FASTEMBED_CACHE_PATH",
    "HF_HUB_OFFLINE",
    "HF_HOME",
    "TRANSFORMERS_OFFLINE",
    "GWORKSPACE_AUTH_MODE",
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
    "GOOGLE_SERVICE_ACCOUNT_KEY_JSON",
    "GOOGLE_SERVICE_ACCOUNT_KEY_FILE",
    "USER_GOOGLE_EMAIL",
    "WORKSPACE_MCP_CREDENTIALS_DIR",
    "WORKSPACE_MCP_TOOL_TIER",
)

# Outbound Gmail tools whose arguments may carry recipients. Any tool name
# matching one of these (after the `google_workspace__` namespace prefix) is
# subject to the recipient allow-list. Names track workspace-mcp 1.21.1: the
# send/reply/forward surface collapsed into a single `send_gmail_message` (reply
# and forward are just that tool with thread_id/quoting), and `draft_gmail_message`
# replaced `create_gmail_draft`. Drafts are gated too (defense-in-depth: a draft
# carries recipients and may be sent later). Re-verify these names on any
# workspace-mcp bump.
_GATED_GMAIL_TOOLS = frozenset({
    "google_workspace__send_gmail_message",
    "google_workspace__draft_gmail_message",
})

# Calendar write tool whose `attendees` argument may carry arbitrary email
# addresses.  `manage_event` is the single MCP tool that creates, updates,
# deletes, and RSVPs — all mutation paths must be gated.
_GATED_CALENDAR_TOOLS = frozenset({
    "google_workspace__manage_event",
})

# Namespace prefix every workspace-mcp tool carries once proxied through the
# gateway.
_GW_PREFIX = "google_workspace__"

# Drive sharing / permission tools exposed at the `complete` tool tier. These
# grant another principal access to a file — the Drive analogue of sending an
# email or inviting a calendar attendee — so they get the same roster egress
# gate (`_check_drive_share`). `manage_drive_access` grants/updates/revokes a
# permission and can transfer ownership (takes an email + role + type);
# `set_drive_file_permissions` configures link sharing. Add any new
# access-granting Drive tool name here.
_GATED_DRIVE_TOOLS = frozenset({
    "google_workspace__manage_drive_access",
    "google_workspace__set_drive_file_permissions",
})

# Permission "type"/"scope" enum values that grant access to a population rather
# than a single addressable person — i.e. public or whole-domain sharing. These
# bypass the per-recipient roster model entirely, so any Drive-share argument
# whose value normalizes to one is refused. Stored in normalized form (lowercase,
# separators stripped) and compared via `_norm_share_token`, so spelling variants
# — "anyoneWithLink", "anyone_with_link", "anyone-with-link" — all match.
_PUBLIC_SHARE_SCOPES = frozenset({
    "anyone",
    "anyonewithlink",
    "anyonecanfind",
    "domain",
})

# Argument keys (normalized: lowercase, separators stripped) that turn on
# public / whole-domain / link-based access. The string scan above only sees
# string *values*; a tool that models "anyone with link" as a boolean/int flag
# (e.g. {"public": true} or the Drive v2 {"withLink": true}) would slip past it.
# `_has_public_share_flag` matches a key EXACTLY against this set (not substring)
# so benign metadata keys like `email_domain` / `published_at` / `public_id`
# don't trip it, then applies a type-agnostic truthiness test to the value.
# Covers the canonical Drive v3 booleans and the legacy v2 link-sharing names;
# a wholly novel key name is a residual gap (the canonical scope *string* form
# is still caught by `_PUBLIC_SHARE_SCOPES`).
_PUBLIC_SHARE_KEYS = frozenset({
    "public",
    "ispublic",
    "makepublic",
    "anyone",
    "anyonewithlink",
    "anyonecanfind",
    "linksharing",
    "sharedlink",
    "shareablelink",
    "sharablelink",
    "withlink",
    "sharedwithlink",
    "weblink",
    "published",
    "allowfilediscovery",
    "allowdiscovery",
    "domainsharing",
    "sharewithdomain",
})

# Values under a public-share key that mean "off / restricted" — these do NOT
# trip the block, so disabling link sharing (the safe direction) is allowed.
_NEGATIVE_FLAG_VALUES = frozenset({
    "", "false", "0", "no", "none", "null", "private", "restricted", "off",
    "disabled", "limited",
})

# Conservative email matcher for scanning free-form Drive-share arguments. We do
# not know every grantee field name across workspace-mcp versions, so the gate
# scans *all* string values rather than an allow-list of keys — every email-like
# token found must resolve to the roster (fail closed on the unknown).
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _norm_share_token(s: str) -> str:
    """Lowercase and strip separators so scope spellings collapse to one form
    (``anyone-with-link`` / ``anyone_with_link`` / ``anyoneWithLink`` → the
    same token)."""
    return re.sub(r"[^a-z0-9]", "", s.strip().lower())

_GMAIL_RECIPIENT_FIELDS = ("to", "cc", "bcc")
# Allow-list of argument keys permitted on gated Gmail tool calls. Allow-list
# rather than block-list, so an unknown key that could smuggle recipients
# (custom headers, raw MIME blob, multipart parts, additional_headers, etc.)
# is rejected by default. Add new keys here only after confirming they cannot
# carry an unvalidated address.
_GMAIL_ALLOWED_ARG_KEYS = frozenset({
    "user_google_email",
    "to",
    "cc",
    "bcc",
    "subject",
    "body",
    "html_body",
    # workspace-mcp 1.21.1 uses body + body_format ("plain"|"html") instead of a
    # separate html_body; keep html_body for back-compat. body_format is an enum,
    # not a recipient.
    "body_format",
    "thread_id",
    "message_id",
    "attachments",
    # Threading metadata — Message-IDs, not addresses. Cannot carry recipients.
    "in_reply_to",
    "references",
    # Plain booleans (1.21.1) — signature inclusion / original-message quoting.
    # Not recipients.
    "include_signature",
    "quote_original",
    # Gmail "Send As" display name — sets the From header's display name,
    # NOT a recipient and NOT the From address (that stays the authenticated
    # user_google_email). Validated for CR/LF below to block header injection.
    "from_name",
    # Gmail "Send As" alias address (1.21.1). Sets the From mailbox to a verified
    # alias of the authenticated user (Gmail rejects unverified aliases, so it
    # can't spoof arbitrary senders) — NOT a recipient, so not roster-checked, but
    # it lands in the From header so it's CR/LF-validated below like from_name.
    "from_email",
})


def _block(field: str, addr: str, tool: str, *, reason: str | None = None) -> str:
    from openexecutive.audit import log_event as audit_log

    logger.warning(
        "blocked outbound gmail send: tool=%s field=%s addr=%s not in allow-list",
        tool, field, addr,
    )
    audit_log(
        "integration_outbound_blocked",
        f"Blocked outbound email to {addr} (tool={tool} field={field})",
        actor="mcp_gateway",
        details={"tool": tool, "field": field, "address": addr},
    )
    # Default message describes a disallowed recipient. Callers pass an
    # explicit `reason` for non-recipient rejections (forbidden arg key,
    # malformed from_name) so the error doesn't misdescribe the cause.
    return json.dumps({
        "error": reason or (
            f"recipient {addr!r} in field {field!r} is not on "
            "EMAIL_ALLOWED_SENDERS — refusing to send. Reply only to "
            "allow-listed senders."
        ),
    })


def _roster_allow_set() -> set[str]:
    """The set of lowercased addresses the Executive may reach outbound.

    Derived from the People roster plus the Executive's own address — the single
    egress allow-list shared by the Gmail, Calendar, and Drive gates so they
    can't drift apart. Reads the live roster on each call (channel access is
    roster-driven and changes at runtime).
    """
    from openexecutive.people.store import list_people

    settings = get_settings()
    allow = {p.email.lower() for p in list_people() if p.email}
    allow.add(settings.exec_email_address.lower())
    return allow


def _check_gmail_recipients(tool: str, arguments: dict[str, Any]) -> str | None:
    """Return None if all recipients are allow-listed, else a JSON error string.

    Prompt injection in inbound mail can steer the Executive into emailing
    arbitrary addresses. The inbound sender allow-list (EMAIL_ALLOWED_SENDERS)
    is mirrored on the outbound side here: every `to`/`cc`/`bcc` must resolve
    to an address on that list (or the Executive's own address, to preserve
    the alert dispatcher self-send path).
    """
    # Reject any argument key not on the allow-list. This is the smuggling-
    # vector mitigation: an unknown key could carry hidden recipients (custom
    # headers, raw MIME blob, multipart parts, additional_headers, etc.).
    for key in arguments:
        if key not in _GMAIL_ALLOWED_ARG_KEYS:
            return _block(
                key, "<forbidden-arg>", tool,
                reason=(
                    f"argument {key!r} is not permitted on {tool} — refusing "
                    "to send. Only a fixed set of recipient/body/threading "
                    "fields is allowed."
                ),
            )

    # from_name sets the From header's display name (Gmail "Send As"). It is
    # not a recipient and does not change the From mailbox (that stays the
    # authenticated user_google_email), but it lands verbatim in a mail header
    # and — unlike to/cc/bcc — is never parsed by getaddresses. A display name
    # has no legitimate use for any control character, so reject the whole C0
    # range (a stricter superset of the CR/LF check applied to recipients):
    # this closes the header-injection vector (e.g. "Exec\nBcc: evil@x.com")
    # without depending on a lenient downstream mailer to normalize it.
    # from_name (display name) and from_email (verified Send-As alias) both land
    # verbatim in the From header and are never parsed by getaddresses. Neither is
    # a recipient, so neither is roster-checked — but a control character in
    # either is a header-injection vector (e.g. "Exec\nBcc: evil@x.com"), so
    # reject the whole C0 range. (Gmail independently rejects an unverified
    # from_email alias, so it can't spoof an arbitrary sender.)
    for header_field in ("from_name", "from_email"):
        value = arguments.get(header_field)
        if value is None:
            continue
        if not isinstance(value, str):
            return _block(
                header_field, f"<non-string:{type(value).__name__}>", tool,
                reason=f"{header_field} must be a string — refusing to send.",
            )
        if any(ord(ch) < 0x20 for ch in value):
            return _block(
                header_field, "<contains-control-char>", tool,
                reason=(
                    f"{header_field} contains a control character "
                    "(header-injection risk) — refusing to send."
                ),
            )

    # Egress gate: the Executive may only send mail to addresses on the
    # People roster or to its own exec address. Used to be a static env
    # allowlist; now derived from the People table so it stays in sync
    # with channel access.
    allow = _roster_allow_set()

    for field in _GMAIL_RECIPIENT_FIELDS:
        value = arguments.get(field)
        if not value:
            continue
        # Normalize to a list of strings; anything else is suspicious.
        items = value if isinstance(value, list) else [value]
        for item in items:
            if not isinstance(item, str):
                return _block(field, f"<non-string:{type(item).__name__}>", tool)
            # Embedded CR/LF in a single field could smuggle additional headers
            # past lenient mail servers (header-injection style).
            if "\n" in item or "\r" in item:
                return _block(field, "<contains-newline>", tool)
        parsed = getaddresses([s for s in items if isinstance(s, str)])
        # Reject malformed input: if parsing yielded no addresses (or any
        # empty-addr tuple) while the input was non-empty, the downstream
        # mailer may interpret it differently — fail closed.
        if not parsed or any(not addr for _name, addr in parsed):
            return _block(field, "<unparseable>", tool)
        for _name, addr in parsed:
            if addr.lower() not in allow:
                return _block(field, addr, tool)
    return None


def _check_calendar_attendees(tool: str, arguments: dict[str, Any]) -> str | None:
    """Return None if all calendar attendees are on the People roster, else a JSON error.

    The typed `create_calendar_event` tool resolves person IDs to emails before
    calling manage_event, so this backstop should almost never fire in normal
    operation.  It exists to make the gate bypass-proof: even a raw
    `call_tool("google_workspace__manage_event", ...)` can't invite a
    non-roster attendee.

    For delete/rsvp actions there are no attendees to check, so those pass
    through immediately (no invitees to validate).  The `action` key is
    required by manage_event and validated by the typed tool; its absence in
    a raw call is handled below by the attendees check path.
    """
    action = arguments.get("action", "")
    if action in ("delete", "rsvp"):
        return None

    attendees = arguments.get("attendees")
    # None = no attendees field at all → pass through (e.g. organizer-only event).
    # Empty list [] = explicitly supplied with no names → also pass through;
    # the typed create_calendar_event tool always supplies at least one attendee,
    # and a raw call with [] creates an organizer-only event (no roster leak).
    # Any non-empty list → every address must be roster-validated.
    if attendees is None:
        return None
    if isinstance(attendees, list) and len(attendees) == 0:
        return None

    allow = _roster_allow_set()

    # attendees may be a list of strings (emails) or dicts with an "email" key.
    items = attendees if isinstance(attendees, list) else [attendees]
    for item in items:
        if isinstance(item, dict):
            email = item.get("email", "")
        elif isinstance(item, str):
            email = item
        else:
            return _block("attendees", f"<non-string:{type(item).__name__}>", tool)
        if not isinstance(email, str) or not email:
            return _block("attendees", "<empty-email>", tool)
        if "\n" in email or "\r" in email:
            return _block("attendees", "<contains-newline>", tool)
        if email.lower() not in allow:
            return _block("attendees", email, tool,
                          reason=f"attendee {email!r} is not on the People roster — "
                                 "refusing to create calendar event.")
    return None


def _iter_arg_strings(value: Any) -> Iterator[str]:
    """Yield every string anywhere in a (possibly nested) argument value.

    Drive-share tool schemas vary across workspace-mcp versions and a grantee
    email can be a top-level string, a list entry, nested in a permission dict,
    or even a dict *key* (e.g. an email-keyed permission map). Walking every
    string in both key and value position — rather than trusting a fixed set of
    field names — keeps the gate fail-closed against an email smuggled through an
    unexpected shape.
    """
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            if isinstance(k, str):
                yield k
            yield from _iter_arg_strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _iter_arg_strings(v)


def _is_truthy_public(value: Any) -> bool:
    """Whether a value under a public-share key actually enables exposure.

    Works for any type so a boolean/int flag can't bypass the scope check:
    False / 0 / None / an explicitly-negative string ("private", "false", …)
    mean "off" and are safe; anything else (True, a non-zero number, "anyone",
    a non-empty container) is treated as enabling public/link/domain access.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() not in _NEGATIVE_FLAG_VALUES
    if isinstance(value, (dict, list, tuple)):
        return len(value) > 0
    return value is not None


def _has_public_share_flag(value: Any) -> bool:
    """True if any key anywhere names a public/domain/link-share control whose
    value turns it on. Complements the string-scope scan by catching the
    typed-flag form (e.g. {"public": true}) the string scan cannot see.
    """
    if isinstance(value, dict):
        for k, v in value.items():
            if (
                isinstance(k, str)
                and _norm_share_token(k) in _PUBLIC_SHARE_KEYS
                and _is_truthy_public(v)
            ):
                return True
            if _has_public_share_flag(v):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_has_public_share_flag(v) for v in value)
    return False


def _check_drive_share(tool: str, arguments: dict[str, Any]) -> str | None:
    """Return None if a Drive share/permission call only grants access to roster
    members, else a JSON error string.

    Two failure modes are refused:
    - **Public / whole-domain sharing** — a `type`/`scope` argument naming a
      population (`anyone`, `anyone_with_link`, `domain`, …) bypasses the
      per-recipient roster model, so it is blocked outright.
    - **Off-roster grantee** — any email-like token found in the arguments must
      resolve to the People roster (or the Executive's own address).

    This mirrors the Gmail/Calendar gates: prompt injection in an inbound doc or
    message could otherwise steer the Executive into sharing a file with an
    arbitrary external address.

    Deliberately fail-closed (same stance as the Gmail gate, which rejects any
    unknown argument key): because grantee field names vary across workspace-mcp
    versions, the email scan looks at EVERY string rather than a fixed set of
    fields. The tradeoff is that an off-roster address appearing in a non-grantee
    free-text field (e.g. a notification message body) is also blocked. That is
    accepted: a backstop that occasionally over-refuses a share is safer than one
    that lets a grantee slip through an unrecognized field, and the Executive can
    re-issue the share without the incidental mention.
    """
    allow = _roster_allow_set()

    # Typed-flag form first: a boolean/int "make public" flag carries no string
    # for the scan below to catch, so check sharing-scope keys against a
    # type-agnostic truthiness test.
    if _has_public_share_flag(arguments):
        return _block(
            "scope", "<public-share-flag>", tool,
            reason=(
                "public/whole-domain Drive sharing is not allowed — share only "
                "with People on the roster."
            ),
        )

    for s in _iter_arg_strings(arguments):
        if _norm_share_token(s) in _PUBLIC_SHARE_SCOPES:
            return _block(
                "scope", s.strip(), tool,
                reason=(
                    f"public/whole-domain Drive sharing ({s.strip()!r}) is not "
                    "allowed — share only with People on the roster."
                ),
            )
        for match in _EMAIL_RE.findall(s):
            if match.lower() not in allow:
                return _block(
                    "share", match, tool,
                    reason=(
                        f"Drive share recipient {match!r} is not on the People "
                        "roster — refusing to grant access."
                    ),
                )
    return None


def _is_drive_share_tool(tool_name: str) -> bool:
    """True if a tool grants/modifies Drive access and must pass the share gate.

    The explicit `_GATED_DRIVE_TOOLS` set is the source of truth; the
    name-pattern fallback is defense-in-depth against workspace-mcp renaming or
    adding an access-granting tool — it never matches a pure read (those carry
    no grantee to leak), so an over-match is harmless (the scan finds no
    off-roster email and passes through).
    """
    if tool_name in _GATED_DRIVE_TOOLS:
        return True
    if not tool_name.startswith(_GW_PREFIX):
        return False
    bare = tool_name[len(_GW_PREFIX):]
    if bare.startswith(("get_", "list_", "search_", "read_", "download_", "check_")):
        return False
    return "drive_access" in bare or "permission" in bare or ("drive" in bare and "share" in bare)


# Recipient fields whose addresses get an outbound-context linkage. `to`/`cc`
# only — a bcc'd person replying is an unusual path, and recording their address
# would leak that they were bcc'd into a linkage row keyed by it.
_OUTBOUND_CONTEXT_RECIPIENT_FIELDS = ("to", "cc")


def _is_error_payload(result_text: str) -> bool:
    """True if a tool result is a JSON object carrying an ``error`` key.

    Used to distinguish a real send from a soft-failure the tool reports in-band
    (no exception raised) so we don't record a linkage for mail that never left.
    """
    try:
        parsed = json.loads(result_text)
    except (ValueError, TypeError):
        return False
    return isinstance(parsed, dict) and "error" in parsed


def _record_email_outbound_context(arguments: dict[str, Any]) -> None:
    """Persist an outbound→inbound linkage for a just-sent email, so a reply
    can be hydrated with the originating conversation's context — the email
    analogue of the DM send handlers in ``schedule_tools``.

    Records one open linkage per ``to``/``cc`` recipient (keyed by bare
    lowercased address), skipping the Executive's own address. Reuses
    ``_record_outbound_context``, which itself only writes when a live session
    is active (``current_session`` set) — so a reply-poller-originated send,
    which has no originating conversation, correctly creates no linkage.

    Best-effort: any failure here must never turn a successful send into an
    error, so the whole body is guarded.
    """
    try:
        # Prefer the plain-text body; fall back to html_body only when body is
        # missing or blank. A plain `body or html_body` would pick a
        # whitespace-only body (truthy) and wrongly discard real html_body text.
        body = arguments.get("body")
        if not (isinstance(body, str) and body.strip()):
            body = arguments.get("html_body")
        if not (isinstance(body, str) and body.strip()):
            return

        from openexecutive.orchestrator.schedule_tools import (
            _record_outbound_context,
        )

        self_addr = get_settings().exec_email_address.lower()
        seen: set[str] = set()
        for field in _OUTBOUND_CONTEXT_RECIPIENT_FIELDS:
            value = arguments.get(field)
            if not value:
                continue
            items = value if isinstance(value, list) else [value]
            for _name, addr in getaddresses([s for s in items if isinstance(s, str)]):
                norm = addr.strip().lower()
                if not norm or norm == self_addr or norm in seen:
                    continue
                seen.add(norm)
                _record_outbound_context(
                    channel="email",
                    channel_ref=norm,
                    text=body,
                    outbound_message_id=None,
                )
    except Exception:
        logger.exception(
            "record_email_outbound_context: persist failed (non-fatal)"
        )


class MCPGateway:
    """Proxies search_tools / call_tool / load_mcp_server to an extensible-mcp subprocess.

    Lifecycle: call start() at app startup, close() at shutdown.
    The subprocess persists for the lifetime of the server process.

    Config: copy mcp_servers.json.example → company/mcp_servers.json and edit.
    The filters.access_control section controls which tools the model can discover
    and call; filters.load_control governs load_mcp_server URL allowlisting.
    """

    def __init__(self) -> None:
        self._session: Any = None
        self._stdio_cm: Any = None

    async def start(self, config_path: Path) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        forwarded_env = {k: os.environ[k] for k in _FORWARDED_ENV_VARS if k in os.environ}
        params = StdioServerParameters(
            command=_UVX_CMD,
            args=["--from", _EXTENSIBLE_MCP_GIT, _EXTENSIBLE_MCP_CMD, "--config", str(config_path)],
            env=forwarded_env or None,
        )
        self._stdio_cm = stdio_client(params)
        read, write = await self._stdio_cm.__aenter__()
        self._session = ClientSession(read, write)
        await self._session.__aenter__()
        await self._session.initialize()
        logger.info("MCPGateway started — config=%s", config_path)

    async def close(self) -> None:
        if self._session is not None:
            with contextlib.suppress(Exception):
                await self._session.__aexit__(None, None, None)
        if self._stdio_cm is not None:
            with contextlib.suppress(Exception):
                await self._stdio_cm.__aexit__(None, None, None)
        self._session = None
        self._stdio_cm = None

    def _require_session(self) -> Any:
        if self._session is None:
            raise RuntimeError("MCPGateway.start() must be called before using the gateway")
        return self._session

    async def search_tools(self, tool_input: dict[str, Any]) -> str:
        session = self._require_session()
        result = await session.call_tool("search_tools", {"query": tool_input["query"]})
        return result.content[0].text if result.content else json.dumps({"tools": []})

    async def call_tool(self, tool_input: dict[str, Any]) -> str:
        session = self._require_session()
        arguments = tool_input.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                logger.warning("call_tool: arguments was a string but not valid JSON — using empty dict")
                arguments = {}
        tool_name = tool_input.get("name", "")
        if tool_name in _GATED_GMAIL_TOOLS:
            blocked = _check_gmail_recipients(tool_name, arguments)
            if blocked is not None:
                return blocked
        if tool_name in _GATED_CALENDAR_TOOLS:
            blocked = _check_calendar_attendees(tool_name, arguments)
            if blocked is not None:
                return blocked
        if _is_drive_share_tool(tool_name):
            blocked = _check_drive_share(tool_name, arguments)
            if blocked is not None:
                return blocked
        # Grimdall execution guardrails (optional, default off). Enforce mode
        # returns an error payload before the subprocess call; shadow mode logs
        # a signed receipt and proceeds. The executive dispatch boundary also
        # evaluates call_tool envelopes, so chat-path calls are double-checked —
        # this hook additionally covers direct callers (scheduler, workflows).
        if is_enabled():
            decision = guard_tool_call(
                tool_name, arguments, session_id=None, turn_id="", actor="mcp_gateway"
            )
            if not decision.allowed:
                return decision.error_result()
        result = await session.call_tool(
            "call_tool",
            {"tool_name": tool_input["name"], "arguments": arguments},
        )
        result_text = result.content[0].text if result.content else json.dumps({"result": None})
        # Record an outbound-context linkage only for a genuinely-sent email.
        # The send tool returns its outcome as text; a soft-error payload
        # (`{"error": ...}`) means nothing was sent, so skip it to avoid a
        # phantom linkage that would hydrate a reply that can never come.
        if tool_name == "google_workspace__send_gmail_message" and not _is_error_payload(result_text):
            _record_email_outbound_context(arguments)
        return result_text

    async def load_mcp_server(self, tool_input: dict[str, Any]) -> str:
        session = self._require_session()
        url: str = tool_input["url"]
        if not url.startswith("https://"):
            return json.dumps({"error": "load_mcp_server requires an HTTPS URL"})
        result = await session.call_tool(
            "load_mcp_server",
            {"name": tool_input["name"], "url": url},
        )
        return result.content[0].text if result.content else json.dumps({"ok": True})


MCP_TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_tools",
        "description": (
            "Search the MCP tool catalog by natural language query. "
            "Returns ranked tool names and descriptions. "
            "Call this first to discover what external tools are available before calling them."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language description of the capability you need",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "call_tool",
        "description": (
            "Invoke a specific external tool by name with arguments. "
            "Use search_tools first to find the correct tool name."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Exact tool name from search_tools results",
                },
                "arguments": {
                    "type": "object",
                    "description": "Tool arguments as key-value pairs",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "load_mcp_server",
        "description": (
            "Connect a new MCP server at runtime by HTTPS URL. "
            "Its tools become immediately searchable and callable."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Short label for the server",
                },
                "url": {
                    "type": "string",
                    "description": "HTTPS URL of the MCP server",
                },
            },
            "required": ["name", "url"],
        },
    },
]

MCP_TOOL_NAMES: frozenset[str] = frozenset(t["name"] for t in MCP_TOOLS)

# Module-level singleton so dispatcher and other non-request code can reach the
# gateway without threading it through every call chain. Set during app lifespan.
_active_gateway: MCPGateway | None = None


def set_active_gateway(gateway: MCPGateway | None) -> None:
    global _active_gateway
    _active_gateway = gateway


def get_active_gateway() -> MCPGateway | None:
    return _active_gateway
