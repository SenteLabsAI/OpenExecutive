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
from collections.abc import Awaitable, Callable, Iterable, Iterator
from email.utils import getaddresses
from pathlib import Path
from typing import Any
from urllib.parse import unquote

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
_EXTENSIBLE_MCP_REV = "6862ebd29c95a1e40b929226637d38b5a5659c80"
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
    "WORKSPACE_MCP_TOOLS",
    # Microsoft 365 (ms-365-mcp-server) child — consumed by the `env` block of
    # the microsoft_365 entry and by docker/ms365-mcp-launch.sh. Same
    # contract as the Google vars above: an entry here is what turns a
    # deployment secret into something the child can see.
    "MS365_MCP_CLIENT_ID",
    "MS365_MCP_TENANT_ID",
    "MS365_MCP_CLIENT_SECRET",
    "MS365_MCP_EXPECTED_USERNAME",
    "MS365_MCP_ORG_MODE",
    "MS365_MCP_ONEDRIVE",
    "MS365_MCP_OAUTH_TOKEN",
    "MS365_MCP_CREDENTIALS_DIR",
    "MS365_MCP_TOKEN_CACHE_PATH",
    "MS365_MCP_SELECTED_ACCOUNT_PATH",
    "MS365_MCP_USE_KEYTAR",
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
# permissions, …). The Executive needs it for two things only — mail
# attachment bytes and OneDrive file content (optionally as Graph's PDF
# rendering) — so the gateway pins its `target` to exactly those shapes, and
# hands back OneDrive content as extracted text rather than base64
# (`_onedrive_content_as_text`).
_M365_DOWNLOAD_TOOL = "microsoft_365__download_bytes"
_M365_ATTACHMENT_TARGET_RE = re.compile(r"/me/messages/([^/?#\\\s]+)/attachments/([^/?#\\\s]+)/\$value")
_M365_DRIVE_CONTENT_TARGET_RE = re.compile(
    r"/drives/([^/?#\\\s]+)/items/([^/?#\\\s]+)/content(\?format=pdf)?"
)

# OneDrive sharing — the Microsoft analogue of `_GATED_DRIVE_TOOLS`, in
# normalized form. `share-drive-item` invites named people (recipients by
# email; an `objectId` / `alias` recipient names nobody the roster can
# check, so it is refused), and `create-drive-item-share-link` mints a link,
# which must be scoped to specific people (`scope: "users"`): an `anonymous`
# or `organization` link, or one with no scope (the tenant's default, often
# anonymous), reaches people the roster never sees.
_M365_DRIVE_INVITE_TOOL = "microsoft_365__share_drive_item"
_M365_DRIVE_LINK_TOOL = "microsoft_365__create_drive_item_share_link"
_M365_DRIVE_SHARE_TOOLS = frozenset({_M365_DRIVE_INVITE_TOOL, _M365_DRIVE_LINK_TOOL})
_M365_UNCHECKABLE_GRANTEE_KEYS = frozenset({"objectid", "alias", "siteuser", "application", "device"})
_M365_LINK_SCOPE_OK = "users"
# An invite must require sign-in (Graph's default is not to, which on many
# tenants makes the invite an open link) and grant view or edit only; a link
# must be a view or edit link (an `embed` link is public on personal drives).
_M365_INVITE_ROLES_OK = frozenset({"read", "write"})
_M365_LINK_TYPES_OK = frozenset({"view", "edit"})
_M365_SHARE_PINNED_KEYS = frozenset({"scope", "roles", "type", "requiresignin"})

# `search-onedrive-files` substitutes `q` into the request path unencoded
# (`/drives/{d}/search(q='{q}')`), so a quote, slash or dot-dot in it walks
# the request to any other Graph GET (mail, mailbox rules, other drives): the
# same bypass the `download-bytes` pin exists to stop. `q` is therefore plain
# search words only.
_M365_DRIVE_SEARCH_TOOL = "microsoft_365__search_onedrive_files"
_M365_DRIVE_SEARCH_Q_MAX = 200
_M365_DRIVE_SEARCH_Q_BAD = frozenset("'\"/\\?#%()&")

# OneDrive writes land only in the Executive's own drives (`list-drives`,
# i.e. /me/drives). The launcher asks for Files.ReadWrite, not .All, which
# already keeps writes there; this holds if an operator widens the scopes,
# where a planted instruction could otherwise have the account copy or save
# company files into any folder a stranger shared with it, with nothing
# shared and no roster check. Reading shared drives stays open.
_M365_DRIVE_WRITE_TOOLS = frozenset({
    "microsoft_365__upload_file_content",
    "microsoft_365__create_onedrive_folder",
    "microsoft_365__move_rename_onedrive_item",
    "microsoft_365__copy_drive_item",
})
_M365_DRIVE_COPY_TOOL = "microsoft_365__copy_drive_item"
_M365_PARENT_REFERENCE_KEYS = frozenset({"driveid", "id"})
_M365_DRIVES_LOOKUP_TOOL = "microsoft_365__list-drives"
# Normalized: the server takes `drive-id` and `driveId` alike, and when both
# are given the first one fills the path, so every spelling is checked.
_M365_DRIVE_ID_KEYS = frozenset({"driveid", "driveitemid"})
_TOO_DEEP_VALUE = object()


