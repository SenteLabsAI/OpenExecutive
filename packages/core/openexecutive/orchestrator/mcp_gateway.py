from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import re
from collections.abc import Callable, Iterable, Iterator
from email.utils import getaddresses
from pathlib import Path
from typing import Any

from openexecutive.config import get_settings, mcp_config_file_present

logger = logging.getLogger(__name__)

_UVX_CMD = "uvx"
# Pinned to a commit, not the default branch. Unpinned, uvx resolved `main` on
# GitHub at every container start, so a published image ran whatever the
# gateway repo held that day: a push there changed every deployment on its next
# restart, rolling back an image tag did not roll the gateway back, and a start
# with no network failed outright because only GitHub can say what `main` is.
# The repo has no release tags, so a commit is the only stable ref.
#
# The commit alone fixes extensible-mcp's own code, not its dependencies: uvx
# re-resolves its ~94 transitive packages against PyPI's version ranges on
# every networked start, so a new release of any of them would still reach
# running deployments on restart. `--exclude-newer` freezes that resolution to
# packages published before the cutoff, which also makes the Dockerfile's
# pre-warm cache exactly what the runtime resolves — so a start needs no
# network when that best-effort pre-warm succeeded. Bump the commit and the
# cutoff together, deliberately; tests/unit/
# test_extensible_mcp_pin.py fails if docker/Dockerfile's pre-warm drifts from
# _EXTENSIBLE_MCP_LAUNCH_ARGS.
_EXTENSIBLE_MCP_REV = "ac2001a09646a8044210042e12e62974f4c9687c"
_EXTENSIBLE_MCP_EXCLUDE_NEWER = "2026-09-22T00:00:00Z"
_EXTENSIBLE_MCP_GIT = f"git+https://github.com/SenteLabsAI/extensible-mcp@{_EXTENSIBLE_MCP_REV}"
_EXTENSIBLE_MCP_CMD = "extensible-mcp"
# Everything after `uvx` up to the command, shared with docker/Dockerfile's pre-warm.
_EXTENSIBLE_MCP_LAUNCH_ARGS = (
    "--exclude-newer",
    _EXTENSIBLE_MCP_EXCLUDE_NEWER,
    "--from",
    _EXTENSIBLE_MCP_GIT,
    _EXTENSIBLE_MCP_CMD,
)

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
    # Microsoft 365 (ms-365-mcp-server) child — consumed by the `env` block of
    # the microsoft_365 entry and by docker/ms365-mcp-launch.sh. Same
    # contract as the Google vars above: an entry here is what turns a
    # deployment secret into something the child can see.
    "MS365_MCP_CLIENT_ID",
    "MS365_MCP_TENANT_ID",
    "MS365_MCP_CLIENT_SECRET",
    "MS365_MCP_EXPECTED_USERNAME",
    "MS365_MCP_ORG_MODE",
    "MS365_MCP_OAUTH_TOKEN",
    "MS365_MCP_CREDENTIALS_DIR",
    "MS365_MCP_TOKEN_CACHE_PATH",
    "MS365_MCP_SELECTED_ACCOUNT_PATH",
    "MS365_MCP_USE_KEYTAR",
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

# Namespace prefix of the Microsoft 365 server (ms-365-mcp-server). Its tool
# names are hyphenated Graph endpoint aliases (`send-mail`,
# `create-calendar-event`); extensible-mcp proxies them verbatim as
# `microsoft_365__send-mail`.
_M365_PREFIX = "microsoft_365__"


def _normalize_tool_name(name: str) -> str:
    """Canonical form for gate lookups: lowercase, ``-`` and ``_`` collapsed.

    The M365 gate sets below are stored in underscore form and matched against
    this, so `microsoft_365__send-mail` and `microsoft_365__send_mail` (should a
    proxy layer ever rewrite hyphens) are gated identically. Google's gates keep
    their exact-name matching — nothing there is hyphenated.
    """
    return name.strip().lower().replace("-", "_")


# Microsoft 365 write tools whose arguments can carry a recipient — the whole
# send / reply / forward / draft surface, in normalized (underscore) form.
# Names track @softeria/ms-365-mcp-server 0.154.2 `dist/endpoints.json`
# (the README drifts; endpoints.json is authoritative). Drafts and
# `update_mail_message` are gated too: a draft carries recipients that
# `send_draft_message` later sends, and an update can PATCH `toRecipients` onto
# it. Re-verify on any ms-365-mcp-server bump (docker/Dockerfile pin).
_GATED_M365_MAIL_TOOLS = frozenset({
    "microsoft_365__send_mail",
    "microsoft_365__reply_mail_message",
    "microsoft_365__reply_all_mail_message",
    "microsoft_365__forward_mail_message",
    "microsoft_365__create_draft_email",
    "microsoft_365__create_reply_draft",
    "microsoft_365__create_reply_all_draft",
    "microsoft_365__create_forward_draft",
    "microsoft_365__update_mail_message",
    "microsoft_365__send_draft_message",
    # Not in the launcher's default allow-list (docker/ms365-mcp-launch.sh) —
    # gated anyway so an operator who widens the list still gets the roster
    # check: inbox rules can forwardTo/redirectTo any address, mailbox settings
    # carry an external auto-reply.
    "microsoft_365__create_mail_rule",
    "microsoft_365__update_mail_rule",
    "microsoft_365__update_mailbox_settings",
})

