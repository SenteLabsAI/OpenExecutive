"""What the Executive is connected to, told to the model in cached block 0.

Without this the model learns what is connected only by calling
``search_tools``, an uncached proxy whose results are gone by the next turn
(prior turns replay as plain text), so it rediscovers the Google tools on most
turns and, when a search comes back empty, tells the user it can't see their
Gmail. ``render_connected_systems`` states what is on and pins the exact
Google tool names it uses constantly, so those are called directly.

Every input is fixed for the process (the MCP servers the gateway started
with, the integration settings in the environment) and the output is sorted,
so block 0's 1h cache stays warm.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openexecutive.config import Settings

GOOGLE_SERVER = "google_workspace"

# Server names are operator-written JSON keys rendered into the prompt; only
# plain identifiers are shown (a `_comment` key is config, not a server).
_SERVER_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")

# The Google Workspace tools the Executive reaches for on most turns, by
# service, as workspace-mcp 1.29.0 (pinned in docker/Dockerfile) names them.
# A new workspace-mcp can rename tools: re-check this list when bumping the
# pin. Apps Script tools stay out, since the gateway refuses them. Listing a
# name here never widens access: every call still runs the gateway's gates
# and a private turn's allow-list (schedule_tools.PRIVATE_TURN_MCP_TOOLS).
GOOGLE_TOOL_MANIFEST: dict[str, tuple[str, ...]] = {
    "Gmail": (
        "google_workspace__search_gmail_messages",
        "google_workspace__get_gmail_message_content",
        "google_workspace__get_gmail_thread_content",
        "google_workspace__get_gmail_attachment_content",
        "google_workspace__draft_gmail_message",
        "google_workspace__send_gmail_message",
    ),
    "Calendar": (
        "google_workspace__list_calendars",
        "google_workspace__get_events",
        "google_workspace__query_freebusy",
        "google_workspace__manage_event",
    ),
    "Drive": (
        "google_workspace__search_drive_files",
        "google_workspace__get_drive_file_content",
        "google_workspace__create_drive_file",
    ),
    "Docs": (
        "google_workspace__search_docs",
        "google_workspace__get_doc_content",
        "google_workspace__create_doc",
    ),
    "Sheets": (
        "google_workspace__read_sheet_values",
        "google_workspace__modify_sheet_values",
        "google_workspace__create_spreadsheet",
    ),
}

PINNED_GOOGLE_TOOLS: frozenset[str] = frozenset(
    name for names in GOOGLE_TOOL_MANIFEST.values() for name in names
)


def _on(flag: bool) -> str:
    return "on" if flag else "off"


def render_connected_systems(
    *,
    mcp_servers: Sequence[str],
    settings: Settings,
) -> str:
    """The ``## Connected Systems`` section of block 0.

    ``mcp_servers`` is what the running gateway was configured with (empty
    when MCP is off or the gateway failed to start, so a Google server that
    never came up is never described as connected). Act as me is named by a
    constant line that points at ``DELEGATION_ADDENDUM``, which block 0
    carries only while it is on.
    """
    servers = sorted({s for s in mcp_servers if _SERVER_NAME_RE.fullmatch(s)})
    google = GOOGLE_SERVER in servers
    others = [s for s in servers if s != GOOGLE_SERVER]
    email = settings.exec_email_address

    lines = [
        "\n\n## Connected Systems\n",
        "This is what is connected right now. Answer \"what can you do?\" or "
        "\"can you see my email?\" from this list; do not call `search_tools` "
        "to find out.\n",
    ]

    if google:
        lines.append(
            f"- **Google Workspace: connected** as your own account ({email}) — "
            "Gmail, Calendar, Drive, Docs and Sheets. Act from your own account: "
            "never ask the user which address to send from — always send, create "
            "events, and own documents from your own account. If you need the "
            "user's email or a third party's, ask for that specifically by name. "
            "You see your own mailbox, calendar and files, plus whatever others "
            "have shared with you; the user's personal inbox is theirs, not yours."
        )
        lines.append(
            "  Call these tools directly with `call_tool` — they are already "
            "available, no `search_tools` needed. Use `search_tools` only for "
            "something not listed here."
        )
        for service in sorted(GOOGLE_TOOL_MANIFEST):
            names = ", ".join(f"`{n}`" for n in GOOGLE_TOOL_MANIFEST[service])
            lines.append(f"  - {service}: {names}")
    else:
        lines.append(
            "- **Google Workspace: not connected.** You cannot read or send email, "
            "see calendars, or open Drive, Docs or Sheets files. If asked, say so "
            "plainly and point the user to the setup page (Settings → Status) rather "
            "than implying access."
        )

    if others:
        lines.append(
            "- **Other tool servers:** "
            + ", ".join(f"`{s}`" for s in others)
            + ". Find their tools with `search_tools`."
        )

    channels = [
        ("Slack", bool(settings.slack_bot_token)),
        ("Discord", bool(settings.discord_bot_token)),
        ("Telegram", bool(settings.telegram_bot_token)),
        ("Google Chat", bool(settings.google_chat_project_number)),
    ]
    lines.append(
        "- **Messaging channels:** "
        + ", ".join(f"{name} {_on(flag)}" for name, flag in channels)
        + ". Reach people only on a channel that is on, through your own "
        "messaging tools."
    )
    if settings.notion_sync_enabled:
        lines.append(
            "- **Notion: synced into your knowledge base.** You read it through "
            "retrieval, not live; you cannot edit Notion."
        )
    elif not any("notion" in s.lower() for s in others):
        # A Notion MCP server, when configured, is listed with the others.
        lines.append("- **Notion:** not connected.")
    # Constant either way, so switching Act as me changes block 0 only by
    # DELEGATION_ADDENDUM itself.
    lines.append(
        "- **Act as me:** on only if a *Writing as Someone* section follows "
        "below; without it, it is off and you cannot write email as anyone but "
        "yourself."
    )
    lines.append(
        "\nEvery send, share and invite still passes the usual checks; a refused "
        "call means that action is not allowed, not that the system is down."
    )
    return "\n".join(lines)