def _top_level_values(arguments: dict[str, Any], norm: str) -> list[Any]:
    """Values of every top-level key that normalizes to ``norm``."""
    return [v for k, v in arguments.items() if isinstance(k, str) and _norm_key(k) == norm]

# ms-365-mcp-server registers its account tools whatever `--enabled-tools`
# says, so the launcher's allow-list cannot remove them and the deny globs in
# mcp_servers.json.example only help an operator who copied them. A chat turn
# that can call these could sign the Executive out (wiping the seeded token),
# switch the mailbox every later call acts as, or start a device-code sign-in
# for an account someone else controls. Setup is the launcher's `--login`, so
# the gateway refuses them outright and keeps them out of `search_tools`
# results. The read-only `verify-login` / `list-accounts` stay callable.
# Normalized (underscore) form, matched via `_normalize_tool_name`.
_BLOCKED_M365_AUTH_TOOLS = frozenset({
    "microsoft_365__login",
    "microsoft_365__logout",
    "microsoft_365__select_account",
    "microsoft_365__remove_account",
})

# Mail fields that name who a message is FROM (or who replies go to). Graph
# lets a delegated caller set `from` / `sender` to any mailbox it holds
# Send As / Send on Behalf rights on, and `replyTo` steers every reply. The
# recipient gate only roster-checks them, so a rostered colleague's address
# would pass as the sender. Pinned to the Executive's own address instead,
# like `_check_acting_account` pins Gmail's acting account. Normalized keys
# (lowercase, alphanumerics only).
_M365_SENDER_KEYS = frozenset({"from", "sender", "replyto"})

# `move-mail-message` to one of these well-known folders is a trash (or a
# junk-mark) with no gate at all. Matched lowercased; a folder's opaque id is
# not resolved, so only the well-known names are caught.
_M365_MOVE_TOOL = "microsoft_365__move_mail_message"
_M365_TRASH_FOLDERS = frozenset({
    "deleteditems",
    "junkemail",
    "recoverableitemsdeletions",
    "recoverableitemspurges",
})

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
_ODATA_ANNOTATION_KEY_RE = re.compile(r"@odata\.[A-Za-z]+")

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


def _is_blocked_m365_auth_tool(tool_name: object) -> bool:
    """True for the Microsoft 365 account tools the gateway never runs
    (`_BLOCKED_M365_AUTH_TOOLS`), whatever the spelling or case."""
    return isinstance(tool_name, str) and _normalize_tool_name(tool_name) in _BLOCKED_M365_AUTH_TOOLS


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
# own Sent folder, so every Google Workspace and Microsoft 365 result is
# scrubbed of tokens — a model turn (anyone's) reading that mail sees
# "RR-[hidden]" / "FC-[hidden]" — except the email poller's own fetch of an
# inbound message, inside reveal_roster_tokens.
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
    """Leave roster answer tokens in mailbox results (the poller's read of
    one inbound message, before any model sees it)."""
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
        current_viewer,
        load_artifact,
        render_artifact_file,
    )

    # Only an artifact the speaker may see themselves can be attached.
    viewer = current_viewer()
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
            rec = await asyncio.to_thread(load_artifact, artifact_id, viewer=viewer)
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


def _has_artifact_entry(value: Any, skip: frozenset[int] = frozenset()) -> bool:
    """Whether an `artifact_id` entry (a dict carrying that key, or a string
    that spells it — a stringified entry) appears anywhere under ``value``,
    not descending into the objects whose ids are in ``skip``."""
    if id(value) in skip:
        return False
    if isinstance(value, dict):
        return "artifact_id" in value or any(_has_artifact_entry(v, skip) for v in value.values())
    if isinstance(value, list):
        return any(_has_artifact_entry(v, skip) for v in value)
    return isinstance(value, str) and "artifact_id" in value


def _coerce_attachment_list(value: Any) -> list[Any] | str:
    """A Graph `attachments` value as a list: a list as is, one object wrapped,
    a JSON string decoded (models stringify nested arguments), None empty.
    Returns an error reason for anything else."""
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return "attachments is not valid JSON"
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return value
    return "attachments must be a list of attachment objects"


