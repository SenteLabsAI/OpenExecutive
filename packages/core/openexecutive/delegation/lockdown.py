"""Act as me: what a turn may not do once it has read the owner's own mail.

``ghostwrite_email`` and the reads of the owner's mailbox
(``search_my_email``, ``read_my_email``, ``read_my_email_attachment``,
``my_email_awaiting_reply``) put mail other people wrote in front of the
model. Text in it could try to steer the model ("open this link with the
invoices in it"). Most of what such text could want is already fenced on its
own: messages, invites and the Executive's own email reach only people on
the roster or addresses the speaker typed, an email written as them is only
a draft in their own mailbox until they send it themselves, facts and profile edits keep only
the speaker's words, and a workflow drafted in chat is saved only once a
person confirms it. What is not fenced is a door to the open internet. So from the round it
runs in until the turn ends, only that stays shut: no outside fetch (a URL
can carry what the mail said anywhere: ``read_document``, research, the
watchlist, ``load_mcp_server``, any gateway tool but
``PRIVATE_TURN_MCP_TOOLS``), no script or library job (it can reach either),
no workflow (it runs later, unattended, and may fetch or script), no post
to everyone (broadcasts, department messages, alerts), and no change to who
is on the roster or holds authority (archiving someone, a department head, a
roster request), since every send and approval check trusts it.

The one roster change that runs is a contact they named themselves in this
message (``speaker_named_contact``). Anything else they would want done goes
on an approval card instead of being refused outright (``propose_actions``):
the card is built in code and shows exactly what will happen, and only their
tap does it, which text in a mail can't give.

Every tool the Executive can be offered is classified here, in exactly one of
``MAIL_TOUCHED_ALLOWED_TOOLS`` or ``MAIL_TOUCHED_WITHHELD_TOOLS`` (a test fails
until a new tool is), and anything unclassified is withheld.

The dispatch guard in ``orchestrator.executive`` refuses a withheld call
without changing the offered tool list (the cached prefix stays the same all
turn), and treats a round that calls any of them as already touched,
because a round's tools run concurrently. The handlers that reach outside
check again (``outside_reach_refusal``). The server-side ``web_search``
stays, as on a turn private to the owner: it cannot be refused at dispatch
without a cache miss. The lockdown lasts for the turn
(``TurnDelegation.read_mail``) and no longer. A later turn of that
conversation stays private to its owner (``touched_mail``), and its history
carries only their words and the Executive's replies, never the mail a tool
returned: their next message is them asking again, having read the reply.
"""
from __future__ import annotations

import json
import re
from typing import Any

# What runs once the turn has read the owner's mail: everything but a door
# to the open internet or to everyone. Each send here is fenced on its own.
MAIL_TOUCHED_ALLOWED_TOOLS: frozenset[str] = frozenset({
    # Reads and analysis.
    "ask_about_person",
    "consult_specialist",
    "find_alerts",
    "get_artifact",
    "list_artifacts",
    "list_department_goals",
    "list_open_loops",
    "list_people",
    "list_saved_tools",
    "list_watchlist",
    "list_workflows",
    "load_skill",
    "lookup_person",
    "search_skills",
    "search_tools",
    "recall_history",
    # The owner's own mailbox (mail_read_tools, delegation_tools).
    "ghostwrite_email",
    "my_email_awaiting_reply",
    "my_email_read_before",
    "read_my_email",
    "read_my_email_attachment",
    "search_my_email",
    # Messages and invites: only people on the roster (_guard_outbound,
    # the roster) and attendees by person id (calendar_tools).
    "message_person",
    "send_discord_dm",
    "send_slack_dm",
    "send_telegram_message",
    "create_calendar_event",
    "create_instant_meeting",
    "cancel_calendar_event",
    # A reminder to the speaker alone, as fixed text with no links.
    "remind_me",
    # Cards and drafts a person approves before anything happens.
    "propose_actions",
    "propose_form_values",
    "draft_workflow",
    # A document kept for the person this turn is for, never published on a
    # turn private to the principal (artifact_tools.handle_draft_artifact).
    "draft_artifact",
    # Facts and profile edits keep only the speaker's own words (fact_tools).
    "remember_fact",
    "forget_fact",
    "update_company_profile",
    # The speaker's own bookkeeping.
    "ack_alert",
    "assign_open_loop",
    "close_open_loop",
    "remove_watchlist_entry",
    # Only a contact the speaker named this turn (speaker_named_contact).
    "upsert_person",
    # Only PRIVATE_TURN_MCP_TOOLS: reads, and sends whose every recipient the
    # gateway checks (mail_touched_withholds).
    "call_tool",
})

