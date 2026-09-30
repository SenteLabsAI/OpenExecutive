from __future__ import annotations

import asyncio
import base64
import contextlib
import contextvars
import html
import json
import logging
import os
import re
import unicodedata
from collections.abc import Iterator
from email.utils import getaddresses
from pathlib import Path
from typing import Any

from openexecutive.config import get_settings, mcp_config_file_present
from openexecutive.memory import drive_reads
from openexecutive.people.identity import RosterAllow
from openexecutive.utils.html_tags import strip_tags

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
# The cutoff must admit fastembed 0.8.1 (published 2026-09-22T20:01Z): 0.8.0
# pads MiniLM batches to a fixed 128 tokens but truncates at 256, so any batch
# mixing shorter and 129-256-token texts fails to stack, and the Google
# Workspace tool index is such a batch — the gateway died building it.
_EXTENSIBLE_MCP_REV = "90800ca3e823f73975099b362b1bb3a7b20dd6eb"
_EXTENSIBLE_MCP_EXCLUDE_NEWER = "2026-09-23T00:00:00Z"
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
)

# Outbound Gmail tools whose arguments may carry recipients. Any tool name
# matching one of these (after the `google_workspace__` namespace prefix) is
# subject to the recipient allow-list. Names track workspace-mcp 1.29.0: the
# send/reply/forward surface is a single `send_gmail_message` (reply is that
# tool with thread_id/quoting; forward is its `forward_message_id` arg, which
# `_GMAIL_ALLOWED_ARG_KEYS` refuses), and `draft_gmail_message` replaced
# `create_gmail_draft`. Drafts are gated too (defense-in-depth: a draft
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

# Apps Script tools run code as the Executive's Google account, outside every
# gate in this module: the code can mail anyone, read or share all of Drive
# and Gmail, and (manage_script_trigger, workspace-mcp 1.29.0) keep running
# after the turn ends. OE never calls them, so every one is refused. The set
# holds the 1.29.0 names and the 1.21.1 names they replaced; the name-pattern
# fallback in `_is_apps_script_tool` keeps a rename from reopening the path.
_BLOCKED_APPS_SCRIPT_TOOLS = frozenset({
    # 1.29.0
    "google_workspace__generate_trigger_code",
    "google_workspace__get_script_activity",
    "google_workspace__get_script_project",
    "google_workspace__get_script_version",
    "google_workspace__list_script_deployments",
    "google_workspace__manage_deployment",
    "google_workspace__manage_script_content",
    "google_workspace__manage_script_project",
    "google_workspace__manage_script_trigger",
    "google_workspace__manage_script_version",
    "google_workspace__run_script_function",
    # 1.21.1
    "google_workspace__create_script_project",
    "google_workspace__create_version",
    "google_workspace__delete_script_project",
    "google_workspace__get_script_content",
    "google_workspace__get_script_metrics",
    "google_workspace__get_version",
    "google_workspace__list_deployments",
    "google_workspace__list_script_processes",
    "google_workspace__list_script_projects",
    "google_workspace__list_versions",
    "google_workspace__update_script_content",
})