def _with_message_attachments(
    arguments: dict[str, Any], path: tuple[str, ...], expanded: list[Any]
) -> dict[str, Any]:
    """A copy of ``arguments`` with ``expanded`` as the message's
    `attachments` at ``path`` (existing key casing kept, the canonical casing
    used for a level that does not exist yet) and no top-level `attachments`.
    Every level on the path is copied, never mutated; the caller has already
    refused a level that exists but is not an object.
    """
    result = {k: v for k, v in arguments.items() if not (isinstance(k, str) and k.lower() == "attachments")}
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


def _non_object_on_path(arguments: dict[str, Any], path: tuple[str, ...]) -> str | None:
    """The first key on ``path`` whose value exists but is not an object (a
    stringified body, a list) — writing attachments through it would replace
    the model's message with an empty one."""
    cursor: Any = arguments
    for key in path:
        nxt = _ci_get(cursor, key)
        if nxt is None:
            return None
        if not isinstance(nxt, dict):
            return key
        cursor = nxt
    return None


async def _expand_m365_artifact_attachments(
    tool: str, normalized: str, arguments: dict[str, Any]
) -> tuple[dict[str, Any], list[str]] | str:
    """Microsoft 365: expand artifact entries into Graph `fileAttachment`
    objects on the message.

    The artifact tool text tells the model to pass `attachments=[{"artifact_id"
    …}]` on "the mail send or draft tool", so the entries may arrive at the top
    level of the arguments (where Gmail takes them) or already inside the
    Graph message (`body.Message.attachments` / `body.attachments`, see
    `_M365_ARTIFACT_MESSAGE_PATHS`). Both are read, expanded together with
    whatever other attachments sit there, and written to the Graph location —
    a top-level list is moved, never left for the server to reject. An
    artifact entry anywhere else, on a tool whose request cannot carry
    attachments, or behind a body that is not an object is refused rather than
    forwarded unexpanded (the server would drop it and the model would believe
    the file went out).
    """
    path = _M365_ARTIFACT_MESSAGE_PATHS.get(normalized)
    top_raw = _ci_get(arguments, "attachments")
    nested_raw = _ci_get(_ci_walk(arguments, path), "attachments") if path else None
    stray = _has_artifact_entry(arguments, frozenset({id(top_raw), id(nested_raw)}))
    if not (stray or _has_artifact_entry(top_raw) or _has_artifact_entry(nested_raw)):
        return arguments, []
    if path is None:
        return _refuse_attachment(tool, arguments, (
            f"{tool} cannot carry attachments; send with "
            f"{_M365_PREFIX}send-mail or create a draft with them instead"
        ))
    if stray:
        return _refuse_attachment(tool, arguments, (
            "an artifact attachment must be in the top-level 'attachments' or in "
            f"the message's 'attachments' ({'.'.join(path)}) for {tool}"
        ))
    bad_level = _non_object_on_path(arguments, path)
    if bad_level is not None:
        return _refuse_attachment(tool, arguments, (
            f"'{bad_level}' must be a JSON object (not a string or list) to carry attachments"
        ))
    nested = _coerce_attachment_list(nested_raw)
    top = _coerce_attachment_list(top_raw)
    if isinstance(nested, str):
        return _refuse_attachment(tool, arguments, nested)
    if isinstance(top, str):
        return _refuse_attachment(tool, arguments, top)
    out = await _expand_artifact_entries(tool, arguments, [*nested, *top], _graph_file_entry)
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


