"""``oe__answer_email_as_me``: a workflow step answers an email in its
creator's own inbox the way Handle it for me does.

The step names a thread, nothing else. The reply goes to the people the
email already went to (the sender first), never an address the step or the
email names, and is written and decided exactly as the inbox watcher does
(``inbox.answer_thread``): the classifier and the ghostwriter, which have no
tools, write it in their voice; Handle it for me's plain-code rules
(``handle_it.refusal``) and the last look for an email that steers an
assistant (``injection_check``) decide whether it goes on its own; anything
they hold waits on the usual reply card for the person. The same daily
limits, the once-per-message ledger (so the watcher never answers it twice)
and the per-thread rules apply.

Who may answer as the creator is ``workflow_email.creator_mailbox``'s rule:
only the creator's own run, with Act as me on and their mailbox connected.
Like a workflow that sends, one that answers is saved only by its creator,
verified (``workflow_email.sends``).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from openexecutive.delegation.workflow_email import REPLY_TOOL

logger = logging.getLogger(__name__)

TOOL = REPLY_TOOL
# Gmail's and Outlook's thread ids: letters, digits and a few marks.
_MAX_THREAD_ID = 300
_THREAD_ID_RE = re.compile(r"[A-Za-z0-9_\-=+/.:]+")

# The classifier, composer and last look, plus the mailbox calls.
TIMEOUT_S = 300.0
DESCRIPTION = (
    "Answer one email in the inbox of the person who created this workflow, AS "
    "them, the way their Handle it for me does: pass `thread_id` (from "
    "oe__search_my_email or oe__my_email_awaiting_reply). The reply is written in "
    "their voice from the email itself, to the people it already went to; it goes "
    "out on its own when Handle it for me's rules allow, and otherwise waits on a "
    "card for them to send. You can't choose its words or its recipients. Works "
    "only when they connected their own mailbox under Act as me."
)
INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "thread_id": {"type": "string", "description": "The email thread to answer."},
    },
    "required": ["thread_id"],
}

_OUTCOMES: dict[str, tuple[str, str]] = {
    "sent": ("sent", "Answered on its own, from their mailbox. Say in your report that it was answered."),
    "card": (
        "waiting",
        "A reply is written and waits on a card for them to send: Handle it for me's "
        "rules held it back, or it is off. Say so in your report.",
    ),
    "not_needed": ("not_needed", "This email doesn't need a reply from them."),
    "skipped": (
        "skipped",
        "This email gets no reply written: it is automatic or a list, already "
        "answered, too old, or not addressed to them.",
    ),
    "theirs_last": ("nothing_to_answer", "The last message in this thread is theirs."),
    "backlog_full": (
        "not_now", "Too many replies are already waiting on them. Say they should clear some first.",
    ),
    "busy": ("not_now", "Their inbox is being checked right now. Try again later."),
    "not_now": (
        "not_now",
        "Not answered now: it is too new, was already looked at, or a daily limit is reached.",
    ),
}


def _error(message: str) -> str:
    return json.dumps({"error": message})


async def handle_answer_email_as_me(tool_input: dict[str, Any], *, mailbox: Any = None) -> str:
    """``oe__answer_email_as_me``. ``mailbox`` replaces the creator's own
    client (tests). Never raises."""
    from openexecutive.delegation import inbox
    from openexecutive.delegation.gmail import (
        STATUS_MESSAGES,
        GmailAuthError,
        GmailError,
        GmailNotFound,
    )
    from openexecutive.delegation.workflow_email import creator_mailbox

    thread_id = str(tool_input.get("thread_id") or "").strip()
    if not thread_id:
        return _error("Pass `thread_id`: the email thread to answer.")
    if len(thread_id) > _MAX_THREAD_ID or not _THREAD_ID_RE.fullmatch(thread_id):
        return _error("That isn't a thread id from their mailbox.")
    writer = await creator_mailbox(mailbox=mailbox)
    if isinstance(writer, str):
        return writer
    person, _, client = writer
    try:
        outcome = await inbox.answer_thread(person, client, thread_id, now=datetime.now(UTC))
    except GmailNotFound:
        return _error("There's no such thread in their mailbox.")
    except GmailAuthError:
        return _error(STATUS_MESSAGES["needs_reconnect"])
    except GmailError:
        logger.warning("workflow reply: the mailbox failed", exc_info=True)
        return _error("Couldn't reach their mailbox just now. Try again later.")
    except Exception:
        logger.warning("workflow reply: answering failed", exc_info=True)
        return _error("Couldn't answer it just now. Try again later.")
    status, note = _OUTCOMES.get(outcome, _OUTCOMES["not_now"])
    return json.dumps({"status": status, "thread_id": thread_id, "note": note})