# Arguments that make workspace-mcp fetch a URL the model chose. Its SSRF guard
# blocks internal hosts only, so a public URL is an exfiltration channel:
# whatever the model puts in the query string leaves the box when the fetch
# runs — before any recipient gate matters (a send to the Executive's own
# address is always allowed). `create_drive_file.fileUrl` also takes file://,
# i.e. reads a local file into Drive. Attachments still reach mail as `path`,
# `content` or an `artifact_id`; Drive files as `content` / `base64_content`.
_URL_FETCH_ARGS: dict[str, frozenset[str]] = {
    "google_workspace__create_drive_file": frozenset({"fileUrl"}),
    "google_workspace__update_drive_file": frozenset({"file_url"}),
    "google_workspace__import_to_google_doc": frozenset({"file_url"}),
    "google_workspace__import_to_google_sheets": frozenset({"file_url"}),
    "google_workspace__import_to_google_slides": frozenset({"file_url"}),
}
# Normalized (lowercased, alphanumerics only) argument keys refused on EVERY
# Google Workspace tool and inside every attachment entry, so a renamed or new
# fetch argument fails closed. `urls` (a contact's websites) is not one.
_URL_FETCH_KEYS = frozenset({"url", "fileurl", "sourceurl", "remoteurl", "downloadurl"})
# Keys Google itself fetches, at any depth: insert_doc_image.image_source,
# Slides createImage.url / replaceImage.url / imageUrl and a page background's
# stretchedPictureFill.contentUrl, Forms image.sourceUri, Docs image_uri. The
# query string still lands on the attacker's host. Rather than name them, any
# key ending in url / uri (or image_source) whose value has a URL scheme is
# refused; a Drive file id (no scheme) passes. The named exceptions are stored
# as text, never fetched: a document hyperlink, a meeting link, a YouTube id.
_GOOGLE_FETCH_KEY_RE = re.compile(r"(url|uri|imagesource)$")
_STORED_URL_KEYS = frozenset({"linkurl", "conferenceuri", "youtubeuri"})
_URL_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
# What a URL parser strips or ignores before the scheme (C0 controls, space,
# DEL, zero-width and bidi format characters, BOM) — removed before the scheme
# test so "\x01https://…" or "\u200bhttps://…" is still a URL here.
_INVISIBLE_RE = re.compile(r"[\x00-\x20\x7f\u200b-\u200f\u2028-\u202f\u2060-\u206f\ufeff]")
# Sheets functions Google evaluates by fetching a URL from the cell VALUE, so
# no key names it. Matched anywhere in any string — a `values` argument may
# arrive JSON-encoded, a CSV cell sits mid-line, "+IMPORTDATA(" needs no "=" —
# on every tool that can write cells: the Sheets tools (sheet / table /
# conditional formatting) and the Drive create / update / import tools, which
# convert CSV into a native Sheet. Other formulas (=SUM(...)) pass, as does
# the word without a call ("IMPORTDATA is a function").
_FORMULA_FETCH_RE = re.compile(r"\b(IMPORTDATA|IMPORTXML|IMPORTHTML|IMPORTFEED|IMAGE)\s*\(", re.IGNORECASE)
_CELL_WRITER_RE = re.compile(r"sheet|table|conditional|drive_file")
# A converter decodes markup before it sees a formula, so text bound for a
# Sheet is also scanned with CDATA markers, then tags and comments, removed
# and character references decoded ("IMPORT<!---->DATA(", "IMPORT<b></b>DATA(",
# "&#x49;MPORTDATA("). Tags go through utils.html_tags.strip_tags, a forward
# scan: a regex over sender-shaped text with many "<" is quadratic.
# What a converter might fold away before the formula is parsed: NUL and other
# control characters (a UTF-16-shaped CSV), Unicode format characters (a soft
# hyphen inside the name). Removed, after NFKC folding of full-width letters
# and parentheses, for one more matching pass.
_FOLDED_NOISE_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# A JSON-encoded string argument is json.loads'ed by the server, so a gate must
# read what the server will read: "\u0049MPORTDATA(" is IMPORTDATA( once
# decoded, and a `requests` list may arrive as one string. Strings up to this
# size that parse to a list or object are walked as that value; a larger one
# on a cell-writing tool is refused, since it cannot be read.
_JSON_STRING_MAX = 2_000_000
# Nesting deeper than any real argument. Every walk stops here and FAILS
# CLOSED: `_nested_fetch_key` reports it as a hit, and `_iter_arg_strings`
# yields this sentinel, which its consumers (the formula scan, the Drive share
# scan) refuse — a payload buried past the cap is unread, and unread means
# refused, never forwarded.
_WALK_DEPTH_MAX = 32
_TOO_DEEP = "<too-deep>"
# An upload that becomes a native Sheet is evaluated by Google, and a binary
# upload (XLSX, ODS, XLS) hides its formulas behind zip members, XML encodings
# and character references — nothing a scan here can read reliably. So such an
# upload may only be text `content`, which is scanned; base64_content and a
# server-side file_path are refused for it. Every other upload (a PNG to
# Drive, a DOCX to Docs) is left alone.
_SHEET_MIME_RE = re.compile(r"spreadsheet|ms-?excel|openxmlformats.*sheet|opendocument\.spreadsheet|csv")
# Free text Google mails to someone the roster never checked: an RSVP comment
# goes to the event's organizer; an out-of-office / focus-time decline message
# goes to whoever invites the Executive during the window. Refused on every
# Google Workspace tool, whatever the spelling of the key.
_MAILED_TEXT_KEYS = frozenset({"rsvpcomment", "declinemessage"})

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
#
# Deliberately NOT allowed (workspace-mcp 1.29.0 send_gmail_message args):
#   - reply_all: workspace-mcp derives To/Cc from the thread itself, so the
#     recipients never pass through the roster check below.
#   - forward_message_id / include_forwarded_attachments: forward any inbox
#     message to `to` with its original attachments — files the model never
#     sees or writes, so nothing it could otherwise put in `body`. Replies
#     still work via thread_id + an explicit, roster-checked `to` (with
#     quote_original, the quoted text goes only to that roster address).
_GMAIL_ALLOWED_ARG_KEYS = frozenset({
    "user_google_email",
    "to",
    "cc",
    "bcc",
    "subject",
    "body",
    "html_body",
    # workspace-mcp (1.21.1+) uses body + body_format ("plain"|"html") instead of a
    # separate html_body; keep html_body for back-compat. body_format is an enum,
    # not a recipient.
    "body_format",
    "thread_id",
    "message_id",
    "attachments",
    # Threading metadata — Message-IDs, not addresses. Cannot carry recipients.
    "in_reply_to",
    "references",
    # Plain booleans (1.21.1+) — signature inclusion / original-message quoting.
    # Not recipients.
    "include_signature",
    "quote_original",
    # Gmail "Send As" display name — sets the From header's display name,
    # NOT a recipient and NOT the From address (that stays the authenticated
    # user_google_email). Validated for CR/LF below to block header injection.
    "from_name",
    # Gmail "Send As" alias address (1.21.1+). Sets the From mailbox to a verified
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


def _check_acting_account(tool: str, arguments: dict[str, Any]) -> str | None:
    """Refuse a Google Workspace call naming an account other than the
    Executive's own; None when it may run.

    workspace-mcp takes the acting account per call (``user_google_email``),
    and nothing checked it, so a model-chosen address could open another
    mailbox the server holds a credential for — or, under
    ``GWORKSPACE_AUTH_MODE=service_account`` (domain-wide delegation),
    possibly anyone's in the domain. Someone's own mailbox is reached only
    through ``delegation.gmail`` (Act as me), never through here. Absent
    means the server's own account and stays allowed."""
    values = [v for v in _arg_values(arguments, "user_google_email") if v is not None]
    if not values:
        return None
    exec_address = get_settings().exec_email_address.strip().lower()
    if all(isinstance(v, str) and v.strip().lower() == exec_address for v in values):
        return None
    value = next(v for v in values if not (isinstance(v, str) and v.strip().lower() == exec_address))
    from openexecutive.audit import log_event as audit_log

    shown = (value if isinstance(value, str) else repr(value))[:200]
    logger.warning("blocked google workspace call as another account: tool=%s", tool)
    audit_log(
        "integration_outbound_blocked",
        f"Blocked a Google Workspace call as another account (tool={tool})",
        actor="mcp_gateway",
        details={"tool": tool, "field": "user_google_email", "address": shown},
    )
    return json.dumps({
        "error": (
            f"Google Workspace tools act only as the Executive's own account "
            f"({exec_address}); refusing user_google_email={shown!r}. Leave it out "
            "or use that address. Do not retry with another account."
        ),
    })


def _refuse(tool: str, field: str, shown: str, *, reason: str) -> str:
    """Refuse a Google Workspace call for a reason other than a recipient (a
    blocked tool, a URL fetch, an RSVP comment). Audited like `_block`, with
    ``shown`` in the row's address slot so /audit reads the same for every
    refusal."""
    from openexecutive.audit import log_event as audit_log

    field = field[:200]
    logger.warning("blocked google workspace call: tool=%s field=%s", tool, field)
    audit_log(
        "integration_outbound_blocked",
        f"Blocked a Google Workspace call (tool={tool} field={field})",
        actor="mcp_gateway",
        details={"tool": tool, "field": field, "address": shown},
    )
    return json.dumps({"error": reason})


def _norm_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


# workspace-mcp 1.29.0 added CamelCaseArgumentsMiddleware (core/server.py): an
# argument key that is not a declared parameter is renamed to its snake_case
# form when that form is declared and absent. So `rsvpComment`, `Attendees` or
# `userGoogleEmail` reach the tool as the real parameter — and a gate that
# matched the exact key never saw them. Every gate therefore reads a parameter
# through these, by normalized spelling, and a refusal fires on any variant.
def _arg_values(arguments: dict[str, Any], name: str) -> list[Any]:
    """Every value stored under ``name`` or a respelling of it."""
    target = _norm_key(name)
    return [v for k, v in arguments.items() if isinstance(k, str) and _norm_key(k) == target]


def _arg(arguments: dict[str, Any], name: str) -> Any:
    """The value under ``name`` — the exact key when present (the middleware
    never overrides one), else the first respelling; None when absent."""
    if name in arguments:
        return arguments[name]
    values = _arg_values(arguments, name)
    return values[0] if values else None


def _is_apps_script_tool(tool_name: object) -> bool:
    """True for any Apps Script tool: the known names, or (fallback) any
    Google Workspace tool whose name says script or deployment. No Gmail /
    Calendar / Drive / Docs / Sheets tool does, and an over-match refuses,
    so it fails closed."""
    if not isinstance(tool_name, str):
        return False
    if tool_name in _BLOCKED_APPS_SCRIPT_TOOLS:
        return True
    if not tool_name.startswith(_GW_PREFIX):
        return False
    bare = tool_name[len(_GW_PREFIX):]
    return "script" in bare or "deployment" in bare


def _check_url_fetch(tool: str, arguments: dict[str, Any]) -> str | None:
    """Return None unless a Google Workspace call asks the server to fetch a
    URL (see `_URL_FETCH_ARGS`); else a JSON error. A key set to null / ""
    fetches nothing and passes."""
    named = _URL_FETCH_ARGS.get(tool, frozenset())
    for key, value in arguments.items():
        if not isinstance(key, str):
            continue
        # A gated Gmail tool's attachments have their own check, after the
        # recipient gate, so a stranger is still refused as a stranger.
        if tool in _GATED_GMAIL_TOOLS and _norm_key(key) == "attachments":
            continue
        if (key in named or _norm_key(key) in _URL_FETCH_KEYS) and value not in (None, ""):
            return _refuse(
                tool, key, "<url-fetch>",
                reason=(
                    f"argument {key!r} makes the server fetch a URL, which can "
                    f"carry data out — refusing. Pass the content itself (or, "
                    "for a Gmail attachment, a path or artifact_id) instead."
                ),
            )
        if _google_fetches(key, value):
            return _refuse(
                tool, key, "<url-fetch>",
                reason=(
                    f"{key!r} names a URL for Google to fetch, which can carry "
                    "data out — refusing. Use a Drive file id instead."
                ),
            )
        nested = _nested_fetch_key(value)
        if nested is not None:
            return _refuse(
                tool, f"{key}.{nested}", "<url-fetch>",
                reason=(
                    f"{nested!r} inside {key!r} names a URL to fetch (by the "
                    "server or by Google), which can carry data out — refusing. "
                    "Use a Drive file id or the content itself."
                ),
            )
    return None


def _is_url(value: Any) -> bool:
    """True when ``value`` is a string a URL parser would fetch from: a scheme
    ("https:", "data:", …) or a protocol-relative "//host", after the
    characters such a parser strips are removed."""
    if not isinstance(value, str):
        return False
    cleaned = _INVISIBLE_RE.sub("", value)
    return cleaned.startswith("//") or _URL_SCHEME_RE.match(cleaned) is not None


def _google_fetches(key: str, value: Any) -> bool:
    """True when ``key`` is one Google fetches and ``value`` is a URL."""
    norm = _norm_key(key)
    if norm in _STORED_URL_KEYS or not _GOOGLE_FETCH_KEY_RE.search(norm):
        return False
    return _is_url(value)


def _nested_fetch_key(value: Any, depth: int = 0) -> str | None:
    """The first key below the top level that carries something to fetch: a
    `_URL_FETCH_KEYS` key with any value, or a key Google fetches
    (`_google_fetches`) whose value has a URL scheme. None when there is none."""
    if depth > _WALK_DEPTH_MAX:
        return _TOO_DEEP
    if isinstance(value, str):
        # A JSON-encoded `requests` list is walked as the list the server decodes.
        parsed = _parsed_json(value)
        return _nested_fetch_key(parsed, depth + 1) if parsed is not None else None
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(k, str):
                if _norm_key(k) in _URL_FETCH_KEYS and v not in (None, ""):
                    return k
                if _google_fetches(k, v):
                    return k
            found = _nested_fetch_key(v, depth + 1)
            if found is not None:
                return found
    elif isinstance(value, (list, tuple)):
        for v in value:
            found = _nested_fetch_key(v, depth + 1)
            if found is not None:
                return found
    return None


def _becomes_sheet(tool: str, arguments: dict[str, Any]) -> bool:
    """True when an upload through ``tool`` is converted into a native Sheet:
    the Sheets import, or a Drive create whose target type is a spreadsheet.
    A Drive update's target is unknown here, so it counts too."""
    bare = tool[len(_GW_PREFIX):] if tool.startswith(_GW_PREFIX) else tool
    if bare in ("import_to_google_sheets", "update_drive_file"):
        return True
    if bare != "create_drive_file":
        return False
    for key, value in arguments.items():
        if (
            isinstance(key, str) and _norm_key(key) in ("mimetype", "contentmimetype")
            and isinstance(value, str) and _SHEET_MIME_RE.search(value.lower())
        ):
            return True
    return False


def _formula_fetches(text: str) -> bool:
    """True when ``text`` holds a fetching formula call, as written, with
    character references decoded, or with markup removed first."""
    if _FORMULA_FETCH_RE.search(text):
        return True
    decoded = html.unescape(text)
    if _FORMULA_FETCH_RE.search(decoded):
        return True
    # CDATA markers first: to the tag strip, "<![CDATA[DATA]]>" is one tag.
    stripped = html.unescape(strip_tags(text.replace("<![CDATA[", "").replace("]]>", "")))
    if _FORMULA_FETCH_RE.search(stripped):
        return True
    folded = _FOLDED_NOISE_RE.sub("", unicodedata.normalize("NFKC", stripped))
    folded = "".join(ch for ch in folded if unicodedata.category(ch) != "Cf")
    return _FORMULA_FETCH_RE.search(folded) is not None


def _check_sheet_formulas(tool: str, arguments: dict[str, Any]) -> str | None:
    """Return None unless a cell-writing call carries a formula Google
    evaluates by fetching a URL (`_FORMULA_FETCH_RE`), or an upload whose
    formulas cannot be read from here; else a JSON error."""
    bare = tool[len(_GW_PREFIX):] if tool.startswith(_GW_PREFIX) else tool
    if not _CELL_WRITER_RE.search(bare):
        return None
    if _becomes_sheet(tool, arguments):
        for key, value in arguments.items():
            if not isinstance(key, str) or value in (None, ""):
                continue
            if _norm_key(key) in ("base64content", "filepath"):
                return _refuse(
                    tool, key, "<uninspected-file>",
                    reason=(
                        f"{key!r} would become a Google Sheet, and a binary or "
                        "server-side file's formulas cannot be checked here — "
                        "refusing. Pass the rows as text `content` (CSV) instead."
                    ),
                )
    for text in _iter_arg_strings(arguments):
        if text is _TOO_DEEP:
            return _refuse(
                tool, "values", "<unreadable>",
                reason="an argument nested this deep cannot be checked — refusing. Flatten it.",
            )
        if len(text) > _JSON_STRING_MAX and text.lstrip()[:1] in ("[", "{"):
            return _refuse(
                tool, "values", "<unreadable>",
                reason="a JSON-encoded argument this large cannot be checked — refusing. Send fewer rows per call.",
            )
        if _formula_fetches(text):
            return _refuse(
                tool, "values", "<url-fetch>",
                reason=(
                    "a formula that fetches a URL (IMPORTDATA / IMPORTXML / "
                    "IMPORTHTML / IMPORTFEED / IMAGE) would make Google carry data "
                    "out — refusing. Write the value itself instead."
                ),
            )
    return None


def _check_mailed_text(tool: str, arguments: dict[str, Any]) -> str | None:
    """Return None unless the call carries text Google would mail to someone
    the roster never checked (`_MAILED_TEXT_KEYS`); else a JSON error."""
    for key, value in arguments.items():
        if isinstance(key, str) and _norm_key(key) in _MAILED_TEXT_KEYS and value not in (None, ""):
            return _refuse(
                tool, key, "<unchecked-recipient>",
                reason=(
                    f"{key!r} is mailed by Google to a person the People roster "
                    "never checked (an event's organizer, whoever sends an "
                    "invitation) — refusing. Leave it out."
                ),
            )
    return None


def _attachment_fetches_url(attachments: Any) -> bool:
    """True when a Gmail ``attachments`` argument asks workspace-mcp to fetch
    a URL for any entry. Unknown shapes count as fetching (fail closed);
    ``artifact_id`` entries and path / content entries do not."""
    if attachments is None:
        return False
    if isinstance(attachments, str):
        try:
            attachments = json.loads(attachments)
        except RecursionError:
            return True
        except ValueError:
            return "://" in attachments or re.search(r"\burl\b", attachments, re.I) is not None
    if isinstance(attachments, dict):
        attachments = [attachments]
    if not isinstance(attachments, list):
        return True
    for entry in attachments:
        if isinstance(entry, dict):
            # Any depth, like every other Workspace argument: a url under
            # `metadata` is still a url, and past the depth cap it is refused.
            if _nested_fetch_key(entry) is not None:
                return True
        elif isinstance(entry, str):
            if "://" in entry:
                return True
        else:
            return True
    return False


def _check_attachment_urls(tool: str, arguments: dict[str, Any]) -> str | None:
    """Return None unless a gated Gmail call attaches by URL; else a JSON error."""
    if not _attachment_fetches_url(arguments.get("attachments")):
        return None
    return _refuse(
        tool, "attachments", "<url-fetch>",
        reason=(
            "an attachment given as a URL makes the server fetch it, which can "
            "carry data out — refusing to send. Attach by path, by content, or "
            "by artifact_id instead."
        ),
    )


def _roster_allow_set() -> RosterAllow:
    """The set of lowercased addresses the Executive may reach outbound.

    Derived from the People roster plus the Executive's own address — the single
    egress allow-list shared by the Gmail, Calendar, and Drive gates so they
    can't drift apart. Reads the live roster on each call (channel access is
    roster-driven and changes at runtime).

    Team members and the Executive's own address are always in it. The
    principal's contacts are in it only when
    ``people_tools.contacts_reachable_now()`` — a turn the principal started on
    a verified surface, or ``grant_contact_egress``. On any other turn a
    contact's address is refused exactly like a stranger's, so the refusal
    cannot reveal that the address belongs to a contact.
    """
    from openexecutive.orchestrator.people_tools import (
        contacts_reachable_now,
        turn_is_private_to_principal,
    )
    from openexecutive.people.store import list_people

    settings = get_settings()
    everyone = list_people(include_contacts=contacts_reachable_now())
    if turn_is_private_to_principal():
        # A turn about the principal's private mail reaches the principal only.
        everyone = [p for p in everyone if p.is_principal]
    # Primary addresses and aliases exactly; for a teammate, also their
    # local part on the company's own domains (people.identity) — the same
    # rule inbound mail is matched by.
    return RosterAllow(everyone, extra=[settings.exec_email_address])


# The one send that may reach an address off the roster: the fixed
# acknowledgement ``integrations.roster_intake.send_email_ack`` sends an
# unknown sender. Set only around that call; admits one send with exactly
# the granted recipient, subject and body and nothing else, then is spent.
_roster_ack: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "roster_ack_grant", default=None
)
_ROSTER_ACK_KEYS = frozenset({"user_google_email", "to", "subject", "body"})

# A roster request's one-time answer token ("RR-" + 20 base32 characters,
# people.roster_requests) proves an email answer came from the principal's
# mailbox, and so does a standing-fact confirmation token ("FC-",
# memory.facts). The confirmation email carrying one sits in the Executive's
# own Sent folder, so every Google Workspace result is scrubbed of tokens — a
# model turn (anyone's) reading that mail sees "RR-[hidden]" / "FC-[hidden]"
# — except the email poller's own fetch of an inbound message, inside
# reveal_roster_tokens.
_ROSTER_TOKEN_RE = re.compile(r"\b(RR|FC)-[A-Z2-7]{20}\b", re.IGNORECASE)
_reveal_tokens: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "reveal_roster_tokens", default=False
)