def _block_unless_rostered(addr: str, tool: str, allow: RosterAllow) -> str | None:
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
        if _ODATA_ANNOTATION_KEY_RE.fullmatch(s):
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
    else ``from`` for a reply; for reply-all, ``replyTo`` and ``from`` both
    plus the original to/cc."""
    if normalized == _M365_SEND_DRAFT_TOOL:
        return [
            addr
            for key in ("toRecipients", "ccRecipients", "bccRecipients")
            for addr in _m365_recipient_addresses(_ci_get(message, key))
        ]
    reply_to = _m365_recipient_addresses(_ci_get(message, "replyTo"))
    sender = _m365_recipient_addresses([_ci_get(message, "from")])
    if normalized in _M365_REPLY_ALL_TOOLS:
        # Fail closed: whether Graph's reply-all addresses `replyTo`, `from`
        # or both, every one of them must be on the roster.
        implied = reply_to + sender
        for key in ("toRecipients", "ccRecipients"):
            implied += _m365_recipient_addresses(_ci_get(message, key))
        return implied
    return reply_to or sender


_Discover = Callable[[str], Awaitable[bool]]


async def _fetch_m365_json(
    session: Any, tool_name: str, arguments: dict[str, Any], discover: _Discover | None = None,
) -> dict[str, Any] | None:
    """One read through the MCP server for a gate lookup; ``None`` on any
    failure (transport error, error payload, non-object). extensible-mcp
    refuses a tool this session's searches never returned, and a chat turn
    that only searched for the reply tool never returned the lookup: then
    ``discover`` it and read once more."""
    try:
        result = await session.call_tool(
            "call_tool", {"tool_name": tool_name, "arguments": arguments},
        )
        text = result.content[0].text if result.content else ""
        if (
            discover is not None
            and isinstance(text, str)
            and text.startswith(f"Error: Tool '{tool_name}' ")
            and _UNDISCOVERED_MARKER in text
            and await discover(tool_name)
        ):
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
    session: Any, tool: str, normalized: str, arguments: dict[str, Any], discover: _Discover | None = None,
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
        {"eventId": event_id, "select": _M365_EVENT_LOOKUP_SELECT}, discover,
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


def _unsafe_id_segment(segment: str) -> bool:
    decoded = unquote(segment)
    return decoded.strip(".") == "" or any(c in decoded for c in "/\\?#")


def _check_m365_download_target(tool: str, arguments: dict[str, Any]) -> str | None:
    """Pin `download-bytes` to mail attachment bytes and OneDrive file content."""
    target = arguments.get("target")
    match = None
    if isinstance(target, str):
        match = _M365_ATTACHMENT_TARGET_RE.fullmatch(target) or _M365_DRIVE_CONTENT_TARGET_RE.fullmatch(
            target
        )
    # A dot segment (`..`, or `%2e%2e`) or an encoded slash would resolve off
    # the pinned shape.
    ids = [g for g in match.groups()[:2]] if match is not None else []
    if match is None or any(_unsafe_id_segment(segment) for segment in ids):
        return _block(
            "target", str(target)[:120], tool,
            reason=(
                f"{tool} may only fetch a mail attachment "
                "(/me/messages/{id}/attachments/{id}/$value) or a OneDrive file "
                "(/drives/{drive-id}/items/{item-id}/content, optionally ?format=pdf) "
                "— refusing."
            ),
        )
    return None


# OneDrive content types `_onedrive_content_as_text` reads, and the suffix the
# knowledge loader reads each as.
_ONEDRIVE_TEXT_TYPES: dict[str, str] = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-excel.sheet.macroenabled.12": ".xlsx",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/csv": ".csv",
}
_ONEDRIVE_TEXT_MAX_BYTES = 20 * 1024 * 1024
_ONEDRIVE_TEXT_MAX_CHARS = 200_000


async def _onedrive_content_as_text(target: str, result_text: str) -> str:
    """A `download-bytes` result for a OneDrive file, as the file's text.

    The server returns the bytes base64-encoded, which the model cannot read.
    A PDF, Word, Excel, text, Markdown or CSV file is read with the knowledge
    loader's extractors (the OneDrive folder sync's); any other type comes back
    as a note naming it, with the PDF-rendering target to retry for Office
    formats. Anything unparseable is returned as it came."""
    try:
        payload = json.loads(result_text)
    except (TypeError, ValueError):
        return result_text
    if not isinstance(payload, dict) or "error" in payload:
        return result_text
    encoded = payload.get("contentBytes")
    content_type = str(payload.get("contentType") or "").split(";")[0].strip().lower()
    if not isinstance(encoded, str):
        return result_text
    if len(encoded) > _ONEDRIVE_TEXT_MAX_BYTES * 4 // 3 + 4:
        return json.dumps({"error": "That OneDrive file is over 20 MB; it can't be read here."})
    suffix = _ONEDRIVE_TEXT_TYPES.get(content_type)
    if suffix is None:
        retry = "" if target.endswith("?format=pdf") else f" For an Office file, retry with target {target}?format=pdf."
        return json.dumps({
            "content_type": content_type or "unknown",
            "note": f"This file can't be shown as text ({content_type or 'unknown type'}).{retry}",
        })
    try:
        data = base64.b64decode(encoded, validate=False)
    except ValueError:
        return json.dumps({"error": "The OneDrive file came back unreadable."})
    from openexecutive.knowledge.drive_sync import _EXTRACT_TIMEOUT_S, _extract

    try:
        text = await asyncio.wait_for(
            asyncio.to_thread(_extract, data, suffix), timeout=_EXTRACT_TIMEOUT_S
        )
    except Exception as exc:
        logger.warning("onedrive read: text extraction failed (%s)", type(exc).__name__)
        return json.dumps({"error": f"Could not read text from that {suffix[1:]} file."})
    return json.dumps({
        "content_type": content_type,
        "text": text[:_ONEDRIVE_TEXT_MAX_CHARS],
        "truncated": len(text) > _ONEDRIVE_TEXT_MAX_CHARS,
    })


def _check_m365_drive_share(tool: str, normalized: str, arguments: dict[str, Any]) -> str | None:
    """Gate a OneDrive share like a Drive share; None when it may run.

    Everything `_check_drive_share` refuses (an off-roster email anywhere in
    the arguments, a public / whole-domain scope or flag) is refused here
    too. On top of that: an `anonymous` or `organization` scope, a link
    whose scope isn't `users`, an `owner` role, and a grantee named by
    anything other than an email."""
    blocked = _check_drive_share(tool, arguments)
    if blocked is not None:
        return blocked
    body = _ci_get(arguments, "body")
    if not isinstance(body, dict):
        # A string body is parsed by the server but would hide its grantee
        # keys from the walk below: only an object is checked, so only an
        # object is sent.
        return _refuse(
            tool, "body", "<non-object>",
            reason=f"{tool} needs its body as a JSON object — refusing.",
        )

    def walk(value: Any, depth: int = 0) -> Iterator[tuple[str, Any]]:
        if depth > _WALK_DEPTH_MAX:
            yield _TOO_DEEP, None
            return
        if isinstance(value, dict):
            for k, v in value.items():
                yield (k if isinstance(k, str) else ""), v
                yield from walk(v, depth + 1)
        elif isinstance(value, (list, tuple)):
            for v in value:
                yield from walk(v, depth + 1)

    for key, _value in walk(arguments):
        if key == _TOO_DEEP:
            return _refuse(
                tool, "share", "<unreadable>",
                reason="a share argument nested this deep cannot be checked — refusing. Flatten it.",
            )
        if _norm_key(key) in _M365_UNCHECKABLE_GRANTEE_KEYS:
            return _block(
                key, "<non-email grantee>", tool,
                reason=f"{tool} may only share with People named by email — refusing.",
            )
    # The pinned fields count only where Graph reads them: the body's top
    # level, or a top-level argument the server merges into the body. The
    # same key anywhere deeper (say inside a recipient) is a decoy that would
    # satisfy the pin while Graph applies its default, so it is refused.
    top_level = [
        (k, v) for k, v in [*body.items(), *arguments.items()]
        if isinstance(k, str) and _norm_key(k) != "body"
    ]
    for value in [v for _, v in top_level]:
        for key, _inner in walk(value):
            if _norm_key(key) in _M365_SHARE_PINNED_KEYS:
                return _refuse(
                    tool, key, "<nested>",
                    reason=f"{tool} takes {key!r} only at the top of its body — refusing.",
                )
    scopes: list[str] = []
    roles: list[Any] = []
    link_types: list[Any] = []
    require_sign_in: list[Any] = []
    for key, value in top_level:
        nk = _norm_key(key)
        if nk == "scope":
            scopes.append(_norm_share_token(value) if isinstance(value, str) else "<non-string>")
        elif nk == "roles":
            roles.extend(value if isinstance(value, list) else [value])
        elif nk == "type":
            link_types.append(value)
        elif nk == "requiresignin":
            require_sign_in.append(value)
    if normalized == _M365_DRIVE_INVITE_TOOL:
        if not require_sign_in or any(v is not True for v in require_sign_in):
            return _refuse(
                tool, "requireSignIn", str(require_sign_in[0]) if require_sign_in else "<default>",
                reason=(
                    f"{tool} must set requireSignIn to true, so only the invited "
                    "People can open the item — refusing."
                ),
            )
        if not roles or any(
            not isinstance(r, str) or _norm_share_token(r) not in _M365_INVITE_ROLES_OK for r in roles
        ):
            return _refuse(
                tool, "roles", ",".join(str(r) for r in roles) or "<default>",
                reason=f"{tool} may grant only the \"read\" or \"write\" role — refusing.",
            )
    if normalized == _M365_DRIVE_LINK_TOOL and (
        not link_types
        or any(not isinstance(t, str) or _norm_share_token(t) not in _M365_LINK_TYPES_OK for t in link_types)
    ):
        return _refuse(
            tool, "type", ",".join(str(t) for t in link_types) or "<default>",
            reason=f"{tool} may make only a \"view\" or \"edit\" link — refusing.",
        )
    for s in _iter_arg_strings(arguments):
        if isinstance(s, str) and _norm_share_token(s) in {"anonymous", "organization", "owner"}:
            return _block(
                "scope", s.strip(), tool,
                reason=(
                    f"OneDrive sharing as {s.strip()!r} is not allowed — share only "
                    "with People on the roster, as view or edit."
                ),
            )
    if normalized == _M365_DRIVE_LINK_TOOL and (not scopes or any(sc != _M365_LINK_SCOPE_OK for sc in scopes)):
        return _block(
            "scope", ",".join(scopes) or "<default>", tool,
            reason=(
                f"{tool} must set scope \"users\" (a link only people already given "
                "access can open) — refusing."
            ),
        )
    return None



def _check_m365_drive_ids(tool: str, arguments: dict[str, Any]) -> str | None:
    """Refuse a dot-only `drive-id` / `driveItem-id`. The server percent-encodes
    them, but `..` survives encoding and the URL parser then resolves it,
    walking the request out of /drives/{id}/items/{id}."""
    for norm in sorted(_M365_DRIVE_ID_KEYS):
        for value in _top_level_values(arguments, norm):
            if not isinstance(value, str) or unquote(value).strip(".") == "":
                return _refuse(
                    tool, norm, str(value)[:40],
                    reason=f"{tool} needs a real drive or item id — refusing.",
                )
    return None


def _check_m365_drive_search(tool: str, arguments: dict[str, Any]) -> str | None:
    """Pin `search-onedrive-files`' `q` to plain search words (see
    `_M365_DRIVE_SEARCH_TOOL`)."""
    values = _top_level_values(arguments, "q")
    q = values[0] if len(values) == 1 else None
    if (
        not isinstance(q, str)
        or not q.strip()
        or len(q) > _M365_DRIVE_SEARCH_Q_MAX
        or ".." in q
        or any(c in _M365_DRIVE_SEARCH_Q_BAD or ord(c) < 32 or ord(c) == 127 for c in q)
    ):
        return _refuse(
            tool, "q", (q[:40] if isinstance(q, str) else "<missing>"),
            reason=(
                f"{tool} takes plain search words only (no quotes, slashes, "
                "brackets, ?, #, % or &) — refusing."
            ),
        )
    return None


async def _check_m365_drive_write(
    session: Any, tool: str, normalized: str, arguments: dict[str, Any], discover: _Discover | None = None,
) -> str | None:
    """Keep OneDrive writes in the Executive's own drives (see
    `_M365_DRIVE_WRITE_TOOLS`). The drive written to is `drive-id`, except
    that a copy writes to its `parentReference.driveId` (a copy may read from
    a shared drive). Any `driveId` in the body must be the Executive's too.
    Fail closed when the drives can't be read."""
    body = _ci_get(arguments, "body")
    if body is not None and not isinstance(body, (dict, str)):
        return _refuse(tool, "body", "<unreadable>", reason=f"{tool} body must be an object — refusing.")
    if normalized != "microsoft_365__upload_file_content" and isinstance(body, str):
        # upload's body is the file's base64; every other body is an object
        # whose driveId must be readable.
        return _refuse(tool, "body", "<non-object>", reason=f"{tool} needs its body as a JSON object — refusing.")
    drive_ids = _top_level_values(arguments, "driveid")
    if not drive_ids or any(not isinstance(d, str) or not d.strip() for d in drive_ids):
        return _refuse(tool, "drive-id", "<missing>", reason=f"{tool} needs a drive-id — refusing.")
    drive_id = drive_ids[0]
    targets: list[str] = []
    body_drive_ids: list[Any] = []
    if isinstance(body, dict):
        body_drive_ids.extend(_iter_dict_values_for_key(body, "driveid"))
    if any(not isinstance(d, str) for d in body_drive_ids):
        return _refuse(tool, "driveId", "<unreadable>", reason=f"{tool} driveId must be a string — refusing.")
    # A destination may be named only by driveId + id: Graph's itemReference
    # also takes a path, siteId or shareId, none of which this gate can pin.
    refs = _top_level_values(arguments, "parentreference")
    if isinstance(body, dict):
        refs += _top_level_values(body, "parentreference")
    for ref in refs:
        if not isinstance(ref, dict) or any(
            not isinstance(k, str) or _norm_key(k) not in _M365_PARENT_REFERENCE_KEYS for k in ref
        ):
            return _refuse(
                tool, "parentReference", "<unpinnable>",
                reason=f"{tool} may name its destination only by driveId and id — refusing.",
            )
    ref_drive_ids = [v for ref in refs for k, v in ref.items() if _norm_key(k) == "driveid"]
    if any(not isinstance(d, str) for d in ref_drive_ids):
        return _refuse(tool, "driveId", "<unreadable>", reason=f"{tool} driveId must be a string — refusing.")
    if normalized == _M365_DRIVE_COPY_TOOL:
        if not ref_drive_ids:
            return _refuse(
                tool, "parentReference", "<missing>",
                reason=f"{tool} needs parentReference.driveId naming where the copy goes — refusing.",
            )
    else:
        targets.extend(drive_ids)
    targets.extend(body_drive_ids)
    targets.extend(ref_drive_ids)
    drives = await _fetch_m365_json(session, _M365_DRIVES_LOOKUP_TOOL, {}, discover)
    values = _ci_get(drives, "value") if drives is not None else None
    own = {
        d.get("id") for d in values if isinstance(d, dict) and isinstance(d.get("id"), str)
    } if isinstance(values, list) else set()
    if not own:
        return _refuse(
            tool, "drive-id", drive_id[:40],
            reason=f"could not read the Executive's own drives to check {tool} — refusing.",
        )
    for target in targets:
        if target not in own:
            return _refuse(
                tool, "drive-id", target[:40],
                reason=(
                    f"{tool} may only write to the Executive's own OneDrive, not a drive "
                    "someone shared with it — refusing. Save it to the Executive's "
                    "OneDrive and share it with the People who need it."
                ),
            )
    return None


