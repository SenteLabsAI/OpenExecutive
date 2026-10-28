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
from dataclasses import dataclass
from email.utils import getaddresses
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from openexecutive.config import Settings, get_settings, mcp_config_file_present
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
# subject to the recipient allow-list. Names track workspace-mcp 2.1.0: the
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
# text, but Exchange still mails every attendee the cancellation: outside the
# typed cancel_calendar_event path (`calendar_cancel_grant`), its attendees
# are roster-checked the same way (`_check_m365_event_delete`).
_M365_EVENT_RESPONSE_TOOLS = frozenset({
    "microsoft_365__accept_calendar_event",
    "microsoft_365__decline_calendar_event",
    "microsoft_365__tentatively_accept_calendar_event",
})
_M365_EVENT_CANCEL_TOOL = "microsoft_365__cancel_calendar_event"
_M365_EVENT_BY_ID_TOOLS = _M365_EVENT_RESPONSE_TOOLS | {_M365_EVENT_CANCEL_TOOL}
_M365_EVENT_DELETE_TOOL = "microsoft_365__delete_calendar_event"
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
# Deliberately NOT allowed (workspace-mcp send_gmail_message args, and since
# 2.1.0 draft_gmail_message's too):
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
    from openexecutive.orchestrator import outside_content

    if outside_content.added_ref(addr):
        # On the roster, but only since this turn (outside_content).
        reason = f"{addr!r} {outside_content.ADDED_REFUSAL}"

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
    from openexecutive.orchestrator import outside_content
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
    # Nobody added (or given a new address) this turn until the next one: a
    # contact planted and used in one turn would pass every check below.
    everyone = [p for p in everyone if not outside_content.added_this_turn(p.id)]
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


# Act as me's ``save_my_email_attachment`` (``orchestrator.mail_file_save``)
# copies a file from the speaker's own mail into the Executive's Drive or
# OneDrive, usually on the very turn that read the mail, where the lockdown
# (``delegation.lockdown``) refuses every gateway call outside
# ``PRIVATE_TURN_MCP_TOOLS``. Its calls are built in code, never by the model:
# the folder is fixed, the file name is cleaned, the bytes come from the
# mailbox client, and the one grantee is the speaker's own address. So the
# lockdown admits exactly those calls: the very ``tool_input`` object the
# handler holds (``code_built_call``), for one of these tools only. A model's
# call is always a different object, so it can never pass as one. Every
# other gate (acting account, URL fetch, sheet formulas, the Drive / OneDrive
# share gates, own-drive writes) still runs on them as on any call.
# ``ghostwrite_email``'s ``drive_files`` (``orchestrator.drive_attach``)
# sizes and downloads the Drive files it attaches the same way: the file id is
# the model's choice, checked there against the saved files and folders, and
# the bytes go onto the principal's draft without reaching the model.
_CODE_BUILT_TOOLS = frozenset({
    "google_workspace__get_drive_file_permissions",
    "google_workspace__get_drive_file_download_url",
    "google_workspace__create_drive_folder",
    "google_workspace__create_drive_file",
    "google_workspace__manage_drive_access",
    "microsoft_365__list-drives",
    "microsoft_365__create-onedrive-folder",
    "microsoft_365__upload-file-content",
    "microsoft_365__share-drive-item",
})
_code_built: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "code_built_call", default=None
)


@contextlib.contextmanager
def code_built_call(tool_input: dict[str, Any]) -> Iterator[None]:
    """Let ``call_tool`` run this exact ``tool_input`` on a turn that read the
    owner's mail (see ``_CODE_BUILT_TOOLS``). For code that built every
    argument itself; never around a model's input."""
    token = _code_built.set(tool_input)
    try:
        yield
    finally:
        _code_built.reset(token)


def _code_built_admits(tool_input: Any) -> bool:
    granted = _code_built.get()
    return (
        granted is not None
        and granted is tool_input
        and isinstance(tool_input, dict)
        and tool_input.get("name") in _CODE_BUILT_TOOLS
    )


# A rename or move the person approved on a card (``delegation.action_cards``,
# ``drive_files``) runs as that very ``tool_input``, built in code from what
# the card showed them: the file, its new name, the folder it goes to. Their
# tap is the approval, so the own-file check (`_check_google_file_write`)
# looks only at the folder it goes to. Every other gate still runs.
_TAPPED_DRIVE_TOOLS = frozenset({"google_workspace__update_drive_file"})
_TAPPED_DRIVE_KEYS = frozenset({"fileid", "name", "addparents", "removeparents"})
_tapped_drive: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "tapped_drive_call", default=None
)


@contextlib.contextmanager
def tapped_drive_call(tool_input: dict[str, Any]) -> Iterator[None]:
    """Run this exact ``tool_input`` as a rename or move the person tapped
    Approve on (see ``_TAPPED_DRIVE_TOOLS``). For code that built every
    argument from a card; never around a model's input."""
    token = _tapped_drive.set(tool_input)
    try:
        yield
    finally:
        _tapped_drive.reset(token)


def _tapped_drive_admits(tool_input: Any) -> bool:
    granted = _tapped_drive.get()
    if granted is None or granted is not tool_input or not isinstance(tool_input, dict):
        return False
    arguments = tool_input.get("arguments")
    return (
        tool_input.get("name") in _TAPPED_DRIVE_TOOLS
        and isinstance(arguments, dict)
        and all(isinstance(k, str) and _norm_key(k) in _TAPPED_DRIVE_KEYS for k in arguments)
    )


# Files ``save_my_email_attachment`` copied from someone's own mail are theirs
# alone (``delegation.saved_files``), but they sit in the Executive's own Drive
# or OneDrive, which every turn's Drive and OneDrive tools can reach. So:
#
# - **Files.** On any turn whose speaker is not a file's owner
#   (``saved_files.viewer_person_id``), a call whose arguments name a recorded
#   file id is refused before it runs (an open, a download, a share, a copy, a
#   move), and a result that names one loses that entry (a JSON list item) or
#   line (workspace-mcp's one line per file), and any id still left is masked.
# - **The folder.** Sharing, moving or listing the folder would reach every
#   file in it, so on every turn, the owner's included, a model call naming a
#   folder a save made is refused, and so is a OneDrive call addressing a path
#   through a folder of that name ("root:/From your email/…", percent-encoded
#   or not). The folder's own entry is dropped from results and its id masked
#   elsewhere. Only the save's own code-built calls (``code_built_call``) touch
#   it, and those skip this filter: they name nothing the model chose.
#
# Ids are matched raw and percent-decoded (a raw Graph path is decoded by
# Graph), and the record is read again after each call, so a file a save
# recorded while a listing ran is hidden from that listing too.
#
# Limits, honestly: it matches ids as text, so it hides a file only where its
# id appears (a listing, a link); a result that names the file without its id
# (an activity feed, a notification quoting its title) still shows the name. A
# copy someone makes of it is a new file and isn't covered. Someone signed in
# to the Executive's account outside the app sees everything. The lookup
# fails closed for the file tools (``_FILE_TOOL_RE``: Drive, OneDrive, Docs,
# Sheets, Slides, Excel): when the tables can't be read they are refused, while mail
# and calendar tools run unfiltered.
_FILE_TOOL_RE = re.compile(
    r"drive|folder|file|download_bytes|doc|sheet|slide|presentation|excel|workbook"
)
_SAVED_FILE_REFUSAL = (
    "{tool} was not run: that file is private to the person it was saved for "
    "and isn't available in this conversation. Do not retry."
)
_SAVED_FOLDER_REFUSAL = (
    "{tool} was not run: the folder of files saved from people's own email "
    "can't be opened, shared, moved or listed from chat; each file there is "
    "shared with its owner alone. Do not retry."
)