def hide_roster_tokens(text: str) -> str:
    """``text`` with every roster answer token replaced by "RR-[hidden]" and
    every standing-fact confirmation token by "FC-[hidden]"."""
    return _ROSTER_TOKEN_RE.sub(lambda m: f"{m.group(1).upper()}-[hidden]", text)


@contextlib.contextmanager
def reveal_roster_tokens() -> Iterator[None]:
    """Leave roster answer tokens in Google Workspace results (the poller's
    read of one inbound message, before any model sees it)."""
    token = _reveal_tokens.set(True)
    try:
        yield
    finally:
        _reveal_tokens.reset(token)


@contextlib.contextmanager
def roster_ack_grant(*, to: str, subject: str, body: str) -> Iterator[None]:
    """Let the Gmail gate pass one send of ``subject`` / ``body`` to ``to``."""
    token = _roster_ack.set({"to": to, "subject": subject, "body": body, "used": False})
    try:
        yield
    finally:
        _roster_ack.reset(token)


def _roster_ack_admits(tool: str, arguments: dict[str, Any]) -> bool:
    """Whether this call is exactly the granted acknowledgement (and spend
    the grant if so). Any other key, a list recipient, a different body or
    subject, a draft instead of a send, or a second call: no."""
    grant = _roster_ack.get()
    if grant is None or grant["used"]:
        return False
    if tool != "google_workspace__send_gmail_message":
        return False
    if set(arguments) != _ROSTER_ACK_KEYS:
        return False
    to = arguments.get("to")
    if not isinstance(to, str) or to != grant["to"] or any(c in to for c in ",;<>\r\n"):
        return False
    if arguments.get("subject") != grant["subject"] or arguments.get("body") != grant["body"]:
        return False
    grant["used"] = True
    return True