def _iter_dict_values_for_key(value: Any, norm: str, depth: int = 0) -> Iterator[Any]:
    """Every value under a key that normalizes to ``norm``, at any depth;
    a too-deep value yields an unreadable marker so the caller refuses."""
    if depth > _WALK_DEPTH_MAX:
        yield _TOO_DEEP_VALUE
        return
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(k, str) and _norm_key(k) == norm:
                yield v
            yield from _iter_dict_values_for_key(v, norm, depth + 1)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _iter_dict_values_for_key(v, norm, depth + 1)

def _m365_sender_fields(value: Any, depth: int = 0) -> Iterator[tuple[str, Any]]:
    """Every ``(field, value)`` in an argument tree that names a sender or a
    reply-to: a key in `_M365_SENDER_KEYS` (any spelling, any depth), or an
    internet-header ``{"name": "Reply-To", "value": …}`` pair. Past
    `_WALK_DEPTH_MAX` yields ``(_TOO_DEEP, None)`` so the caller refuses."""
    if depth > _WALK_DEPTH_MAX:
        yield _TOO_DEEP, None
        return
    if isinstance(value, dict):
        header_name = _ci_get(value, "name")
        if isinstance(header_name, str) and _norm_key(header_name) in _M365_SENDER_KEYS:
            yield header_name, _ci_get(value, "value")
        for k, v in value.items():
            if isinstance(k, str) and _norm_key(k) in _M365_SENDER_KEYS:
                yield k, v
            yield from _m365_sender_fields(v, depth + 1)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _m365_sender_fields(v, depth + 1)