# The subset of the mail writes above whose recipients are NOT in the
# arguments at all: Graph's reply / reply-all / send-draft actions address
# whoever the referenced message (`messageId`) names. The argument walk in
# `_check_m365_recipients` sees nothing to check for them, so a reply to an
# unrostered sender would sail through — the gate must read the referenced
# message from the server first (`_check_m365_referenced_message`) and
# validate the recipients Graph will derive from it. Fail-closed: a lookup
# that fails, errors, or returns no addressable recipient refuses the call.
_M365_REPLY_BY_ID_TOOLS = frozenset({
    "microsoft_365__reply_mail_message",
    "microsoft_365__reply_all_mail_message",
    "microsoft_365__create_reply_draft",
    "microsoft_365__create_reply_all_draft",
    "microsoft_365__send_draft_message",
})
_M365_REPLY_ALL_TOOLS = frozenset({
    "microsoft_365__reply_all_mail_message",
    "microsoft_365__create_reply_all_draft",
})
_M365_SEND_DRAFT_TOOL = "microsoft_365__send_draft_message"
_M365_MESSAGE_LOOKUP_TOOL = "microsoft_365__get-mail-message"
_M365_MESSAGE_LOOKUP_SELECT = "id,from,replyTo,toRecipients,ccRecipients,bccRecipients"

# Calendar actions whose free-text `comment` Exchange EMAILS to people the
# arguments never name: an RSVP (accept / decline / tentatively-accept with
# sendResponse) goes to the event's ORGANIZER, a cancel goes to every
# ATTENDEE. Like the reply family, the gate reads the event first
# (`_check_m365_referenced_event` → get-calendar-event) and roster-checks the
# implied recipients; fail-closed. `delete-calendar-event` carries no free
# text and stays ungated (it is the typed cancel_calendar_event path).
_M365_EVENT_RESPONSE_TOOLS = frozenset({
    "microsoft_365__accept_calendar_event",
    "microsoft_365__decline_calendar_event",
    "microsoft_365__tentatively_accept_calendar_event",
})
_M365_EVENT_CANCEL_TOOL = "microsoft_365__cancel_calendar_event"
_M365_EVENT_BY_ID_TOOLS = _M365_EVENT_RESPONSE_TOOLS | {_M365_EVENT_CANCEL_TOOL}
_M365_EVENT_LOOKUP_TOOL = "microsoft_365__get-calendar-event"
_M365_EVENT_LOOKUP_SELECT = "id,organizer,attendees"

# `download-bytes` is a generic authenticated Graph GET proxy (any path under
# the token's scopes), which would let the read side of the launcher's
# allow-list be bypassed (`/me/messages/{id}/$value` MIME, calendar
# permissions, …). The Executive only needs it for mail attachment bytes, so
# the gateway pins its `target` to exactly that shape.
_M365_DOWNLOAD_TOOL = "microsoft_365__download_bytes"
_M365_ATTACHMENT_TARGET_RE = re.compile(r"^/me/messages/[^/?#]+/attachments/[^/?#]+/\$value$")

# Microsoft 365 calendar mutations that carry an `attendees` / `ToRecipients` /
# `emailAddress` list in their ARGUMENTS. Delete and the read tools carry no
# invitee and pass through; RSVP / cancel are gated separately by an event
# lookup (`_M365_EVENT_BY_ID_TOOLS`) because their comment is emailed to
# people the arguments never name. The "specific calendar", forward and
# permission-sharing tools are outside the launcher's default allow-list but
# gated here too (see the mail set above for why).
_GATED_M365_CALENDAR_TOOLS = frozenset({
    "microsoft_365__create_calendar_event",
    "microsoft_365__update_calendar_event",
    "microsoft_365__create_specific_calendar_event",
    "microsoft_365__update_specific_calendar_event",
    "microsoft_365__forward_calendar_event",
    "microsoft_365__create_my_calendar_permission",
    "microsoft_365__update_my_calendar_permission",
})

# Argument keys (lowercased) whose values are free text the gate does NOT scan
# for addresses: a reply that quotes a signature, or a body that mentions a
# vendor's address, must not be refused as if it were addressed to them. Safe
# only because the server's send/reply/event shapes are JSON objects — a body
# string cannot address anyone; every recipient-carrying field
# (`toRecipients[].emailAddress.address`, `attendees[]…`, `replyTo`, custom
# headers, anything unforeseen) is outside this set and stays fail-closed.
# `body` itself is NOT here: it is also the name of the request-body wrapper
# (`{"body": {"Message": …}}`), so exempting it would exempt everything.
_M365_FREE_TEXT_KEYS = frozenset({"content", "subject", "comment", "bodypreview"})
# OData annotation KEYS (`@odata.type` on a Graph fileAttachment, `@odata.id`,
# …) carry a literal `@` that is not an address. The key itself is skipped by
# the malformed-address check; its VALUE is still scanned like any other.
_ODATA_ANNOTATION_KEY_RE = re.compile(r"^@odata\.[A-Za-z]+$")