# Artifact attachments on one email. Gmail caps a message at 25 MB and base64
# inflates the payload by a third, so the rendered artifacts together stay
# well under it; the count cap bounds how much one tool call can make the
# server render (each entry is a full docx / xlsx build).
_MAX_ARTIFACT_ATTACHMENTS = 5
_MAX_ARTIFACT_ATTACHMENT_BYTES = 15 * 1024 * 1024
_ARTIFACT_ATTACHMENT_KEYS = frozenset({"artifact_id", "as"})


def _recipients(arguments: dict[str, Any]) -> list[str]:
    """Every to/cc/bcc address on a Gmail call, for audit rows."""
    raw = [arguments.get(f) for f in _GMAIL_RECIPIENT_FIELDS]
    values = [
        str(v) for item in raw
        for v in (item if isinstance(item, list) else [item]) if v
    ]
    return [addr for _, addr in getaddresses(values) if addr]


def _refuse_attachment(tool: str, arguments: dict[str, Any], reason: str) -> str:
    from openexecutive.audit import log_event as audit_log

    logger.warning("refused artifact attachment on %s: %s", tool, reason)
    audit_log(
        "artifact_attachment_refused",
        f"Refused an artifact attachment on {tool}: {reason[:160]}",
        actor="mcp_gateway",
        details={"tool": tool, "reason": reason, "recipients": _recipients(arguments)},
    )
    return json.dumps({"error": f"attachment: {reason}"})