def _check_m365_sender(tool: str, arguments: dict[str, Any]) -> str | None:
    """Pin an Outlook mail write's ``from`` / ``sender`` / ``replyTo`` to the
    Executive's own address; None when it may run.

    Every string under such a field that contains ``@`` must be exactly the
    Executive's address (case-insensitive). Display names (no ``@``) pass, as
    does leaving the field out — Graph then sends as the signed-in mailbox."""
    exec_address = get_settings().exec_email_address.strip().lower()
    for field, value in _m365_sender_fields(arguments):
        if field == _TOO_DEEP:
            return _block(
                "sender", "<too-deep>", tool,
                reason=f"an argument of {tool} is nested too deep to check its sender — refusing.",
            )
        for s in _iter_arg_strings(value):
            if s == _TOO_DEEP:
                return _block(
                    field, "<too-deep>", tool,
                    reason=f"{field!r} of {tool} is nested too deep to check — refusing.",
                )
            if "@" in s and s.strip().lower() != exec_address:
                return _block(
                    field, s[:200], tool,
                    reason=(
                        f"Outlook mail goes out only from the Executive's own address "
                        f"({exec_address}); refusing {field}={s[:200]!r}. Leave it out "
                        "or use that address. Do not retry with another sender."
                    ),
                )
    return None