# The one M365 send whose arguments name the recipients explicitly, so an
# outbound-context linkage can be recorded after it succeeds. Reply/forward
# tools identify the recipient by message id only — nothing to key a linkage on.
_M365_RECORD_SEND_TOOL = "microsoft_365__send_mail"

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


# Artifact attachments on one email. Gmail caps a message at 25 MB and base64
# inflates the payload by a third, so the rendered artifacts together stay
# well under it; the count cap bounds how much one tool call can make the
# server render (each entry is a full docx / xlsx build).
_MAX_ARTIFACT_ATTACHMENTS = 5
_MAX_ARTIFACT_ATTACHMENT_BYTES = 15 * 1024 * 1024
_ARTIFACT_ATTACHMENT_KEYS = frozenset({"artifact_id", "as"})


# Where Graph expects a message's `attachments` for each Microsoft 365 mail
# write, as a key path (matched case-insensitively; created with this casing
# when absent). The send / reply / forward ACTIONS and the createReply* draft
# actions wrap the message in a `Message` parameter; `create-draft-email`
# POSTs the message itself. `update-mail-message` (PATCH) cannot add
# attachments and `send-draft-message` takes no body, so an artifact entry on
# either is refused rather than passed through unexpanded.
_M365_ARTIFACT_MESSAGE_PATHS: dict[str, tuple[str, ...]] = {
    "microsoft_365__send_mail": ("body", "Message"),
    "microsoft_365__reply_mail_message": ("body", "Message"),
    "microsoft_365__reply_all_mail_message": ("body", "Message"),
    "microsoft_365__forward_mail_message": ("body", "Message"),
    "microsoft_365__create_reply_draft": ("body", "Message"),
    "microsoft_365__create_reply_all_draft": ("body", "Message"),
    "microsoft_365__create_forward_draft": ("body", "Message"),
    "microsoft_365__create_draft_email": ("body",),
}
_GRAPH_FILE_ATTACHMENT_TYPE = "#microsoft.graph.fileAttachment"


def _recipients(arguments: dict[str, Any]) -> list[str]:
    """Every to/cc/bcc address on a Gmail call, for audit rows."""
    raw = [arguments.get(f) for f in _GMAIL_RECIPIENT_FIELDS]
    values = [
        str(v) for item in raw
        for v in (item if isinstance(item, list) else [item]) if v
    ]
    return [addr for _, addr in getaddresses(values) if addr]


def _audit_recipients(tool: str, arguments: dict[str, Any]) -> list[str]:
    """The addresses an attachment audit row names: Gmail's flat to/cc/bcc,
    or the Graph message's recipient lists for a Microsoft 365 tool (a reply
    or forward names none in its arguments — the row then carries []).
    """
    normalized = _normalize_tool_name(tool)
    if not normalized.startswith(_M365_PREFIX):
        return _recipients(arguments)
    message = _ci_walk(arguments, _M365_ARTIFACT_MESSAGE_PATHS.get(normalized, ("body",)))
    out: list[str] = []
    for field in ("toRecipients", "ccRecipients", "bccRecipients"):
        out.extend(_m365_recipient_addresses(_ci_get(message, field)))
    return out


def _refuse_attachment(tool: str, arguments: dict[str, Any], reason: str) -> str:
    from openexecutive.audit import log_event as audit_log

    logger.warning("refused artifact attachment on %s: %s", tool, reason)
    audit_log(
        "artifact_attachment_refused",
        f"Refused an artifact attachment on {tool}: {reason[:160]}",
        actor="mcp_gateway",
        details={
            "tool": tool, "reason": reason, "recipients": _audit_recipients(tool, arguments),
        },
    )
    return json.dumps({"error": f"attachment: {reason}"})


def _normalize_attachment_list(attachments: Any) -> list[Any] | None | str:
    """The attachment entries as a list, None when there is nothing artifact-
    shaped to expand, or an error reason. Models sometimes stringify nested
    arguments, or send one object instead of a list; both are normalised so
    an artifact entry can never reach the MCP unexpanded (workspace-mcp would
    skip it and send the mail without it; Graph would reject the shape).
    """
    if isinstance(attachments, str) and "artifact_id" in attachments:
        try:
            attachments = json.loads(attachments)
        except json.JSONDecodeError:
            return "attachments is not valid JSON"
    if isinstance(attachments, dict):
        attachments = [attachments]
    if not isinstance(attachments, list) or not any(
        isinstance(a, dict) and "artifact_id" in a for a in attachments
    ):
        return None
    return attachments


def _gmail_file_entry(file: Any) -> dict[str, str]:
    """workspace-mcp's own attachment shape."""
    return {
        "content": base64.b64encode(file.content).decode("ascii"),
        "filename": file.filename,
        "mime_type": file.mime,
    }


def _graph_file_entry(file: Any) -> dict[str, str]:
    """Graph's `fileAttachment` shape, inline in the message."""
    return {
        "@odata.type": _GRAPH_FILE_ATTACHMENT_TYPE,
        "name": file.filename,
        "contentType": file.mime,
        "contentBytes": base64.b64encode(file.content).decode("ascii"),
    }


