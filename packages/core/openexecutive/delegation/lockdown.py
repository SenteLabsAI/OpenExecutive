"""Act as me: what a turn may still do once it has read the owner's own mail.

``ghostwrite_email`` and the reads of the owner's mailbox
(``search_my_email``, ``read_my_email``, ``my_email_awaiting_reply``) read
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
cannot be refused at dispatch without a cache miss. The lockdown lasts for
the turn; the owner's next message starts afresh, except in a conversation
that has read their mail (``sessions.mail_private``), where every turn
starts touched (``settings.pin_turn_delegation``).
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
    # Reads of the owner's own mailbox (mail_read_tools).
    "my_email_awaiting_reply",
    "read_my_email",
    "search_my_email",
    "propose_form_values",
    # The speaker's own notes (Always in the loop): a read, kept to the turn.
    "recall_history",
    "search_skills",
    "search_tools",
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
# a change (each of these is also in PRIVATE_TURN_MCP_TOOLS).
MAIL_TOUCHED_MCP_READS: frozenset[str] = frozenset({
    "google_workspace__get_events",
    "google_workspace__get_gmail_message_content",
    "google_workspace__get_gmail_thread_content",
    "google_workspace__list_calendars",
    "google_workspace__query_freebusy",
    "google_workspace__search_drive_files",
    "google_workspace__search_gmail_messages",
    # The same reads of an Outlook mailbox (EMAIL_PROVIDER=microsoft).
    "microsoft_365__get-calendar-event",
    "microsoft_365__get-calendar-view",
    "microsoft_365__get-mail-message",
    "microsoft_365__list-calendar-events",
    "microsoft_365__list-calendars",
    "microsoft_365__list-mail-folder-messages",
    "microsoft_365__list-mail-messages",
})

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
        inner = tool_input.get("name") if isinstance(tool_input, dict) else None
        return not (isinstance(inner, str) and inner in MAIL_TOUCHED_MCP_READS)
    return tool_name not in MAIL_TOUCHED_ALLOWED_TOOLS


def mail_touched_withheld_error(label: str) -> str:
    """The JSON error tool_result for a refused call (``label`` is the tool,
    or for call_tool the tool it asked for)."""
    return json.dumps({"error": f"{label} was not run. {REFUSAL}"})


def mail_touched_refusal(label: str) -> str | None:
    """For a send path's own check: the refusal when the current turn has
    read the owner's mail, else None. Never raises, and fails closed: a pin
    that can't be read counts as touched."""
    from openexecutive.delegation.settings import turn_touched_delegate_mail

    try:
        touched = turn_touched_delegate_mail()
    except Exception:
        touched = True
    return mail_touched_withheld_error(label) if touched else None