def _check_m365_move_destination(tool: str, arguments: dict[str, Any]) -> str | None:
    """Refuse `move-mail-message` into Deleted Items, Junk or Recoverable
    Items (a trash with no gate); None when it may run. Every
    ``destinationId`` (any spelling, any depth) is checked."""
    stack: list[tuple[Any, int]] = [(arguments, 0)]
    while stack:
        value, depth = stack.pop()
        if depth > _WALK_DEPTH_MAX:
            return _block(
                "destinationId", "<too-deep>", tool,
                reason=f"an argument of {tool} is nested too deep to check — refusing.",
            )
        if isinstance(value, dict):
            for k, v in value.items():
                if isinstance(k, str) and _norm_key(k) == "destinationid":
                    if not isinstance(v, str):
                        return _block(
                            "destinationId", repr(v)[:120], tool,
                            reason=f"{tool} needs a folder id string as destinationId — refusing.",
                        )
                    if _norm_key(v) in _M365_TRASH_FOLDERS:
                        return _block(
                            "destinationId", v[:120], tool,
                            reason=(
                                f"{tool} may not move mail to {v.strip()!r} (deleted items, "
                                "junk or recoverable items) — refusing. Leave the message "
                                "where it is or move it to a regular folder."
                            ),
                        )
                stack.append((v, depth + 1))
        elif isinstance(value, (list, tuple)):
            stack.extend((v, depth + 1) for v in value)
    return None