async def _expand_artifact_attachments(
    tool: str, arguments: dict[str, Any]
) -> tuple[dict[str, Any], list[str]] | str:
    """Replace `{"artifact_id": "alert:5", "as"?: "docx"}` attachment entries
    with the rendered file (`content` base64 + `filename` + `mime_type`, the
    shape workspace-mcp's Gmail tools accept).

    Lets the Executive email one of its artifacts without ever holding the
    bytes: the file is rendered server-side exactly as `/artifacts/{id}/
    download` serves it, and only artifact rows can resolve (see
    `artifact_records`). At most `_MAX_ARTIFACT_ATTACHMENTS` distinct entries
    (repeats are dropped) and `_MAX_ARTIFACT_ATTACHMENT_BYTES` in total;
    rendering runs off the event loop. Returns the (copied) arguments plus
    the attached artifact ids, or an audited JSON error string. Other
    attachment entries pass through untouched.
    """
    attachments = arguments.get("attachments")
    # Models sometimes stringify nested arguments, or send one object instead
    # of a list. Normalise both, so an artifact entry can never reach the MCP
    # unexpanded (workspace-mcp would skip it and send the mail without it).
    if isinstance(attachments, str) and "artifact_id" in attachments:
        try:
            attachments = json.loads(attachments)
        except json.JSONDecodeError:
            return _refuse_attachment(tool, arguments, "attachments is not valid JSON")
    if isinstance(attachments, dict):
        attachments = [attachments]
    if not isinstance(attachments, list) or not any(
        isinstance(a, dict) and "artifact_id" in a for a in attachments
    ):
        return arguments, []

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
        rendered[(artifact_id, as_)] = {
            "content": base64.b64encode(file.content).decode("ascii"),
            "filename": file.filename,
            "mime_type": file.mime,
        }
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
    return {**arguments, "attachments": expanded}, attached


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

    if _roster_ack_admits(tool, arguments):
        from openexecutive.audit import log_event as audit_log

        audit_log(
            "integration_outbound",
            "Sent the fixed acknowledgement to a sender not on the roster",
            actor="mcp_gateway",
            details={"tool": tool, "kind": "roster_ack"},
            private=True,
        )
        return None

    # Egress gate: the Executive may only send mail to addresses on the
    # People roster or to its own exec address. Used to be a static env
    # allowlist; now derived from the People table so it stays in sync
    # with channel access. Contacts only on the principal's own turns.
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
    # rsvp_comment (text mailed to the organizer) is refused for every tool by
    # _check_mailed_text, before this gate runs.
    # Action is matched as the server matches it (case- and space-insensitive),
    # so a "Delete" is a delete here too rather than a stricter accident.
    action = str(_arg(arguments, "action") or "").strip().lower()
    if action in ("delete", "rsvp"):
        return None

    # Every spelling of `attendees` is read: the server renames `Attendees`
    # to the real parameter, so a variant is as good as the exact key.
    # None = no attendees field at all → pass through (e.g. organizer-only event).
    # Empty list [] = explicitly supplied with no names → also pass through;
    # the typed create_calendar_event tool always supplies at least one attendee,
    # and a raw call with [] creates an organizer-only event (no roster leak).
    # Any non-empty list → every address must be roster-validated.
    supplied = [v for v in _arg_values(arguments, "attendees") if v is not None]
    if not supplied:
        return None
    if all(isinstance(v, list) and len(v) == 0 for v in supplied):
        return None

    allow = _roster_allow_set()

    # attendees may be a list of strings (emails) or dicts with an "email" key.
    items: list[Any] = []
    for attendees in supplied:
        items.extend(attendees if isinstance(attendees, list) else [attendees])
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


