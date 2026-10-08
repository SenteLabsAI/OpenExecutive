"""Act as me: what a turn may still do once it has read the owner's own mail.

``ghostwrite_email`` and the reads of the owner's mailbox
(``search_my_email``, ``read_my_email``, ``read_my_email_attachment``,
``my_email_awaiting_reply``) read
mail other people wrote to the owner. From the
round it runs in until the turn ends, nothing that reaches anyone else runs:
no message, post, broadcast or invite, no queued or started work, no fetch of
an outside address, and no write to state other people read (the roster,
goals, skills, the watchlist, alerts, memory). Text from that mail could
otherwise steer the model into carrying it somewhere, or into acting on it.

Every tool the Executive can be offered is classified here, in exactly one of
``MAIL_TOUCHED_ALLOWED_TOOLS`` or ``MAIL_TOUCHED_WITHHELD_TOOLS`` (a test fails
until a new tool is), and anything unclassified is withheld. Through MCP
``call_tool`` only the Google Workspace reads stay (``MAIL_TOUCHED_MCP_READS``).

The dispatch guard in ``orchestrator.executive`` refuses a withheld call
without changing the offered tool list (the cached prefix stays the same all
turn), and treats a round that calls any of them as already touched,
because a round's tools run concurrently. The send paths check again
(``mail_touched_refusal``) so nothing that reaches them in such a turn runs.
The server-side ``web_search`` stays, as on a turn private to the owner: it
cannot be refused at dispatch without a cache miss. A later turn of that
conversation releases only ``CARRIED_RELEASED_TOOLS`` (messages and invites
to people on the roster) and refuses the rest of what the reading turn
refuses, and any ``call_tool`` outside ``PRIVATE_TURN_MCP_TOOLS``. The lockdown lasts for
the turn (``TurnDelegation.read_mail``); the owner's next message starts
afresh, even in a conversation that read their mail: it stays private to
them (``touched_mail``), but history carries only their words and the
Executive's replies, never the mail a tool returned.
"""
from __future__ import annotations

import json
from typing import Any

# Reads, analysis and drafting into the owner's own Gmail: nothing reaches
# anyone else and nothing is kept for later turns.
MAIL_TOUCHED_ALLOWED_TOOLS: frozenset[str] = frozenset({
    "ask_about_person",
    "consult_specialist",
    "draft_workflow",
    # A read of the briefing board. What it makes ackable stays out of
    # reach: ack_alert is withheld below.
    "find_alerts",
    "get_artifact",
    "ghostwrite_email",
    "list_artifacts",
    "list_department_goals",
    "list_open_loops",
    "list_people",
    "list_watchlist",
    "list_workflows",
    "load_skill",
    "lookup_person",
    # A reminder to the speaker alone, as fixed text with no links; nothing
    # runs when it fires (reminder_tools).
    "remind_me",
    # Reads of the owner's own mailbox (mail_read_tools).
    "my_email_awaiting_reply",
    "read_my_email",
    "read_my_email_attachment",
    "search_my_email",
    "propose_form_values",
    # The speaker's own notes (Always in the loop): a read, kept to the turn.
    "recall_history",
    "search_skills",
    "search_tools",
    # Names and descriptions of the saved tools: a local read.
    "list_saved_tools",
    # Only the reads in MAIL_TOUCHED_MCP_READS (see mail_touched_withholds).
    "call_tool",
})

MAIL_TOUCHED_WITHHELD_TOOLS: frozenset[str] = frozenset({
    # Messages, posts and invites.
    "message_person",
    "send_discord_dm",
    "send_slack_dm",
    "send_telegram_message",
    "send_department_message",
    "send_company_broadcast",
    "create_calendar_event",
    "create_instant_meeting",
    "cancel_calendar_event",
    "create_alert",
    # Work queued or started outside the turn.
    "schedule_followup",
    "suggest_workflow",
    "run_workflow",
    "save_workflow",
    "run_executive_research",
    # A sandboxed script over the gateway tools: most of what it can call
    # reaches someone, so the whole script is refused (fail closed).
    "run_script",
    # Library work on files, whose results are kept for later download.
    "run_python_job",
    # Fetches of an outside address.
    "read_document",
    "load_mcp_server",
    "add_watchlist_entry",
    "tune_watchlist_entry",
    "remove_watchlist_entry",
    # State other people read, and memory later turns read.
    "draft_artifact",
    "upsert_person",
    "archive_person",
    "set_department_head",
    "resolve_roster_request",
    "create_goal",
    "update_department_goal",
    "update_company_profile",
    "create_skill",
    "update_skill",
    "delete_skill",
    "assign_open_loop",
    "close_open_loop",
    "ack_alert",
    "record_decision_outcome",
    "remember_fact",
    "forget_fact",
})