async def _check_m365_referenced_message(
    session: Any, tool: str, normalized: str, arguments: dict[str, Any], discover: _Discover | None = None,
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
        {"messageId": message_id, "select": _M365_MESSAGE_LOOKUP_SELECT}, discover,
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


def _drop_blocked_tools(text: object) -> Any:
    """``search_tools`` output without the tools `call_tool` always refuses
    (the Microsoft 365 account tools), so the model is never offered them.
    Output naming none of them comes back unchanged."""
    from openexecutive.workflows.tool_catalog import filter_search_results, parse_search_results

    if not isinstance(text, str):
        return text
    if not any(_is_blocked_m365_auth_tool(t.name) for t in parse_search_results(text)):
        return text
    return filter_search_results(text, lambda name: not _is_blocked_m365_auth_tool(name))


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
        text = result.content[0].text if result.content else json.dumps({"tools": []})
        return _drop_blocked_tools(text)

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
        # Act as me: once the turn has read the principal's own mail, only
        # the Google Workspace reads run (delegation.lockdown).
        from openexecutive.delegation.lockdown import outside_reach_refusal

        refused = outside_reach_refusal(
            str(tool_input.get("name") or "call_tool")[:200], tool_input, tool_name="call_tool"
        )
        if refused is not None:
            return refused
        session = self._require_session()
        arguments = tool_input.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (json.JSONDecodeError, RecursionError):
                logger.warning("call_tool: arguments was a string but not valid JSON — using empty dict")
                arguments = {}
        tool_name = tool_input.get("name", "")
        normalized = _normalize_tool_name(tool_name)
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
        if _is_blocked_m365_auth_tool(tool_name):
            return _refuse(
                tool_name, "tool", "<m365-account>",
                reason=(
                    f"{tool_name} is not available: the Microsoft 365 sign-in is "
                    "managed by the operator (ms365-mcp-launch.sh --login), not "
                    "from chat. verify-login and list-accounts still report the "
                    "signed-in account."
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
        if normalized.startswith(_M365_PREFIX) and not isinstance(arguments, dict):
            # As for Google: every Outlook gate below reads a dict.
            return _refuse(
                tool_name, "arguments", "<non-object>",
                reason="arguments must be a JSON object — refusing.",
            )
        if normalized in _GATED_M365_MAIL_TOOLS:
            blocked = _check_m365_sender(tool_name, arguments)
            if blocked is not None:
                return blocked
        if normalized in _GATED_M365_MAIL_TOOLS or normalized in _GATED_M365_CALENDAR_TOOLS:
            blocked = _check_m365_recipients(tool_name, arguments)
            if blocked is not None:
                return blocked
        if normalized in _M365_REPLY_BY_ID_TOOLS:
            blocked = await _check_m365_referenced_message(
                session, tool_name, normalized, arguments, self._discover
            )
            if blocked is not None:
                return blocked
        if normalized in _M365_EVENT_BY_ID_TOOLS:
            blocked = await _check_m365_referenced_event(
                session, tool_name, normalized, arguments, self._discover
            )
            if blocked is not None:
                return blocked
        if normalized == _M365_DOWNLOAD_TOOL:
            blocked = _check_m365_download_target(tool_name, arguments)
            if blocked is not None:
                return blocked
        if any(isinstance(k, str) and _norm_key(k) in _M365_DRIVE_ID_KEYS for k in arguments):
            blocked = _check_m365_drive_ids(tool_name, arguments)
            if blocked is not None:
                return blocked
        if normalized == _M365_DRIVE_SEARCH_TOOL:
            blocked = _check_m365_drive_search(tool_name, arguments)
            if blocked is not None:
                return blocked
        if normalized in _M365_DRIVE_SHARE_TOOLS:
            blocked = _check_m365_drive_share(tool_name, normalized, arguments)
            if blocked is not None:
                return blocked
        if normalized in _M365_DRIVE_WRITE_TOOLS:
            blocked = await _check_m365_drive_write(
                session, tool_name, normalized, arguments, self._discover
            )
            if blocked is not None:
                return blocked
        if normalized == _M365_MOVE_TOOL:
            blocked = _check_m365_move_destination(tool_name, arguments)
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
        # from every mailbox read (Google Workspace or Microsoft 365) but the
        # poller's own.
        if (
            isinstance(tool_name, str)
            and _normalize_tool_name(tool_name).startswith((_GW_PREFIX, _M365_PREFIX))
            and not _reveal_tokens.get()
        ):
            result_text = hide_roster_tokens(result_text)
        if tool_name in drive_reads.DRIVE_READ_TOOLS:
            result_text = _remember_drive_read(tool_name, arguments, result_text)
        if normalized == _M365_DOWNLOAD_TOOL and isinstance(arguments.get("target"), str):
            target = arguments["target"]
            if _M365_DRIVE_CONTENT_TARGET_RE.fullmatch(target):
                result_text = await _onedrive_content_as_text(target, result_text)
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
        from openexecutive.delegation.lockdown import outside_reach_refusal

        if (refused := outside_reach_refusal("load_mcp_server")) is not None:
            return refused
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