@dataclass(frozen=True)
class _SavedView:
    """What this turn may not see or name: other people's saved files, and
    every folder the saves made."""

    files: frozenset[str] = frozenset()
    folders: frozenset[str] = frozenset()

    def __bool__(self) -> bool:
        return bool(self.files or self.folders)


def _saved_files_hidden(normalized: str) -> _SavedView | str:
    """What this turn may not see, or the refusal when that can't be read
    for a file tool. Only Google Workspace and Microsoft 365 tools are
    filtered."""
    if not normalized.startswith((_GW_PREFIX, _M365_PREFIX)):
        return _SavedView()
    from openexecutive.delegation import saved_files

    try:
        files, folders = saved_files.protected(saved_files.viewer_person_id())
        return _SavedView(files, folders)
    except Exception:
        logger.warning("saved files: couldn't read which files are private", exc_info=True)
        if _FILE_TOOL_RE.search(normalized.partition("__")[2]):
            return json.dumps({
                "error": (
                    "Couldn't check which files are private just now, so "
                    f"{normalized[:200]} was not run. Try again in a moment."
                ),
            })
        return _SavedView()


def _arguments_text(arguments: Any) -> str:
    try:
        return json.dumps(arguments, default=str)
    except (TypeError, ValueError, RecursionError):
        return str(arguments)


def _argument_spellings(arguments: Any) -> tuple[str, ...]:
    """The arguments as text, raw and percent-decoded until stable: a raw
    Graph path (``download-bytes``) is decoded by Graph, so "%30%31…" names
    the same item as "01…"."""
    spellings = [_arguments_text(arguments)]
    for _ in range(3):
        decoded = unquote(spellings[-1])
        if decoded == spellings[-1]:
            break
        spellings.append(decoded)
    return tuple(spellings)


def _names_any(arguments: Any, ids: frozenset[str]) -> bool:
    return any(
        item_id in text for text in _argument_spellings(arguments) for item_id in ids
    )


def _names_saved_file(arguments: Any, hidden: frozenset[str]) -> bool:
    return bool(hidden) and _names_any(arguments, hidden)


def _names_saved_folder(normalized: str, arguments: Any, folders: frozenset[str]) -> bool:
    """Whether a model call names a folder the saves made, by id anywhere in
    its arguments, or (OneDrive) by a path through a folder of that name."""
    if folders and _names_any(arguments, folders):
        return True
    if not normalized.startswith(_M365_PREFIX) or not _FILE_TOOL_RE.search(normalized.partition("__")[2]):
        # Only a file tool takes a path: a mail subject or event title that
        # happens to read "Re: From your email: …" isn't one.
        return False
    from openexecutive.delegation.saved_files import FOLDER_NAME

    wanted = " ".join(FOLDER_NAME.casefold().split())
    for value in _iter_arg_strings(arguments):
        if value is _TOO_DEEP:
            return True
        plain = " ".join(unquote(value).casefold().split())
        if ":" in plain and wanted in plain:
            return True
    return False


def _prune_saved(value: Any, present: list[str], depth: int = 0) -> Any:
    """``value`` without the list entries that name one of ``present``."""
    if depth > _WALK_DEPTH_MAX:
        return value
    if isinstance(value, list):
        kept = []
        for item in value:
            try:
                text = json.dumps(item, default=str)
            except (TypeError, ValueError, RecursionError):
                text = str(item)
            if not any(file_id in text for file_id in present):
                kept.append(_prune_saved(item, present, depth + 1))
        return kept
    if isinstance(value, dict):
        return {k: _prune_saved(v, present, depth + 1) for k, v in value.items()}
    return value


def _hide_saved_files(text: str, hidden: frozenset[str]) -> str:
    """``text`` with every entry naming one of ``hidden`` dropped: a JSON
    result's list items, then any line still naming one, then any id still
    left masked. Unchanged when it names none."""
    present = [file_id for file_id in hidden if file_id in text]
    if not present:
        return text
    parsed = _parsed_json(text)
    if parsed is not None:
        text = json.dumps(_prune_saved(parsed, present))
    lines = [line for line in text.splitlines() if not any(file_id in line for file_id in present)]
    text = "\n".join(lines)
    for file_id in present:
        text = text.replace(file_id, "[hidden]")
    return text or "No files found."


def _drop_folder_entries(value: Any, folders: frozenset[str], depth: int = 0) -> Any:
    """``value`` without the list entries that are one of ``folders`` (a dict
    whose ``id`` is one)."""
    if depth > _WALK_DEPTH_MAX:
        return value
    if isinstance(value, list):
        return [
            _drop_folder_entries(v, folders, depth + 1) for v in value
            if not (isinstance(v, dict) and any(
                isinstance(k, str) and k.lower() == "id" and x in folders for k, x in v.items()
            ))
        ]
    if isinstance(value, dict):
        return {k: _drop_folder_entries(v, folders, depth + 1) for k, v in value.items()}
    return value


def _hide_saved_folders(text: str, folders: frozenset[str]) -> str:
    """``text`` without the saves' folders: their own entries dropped (a JSON
    list item whose id is one, a workspace-mcp line "(ID: <id>"), and the id
    masked anywhere else (a file's parent), so the file itself still shows."""
    present = [folder_id for folder_id in folders if folder_id in text]
    if not present:
        return text
    parsed = _parsed_json(text)
    if parsed is not None:
        text = json.dumps(_drop_folder_entries(parsed, frozenset(present)))
    lines = [line for line in text.splitlines() if not any(f"ID: {f}" in line for f in present)]
    text = "\n".join(lines)
    for folder_id in present:
        text = text.replace(folder_id, "[hidden]")
    return text or "No files found."


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


def _mail_recipients(*values: Any) -> list[str]:
    """Every address in Gmail-style ``to`` / ``cc`` / ``bcc`` values (a
    string or a list of them)."""
    found: list[str] = []
    for value in values:
        items = value if isinstance(value, list) else [value]
        found += [a for _, a in getaddresses([v for v in items if isinstance(v, str)]) if a]
    return found