def _pin_calendar_notifications(arguments: dict[str, Any]) -> dict[str, Any]:
    """Keep a manage_event call that named no attendees from emailing the
    event's existing guests.

    The attendee gate checks only the ``attendees`` argument. An update that
    leaves it out keeps the event's current guest list — which may hold
    people off the roster — and Google mails every guest the new title,
    description or time unless ``send_updates`` is "none". So such a call is
    forced to "none": the change lands on the calendar and nobody is emailed
    text the gate never saw. A call that passes attendees had them checked
    and keeps its own ``send_updates``; delete and rsvp carry no new text; a
    create without attendees has nobody to mail, so the pin is moot there.
    """
    action = str(_arg(arguments, "action") or "").strip().lower()
    if action in ("delete", "rsvp"):
        return arguments
    supplied = [v for v in _arg_values(arguments, "attendees") if v is not None]
    if any(not (isinstance(v, list) and not v) for v in supplied):
        return arguments
    if _arg_values(arguments, "send_updates") == ["none"]:
        return arguments
    logger.info("manage_event without attendees: send_updates pinned to none")
    # Every spelling goes, so no `sendUpdates: "all"` survives beside the pin.
    pinned = {k: v for k, v in arguments.items()
              if not (isinstance(k, str) and _norm_key(k) == "sendupdates")}
    pinned["send_updates"] = "none"
    return pinned