async def _expand_artifact_entries(
    tool: str,
    arguments: dict[str, Any],
    attachments: list[Any],
    to_entry: Callable[[Any], dict[str, str]],
) -> tuple[list[Any], list[str]] | str:
    """Replace `{"artifact_id": "alert:5", "as"?: "docx"}` entries in
    ``attachments`` with the rendered file in the backend's shape
    (``to_entry``), keeping every other entry as it is.

    Lets the Executive email one of its artifacts without ever holding the
    bytes: the file is rendered server-side exactly as `/artifacts/{id}/
    download` serves it, and only artifact rows can resolve (see
    `artifact_records`). At most `_MAX_ARTIFACT_ATTACHMENTS` distinct entries
    (repeats are dropped) and `_MAX_ARTIFACT_ATTACHMENT_BYTES` in total;
    rendering runs off the event loop. Returns the expanded list plus the
    attached artifact ids, or an audited JSON error string.
    """
    from openexecutive.orchestrator.artifact_records import (
        ArtifactNotFound,
        MalformedArtifactId,
        load_artifact,
        render_artifact_file,
    )

    wanted: list[tuple[str, str | None]] = []
    for entry in attachments:
        if not (isinstance(entry, dict) and "artifact_id" in entry):
            continue
        extra = set(entry) - _ARTIFACT_ATTACHMENT_KEYS
        if extra:
            return _refuse_attachment(tool, arguments, (
                "an artifact attachment takes only 'artifact_id' and an "
                f"optional 'as' format; got {sorted(extra)}"
            ))
        as_raw = entry.get("as")
        key = (str(entry.get("artifact_id") or "").strip(),
               str(as_raw).strip().lower() if as_raw else None)
        if key not in wanted:
            wanted.append(key)
    if len(wanted) > _MAX_ARTIFACT_ATTACHMENTS:
        return _refuse_attachment(tool, arguments, (
            f"at most {_MAX_ARTIFACT_ATTACHMENTS} artifacts per email; got {len(wanted)}"
        ))

    rendered: dict[tuple[str, str | None], dict[str, str]] = {}
    attached: list[str] = []
    total = 0
    for artifact_id, as_ in wanted:
        try:
            rec = await asyncio.to_thread(load_artifact, artifact_id)
            file = await asyncio.to_thread(render_artifact_file, rec, as_)
        except (MalformedArtifactId, ArtifactNotFound) as exc:
            return _refuse_attachment(tool, arguments, str(exc))
        except Exception:
            logger.exception("artifact attachment render failed: %s", artifact_id)
            return _refuse_attachment(tool, arguments, f"could not render {artifact_id!r}")
        total += len(file.content)
        if total > _MAX_ARTIFACT_ATTACHMENT_BYTES:
            return _refuse_attachment(tool, arguments, (
                f"artifacts total {total} bytes, over the "
                f"{_MAX_ARTIFACT_ATTACHMENT_BYTES}-byte email limit — send "
                "a link instead"
            ))
        rendered[(artifact_id, as_)] = to_entry(file)
        attached.append(rec.id)

    expanded: list[Any] = []
    emitted: set[tuple[str, str | None]] = set()
    for entry in attachments:
        if not (isinstance(entry, dict) and "artifact_id" in entry):
            expanded.append(entry)
            continue
        as_raw = entry.get("as")
        key = (str(entry.get("artifact_id") or "").strip(),
               str(as_raw).strip().lower() if as_raw else None)
        if key not in emitted:
            emitted.add(key)
            expanded.append(rendered[key])
    return expanded, attached


async def _expand_artifact_attachments(
    tool: str, arguments: dict[str, Any]
) -> tuple[dict[str, Any], list[str]] | str:
    """Gmail: expand artifact entries in the top-level ``attachments`` into
    workspace-mcp's `{content, filename, mime_type}` shape. Returns the
    (copied) arguments plus the attached artifact ids, or an audited JSON
    error string; arguments without an artifact entry come back untouched.
    """
    attachments = _normalize_attachment_list(arguments.get("attachments"))
    if attachments is None:
        return arguments, []
    if isinstance(attachments, str):
        return _refuse_attachment(tool, arguments, attachments)
    out = await _expand_artifact_entries(tool, arguments, attachments, _gmail_file_entry)
    if isinstance(out, str):
        return out
    expanded, attached = out
    return {**arguments, "attachments": expanded}, attached


def _ci_walk(mapping: Any, path: tuple[str, ...]) -> Any:
    """`_ci_get` along a key path; None as soon as a step is missing."""
    cursor = mapping
    for key in path:
        cursor = _ci_get(cursor, key)
        if cursor is None:
            return None
    return cursor


def _with_message_attachments(
    arguments: dict[str, Any], path: tuple[str, ...], expanded: list[Any]
) -> dict[str, Any]:
    """A copy of ``arguments`` with ``expanded`` as the message's
    `attachments` at ``path`` (existing key casing kept, the canonical casing
    used for a level that does not exist yet) and no top-level `attachments`.
    """
    result = dict(arguments)
    result.pop("attachments", None)
    cursor: dict[str, Any] = result
    for key in path:
        actual = next(
            (k for k in cursor if isinstance(k, str) and k.lower() == key.lower()), key
        )
        existing = cursor.get(actual)
        child: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
        cursor[actual] = child
        cursor = child
    actual = next((k for k in cursor if isinstance(k, str) and k.lower() == "attachments"),
                  "attachments")
    cursor[actual] = expanded
    return result