def _m365_recipients(message: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for key in ("toRecipients", "ccRecipients", "bccRecipients"):
        entries = message.get(key)
        for entry in entries if isinstance(entries, list) else []:
            address = entry.get("emailAddress") if isinstance(entry, dict) else None
            if isinstance(address, dict) and isinstance(address.get("address"), str):
                found.append(address["address"])
    return found


def _with_disclosure(
    tool_name: Any, normalized: str, arguments: dict[str, Any], *, bare: bool = False,
) -> dict[str, Any] | str:
    """The Executive's own mail send or draft with the AI disclosure line
    under its body (``orchestrator.disclosure``), or a JSON refusal for HTML
    that could hide it. Runs after every gate, on a copy; a shape it doesn't
    know is left as it is. ``bare``: the acknowledgement to a new sender,
    whose line names nobody."""
    from openexecutive.orchestrator.disclosure import applies, hides_what_follows, with_mail_line

    def html_refusal(key: str, text: str, recipients: list[str] | None) -> str | None:
        if applies(recipients) and hides_what_follows(text):
            return _refuse(
                str(tool_name), key, "<hidden-markup>",
                reason=(
                    "this HTML has markup that can hide text after it (a comment, a style or "
                    "script block, or text styled out of sight), so the line saying it was "
                    "written by an AI could be hidden — refusing. Send it as plain text, or "
                    "as HTML without that markup."
                ),
            )
        return None

    if tool_name in _GATED_GMAIL_TOOLS:
        out = dict(arguments)
        recipients = _mail_recipients(out.get("to"), out.get("cc"), out.get("bcc"))
        if isinstance(out.get("html_body"), str) and out["html_body"]:
            refused = html_refusal("html_body", out["html_body"], recipients)
            if refused is not None:
                return refused
            out["html_body"] = with_mail_line(out["html_body"], recipients=recipients, as_html=True, bare=bare)
        if isinstance(out.get("body"), str) and out["body"]:
            as_html = str(out.get("body_format") or "").lower() == "html"
            refused = html_refusal("body", out["body"], recipients) if as_html else None
            if refused is not None:
                return refused
            out["body"] = with_mail_line(out["body"], recipients=recipients, as_html=as_html, bare=bare)
        return out
    if normalized not in _GATED_M365_MAIL_TOOLS or normalized == _M365_SEND_DRAFT_TOOL:
        return arguments
    body = arguments.get("body")
    if not isinstance(body, dict):
        return arguments
    body = dict(body)
    # A reply or forward carries its words as Comment, which Graph puts in
    # the HTML reply; a new message or a draft as a Graph message (under
    # Message for send_mail).
    if isinstance(body.get("Comment"), str) and body["Comment"]:
        refused = html_refusal("Comment", body["Comment"], None)
        if refused is not None:
            return refused
        body["Comment"] = with_mail_line(body["Comment"], as_html=True)
        return {**arguments, "body": body}
    wrapped = isinstance(body.get("Message"), dict)
    message = body["Message"] if wrapped else body
    content = message.get("body")
    if not (isinstance(content, dict) and isinstance(content.get("content"), str) and content["content"]):
        return arguments
    as_html = str(content.get("contentType") or "").lower() == "html"
    recipients = _m365_recipients(message)
    refused = html_refusal("body", content["content"], recipients) if as_html else None
    if refused is not None:
        return refused
    content = {
        **content,
        "content": with_mail_line(content["content"], recipients=recipients, as_html=as_html),
    }
    message = {**message, "body": content}
    return {**arguments, "body": {**body, "Message": message} if wrapped else message}


def _pin_calendar_notifications(arguments: dict[str, Any]) -> dict[str, Any]:
    """Keep a manage_event call that named no attendees from emailing the
    event's existing guests.

    The attendee gate checks only the ``attendees`` argument. An update that
    leaves it out keeps the event's current guest list — which may hold
    people off the roster — and Google mails every guest the new title,
    description or time unless ``send_updates`` is "none". So such a call is
    forced to "none": the change lands on the calendar and nobody is emailed
    text the gate never saw. A call that passes attendees had them checked
    and keeps its own ``send_updates``; an rsvp carries no new text; a
    create without attendees has nobody to mail, so the pin is moot there.

    A delete is pinned too: Google mails every guest the cancellation, and
    the guest list is one the gate never saw. Only the typed
    ``cancel_calendar_event`` path, which cancels a meeting the Executive
    booked with rostered attendees, keeps its notice
    (``calendar_cancel_grant``).
    """
    action = str(_arg(arguments, "action") or "").strip().lower()
    if action == "rsvp":
        return arguments
    if action == "delete":
        granted = _calendar_cancel.get()
        event_ids = [v for v in _arg_values(arguments, "event_id") if v is not None]
        if granted is not None and event_ids == [granted]:
            return arguments
        return _without_notifications(arguments)
    supplied = [v for v in _arg_values(arguments, "attendees") if v is not None]
    if any(not (isinstance(v, list) and not v) for v in supplied):
        return arguments
    if _arg_values(arguments, "send_updates") == ["none"]:
        return arguments
    logger.info("manage_event without attendees: send_updates pinned to none")
    return _without_notifications(arguments)


def _without_notifications(arguments: dict[str, Any]) -> dict[str, Any]:
    """``arguments`` with ``send_updates`` pinned to "none". Every spelling
    goes, so no `sendUpdates: "all"` survives beside the pin."""
    if _arg_values(arguments, "send_updates") == ["none"]:
        return arguments
    pinned = {k: v for k, v in arguments.items()
              if not (isinstance(k, str) and _norm_key(k) == "sendupdates")}
    pinned["send_updates"] = "none"
    return pinned


# The one manage_event delete that may mail its guests: the typed
# cancel_calendar_event of a meeting the Executive booked (its attendees were
# roster-checked when it was booked). Set only around that call, to the event
# id, by ``calendar_cancel_grant``.
_calendar_cancel: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "calendar_cancel_grant", default=None
)


@contextlib.contextmanager
def calendar_cancel_grant(event_id: str) -> Iterator[None]:
    """Let a manage_event delete of ``event_id`` keep its cancellation notice."""
    token = _calendar_cancel.set(event_id)
    try:
        yield
    finally:
        _calendar_cancel.reset(token)


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


class GatewayCallTimeout(TimeoutError):
    """A call through the MCP gateway got no answer within
    ``MCP_TOOL_TIMEOUT_S``. The workspace server may still have run it."""


def _tool_timeout_s() -> float:
    """``MCP_TOOL_TIMEOUT_S``; a settings stub without it (tests patch
    ``get_settings`` here) gets config's default."""
    return float(getattr(get_settings(), "mcp_tool_timeout_s", 120.0))


async def _session_call(
    session: Any, name: str, arguments: dict[str, Any], timeout: float | None = None,
) -> Any:
    """``session.call_tool`` with the per-call ceiling. Without one, a
    workspace server that never answers (a hung Google or Microsoft request,
    a stuck child process) held whatever awaited it for good: the email
    poller, which handles one message at a time, a brief, a chat turn. The
    call is cancelled on our side; the server may still finish it."""
    if timeout is None:
        timeout = _tool_timeout_s()
    try:
        return await asyncio.wait_for(session.call_tool(name, arguments), timeout=timeout)
    except TimeoutError as exc:
        tool = arguments.get("tool_name") if name == "call_tool" else name
        logger.warning("MCPGateway: %s got no answer within %.0fs", str(tool)[:200], timeout)
        raise GatewayCallTimeout(f"{str(tool)[:200]} got no answer within {timeout:.0f}s") from exc