def _parsed_json(text: str) -> dict[str, Any] | list[Any] | None:
    """The list or object a string argument encodes, else None. The server
    json.loads such a string, so a gate reads it the same way."""
    head = text.lstrip()[:1]
    if head not in ("[", "{") or len(text) > _JSON_STRING_MAX:
        return None
    try:
        parsed = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return parsed if isinstance(parsed, (dict, list)) else None


def _iter_arg_strings(value: Any, depth: int = 0) -> Iterator[str]:
    """Yield every string anywhere in a (possibly nested) argument value.

    Drive-share tool schemas vary across workspace-mcp versions and a grantee
    email can be a top-level string, a list entry, nested in a permission dict,
    or even a dict *key* (e.g. an email-keyed permission map). Walking every
    string in both key and value position — rather than trusting a fixed set of
    field names — keeps the gate fail-closed against an email smuggled through an
    unexpected shape. A string that encodes JSON is also walked as the value it
    encodes, which is what the server sees after json.loads. Past
    `_WALK_DEPTH_MAX` it yields `_TOO_DEEP` once instead of going silent, so a
    consumer refuses what it could not read.
    """
    if depth > _WALK_DEPTH_MAX:
        yield _TOO_DEEP
        return
    if isinstance(value, str):
        yield value
        parsed = _parsed_json(value)
        if parsed is not None:
            yield from _iter_arg_strings(parsed, depth + 1)
    elif isinstance(value, dict):
        for k, v in value.items():
            if isinstance(k, str):
                yield k
            yield from _iter_arg_strings(v, depth + 1)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _iter_arg_strings(v, depth + 1)


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

        if s is _TOO_DEEP:

            return _refuse(

                tool, "share", "<unreadable>",

                reason="a share argument nested this deep cannot be checked — refusing. Flatten it.",

            )
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
            _resolve_recipient_person_id,
        )

        self_addr = get_settings().exec_email_address.lower()
        seen: set[str] = set()
        primary_taken = False
        for field in _OUTBOUND_CONTEXT_RECIPIENT_FIELDS:
            value = arguments.get(field)
            if not value:
                continue
            items = value if isinstance(value, list) else [value]
            for _name, addr in getaddresses([s for s in items if isinstance(s, str)]):
                norm = addr.strip().lower()
                if not norm or norm == self_addr or norm in seen:
                    continue
                # Only the first rostered "to" address is who the email was
                # addressed to; every recipient still gets reply linkage.
                primary = (
                    field == "to" and not primary_taken
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
    except Exception:
        logger.exception(
            "record_email_outbound_context: persist failed (non-fatal)"
        )


# extensible-mcp's refusal for a tool no search in this session has returned
# (DiscoveredToolsFilter, surfaced by its call_tool handler as "Error: ...").
_UNDISCOVERED_MARKER = "has not been discovered via search_tools"


def _remember_drive_read(tool_name: str, arguments: dict[str, Any], result_text: str) -> str:
    """Keep what a Drive read showed for the rest of the conversation
    (``memory.drive_reads``), and return the result the model sees: a search
    that matched nothing gains a note to report the query rather than
    conclude the file does not exist.

    Records only for a rostered speaker in a live session (``current_session``
    set), so a workflow step keeps nothing, and never on a turn private to the
    principal or one that touched the speaker's own mailbox (Act as me).
    Best-effort: a store failure never costs the model the result it asked
    for."""
    from openexecutive.orchestrator.schedule_tools import current_session

    session = current_session.get()
    if drive_reads.may_remember(session):
        try:
            drive_reads.record_drive_result(
                session.session_id, session.caller_person_id, tool_name, arguments, result_text
            )
        except Exception:
            logger.exception("drive_reads: failed to record %s result", tool_name)
    if drive_reads.is_empty_search(tool_name, result_text):
        result_text += drive_reads.empty_search_note(arguments)
    return result_text


def _is_undiscovered_refusal(tool_name: object, result_text: object) -> bool:
    """Whether the gateway refused a pinned Google tool only because this
    session had not discovered it yet. Pinned names only: any other tool
    still needs the model's own search, as before."""
    from openexecutive.prompts.connected_systems import PINNED_GOOGLE_TOOLS

    return (
        isinstance(tool_name, str)
        and tool_name in PINNED_GOOGLE_TOOLS
        and isinstance(result_text, str)
        and result_text.startswith(f"Error: Tool '{tool_name}' ")
        and _UNDISCOVERED_MARKER in result_text
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
        # The servers the config named when this gateway started. The
        # Connected Systems prompt section reads it, so a gateway that never
        # started never reads as connected.
        self.server_names: tuple[str, ...] = ()

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
        self.server_names = tuple(configured_server_names(config_path))
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

    async def _discover(self, tool_name: str) -> bool:
        """Run the exact-name search that registers ``tool_name`` with
        extensible-mcp's discovered-tools filter (it only runs a tool one of
        this session's ``search_tools`` results returned, and remembers it for
        the session's life), the way ``tool_catalog.resolve`` does. True when
        the search returned it. Best-effort: never raises."""
        from openexecutive.workflows.tool_catalog import parse_search_results

        query = re.sub(r"[_\-]+", " ", tool_name).strip()
        try:
            text = await self.search_tools({"query": query, "top_k": 10})
        except Exception as exc:
            logger.warning(
                "MCPGateway: discovering %s failed (%s)", tool_name, type(exc).__name__
            )
            return False
        if any(info.name == tool_name for info in parse_search_results(text)):
            return True
        logger.warning(
            "MCPGateway: pinned tool %s not found by search — has workspace-mcp "
            "renamed it? (prompts/connected_systems.GOOGLE_TOOL_MANIFEST)",
            tool_name,
        )
        return False

    async def prime_pinned_tools(self) -> list[str]:
        """Discover every pinned Google tool (PINNED_GOOGLE_TOOLS) up front, so
        the model's direct ``call_tool`` on one works without a search of its
        own. Returns the names not found, a drift signal. Run once at startup
        when Google is configured."""
        from openexecutive.prompts.connected_systems import PINNED_GOOGLE_TOOLS

        missing = [n for n in sorted(PINNED_GOOGLE_TOOLS) if not await self._discover(n)]
        logger.info(
            "MCPGateway: primed %d pinned Google tools (%d missing)",
            len(PINNED_GOOGLE_TOOLS) - len(missing), len(missing),
        )
        return missing

    async def call_tool(self, tool_input: dict[str, Any]) -> str:
        session = self._require_session()
        arguments = tool_input.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (json.JSONDecodeError, RecursionError):
                logger.warning("call_tool: arguments was a string but not valid JSON — using empty dict")
                arguments = {}
        tool_name = tool_input.get("name", "")
        attached_artifacts: list[str] = []
        # Every Google Workspace call acts as the Executive's own account.
        if _is_apps_script_tool(tool_name):
            return _refuse(
                tool_name, "tool", "<apps-script>",
                reason=(
                    f"{tool_name} is not available: Apps Script runs code as "
                    "the Executive's Google account outside the outbound "
                    "gates. Do not retry with another script tool."
                ),
            )
        if isinstance(tool_name, str) and tool_name.startswith(_GW_PREFIX):
            # Every gate below reads a dict; anything else would skip them all.
            if not isinstance(arguments, dict):
                return _refuse(
                    tool_name, "arguments", "<non-object>",
                    reason="arguments must be a JSON object — refusing.",
                )
            blocked = _check_acting_account(tool_name, arguments)
            if blocked is not None:
                return blocked
            blocked = _check_url_fetch(tool_name, arguments)
            if blocked is not None:
                return blocked
            blocked = _check_mailed_text(tool_name, arguments)
            if blocked is not None:
                return blocked
            blocked = _check_sheet_formulas(tool_name, arguments)
            if blocked is not None:
                return blocked
        if tool_name in _GATED_GMAIL_TOOLS:
            blocked = _check_gmail_recipients(tool_name, arguments)
            if blocked is not None:
                return blocked
            blocked = _check_attachment_urls(tool_name, arguments)
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
            arguments = _pin_calendar_notifications(arguments)
        if _is_drive_share_tool(tool_name):
            blocked = _check_drive_share(tool_name, arguments)
            if blocked is not None:
                return blocked
        result = await session.call_tool(
            "call_tool",
            {"tool_name": tool_input["name"], "arguments": arguments},
        )
        result_text = result.content[0].text if result.content else json.dumps({"result": None})
        # A pinned tool called before startup priming reached it: the prompt
        # told the model to call it directly, so discover it and retry once.
        # The refused call ran nothing, and every gate above has already
        # passed these same arguments.
        if _is_undiscovered_refusal(tool_name, result_text) and await self._discover(tool_name):
            result = await session.call_tool(
                "call_tool",
                {"tool_name": tool_input["name"], "arguments": arguments},
            )
            result_text = result.content[0].text if result.content else json.dumps({"result": None})
        # On the final result, retry or not: roster answer tokens are hidden
        # from every Google Workspace read but the poller's own.
        if (
            isinstance(tool_name, str) and tool_name.startswith(_GW_PREFIX)
            and not _reveal_tokens.get()
        ):
            result_text = hide_roster_tokens(result_text)
        if tool_name in drive_reads.DRIVE_READ_TOOLS:
            result_text = _remember_drive_read(tool_name, arguments, result_text)
        # Record an outbound-context linkage only for a genuinely-sent email.
        # The send tool returns its outcome as text; a soft-error payload
        # (`{"error": ...}`) means nothing was sent, so skip it to avoid a
        # phantom linkage that would hydrate a reply that can never come.
        if tool_name == "google_workspace__send_gmail_message" and not _is_error_payload(result_text):
            _record_email_outbound_context(arguments)
        if attached_artifacts and not _is_error_payload(result_text):
            from openexecutive.audit import log_event as audit_log

            audit_log(
                "artifact_attached",
                f"Attached {', '.join(attached_artifacts)} via {tool_name}",
                actor="mcp_gateway",
                details={
                    "tool": tool_name,
                    "artifact_ids": attached_artifacts,
                    "recipients": _recipients(arguments),
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


def gateway_server_names(gateway: object) -> tuple[str, ...]:
    """The servers ``gateway`` started with, or () for no gateway (or a
    stand-in without the attribute)."""
    names = getattr(gateway, "server_names", ())
    if not isinstance(names, tuple | list):
        return ()
    return tuple(n for n in names if isinstance(n, str))