async def _expand_m365_artifact_attachments(
    tool: str, normalized: str, arguments: dict[str, Any]
) -> tuple[dict[str, Any], list[str]] | str:
    """Microsoft 365: expand artifact entries into Graph `fileAttachment`
    objects on the message.

    The artifact tool text tells the model to pass `attachments=[{"artifact_id"
    …}]` on "the mail send or draft tool", so the entries may arrive at the top
    level of the arguments (where Gmail takes them) or already inside the
    Graph message (`body.Message.attachments` / `body.attachments`, see
    `_M365_ARTIFACT_MESSAGE_PATHS`). Both are read, expanded together, and
    written to the Graph location — a top-level list is moved, never left for
    the server to reject. Tools whose request cannot carry attachments refuse
    an artifact entry instead of passing it through.
    """
    path = _M365_ARTIFACT_MESSAGE_PATHS.get(normalized)
    top = _normalize_attachment_list(arguments.get("attachments"))
    nested_raw = _ci_get(_ci_walk(arguments, path), "attachments") if path else None
    nested = _normalize_attachment_list(nested_raw)
    if isinstance(top, str):
        return _refuse_attachment(tool, arguments, top)
    if isinstance(nested, str):
        return _refuse_attachment(tool, arguments, nested)
    if top is None and nested is None:
        return arguments, []
    if path is None:
        return _refuse_attachment(tool, arguments, (
            f"{tool} cannot carry attachments; send with "
            f"{_M365_PREFIX}send-mail or create a draft with them instead"
        ))
    combined: list[Any] = []
    if nested is not None:
        combined.extend(nested)
    elif isinstance(nested_raw, list):
        combined.extend(nested_raw)
    if top is not None:
        combined.extend(top)
    elif isinstance(arguments.get("attachments"), list):
        combined.extend(arguments["attachments"])
    out = await _expand_artifact_entries(tool, arguments, combined, _graph_file_entry)
    if isinstance(out, str):
        return out
    expanded, attached = out
    return _with_message_attachments(arguments, path, expanded), attached


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


def _iter_arg_strings_skipping(value: Any, skip_keys: frozenset[str]) -> Iterator[str]:
    """`_iter_arg_strings`, except a dict entry whose lowercased key is in
    ``skip_keys`` yields the key and is not descended into.

    The M365 gate walks Graph's nested recipient shapes with this, exempting
    only the free-text fields in `_M365_FREE_TEXT_KEYS`. The Drive gate keeps
    the plain walker — its scan-everything stance is deliberate there.
    """
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            if isinstance(k, str):
                yield k
                if k.lower() in skip_keys:
                    continue
            yield from _iter_arg_strings_skipping(v, skip_keys)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _iter_arg_strings_skipping(v, skip_keys)


def _block_unless_rostered(addr: str, tool: str, allow: set[str]) -> str | None:
    """One address against the roster: refuse a control character (header
    injection) or an address outside ``allow``. Shared by both M365 gates so
    the two paths cannot drift."""
    if any(ord(ch) < 0x20 for ch in addr):
        return _block(
            "recipient", "<contains-control-char>", tool,
            reason=(
                f"a recipient of {tool} contains a control character "
                "(header-injection risk) — refusing."
            ),
        )
    if addr.strip().lower() not in allow:
        return _block(
            "recipient", addr, tool,
            reason=(
                f"Microsoft 365 recipient/attendee {addr!r} is not on the People "
                "roster — refusing. Only rostered people (and the Executive's own "
                "mailbox) may be addressed."
            ),
        )
    return None


def _check_m365_recipients(tool: str, arguments: dict[str, Any]) -> str | None:
    """Roster egress gate for Microsoft 365 mail and calendar writes.

    Graph nests addresses (`body.Message.toRecipients[].emailAddress.address`,
    `body.attendees[].emailAddress.address`, `replyTo`, `internetMessageHeaders`
    values…), and ms-365-mcp-server exposes those shapes as-is, so a fixed
    field allow-list in the style of `_check_gmail_recipients` would either
    miss a path or reject every call. Instead every string in the argument
    tree — keys and values, at any depth — except the free-text fields in
    `_M365_FREE_TEXT_KEYS` is scanned: any email-shaped token must be on the
    People roster (or the Executive's own address), and any C0 control
    character is refused (a display name or header value with an embedded
    newline is a header-injection attempt). Fail-closed: a recipient smuggled
    through an unforeseen key is still found by the walk.

    Returns None to allow, else the JSON error string from `_block` (which also
    writes the `integration_outbound_blocked` audit row).
    """
    allow = _roster_allow_set()
    for s in _iter_arg_strings_skipping(arguments, _M365_FREE_TEXT_KEYS):
        if _ODATA_ANNOTATION_KEY_RE.match(s):
            continue
        if any(ord(ch) < 0x20 for ch in s):
            return _block(
                "recipient", "<contains-control-char>", tool,
                reason=(
                    f"an argument of {tool} contains a control character "
                    "(header-injection risk) — refusing."
                ),
            )
        # `_EMAIL_RE` is ASCII-only, so an internationalized address
        # (`x@evïl.example`, `x@evil.срб`) would yield no match and slip past
        # the roster check while Graph still delivers it. Any non-exempt
        # string that looks like an address but is not pure ASCII is refused.
        if "@" in s and not s.isascii():
            return _block(
                "recipient", "<non-ascii-address>", tool,
                reason=(
                    f"an argument of {tool} contains a non-ASCII address — refusing "
                    "(the roster holds ASCII addresses only)."
                ),
            )
        matches = _EMAIL_RE.findall(s)
        # A quoted local part (`"rostered@x.com"@evil.com`) or a stray `@`
        # can hide an address the regex does not extract. Every `@` in a
        # non-exempt string must belong to exactly one extracted address.
        if "@" in s and ('"' in s or s.count("@") != len(matches)):
            return _block(
                "recipient", "<malformed-address>", tool,
                reason=(
                    f"an argument of {tool} contains an address the roster check "
                    "cannot parse unambiguously — refusing."
                ),
            )
        for match in matches:
            blocked = _block_unless_rostered(match, tool, allow)
            if blocked is not None:
                return blocked
    return None