async def _fetch_m365_json(
    session: Any, tool_name: str, arguments: dict[str, Any], discover: _Discover | None = None,
) -> dict[str, Any] | None:
    """One read through the MCP server for a gate lookup; ``None`` on any
    failure (transport error, error payload, non-object). extensible-mcp
    refuses a tool this session's searches never returned, and a chat turn
    that only searched for the reply tool never returned the lookup: then
    ``discover`` it and read once more."""
    try:
        result = await _session_call(
            session, "call_tool", {"tool_name": tool_name, "arguments": arguments},
        )
        text = result.content[0].text if result.content else ""
        if (
            discover is not None
            and isinstance(text, str)
            and text.startswith(f"Error: Tool '{tool_name}' ")
            and _UNDISCOVERED_MARKER in text
            and await discover(tool_name)
        ):
            result = await _session_call(
                session, "call_tool", {"tool_name": tool_name, "arguments": arguments},
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


async def _check_m365_event_delete(
    session: Any, tool: str, arguments: dict[str, Any], discover: _Discover | None = None,
) -> str | None:
    """Exchange mails a deleted meeting's cancellation to every attendee, a
    guest list the arguments never name. The typed cancel of that one event
    (`calendar_cancel_grant`) passes; any other delete runs only when the
    event has no attendees or all of them are on the roster. Fail-closed."""
    # Every spelling, and exactly one: two could check one event and delete
    # another.
    values = _arg_values(arguments, "eventId")
    event_id = values[0] if len(values) == 1 else None
    if not isinstance(event_id, str) or not event_id.strip():
        return _block(
            "eventId", "<missing>", tool,
            reason=f"{tool} needs one eventId so its attendees can be roster-checked — refusing.",
        )
    if _calendar_cancel.get() == event_id:
        return None
    event = await _fetch_m365_json(
        session, _M365_EVENT_LOOKUP_TOOL,
        {"eventId": event_id, "select": _M365_EVENT_LOOKUP_SELECT}, discover,
    )
    if event is None:
        return _block(
            "eventId", event_id, tool,
            reason=f"could not read event {event_id!r} to roster-check who {tool} would notify — refusing.",
        )
    allow = _roster_allow_set()
    for addr in _m365_recipient_addresses(_ci_get(event, "attendees")):
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


# Google Docs, Sheets, Slides, Forms and Drive writes land only in files and
# folders the Executive's own account owns, or the company's own
# (`_company_item`): owned by someone on a company domain or kept on a shared
# drive only the company organizes, and open to nobody outside the company.
# The twin of `_M365_DRIVE_WRITE_TOOLS`. A stranger can share a doc, sheet or
# folder with the Executive's account, and planted text could then have it
# append company data there ("add the Q3 numbers to this sheet"), with
# nothing shared and no roster check; a company file shared outside the
# company is the same open door. Every Google write that names a file
# (`_GOOGLE_WRITTEN_FILE_KEYS`) or a destination folder
# (`_GOOGLE_DESTINATION_KEYS`) is checked; a write is any tool whose name
# starts with a write verb (`outside_content.gateway_call_reads`). Sharing has
# its own gate (`_GATED_DRIVE_TOOLS`), and mail and calendar name no file.
# Reading what others shared stays open. A rename or move the person approved
# on a card (`tapped_drive_call`) checks only where the file goes.
_GOOGLE_WRITTEN_FILE_KEYS = frozenset({"documentid", "spreadsheetid", "presentationid", "formid", "fileid"})
_GOOGLE_DESTINATION_KEYS = frozenset({"folderid", "parentfolderid", "addparents"})
# Writes whose file argument is only read: the copy or PDF goes to the
# destination folder, which is checked (and a copy with none stays beside its
# source, owned by whoever owns that).
_GOOGLE_SOURCE_ONLY_TOOLS = frozenset({
    "google_workspace__copy_drive_file",
    "google_workspace__export_doc_to_pdf",
})
_GOOGLE_OWNER_LOOKUP_TOOL = "google_workspace__get_drive_file_permissions"
# The one "Owners:" line of the lookup's answer; the address in the last
# parentheses is the owner's (a display name comes before it, so it can't
# stand in for the address).
_GOOGLE_OWNERS_LINE_RE = re.compile(r"Owners: .* \(([^()\s]+@[^()\s]+)\)")


def _google_ids(arguments: dict[str, Any], keys: frozenset[str]) -> list[Any] | None:
    """Every id under one of ``keys`` (any spelling); a comma-separated
    string (``add_parents``) or a list gives each id. None when one isn't a
    string."""
    ids: list[Any] = []
    for k, v in arguments.items():
        if not isinstance(k, str) or _norm_key(k) not in keys or v is None:
            continue
        values = v if isinstance(v, list) else str(v).split(",") if isinstance(v, str) else [v]
        for item in values:
            if not isinstance(item, str):
                return None
            if item.strip():
                ids.append(item.strip())
    return ids


async def _google_item_text(session: Any, file_id: str, discover: _Discover | None) -> str | None:
    """The owner lookup's answer for Drive item ``file_id``, or None when it
    can't be read."""
    arguments = {"file_id": file_id}
    try:
        result = await _session_call(
            session, "call_tool", {"tool_name": _GOOGLE_OWNER_LOOKUP_TOOL, "arguments": arguments},
        )
        text = result.content[0].text if result.content else ""
        if (
            discover is not None
            and isinstance(text, str)
            and _UNDISCOVERED_MARKER in text
            and await discover(_GOOGLE_OWNER_LOOKUP_TOOL)
        ):
            result = await _session_call(
                session, "call_tool", {"tool_name": _GOOGLE_OWNER_LOOKUP_TOOL, "arguments": arguments},
            )
            text = result.content[0].text if result.content else ""
    except Exception:
        logger.warning("google gate: owner lookup failed", exc_info=True)
        return None
    if not isinstance(text, str) or _is_error_payload(text):
        return None
    return text


def _google_owner_of(text: str) -> str | None:
    """The owner address in an owner lookup's answer, lowercased, or None when
    it doesn't name exactly one owner line (a shared drive item has none)."""
    # A file's name is the line above and can hold a line break: more than one
    # owners line is refused, never guessed between.
    lines = [line for line in text.splitlines() if line.startswith("Owners: ")]
    if len(lines) != 1:
        return None
    match = _GOOGLE_OWNERS_LINE_RE.fullmatch(lines[0].rstrip())
    return match.group(1).strip().lower() if match else None


async def _google_file_owner(session: Any, file_id: str, discover: _Discover | None) -> str | None:
    """The owner address of Drive item ``file_id``, lowercased, or None when
    it can't be read or doesn't name exactly one owner line (a shared drive
    item has none)."""
    text = await _google_item_text(session, file_id, discover)
    return None if text is None else _google_owner_of(text)


# One permission line of the owner lookup (workspace-mcp's
# ``format_permission_info``): "    - User: a@b.com (writer) [id: 1]", with
# " | inherited from: …" after it on a shared drive. Any other line in the
# list (an unknown kind) counts as someone outside the company.
_GOOGLE_PERMISSION_RE = re.compile(
    r"\s+- (?:(?P<kind>User|Group): (?P<addr>[^\s()]+@[^\s()]+)|Domain: (?P<domain>[^\s()]+)"
    r"|(?P<anyone>Anyone) with the link) \((?P<role>\w+)\)(?: \[id: [^\]]*\])?(?: \|.*)?"
)


@dataclass(frozen=True)
class _DriveItem:
    """What the owner lookup says of one Drive item: its owner (None on a
    shared drive), whether it is on a shared drive, and who can open it as
    ``(kind, address or domain, role)``, kind "user", "group", "domain",
    "anyone" or "other"."""

    owner: str | None
    shared_drive: bool
    permissions: tuple[tuple[str, str, str], ...]


def _parse_drive_item(text: str, item_id: str) -> _DriveItem | None:
    """Read ``text`` (the owner lookup for ``item_id``) from its last
    ``ID: <item_id>`` line on. Everything before that line is the item's name
    (the first line), which whoever named the file chose, line breaks and
    all; everything after it is Drive's own. None when it can't be read."""
    lines = text.splitlines()
    anchor = f"ID: {item_id}"
    starts = [i for i, line in enumerate(lines) if line.rstrip() == anchor]
    if not starts:
        return None
    rest = [line.rstrip() for line in lines[starts[-1] + 1:]]
    # Drive's own lines in their fixed order: a line break in the type or an
    # owner's display name (chosen by whoever uploaded or owns it) shifts
    # them, and is refused.
    if len(rest) < 4 or not (
        rest[0].startswith("Type: ") and rest[1].startswith("Parents: ")
        and rest[2].startswith("Owners: ") and rest[3].startswith("Size: ")
    ):
        return None
    if sum(1 for line in rest if line.startswith("Owners: ")) != 1:
        return None
    owner: str | None = None
    if rest[2] != "Owners: None available":
        match = _GOOGLE_OWNERS_LINE_RE.fullmatch(rest[2])
        if match is None:
            return None
        owner = match.group(1).strip().lower()
    shared_drive = any(line.startswith("Shared Drive ID: ") for line in rest)
    # The permission list: exactly one header, right after its count, then
    # exactly that many entries and a blank line.
    headers = [i for i, line in enumerate(rest) if line == "  Permissions:"]
    permissions: list[tuple[str, str, str]] = []
    if headers:
        if len(headers) != 1:
            return None
        at = headers[0]
        count = re.fullmatch(r"  Number of permissions: (\d+)", rest[at - 1]) if at else None
        if count is None:
            return None
        entries = rest[at + 1:at + 1 + int(count.group(1))]
        if len(entries) != int(count.group(1)) or rest[at + 1 + len(entries):at + 2 + len(entries)] != [""]:
            return None
        for line in entries:
            found = _GOOGLE_PERMISSION_RE.fullmatch(line) if line.startswith("    - ") else None
            if found is None:
                permissions.append(("other", "", ""))
            elif found.group("addr"):
                # A group can hold anyone, so only a user counts as one of the company's.
                kind = "user" if found.group("kind") == "User" else "group"
                permissions.append((kind, found.group("addr").lower(), found.group("role").lower()))
            elif found.group("domain"):
                permissions.append(("domain", found.group("domain").lower(), found.group("role").lower()))
            else:
                permissions.append(("anyone", "", found.group("role").lower()))
    elif "  No additional permissions (private file)" not in rest:
        return None
    return _DriveItem(owner=owner, shared_drive=shared_drive, permissions=tuple(permissions))


def _company_domains_with(exec_address: str) -> frozenset[str]:
    """The company's own email domains (``people.identity.company_domains``),
    and the Executive's own unless it is free mail."""
    from openexecutive.people.identity import FREEMAIL_DOMAINS, company_domains

    domains = set(company_domains())
    _, _, exec_domain = exec_address.rpartition("@")
    if exec_domain and exec_domain not in FREEMAIL_DOMAINS:
        domains.add(exec_domain)
    return frozenset(domains)


def _company_item(item: _DriveItem, exec_address: str, domains: frozenset[str]) -> bool:
    """Whether a Drive item is the company's own: owned by the Executive or
    someone on a company domain, or on a shared drive whose every organizer
    is; and nobody outside the company can open it (every user and domain
    it is shared with is the company's; never a group, which can hold
    anyone, nor "anyone with the link"). A file owned by someone else must show who can open it: the list
    is hidden from a reader, and a hidden list proves nothing."""

    def ours(address: str) -> bool:
        return address == exec_address or address.rpartition("@")[2] in domains

    if item.owner is not None:
        if not ours(item.owner):
            return False
        if item.owner != exec_address and not item.permissions:
            return False
    elif not item.shared_drive or not any(role == "organizer" for _, _, role in item.permissions):
        return False
    for kind, who, _role in item.permissions:
        if kind == "user" and ours(who):
            continue
        if kind == "domain" and who in domains:
            continue
        return False
    return True


async def _google_company_items(
    session: Any, ids: Iterable[str], discover: _Discover | None = None,
) -> set[str]:
    """The ids in ``ids`` that are the company's own (``_company_item``).
    Fails closed: an item whose lookup can't be read is not."""
    exec_address = get_settings().exec_email_address.strip().lower()
    domains: frozenset[str] | None = None
    out: set[str] = set()
    for item_id in ids:
        text = await _google_item_text(session, item_id, discover)
        if text is None:
            continue
        # The Executive's own files too: one it shared outside the company
        # holds outside words like any other.
        item = _parse_drive_item(text, item_id)
        if item is None:
            continue
        if domains is None:
            domains = _company_domains_with(exec_address)
        if _company_item(item, exec_address, domains):
            out.add(item_id)
    return out


# Google tools whose name starts with one of these, and names no write verb
# after it, only read; every other name is checked as a write (fail closed:
# a new write verb is covered).
_GOOGLE_READ_VERBS = frozenset({
    "check", "debug", "get", "inspect", "list", "query", "read", "search",
})


def _google_reads_only(tool: str) -> bool:
    from openexecutive.orchestrator.outside_content import _WRITE_VERBS

    words = tool.split("__")[-1].lower().replace("-", "_").split("_")
    return words[0] in _GOOGLE_READ_VERBS and not any(w in _WRITE_VERBS for w in words[1:])


# What a refused Drive write tells the model to do instead: a rename or move
# the person asked for goes on a card they tap (``action_cards``,
# ``drive_files``), which runs it with only the destination checked.
_GOOGLE_WRITE_CARD_HINT = (
    " If the person asked you to rename or move these files, put that on an "
    "approval card (propose_actions, kind drive_files) for them to tap, and tell "
    "them it is waiting. Do not ask them to share it again: sharing won't change this."
)


async def _check_google_file_write(
    session: Any, tool: str, arguments: dict[str, Any], discover: _Discover | None = None,
    *, tapped: bool = False,
) -> str | None:
    """Keep Google writes in files and folders the Executive's own account
    owns or the company's own (see `_GOOGLE_WRITTEN_FILE_KEYS`). Fail closed
    when an owner can't be read. ``tapped``: a rename or move the person
    approved on a card, so only the folder it goes to is checked."""
    if _google_reads_only(tool) or tool in _GATED_DRIVE_TOOLS:
        return None
    files = [] if tool in _GOOGLE_SOURCE_ONLY_TOOLS or tapped else _google_ids(arguments, _GOOGLE_WRITTEN_FILE_KEYS)
    folders = _google_ids(arguments, _GOOGLE_DESTINATION_KEYS)
    if files is None or folders is None:
        return _refuse(tool, "file_id", "<unreadable>", reason=f"{tool} ids must be strings — refusing.")
    # "root" is the Executive's own My Drive (the server's default folder).
    targets = list(dict.fromkeys(t for t in [*files, *folders] if t.lower() != "root"))
    if not targets:
        return None
    exec_address = get_settings().exec_email_address.strip().lower()
    domains: frozenset[str] | None = None
    for target in targets[:10]:
        text = await _google_item_text(session, target, discover)
        owner = None if text is None else _google_owner_of(text)
        if owner == exec_address:
            continue
        item = None if text is None else _parse_drive_item(text, target)
        if owner is None and item is None:
            return _refuse(
                tool, "file_id", target[:60],
                reason=f"could not read who owns {target[:60]!r} to check {tool} — refusing." + _GOOGLE_WRITE_CARD_HINT,
            )
        if item is not None:
            if domains is None:
                domains = _company_domains_with(exec_address)
            if _company_item(item, exec_address, domains):
                continue
        return _refuse(
            tool, "file_id", target[:60],
            reason=(
                f"{tool} may only write to files and folders the Executive's own "
                "Google account owns, or the company's own: owned by someone at the "
                "company or on its shared drive, and not shared with anyone outside "
                "it. This one is not — refusing. Save it to a new file in the "
                "Executive's Drive and share that with the People who need it."
                + _GOOGLE_WRITE_CARD_HINT
            ),
        )
    if len(targets) > 10:
        return _refuse(tool, "file_id", "<too-many>", reason=f"{tool} names too many files — refusing.")
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


# Outlook replies that answer one message by its id (a draft only waits).
_M365_SENT_REPLY_TOOLS = frozenset({
    "microsoft_365__reply_mail_message",
    "microsoft_365__reply_all_mail_message",
})


def _sent_to(tool_name: str, normalized: str, arguments: dict[str, Any]) -> set[str]:
    """The lowercased to/cc addresses of a Gmail or Outlook send; empty for
    any other tool (an Outlook reply names none in its arguments)."""
    out: set[str] = set()
    if tool_name == "google_workspace__send_gmail_message":
        for field in _OUTBOUND_CONTEXT_RECIPIENT_FIELDS:
            value = arguments.get(field)
            items = value if isinstance(value, list) else [value]
            for _name, addr in getaddresses([s for s in items if isinstance(s, str)]):
                if addr.strip():
                    out.add(addr.strip().lower())
    elif normalized == _M365_RECORD_SEND_TOOL:
        message = _ci_get(_ci_get(arguments, "body"), "message")
        for key in ("toRecipients", "ccRecipients"):
            for addr in _m365_recipient_addresses(_ci_get(message, key)):
                if addr.strip():
                    out.add(addr.strip().lower())
    return out


async def _clear_replaced_drafts(tool_name: str, normalized: str, arguments: dict[str, Any]) -> None:
    """The Executive just sent an email itself: take this conversation's
    unsent chat drafts of it (same people, same subject) out of the writer's Drafts
    (``delegation.redrafts.clear_after_send``). Never raises."""
    from openexecutive.delegation.settings import DelegationOverride
    from openexecutive.orchestrator.schedule_tools import current_session

    session = current_session.get()
    if session is None:
        return
    try:
        sent = _sent_to(tool_name, normalized, arguments)
        if not sent:
            return
        from openexecutive.delegation.redrafts import clear_after_send

        if tool_name == "google_workspace__send_gmail_message":
            subject = arguments.get("subject")
        else:
            subject = _ci_get(_ci_get(_ci_get(arguments, "body"), "message"), "subject")
        override = getattr(session, "delegation_override", None)
        client = override.gmail if isinstance(override, DelegationOverride) else None
        await clear_after_send(
            str(session.session_id), sent, subject=subject if isinstance(subject, str) else "", client=client,
        )
    except Exception:
        logger.exception("clear_replaced_drafts: could not clear drafts (non-fatal)")


def _note_mail_sent(tool_name: str, normalized: str, arguments: dict[str, Any]) -> None:
    """Mark on the conversation (``Session.mail_sent_to`` /
    ``mail_replied_ids``) who a send the mailbox accepted went to, so the
    email poller can tell whether an inbound email got its reply
    (``integrations.unanswered_mail``). Nothing without a live session, and
    never raises: the send already happened."""
    from openexecutive.orchestrator.schedule_tools import current_session

    session = current_session.get()
    if session is None:
        return
    try:
        if tool_name == "google_workspace__send_gmail_message" or normalized == _M365_RECORD_SEND_TOOL:
            session.mail_sent_to.update(_sent_to(tool_name, normalized, arguments))
        elif normalized in _M365_SENT_REPLY_TOOLS:
            message_id = _ci_get(arguments, "messageId")
            if isinstance(message_id, str) and message_id:
                session.mail_replied_ids.add(message_id)
    except Exception:
        logger.exception("note_mail_sent: could not record the send (non-fatal)")


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


@dataclass(frozen=True)
class PinnedToolsProbe:
    """What startup priming learned about the pinned Google tools.

    One value, assigned once, so a reader never has to reconcile two attributes
    that were written separately: ``missing`` is the names the probe could not
    find, or ``None`` when the probe itself failed and found out nothing. The
    gateway's attribute being ``None`` means the probe has not finished (or
    never ran, because Google is not configured).
    """

    missing: tuple[str, ...] | None


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
        # What startup priming found, for /health and the Setup status page.
        # None until priming finishes, so a probe that crashed can never read
        # as "every pinned tool is present" — only PinnedToolsProbe(missing=())
        # asserts that.
        self.pinned_tools: PinnedToolsProbe | None = None
        # Servers whose sign-in has died (``mark_needs_reconnect``): the
        # Connected Systems prompt section says so instead of "connected",
        # and the Setup page shows it. Cleared by ``mark_reconnected`` or by
        # the next call to one of that server's tools that answers without
        # an authorization error.
        self.needs_reconnect: set[str] = set()
        # MCP_TOOL_TIMEOUT_S, read on first use (`_call_timeout`) and kept:
        # `get_settings()` builds a fresh Settings on every call.
        self._tool_timeout_s: float | None = None

    def _call_timeout(self, settings: Settings | None = None) -> float:
        """The per-call ceiling, read once per gateway (from ``settings``
        when the caller already holds a read)."""
        if self._tool_timeout_s is None:
            self._tool_timeout_s = (
                float(getattr(settings, "mcp_tool_timeout_s", 120.0))
                if settings is not None
                else _tool_timeout_s()
            )
        return self._tool_timeout_s

    def mark_needs_reconnect(self, server: str, reason: str | None = None) -> bool:
        """Note that ``server``'s grant no longer works. True when this is
        news (the first report), so the caller can log or notify once."""
        if server in self.needs_reconnect:
            return False
        self.needs_reconnect.add(server)
        logger.warning(
            "MCP server %s needs reconnecting%s; the prompt now says so",
            server, f": {reason[:200]}" if reason else "",
        )
        return True

    def mark_reconnected(self, server: str) -> bool:
        """The grant works again (a reconnect, or a call that succeeded)."""
        if server not in self.needs_reconnect:
            return False
        self.needs_reconnect.discard(server)
        logger.info("MCP server %s is connected again", server)
        return True

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
        result = await _session_call(session, "search_tools", args, self._call_timeout())
        text = result.content[0].text if result.content else json.dumps({"tools": []})
        return _drop_blocked_tools(text)

    async def _discover(self, tool_name: str, settings: Settings | None = None) -> bool:
        """Run the exact-name search that registers ``tool_name`` with
        extensible-mcp's discovered-tools filter (it only runs a tool one of
        this session's ``search_tools`` results returned, and remembers it for
        the session's life), the way ``tool_catalog.resolve`` does. True when
        the search returned it. Best-effort: never raises.

        A caller that probes many names passes ``settings`` so the whole run
        shares one read: ``get_settings()`` builds a fresh ``Settings`` every
        call, so reading it here cost one environment read per missing tool.
        Left out (the ``call_tool`` retry, which probes one name), it is read
        only on the path that needs it.
        """
        from openexecutive.workflows.tool_catalog import parse_search_results

        query = re.sub(r"[_\-]+", " ", tool_name).strip()
        try:
            text = await self.search_tools({"query": query, "top_k": 10})
            found = any(info.name == tool_name for info in parse_search_results(text))
        except Exception as exc:
            logger.warning(
                "MCPGateway: discovering %s failed (%s)", tool_name, type(exc).__name__
            )
            return False
        if found:
            return True
        if settings is None:
            settings = get_settings()
        # Both causes, because the old text named only the rename and sent the
        # operator to the manifest for what was really a service-list setting.
        logger.warning(
            "MCPGateway: pinned tool %s not found by search — not loaded under "
            "WORKSPACE_MCP_TOOLS=%s / WORKSPACE_MCP_TOOL_TIER=%s, or renamed by "
            "workspace-mcp? (prompts/connected_systems.GOOGLE_TOOL_MANIFEST)",
            tool_name,
            settings.workspace_mcp_tools,
            settings.workspace_mcp_tool_tier,
        )
        return False

    async def prime_pinned_tools(self) -> list[str]:
        """Discover the pinned Google tools this install actually loaded, so the
        model's direct ``call_tool`` on one works without a search of its own.
        Returns the names not found, a drift signal. Run once at startup when
        Google is configured.

        Also records the outcome on ``self.pinned_tools`` for ``/health`` and
        the Setup status page, which is the authoritative reading: the return
        value cannot express "the probe itself failed", and nothing awaits it.

        Probes the same filtered set the prompt advertises
        (``connected_systems.pinned_google_tools``), not the whole manifest: with
        a ``WORKSPACE_MCP_TOOLS`` subset or the ``core`` tier, the rest were
        never registered, so probing them produced a warning per tool every boot
        and told the operator to look for a rename that had not happened."""
        from openexecutive.prompts.connected_systems import pinned_google_tools

        settings = get_settings()
        pinned = pinned_google_tools(settings)
        self._call_timeout(settings)
        try:
            missing = [n for n in sorted(pinned) if not await self._discover(n, settings)]
        except asyncio.CancelledError:
            # Shutdown cancels this task (api.main). Leave pinned_tools unset:
            # the probe did not finish, and the process is going away.
            raise
        except BaseException:
            # Not `except Exception`: anyio wraps a session failure in
            # BaseExceptionGroup as soon as one member is a BaseException, and
            # that group is not an Exception — the same reasoning the gateway's
            # own startup guard in api.main spells out. Letting one through
            # here would leave pinned_tools None forever, which the Setup
            # status page would show as "still checking" rather than a failure.
            logger.warning("MCPGateway: priming the pinned Google tools failed", exc_info=True)
            self.pinned_tools = PinnedToolsProbe(missing=None)
            return []
        self.pinned_tools = PinnedToolsProbe(missing=tuple(missing))
        logger.info(
            "MCPGateway: primed %d pinned Google tools (%d missing)",
            len(pinned) - len(missing), len(missing),
        )
        return missing

    async def call_tool(self, tool_input: dict[str, Any]) -> str:
        # Act as me: once the turn has read the principal's own mail, only
        # PRIVATE_TURN_MCP_TOOLS run: reads, and sends whose every recipient
        # the roster checks below (delegation.lockdown).
        from openexecutive.delegation.lockdown import outside_reach_refusal

        refused = outside_reach_refusal(
            str(tool_input.get("name") or "call_tool")[:200], tool_input, tool_name="call_tool"
        )
        code_built = _code_built_admits(tool_input)
        if refused is not None and not code_built:
            return refused
        # A turn another AI app asked over Open Executive's MCP server runs
        # gateway reads only (schedule_tools.MCP_CLIENT_MCP_TOOLS), whether or
        # not it may act: a mail send or draft goes on an approval card. The
        # chat loop already refuses anything else; this holds for any other
        # path.
        from openexecutive.orchestrator.schedule_tools import (
            current_session,
            mcp_client_acting,
            mcp_client_withheld_error,
            mcp_client_withholds,
            mcp_rules_turn,
        )

        turn_session = current_session.get()
        if mcp_rules_turn(turn_session) and mcp_client_withholds("call_tool", tool_input):
            return mcp_client_withheld_error(
                str(tool_input.get("name") or "call_tool")[:200],
                acting=mcp_client_acting(turn_session),
            )
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
        # Files saved from someone's own mail are theirs alone, and their
        # folder is no model's to name (see `_saved_files_hidden`). The save's
        # own code-built calls are the one exception.
        hidden = _SavedView() if code_built else _saved_files_hidden(normalized)
        if isinstance(hidden, str):
            return hidden
        if _names_saved_file(arguments, hidden.files):
            return json.dumps({"error": _SAVED_FILE_REFUSAL.format(tool=str(tool_name)[:200])})
        if not code_built and _names_saved_folder(normalized, arguments, hidden.folders):
            return json.dumps({"error": _SAVED_FOLDER_REFUSAL.format(tool=str(tool_name)[:200])})
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
            blocked = await _check_google_file_write(
                session, tool_name, arguments, self._discover, tapped=_tapped_drive_admits(tool_input),
            )
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
        if normalized == _M365_EVENT_DELETE_TOOL:
            blocked = await _check_m365_event_delete(session, tool_name, arguments, self._discover)
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
        # Last, once every gate passed: the AI disclosure line, on what goes
        # out only; what is recorded below stays the words the model wrote.
        wire_arguments = arguments
        if tool_name in _GATED_GMAIL_TOOLS or normalized in _GATED_M365_MAIL_TOOLS:
            # The roster acknowledgement (the one send to an address nobody
            # checked) gets the line that names nobody, as the ack itself.
            grant = _roster_ack.get()
            disclosed = _with_disclosure(
                tool_name, normalized, arguments, bare=bool(grant is not None and grant["used"]),
            )
            if isinstance(disclosed, str):
                return disclosed
            wire_arguments = disclosed
        wire_input = {"tool_name": tool_input["name"], "arguments": wire_arguments}
        try:
            result = await _session_call(session, "call_tool", wire_input, self._call_timeout())
            result_text = result.content[0].text if result.content else json.dumps({"result": None})
            # A pinned tool called before startup priming reached it: the prompt
            # told the model to call it directly, so discover it and retry once.
            # The refused call ran nothing, and every gate above has already
            # passed these same arguments.
            if _is_undiscovered_refusal(tool_name, result_text) and await self._discover(tool_name):
                result = await _session_call(session, "call_tool", wire_input, self._call_timeout())
                result_text = result.content[0].text if result.content else json.dumps({"result": None})
        except GatewayCallTimeout:
            # An error payload, not a raise: the model reads it like any tool
            # error, and every caller that checks for one (`_is_error_payload`)
            # treats the call as failed. Not a failed send, though: the server
            # may still have run it, so say to check before calling again.
            # Nothing below runs: no outbound linkage or attachment row for a
            # send that may not have happened, and no grant-health reading.
            return json.dumps({
                "error": (
                    f"{str(tool_name)[:200]} did not answer within "
                    f"{self._call_timeout():.0f}s. It may still have run: "
                    "check its effect before calling it again."
                ),
                "timed_out": True,
            })
        # On the final result, retry or not: roster answer tokens are hidden
        # from every mailbox read (Google Workspace or Microsoft 365) but the
        # poller's own.
        if (
            isinstance(tool_name, str)
            and _normalize_tool_name(tool_name).startswith((_GW_PREFIX, _M365_PREFIX))
            and not _reveal_tokens.get()
        ):
            result_text = hide_roster_tokens(result_text)
        if not code_built:
            # Read again: a save that landed while this call ran (its file
            # already in a listing, recorded only once its upload returned)
            # is hidden too.
            after = _saved_files_hidden(normalized)
            if isinstance(after, str):
                return json.dumps({"error": (
                    "Couldn't check which files are private just now, so the "
                    f"result of {normalized[:200]} was withheld. It did run: "
                    "check its effect before calling it again."
                )})
            hidden = _SavedView(hidden.files | after.files, hidden.folders | after.folders)
        if hidden:
            # Before anything below keeps or reads the result.
            result_text = _hide_saved_files(result_text, hidden.files)
            result_text = _hide_saved_folders(result_text, hidden.folders)
        self._note_grant_health(tool_name, result_text)
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
        # A plain-text "Error …" result (FastMCP, extensible-mcp) is no send
        # either: marking it would hide the unanswered-email card.
        if not _is_error_payload(result_text) and not result_text.lstrip().lower().startswith("error"):
            _note_mail_sent(tool_name, normalized, arguments)
            await _clear_replaced_drafts(tool_name, normalized, arguments)
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

    def _note_grant_health(self, tool_name: object, result_text: str) -> None:
        """Read a tool's answer for a dead grant (``grant_failure``) and keep
        ``needs_reconnect`` in step: an authorization failure marks the
        server, an answer that is not one clears it."""
        server = _server_of(tool_name)
        if server is None:
            return
        failure = grant_failure(result_text)
        if failure is not None:
            self.mark_needs_reconnect(server, failure)
        elif server in self.needs_reconnect:
            self.mark_reconnected(server)

    async def company_drive_items(self, ids: Iterable[str]) -> set[str]:
        """The Google Drive ids in ``ids`` that are the company's own
        (``_company_item``), so reading them is not reading outside content
        (``outside_content.vet_reads``). Empty when it can't tell."""
        try:
            session = self._require_session()
        except Exception:
            return set()
        try:
            return await _google_company_items(session, ids, self._discover)
        except Exception:
            logger.warning("company_drive_items: lookup failed", exc_info=True)
            return set()

    async def drive_item_names(self, ids: Iterable[str]) -> dict[str, str]:
        """Each Drive item's current name, as the owner lookup gives it (for
        a card to show; whoever named the file chose it). Missing when it
        can't be read."""
        out: dict[str, str] = {}
        try:
            session = self._require_session()
        except Exception:
            return out
        for item_id in ids:
            text = await _google_item_text(session, item_id, self._discover)
            first = (text or "").split("\n", 1)[0]
            if first.startswith("File: "):
                out[item_id] = first[len("File: "):].strip()
        return out

    async def tapped_drive_change(self, file_id: str, *, name: str = "", move_to: str = "") -> str | None:
        """Rename and/or move one Drive file as the person approved it on a
        card (``tapped_drive_call``): a move takes it out of every folder it
        is in now. None when it worked, else what went wrong in plain words."""
        arguments: dict[str, Any] = {"file_id": file_id}
        if name:
            arguments["name"] = name
        if move_to:
            session = self._require_session()
            text = await _google_item_text(session, file_id, self._discover)
            item_lines = (text or "").splitlines()
            anchor = f"ID: {file_id}"
            starts = [i for i, line in enumerate(item_lines) if line.rstrip() == anchor]
            # Drive's own Parents line, second after the id, in a lookup
            # whose lines are all where Drive puts them.
            after = item_lines[starts[-1] + 1:starts[-1] + 3] if starts else []
            parents_line = after[1].rstrip() if (
                len(after) == 2 and after[1].startswith("Parents: ") and _parse_drive_item(text or "", file_id)
            ) else None
            if parents_line is None:
                return "Couldn't read which folder it is in now."
            parents = [
                p.strip() for p in parents_line[len("Parents: "):].split(",")
                if re.fullmatch(r"[A-Za-z0-9_-]{10,200}", p.strip())
            ]
            arguments["add_parents"] = move_to
            if parents:
                arguments["remove_parents"] = ",".join(p for p in parents if p != move_to)
                if not arguments["remove_parents"]:
                    del arguments["remove_parents"]
        tool_input = {"name": "google_workspace__update_drive_file", "arguments": arguments}
        with tapped_drive_call(tool_input):
            result = await self.call_tool(tool_input)
        text = result if isinstance(result, str) else ""
        head = text.lstrip()
        if head.startswith("{"):
            try:
                parsed = json.loads(head)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict) and parsed.get("error"):
                return str(parsed["error"])[:300]
        if not head or head.lower().startswith(("error", "tool error")):
            return (head or "Drive didn't answer.")[:300]
        return None

    async def load_mcp_server(self, tool_input: dict[str, Any]) -> str:
        from openexecutive.delegation.lockdown import outside_reach_refusal

        if (refused := outside_reach_refusal("load_mcp_server")) is not None:
            return refused
        session = self._require_session()
        url: str = tool_input["url"]
        if not url.startswith("https://"):
            return json.dumps({"error": "load_mcp_server requires an HTTPS URL"})
        result = await _session_call(
            session,
            "load_mcp_server",
            {"name": tool_input["name"], "url": url},
            self._call_timeout(),
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


# What a Google or Microsoft tool answers once the stored grant is dead:
# workspace-mcp relays google-auth's "invalid_grant: Token has been expired or
# revoked", the Graph server a 401 / InvalidAuthenticationToken. A rate limit,
# a missing scope on one call or a 403 on a single file is not a dead grant,
# so none of those match.
_GRANT_FAILURE_RE = re.compile(
    r"invalid_grant|token has been (?:expired|revoked)|refresh ?token (?:is )?(?:expired|revoked|invalid)"
    r"|invalidauthenticationtoken|(?:^|[^\w])401(?:[^\w]|$).{0,80}unauthori[sz]ed"
    r"|unauthori[sz]ed.{0,80}(?:^|[^\w])401(?:[^\w]|$)|reauthentication (?:is )?required"
    r"|credentials (?:have |are )?(?:expired|been revoked)|invalid authentication credentials"
    r"|\bUNAUTHENTICATED\b",
    re.IGNORECASE | re.DOTALL,
)
# How much of a result is read for it: an error comes first; a long mailbox
# listing quoting the words "invalid_grant" is not one.
_GRANT_FAILURE_SCAN_CHARS = 4000


# How a failed call reads as text (the gateway's gates and the MCP client
# render errors these ways; workflows/action_step.py reads the same). A
# successful answer that happens to open with "error" ("Errors in Q3…") is
# not one, and a model-echoed argument never starts a result.
_ERROR_TEXT_PREFIXES = ("Error: ", "Error calling tool ", "Tool error:", "Error executing tool ")


def grant_failure(result_text: object) -> str | None:
    """The authorization failure a tool result reports, in a line, or None
    when the result is not one. Only an error payload counts (``{"error":
    …}``, or text that opens with an error marker), never a successful
    answer that merely quotes those words."""
    if not isinstance(result_text, str) or not result_text:
        return None
    head = result_text[:_GRANT_FAILURE_SCAN_CHARS]
    error: str | None = None
    try:
        parsed = json.loads(head) if head.lstrip().startswith("{") else None
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        if "error" not in parsed:
            return None
        error = json.dumps(parsed.get("error"))[:_GRANT_FAILURE_SCAN_CHARS]
    elif head.lstrip().startswith(_ERROR_TEXT_PREFIXES):
        error = head
    if error is None:
        return None
    match = _GRANT_FAILURE_RE.search(error)
    if match is None:
        return None
    start = max(match.start() - 40, 0)
    return " ".join(error[start:match.end() + 80].split())[:200]


def _server_of(tool_name: object) -> str | None:
    """The server a prefixed tool name belongs to (``google_workspace__x`` →
    ``google_workspace``), or None."""
    if not isinstance(tool_name, str):
        return None
    normalized = _normalize_tool_name(tool_name)
    server, sep, _ = normalized.partition("__")
    return server if sep and server else None


def gateway_server_names(gateway: object) -> tuple[str, ...]:
    """The servers ``gateway`` started with, or () for no gateway (or a
    stand-in without the attribute)."""
    names = getattr(gateway, "server_names", ())
    if not isinstance(names, tuple | list):
        return ()
    return tuple(n for n in names if isinstance(n, str))


def gateway_reconnect_needed(gateway: object) -> tuple[str, ...]:
    """The servers ``gateway`` has marked as needing a reconnect, sorted; ()
    for no gateway or a stand-in without the attribute."""
    names = getattr(gateway, "needs_reconnect", ())
    if not isinstance(names, set | frozenset | tuple | list):
        return ()
    return tuple(sorted(n for n in names if isinstance(n, str)))


# The supervisor that starts the gateway and retries while it is down
# (orchestrator/gateway_boot.py). Set by the app lifespan next to the active
# gateway, so code that is about to need the gateway can ask for a start now
# (``ensure_active_gateway``) and the Setup page can say why it is down.
_supervisor: Any = None


def set_gateway_supervisor(supervisor: Any) -> None:
    global _supervisor
    _supervisor = supervisor


def get_gateway_supervisor() -> Any:
    return _supervisor


async def ensure_active_gateway() -> MCPGateway | None:
    """The active gateway, after a start attempt if it is down and the
    supervisor is not already in one. The lazy re-check before a scheduled
    delivery: with no supervisor (tests, a process started without the
    lifespan) this is just ``get_active_gateway``."""
    if _active_gateway is not None or _supervisor is None:
        return _active_gateway
    try:
        await _supervisor.ensure()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("MCP gateway re-check failed")
    return _active_gateway


def gateway_problem(*, with_error: bool = True) -> str | None:
    """Why there is no active gateway, in one line, or None while there is
    one. Without a supervisor the answer is only that it is not running.
    ``with_error=False`` leaves the failure's text out (a page every team
    member reads)."""
    if _active_gateway is not None:
        return None
    if _supervisor is None:
        return "the MCP gateway is not running"
    problem = _supervisor.problem(with_error=with_error)
    return str(problem) if problem else "the MCP gateway is not running"