# The Google Workspace reads call_tool may still run: never a draft, a send or
# a change (each of these is also in PRIVATE_TURN_MCP_TOOLS). Drive, Docs and
# Sheets reads stay so "check my mail against the file on the Drive" works in
# one conversation: what they return can reach no one while the lockdown holds,
# and Drive reads are not remembered on such a turn (drive_reads.may_remember).
# Accepted residual: opening a file someone else owns shows in their file
# activity, so mail that steers which file opens could signal a few bits.
MAIL_TOUCHED_MCP_READS: frozenset[str] = frozenset({
    "google_workspace__get_doc_content",
    "google_workspace__get_drive_file_content",
    "google_workspace__get_events",
    "google_workspace__get_gmail_message_content",
    "google_workspace__get_gmail_thread_content",
    "google_workspace__get_spreadsheet_info",
    "google_workspace__list_calendars",
    "google_workspace__list_docs_in_folder",
    "google_workspace__list_drive_items",
    "google_workspace__list_sheet_tables",
    "google_workspace__list_spreadsheets",
    "google_workspace__query_freebusy",
    "google_workspace__read_sheet_values",
    "google_workspace__search_docs",
    "google_workspace__search_drive_files",
    "google_workspace__search_gmail_messages",
    # The same reads of an Outlook mailbox and OneDrive (EMAIL_PROVIDER=microsoft);
    # a OneDrive file opens through download-bytes (is_onedrive_file_read).
    "microsoft_365__get-calendar-event",
    "microsoft_365__get-calendar-view",
    "microsoft_365__get-drive-item",
    "microsoft_365__get-drive-root-item",
    "microsoft_365__get-mail-message",
    "microsoft_365__list-calendar-events",
    "microsoft_365__list-calendars",
    "microsoft_365__list-drives",
    "microsoft_365__list-folder-files",
    "microsoft_365__list-mail-folder-messages",
    "microsoft_365__list-mail-messages",
    "microsoft_365__search-onedrive-files",
})

# What a later turn of a conversation that once read the owner's mail may do
# again: only what reaches people the roster allows, each checked in its
# handler (the DMs and message_person by the roster, calendar invites by
# their attendees' person ids). The turn holds the mail only as the
# Executive's own replies, but a reply can repeat text the mail planted, so
# everything else the reading turn refuses stays refused for the whole
# conversation: outside fetches and scripts (a URL could carry it anywhere),
# broadcasts and channel posts (no named recipient), queued work (it runs
# later unattended, where it may fetch and research) and writes to state
# others or later turns read (facts, skills, the profile, the roster,
# documents), which would keep the planted text. Through ``call_tool``, only
# ``PRIVATE_TURN_MCP_TOOLS``: reads, and sends whose every recipient the
# gateway checks.
CARRIED_RELEASED_TOOLS: frozenset[str] = frozenset({
    "cancel_calendar_event",
    "create_calendar_event",
    "create_instant_meeting",
    "message_person",
    "send_discord_dm",
    "send_slack_dm",
    "send_telegram_message",
})

CARRIED_REFUSAL = (
    "This conversation read the user's own mail, so the only actions that run "
    "in it are messages and invites to people on their roster: anything else "
    "could carry what the mail said elsewhere, or keep it. Tell the user to "
    "ask for it in a new conversation. Do not retry it here."
)

REFUSAL = (
    "This turn read the user's own mail, so nothing that reaches anyone else "
    "runs until it ends: no message, post, invite, queued work, outside fetch "
    "or shared change. Tell the user it can be done if they ask again in a new "
    "message. Do not retry it in this turn."
)


def mail_touched_withholds(tool_name: str, tool_input: Any) -> bool:
    """Whether a call to ``tool_name`` is refused once the turn has read the
    owner's mail. Anything unclassified is (fail closed)."""
    if tool_name == "call_tool":
        from openexecutive.orchestrator.schedule_tools import is_onedrive_file_read

        inner = tool_input.get("name") if isinstance(tool_input, dict) else None
        return not (
            (isinstance(inner, str) and inner in MAIL_TOUCHED_MCP_READS)
            or is_onedrive_file_read(tool_input)
        )
    return tool_name not in MAIL_TOUCHED_ALLOWED_TOOLS


def carried_withholds(tool_name: str, tool_input: Any) -> bool:
    """Whether a call to ``tool_name`` is refused in a conversation that once
    read the owner's mail (every later turn, not only the reading one)."""
    if tool_name == "call_tool":
        from openexecutive.orchestrator.schedule_tools import private_turn_allows_mcp_call

        return not private_turn_allows_mcp_call(tool_input)
    # Fail closed: anything neither a read nor released stays refused.
    return tool_name not in MAIL_TOUCHED_ALLOWED_TOOLS and tool_name not in CARRIED_RELEASED_TOOLS


def carried_withheld_error(label: str) -> str:
    return json.dumps({"error": f"{label} was not run. {CARRIED_REFUSAL}"})


def outside_reach_refusal(label: str, tool_input: Any = None, tool_name: str = "") -> str | None:
    """For a handler that can reach an outside address: the refusal when this
    turn read the owner's mail, or runs in a conversation that did, else
    None. ``tool_name`` and ``tool_input`` (for ``call_tool``) narrow it to
    what ``mail_touched_withholds`` / ``carried_withholds`` refuse; by
    default ``label`` is the tool. Never raises; fails closed."""
    from openexecutive.delegation.settings import (
        turn_read_delegate_mail,
        turn_touched_delegate_mail,
    )

    name = tool_name or label
    try:
        if turn_read_delegate_mail():
            return mail_touched_withheld_error(label) if mail_touched_withholds(name, tool_input) else None
        if turn_touched_delegate_mail():
            return carried_withheld_error(label) if carried_withholds(name, tool_input) else None
        return None
    except Exception:
        return mail_touched_withheld_error(label)


def mail_touched_withheld_error(label: str) -> str:
    """The JSON error tool_result for a refused call (``label`` is the tool,
    or for call_tool the tool it asked for)."""
    return json.dumps({"error": f"{label} was not run. {REFUSAL}"})


def mail_touched_refusal(label: str) -> str | None:
    """For a send path's own check: the refusal when the current turn has
    read the owner's mail, else None. Never raises, and fails closed: a pin
    that can't be read counts as touched."""
    from openexecutive.delegation.settings import turn_read_delegate_mail

    try:
        touched = turn_read_delegate_mail()
    except Exception:
        touched = True
    return mail_touched_withheld_error(label) if touched else None
