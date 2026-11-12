"""Reading a thread in someone's own mailbox, for a reply written as them:
what the composer is shown of it, and who the reply goes to. Shared by chat
(``orchestrator.delegation_tools``) and the inbox watcher (``delegation.inbox``).

Who a reply answers: the message they name (``message_id``), else the latest
message from someone they named (``to``), else the newest message from
someone else, as a mail app's Reply would. It goes to that message's sender
(never its ``Reply-To``), or to the people they named, with the message's
other recipients on ``reply_all``. It always stays in the thread. Sending is
decided by Approvals, not here.

The composer may restate what the writer already said in the thread, so which
words are theirs is never written inside the thread text, where a sender's
name, date or body could imitate it with any look-alike. ``thread_text``
labels every message alike; ``writer_said`` lists the writer's own words
apart, taken only from mail their mailbox sent (SENT, from their address),
and the composer gets it as its own block after the thread, which text inside
the thread cannot close (``ghostwriter``).
"""
from __future__ import annotations

import re
from typing import Any

MAX_RECIPIENTS = 10
THREAD_MESSAGES = 6
THREAD_MESSAGE_CHARS = 1500

_HEADER_LINE_RE = re.compile(r"^(\s*)(\[\s*\d+\s*\]\s*From\s*:)", re.IGNORECASE | re.MULTILINE)


def _quote_headers(text: str) -> str:
    """A body line that reads like one of these message headers, quoted, so
    a body can't pass for a message of its own."""
    return _HEADER_LINE_RE.sub(r"\1> \2", text)


def _shown_messages(thread: Any) -> list[Any]:
    return [m for m in thread.messages if "DRAFT" not in m.labels][-THREAD_MESSAGES:]


def mine(message: Any, own: str) -> bool:
    """Whether the writer wrote ``message``: their own mailbox sent it. A
    From header alone can name anyone."""
    return message.from_addr == own and "SENT" in message.labels


def thread_text(thread: Any, own: str) -> str:
    """The last few messages of the thread, each only its sender's own words,
    all labelled alike: nothing here says which are the writer's
    (``writer_said`` does)."""
    from openexecutive.delegation.ghostwriter import one_line
    from openexecutive.integrations.email_poller import sender_new_text
    from openexecutive.utils.prompt_blocks import plain

    parts = []
    for i, m in enumerate(_shown_messages(thread), 1):
        who = one_line(m.from_name or m.from_addr, 120)
        # Plain first, so no hidden or look-alike character dodges the quoting.
        text = _quote_headers(plain(sender_new_text(m.text or "")[:THREAD_MESSAGE_CHARS]))
        parts.append(f"[{i}] From: {who} — {one_line(m.date, 60)}\n{text}")
    return "\n\n".join(parts)


def writer_said(thread: Any, own: str) -> str:
    """What the writer themselves wrote among ``thread_text``'s messages,
    numbered as there: only mail their own mailbox sent. "" when none."""
    from openexecutive.delegation.ghostwriter import one_line
    from openexecutive.integrations.email_poller import sender_new_text

    parts = []
    for i, m in enumerate(_shown_messages(thread), 1):
        if mine(m, own):
            text = sender_new_text(m.text or "")[:THREAD_MESSAGE_CHARS]
            parts.append(f"[{i}] {one_line(m.date, 60)}\n{text}")
    return "\n\n".join(parts)


def plan_reply(
    thread: Any, own: str, reply_all: bool, *, to: list[str] | None = None, message_id: str | None = None,
) -> dict[str, Any] | str:
    """Recipients, subject and threading headers for a reply, or why not.

    ``message_id`` picks the message of the thread it answers; ``to`` (already
    checked addresses) names who it goes to and, without ``message_id``,
    answers the latest message from one of them. Without either it answers the
    newest message from someone else; with no such message, their own latest,
    to the people that went to."""
    from openexecutive.delegation.gmail import references_header

    wanted = [a for a in dict.fromkeys(a.strip().lower() for a in to or []) if a and a != own]
    if to and not wanted:
        return "A reply can't go only to them. Pass who it goes to in `to`."
    shown = [m for m in thread.messages if "DRAFT" not in m.labels]
    received = [m for m in shown if m.from_addr and m.from_addr != own and "SENT" not in m.labels]
    target = None
    if message_id:
        target = next((m for m in shown if m.id == message_id), None)
        if target is None:
            return "That message_id isn't a message of this thread. Leave it out, or pass one read_my_email listed."
    elif wanted:
        target = next((m for m in reversed(shown) if (m.from_addr or "").lower() in wanted), None)
    if target is None:
        target = received[-1] if received else (shown[-1] if shown else None)
    if target is None:
        return "There's no message in that thread to reply to."
    from_me = target.from_addr == own
    flags: list[str] = []
    if not from_me and target.reply_to and target.reply_to != target.from_addr:
        flags.append("reply_to_ignored")
    if target.mailing_list:
        flags.append("mailing_list")
    if shown and shown[-1].from_addr == own:
        flags.append("you_replied_last")
    if wanted:
        recipients = wanted
    elif from_me:
        recipients = [a for a in dict.fromkeys(target.to) if a != own]
    else:
        recipients = [target.from_addr]
    if not recipients:
        return "Nobody to send that reply to. Pass `to`."
    cc: list[str] = []
    if reply_all:
        everyone = [target.from_addr, *target.to, *target.cc]
        cc = [a for a in dict.fromkeys(everyone) if a and a != own and a not in recipients]
        if len(cc) > MAX_RECIPIENTS:
            cc = cc[:MAX_RECIPIENTS]
            flags.append("cc_trimmed")
    subject = target.subject or next((m.subject for m in thread.messages if m.subject), "")
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}".strip()
    return {
        "to": recipients[:MAX_RECIPIENTS],
        "cc": cc,
        "subject": subject,
        "in_reply_to": target.message_id_header or None,
        "references": references_header(target.references, target.message_id_header),
        "flags": flags,
        "last_text": target.text,
    }