def _m365_implied_recipients(normalized: str, message: dict[str, Any]) -> list[str]:
    """The addresses Graph will put on the wire for a by-id action on
    ``message``: the draft's own to/cc/bcc for send-draft; ``replyTo`` if set
    else ``from`` for a reply, plus the original to/cc for reply-all."""
    if normalized == _M365_SEND_DRAFT_TOOL:
        return [
            addr
            for key in ("toRecipients", "ccRecipients", "bccRecipients")
            for addr in _m365_recipient_addresses(_ci_get(message, key))
        ]
    reply_to = _m365_recipient_addresses(_ci_get(message, "replyTo"))
    implied = reply_to or _m365_recipient_addresses([_ci_get(message, "from")])
    if normalized in _M365_REPLY_ALL_TOOLS:
        for key in ("toRecipients", "ccRecipients"):
            implied += _m365_recipient_addresses(_ci_get(message, key))
    return implied


async def _fetch_m365_json(session: Any, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
    """One read through the MCP server for a gate lookup; ``None`` on any
    failure (transport error, error payload, non-object)."""
    try:
        result = await session.call_tool(
            "call_tool", {"tool_name": tool_name, "arguments": arguments},
        )
        text = result.content[0].text if result.content else ""
        parsed = json.loads(text) if isinstance(text, str) and text.strip() else None
    except Exception:
        logger.warning("m365 gate: lookup via %s failed", tool_name, exc_info=True)
        return None
    if not isinstance(parsed, dict) or "error" in parsed:
        return None
    return parsed


async def _check_m365_referenced_event(
    session: Any, tool: str, normalized: str, arguments: dict[str, Any],
) -> str | None:
    """Roster gate for RSVP / cancel: the `comment` is emailed to the event's
    organizer (RSVP) or every attendee (cancel), none of whom is in the
    arguments. Reads the event and checks those addresses; fail-closed."""
    event_id = arguments.get("eventId")
    if not isinstance(event_id, str) or not event_id.strip():
        return _block(
            "eventId", "<missing>", tool,
            reason=f"{tool} needs an eventId so its recipients can be roster-checked — refusing.",
        )
    event = await _fetch_m365_json(
        session, _M365_EVENT_LOOKUP_TOOL,
        {"eventId": event_id, "select": _M365_EVENT_LOOKUP_SELECT},
    )
    if event is None:
        return _block(
            "eventId", event_id, tool,
            reason=f"could not read event {event_id!r} to roster-check the recipients of {tool} — refusing.",
        )
    if normalized == _M365_EVENT_CANCEL_TOOL:
        implied = _m365_recipient_addresses(_ci_get(event, "attendees"))
    else:
        implied = _m365_recipient_addresses([_ci_get(event, "organizer")])
    if not implied:
        return _block(
            "eventId", event_id, tool,
            reason=f"event {event_id!r} names no recipient for {tool} to notify — refusing.",
        )
    allow = _roster_allow_set()
    for addr in implied:
        blocked = _block_unless_rostered(addr, tool, allow)
        if blocked is not None:
            return blocked
    return None


def _check_m365_download_target(tool: str, arguments: dict[str, Any]) -> str | None:
    """Pin `download-bytes` to mail attachment bytes only."""
    target = arguments.get("target")
    if not isinstance(target, str) or not _M365_ATTACHMENT_TARGET_RE.match(target):
        return _block(
            "target", str(target)[:120], tool,
            reason=(
                f"{tool} may only fetch a mail attachment "
                "(/me/messages/{id}/attachments/{id}/$value) — refusing."
            ),
        )
    return None


async def _check_m365_referenced_message(
    session: Any, tool: str, normalized: str, arguments: dict[str, Any],
) -> str | None:
    """Roster gate for M365 tools that address recipients via ``messageId``.

    Reads the referenced message through the MCP server (the source of truth
    for who Graph will address) and validates the recipients the action
    implies (`_m365_implied_recipients`). Every implied address must be on the
    roster (or be the Executive's own mailbox). Any lookup failure, or a
    message that implies no addressable recipient, refuses the call.
    """
    message_id = arguments.get("messageId")
    if not isinstance(message_id, str) or not message_id.strip():
        return _block(
            "messageId", "<missing>", tool,
            reason=f"{tool} needs a messageId so its recipients can be roster-checked — refusing.",
        )
    message = await _fetch_m365_json(
        session, _M365_MESSAGE_LOOKUP_TOOL,
        {"messageId": message_id, "select": _M365_MESSAGE_LOOKUP_SELECT},
    )
    if message is None:
        return _block(
            "messageId", message_id, tool,
            reason=(
                f"could not read message {message_id!r} to roster-check the recipients "
                f"of {tool} — refusing."
            ),
        )
    implied = _m365_implied_recipients(normalized, message)
    if not implied:
        return _block(
            "messageId", message_id, tool,
            reason=f"message {message_id!r} names no recipient for {tool} to address — refusing.",
        )
    allow = _roster_allow_set()
    for addr in implied:
        blocked = _block_unless_rostered(addr, tool, allow)
        if blocked is not None:
            return blocked
    return None


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

        addresses: list[tuple[str, bool]] = []
        for field in _OUTBOUND_CONTEXT_RECIPIENT_FIELDS:
            value = arguments.get(field)
            if not value:
                continue
            items = value if isinstance(value, list) else [value]
            addresses.extend(
                (addr, field == "to")
                for _name, addr in getaddresses([s for s in items if isinstance(s, str)])
            )
        _record_outbound_context_for(addresses, body)
    except Exception:
        logger.exception(
            "record_email_outbound_context: persist failed (non-fatal)"
        )


def _record_outbound_context_for(
    addresses: Iterable[tuple[str, bool]], body: str
) -> None:
    """Record one open ``email`` linkage per distinct recipient address.

    Shared by the Gmail and Microsoft 365 recorders: ``addresses`` pairs each
    address with whether it came from the primary (``to``) field. Normalizes to
    the bare lowercased address, skips the Executive's own mailbox and
    duplicates, and defers to `_record_outbound_context` (which itself only
    writes when a live session is active). Only the first rostered "to"
    address is who the email was addressed to (``record_outcome``); every
    recipient still gets reply linkage.
    """
    from openexecutive.orchestrator.schedule_tools import (
        _record_outbound_context,
        _resolve_recipient_person_id,
    )

    self_addr = get_settings().exec_email_address.lower()
    seen: set[str] = set()
    primary_taken = False
    for addr, is_to in addresses:
        norm = addr.strip().lower()
        if not norm or norm == self_addr or norm in seen:
            continue
        primary = (
            is_to and not primary_taken
            and _resolve_recipient_person_id("email", norm) is not None
        )
        primary_taken = primary_taken or primary
        seen.add(norm)
        _record_outbound_context(
            channel="email",
            channel_ref=norm,
            text=body,
            outbound_message_id=None,
            record_outcome=primary,
        )


def _ci_get(mapping: Any, key: str) -> Any:
    """Case-insensitive dict lookup (Graph action parameters arrive as
    ``Message``/``SaveToSentItems`` in the tool schema, ``message`` in the docs)."""
    if not isinstance(mapping, dict):
        return None
    for k, v in mapping.items():
        if isinstance(k, str) and k.lower() == key.lower():
            return v
    return None


def _m365_recipient_addresses(recipients: Any) -> list[str]:
    """Addresses from a Graph ``recipient[]`` list
    (``[{"emailAddress": {"address": …}}]``); tolerant of missing parts."""
    out: list[str] = []
    if not isinstance(recipients, list):
        return out
    for item in recipients:
        email_obj = _ci_get(item, "emailAddress")
        addr = _ci_get(email_obj, "address")
        if isinstance(addr, str):
            out.append(addr)
    return out


def _record_m365_outbound_context(arguments: dict[str, Any]) -> None:
    """The Microsoft 365 twin of `_record_email_outbound_context`, reading the
    nested Graph `sendMail` shape: ``body.Message.body.content`` for the text,
    ``body.Message.toRecipients`` + ``ccRecipients`` for the linkages (bcc
    skipped, same reasoning as `_OUTBOUND_CONTEXT_RECIPIENT_FIELDS`).

    Best-effort: a failure here never turns a successful send into an error.
    """
    try:
        message = _ci_get(_ci_get(arguments, "body"), "message")
        if message is None:
            return
        content = _ci_get(_ci_get(message, "body"), "content")
        if not (isinstance(content, str) and content.strip()):
            return
        addresses = [
            (addr, True) for addr in _m365_recipient_addresses(_ci_get(message, "toRecipients"))
        ] + [
            (addr, False) for addr in _m365_recipient_addresses(_ci_get(message, "ccRecipients"))
        ]
        _record_outbound_context_for(addresses, content)
    except Exception:
        logger.exception(
            "record_m365_outbound_context: persist failed (non-fatal)"
        )


# Ceiling on the MCP config we will read into memory. The real file is a
# handful of server entries; anything past this is a mistake or a symlink to
# something that is not a config, and reading it during startup is how you get
# the boot hang this function exists to prevent.
_MAX_CONFIG_BYTES = 1 << 20  # 1 MiB


def configured_server_names(config_path: Path) -> list[str]:
    """Names the MCP config at `config_path` defines under `mcpServers`.

    Empty when the file is absent, unreadable, oversized, not a JSON object, or
    defines no servers. Callers use this to decide whether starting the gateway
    can accomplish anything: extensible-mcp's own config loader ends with
    `ValueError("Config must define at least one server in 'mcpServers'")` and
    exits, and because the child is already gone by then the only thing that
    reaches us is anyio's "Attempted to exit cancel scope in a different task"
    from `stdio_client` unwinding — a traceback that names nothing about
    configuration (#122). Checking first is what makes the failure legible.

    That an empty `mcpServers` is fatal to the child rather than merely idle is
    confirmed in #122 by the maintainer reading extensible-mcp's own
    `load_config`, so skipping the gateway here removes no working
    configuration: a "servers are added at runtime via load_mcp_server" config
    never brought the gateway up either. It only makes the failure legible.

    NEVER RAISES, and the breadth of the `except` is deliberate: this reads an
    operator-owned path during startup, where an exception is the crash loop
    #122 was filed for rather than a bug report. Nothing here is subtle enough
    to be worth a narrower catch — `json.loads` answers deeply-nested input
    with `RecursionError`, which is not a `ValueError`, and a read can fail in
    as many ways as the filesystem has moods. `mcp_config_file_present` keeps a
    directory or a symlink to a FIFO from ever being opened.

    Every key under `mcpServers` counts as a server, `_comment` included. That
    is deliberate: a stray key there is a misconfiguration worth surfacing as a
    server name in the log, not one worth hiding.
    """
    if not mcp_config_file_present(config_path):
        return []
    try:
        # Bounded read rather than stat-then-read: a stat cannot bind what a
        # later read returns (a file that grows in between, or a /proc-style
        # file reporting size 0), and one byte past the ceiling is all it takes
        # to know we are over it.
        with config_path.open("rb") as fh:
            blob = fh.read(_MAX_CONFIG_BYTES + 1)
        if len(blob) > _MAX_CONFIG_BYTES:
            logger.warning(
                "MCP config %s is larger than the %d-byte ceiling; treating it "
                "as defining no servers",
                config_path, _MAX_CONFIG_BYTES,
            )
            return []
        raw = json.loads(blob)
    except Exception:
        logger.warning(
            "MCP config %s could not be read as JSON; treating it as defining "
            "no servers", config_path, exc_info=True,
        )
        return []
    servers = raw.get("mcpServers") if isinstance(raw, dict) else None
    if not isinstance(servers, dict):
        return []
    return sorted(str(name) for name in servers)


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
            args=[*_EXTENSIBLE_MCP_LAUNCH_ARGS, "--config", str(config_path)],
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
        args: dict[str, Any] = {"query": tool_input["query"]}
        # Optional: callers resolving an exact tool name widen the net
        # (extensible-mcp defaults to 5 results).
        if isinstance(tool_input.get("top_k"), int):
            args["top_k"] = tool_input["top_k"]
        result = await session.call_tool("search_tools", args)
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
        normalized = _normalize_tool_name(tool_name)
        attached_artifacts: list[str] = []
        if tool_name in _GATED_GMAIL_TOOLS:
            blocked = _check_gmail_recipients(tool_name, arguments)
            if blocked is not None:
                return blocked
            # Only after the recipients pass: render any artifact the model
            # asked to attach, so a blocked send never renders anything.
            expanded = await _expand_artifact_attachments(tool_name, arguments)
            if isinstance(expanded, str):
                return expanded
            arguments, attached_artifacts = expanded
        if tool_name in _GATED_CALENDAR_TOOLS:
            blocked = _check_calendar_attendees(tool_name, arguments)
            if blocked is not None:
                return blocked
        if _is_drive_share_tool(tool_name):
            blocked = _check_drive_share(tool_name, arguments)
            if blocked is not None:
                return blocked
        if normalized in _GATED_M365_MAIL_TOOLS or normalized in _GATED_M365_CALENDAR_TOOLS:
            blocked = _check_m365_recipients(tool_name, arguments)
            if blocked is not None:
                return blocked
        if normalized in _M365_REPLY_BY_ID_TOOLS:
            blocked = await _check_m365_referenced_message(session, tool_name, normalized, arguments)
            if blocked is not None:
                return blocked
        if normalized in _M365_EVENT_BY_ID_TOOLS:
            blocked = await _check_m365_referenced_event(session, tool_name, normalized, arguments)
            if blocked is not None:
                return blocked
        if normalized == _M365_DOWNLOAD_TOOL:
            blocked = _check_m365_download_target(tool_name, arguments)
            if blocked is not None:
                return blocked
        if normalized in _GATED_M365_MAIL_TOOLS:
            # Same order as Gmail: only after every recipient gate above passed.
            expanded = await _expand_m365_artifact_attachments(tool_name, normalized, arguments)
            if isinstance(expanded, str):
                return expanded
            arguments, attached_artifacts = expanded
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
        elif normalized == _M365_RECORD_SEND_TOOL and not _is_error_payload(result_text):
            _record_m365_outbound_context(arguments)
        if attached_artifacts and not _is_error_payload(result_text):
            from openexecutive.audit import log_event as audit_log

            audit_log(
                "artifact_attached",
                f"Attached {', '.join(attached_artifacts)} via {tool_name}",
                actor="mcp_gateway",
                details={
                    "tool": tool_name,
                    "artifact_ids": attached_artifacts,
                    "recipients": _audit_recipients(tool_name, arguments),
                },
            )
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