MAIL_TOUCHED_WITHHELD_TOOLS: frozenset[str] = frozenset({
    # Fetches of an outside address: a URL can carry the mail anywhere.
    "read_document",
    "load_mcp_server",
    "add_watchlist_entry",
    "tune_watchlist_entry",
    "run_executive_research",
    # Scripts and library jobs, which can reach either.
    "run_script",
    "run_python_job",
    # Workflows run later, unattended, and may fetch or script.
    "suggest_workflow",
    "run_workflow",
    "save_workflow",
    # Who is on the roster and who holds authority: every send and approval
    # check trusts it, so mail must not change it (contacts the speaker names
    # are the one exception, speaker_named_contact).
    "archive_person",
    "set_department_head",
    "resolve_roster_request",
    # Text that steers later turns: a follow-up's intent runs unattended as
    # a prompt when it is due, and goals and decision outcomes are shown to
    # every later turn. Mail must not write either.
    "schedule_followup",
    "create_goal",
    "update_department_goal",
    "record_decision_outcome",
    # Posts to everyone, with no recipient to check.
    "send_department_message",
    "send_company_broadcast",
    "create_alert",
})

REFUSAL = (
    "This turn read the user's own mail, so until it ends nothing opens a link "
    "or outside address, runs a script or workflow, posts to everyone, sets "
    "a follow-up, goal or decision outcome, or changes who is on the roster or "
    "holds authority. "
    "Tell the user it will work if they ask again in their next message. "
    "Do not retry it in this turn."
)

# The roster change a mail-reading turn can still offer: a card the person
# approves with one tap (``propose_actions``, kind ``add_contact``).
CONTACT_REFUSAL = (
    "This turn read the user's own mail, so a contact is only added here when "
    "they named the person in this message. Put it on an approval card instead "
    "(propose_actions, kind add_contact, with the name and address) so they "
    "can add them with one tap here in the chat, and tell them it is waiting. "
    "Do not retry upsert_person in this turn."
)

# What ``upsert_person`` may set on a turn that read the owner's mail: who a
# contact is and how to email them. Never the team (a seat, a sign-in), an
# approval scope, a chat account or another address that matches their mail.
_CONTACT_FIELDS = frozenset({
    "email", "full_name", "kind", "on_leave_until", "person_id",
    "preferred_channel", "response_sla_hours", "role",
})
_NAME_WORD = re.compile(r"[^\W\d_][\w\-]*", re.UNICODE)


# Words a request to add someone is made of, and other everyday words: a name
# made of these proves nothing ("add Email Billing as a contact" would match
# "find her email and add her as a contact").
_COMMON_WORDS = frozenset({
    "a", "about", "add", "added", "address", "after", "again", "all", "also",
    "am", "an", "and", "any", "are", "as", "ask", "at", "back", "be", "been",
    "before", "but", "by", "call", "can", "card", "cc", "chat", "client",
    "colleague", "company", "contact", "contacts", "could", "did", "do",
    "does", "email", "emails", "find", "firm", "for", "from", "get", "give",
    "had", "has", "have", "he", "her", "here", "hers", "him", "his", "how",
    "i", "if", "in", "inbox", "info", "into", "is", "it", "its", "just",
    "last", "let", "like", "list", "mail", "me", "message", "my", "name",
    "new", "next", "no", "not", "note", "now", "of", "on", "one", "or", "our",
    "out", "over", "people", "person", "please", "put", "reply", "roster",
    "said", "save", "say", "see", "send", "sent", "she", "should", "so",
    "some", "than", "thank", "thanks", "that", "the", "their", "them", "then",
    "there", "they", "this", "to", "too", "up", "us", "was", "we", "what",
    "when", "where", "which", "who", "will", "with", "would", "yes", "you",
    "your",
})


def _name_words(name: str) -> set[str]:
    return {
        w.lower() for w in _NAME_WORD.findall(name or "")
        if len(w) >= 3 and w.lower() not in _COMMON_WORDS
    }


# A word that asks for a roster change: "add Jamie as a contact", "save her
# address". Without one ("reply to Jamie", "any new mail from Jamie?"), a name
# alone is no request. Changing a contact they already have may also be asked
# as "update", "change" or "new" (a changed address must still be typed).
_CONTACT_ADD_WORDS = frozenset({"add", "contact", "contacts", "save"})
_CONTACT_CHANGE_WORDS = _CONTACT_ADD_WORDS | {"change", "new", "update"}


def _sent_by(email: str, senders: dict[str, str], full_name: str) -> bool:
    """Whether ``email`` sent mail this turn read under the contact's name: a
    word of ``full_name`` in the sender's display name or the address's local
    part ("priya.shah@"), so another sender can't lend them its address."""
    if email not in senders:
        return False
    local = email.split("@", 1)[0].replace(".", " ").replace("_", " ")
    return bool(_name_words(full_name) & _name_words(f"{senders[email]} {local}"))


def speaker_named_contact(tool_input: Any) -> bool:
    """Whether an ``upsert_person`` call on a turn that read the owner's
    mail is one they asked for themselves: a contact (new, or already one),
    only ``_CONTACT_FIELDS``, and a name with a word the speaker typed this
    turn (``own_words``), for the stored name too on an update, in a message
    that asks for a roster change (``_CONTACT_ADD_WORDS``). The roster is
    what every send check trusts, so mail must not add to it. A new
    contact's address is one they typed or one that sent the mail this turn
    read under the contact's name (``_sent_by``), never one a mail's text only
    mentions; on an update a changed address must be one they typed, so mail
    can't redirect someone they already have. Plain code, no model: fails
    closed."""
    from openexecutive.delegation.settings import own_words, turn_delegation, typed_addresses
    from openexecutive.orchestrator.schedule_tools import current_session

    try:
        if not isinstance(tool_input, dict) or set(tool_input) - _CONTACT_FIELDS:
            return False
        if tool_input.get("preferred_channel", "email") not in ("email", "any"):
            return False
        pinned = turn_delegation(current_session.get())
        if pinned is None:
            return False
        words = own_words(pinned.speaker_text)
        if not words:
            return False
        asked = {w.lower() for w in _NAME_WORD.findall(words)}
        new = tool_input.get("person_id") is None
        if not asked & (_CONTACT_ADD_WORDS if new else _CONTACT_CHANGE_WORDS):
            return False
        typed = _name_words(words)
        if not _name_words(str(tool_input.get("full_name", ""))) & typed:
            return False
        if tool_input.get("person_id") is None:
            if str(tool_input.get("kind", "")).strip().lower() != "contact":
                return False
            email = str(tool_input.get("email") or "").strip().lower()
            return (
                not email
                or email in typed_addresses(pinned.speaker_text)
                or _sent_by(email, pinned.mail_senders, str(tool_input.get("full_name", "")))
            )
        from openexecutive.people import store as people_store

        existing = people_store.get_person(int(tool_input["person_id"]))
        if existing is None or existing.kind != "contact" or existing.is_principal:
            return False
        if str(tool_input.get("kind", "contact")).strip().lower() != "contact":
            return False
        if not _name_words(existing.full_name) & typed:
            return False
        email = str(tool_input.get("email") or "").strip().lower()
        return not email or email == (existing.email or "").lower() or email in typed_addresses(pinned.speaker_text)
    except Exception:
        return False


def mail_touched_withholds(tool_name: str, tool_input: Any) -> bool:
    """Whether a call to ``tool_name`` is refused once the turn has read the
    owner's mail. Anything unclassified is (fail closed)."""
    if tool_name == "call_tool":
        from openexecutive.orchestrator.schedule_tools import private_turn_allows_mcp_call

        return not private_turn_allows_mcp_call(tool_input)
    if tool_name == "upsert_person":
        return not speaker_named_contact(tool_input)
    return tool_name not in MAIL_TOUCHED_ALLOWED_TOOLS


def outside_reach_refusal(label: str, tool_input: Any = None, tool_name: str = "") -> str | None:
    """For a handler that can reach an outside address: the refusal when this
    turn read the owner's mail, else None. ``tool_name`` and ``tool_input``
    (for ``call_tool``) narrow it to what ``mail_touched_withholds`` refuses;
    by default ``label`` is the tool. Never raises; fails closed."""
    from openexecutive.delegation.settings import turn_read_delegate_mail

    name = tool_name or label
    try:
        if turn_read_delegate_mail():
            return mail_touched_withheld_error(label) if mail_touched_withholds(name, tool_input) else None
        return None
    except Exception:
        return mail_touched_withheld_error(label)


def mail_touched_withheld_error(label: str) -> str:
    """The JSON error tool_result for a refused call (``label`` is the tool,
    or for call_tool the tool it asked for)."""
    why = CONTACT_REFUSAL if label == "upsert_person" else REFUSAL
    return json.dumps({"error": f"{label} was not run. {why}"})
