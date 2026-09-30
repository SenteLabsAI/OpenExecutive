"""Reading a thread in someone's own mailbox, for a reply written as them:
what the composer is shown of it, and who the reply goes to. Shared by chat
(``orchestrator.delegation_tools``) and the inbox watcher (``delegation.inbox``).

Recipients are decided here, never by a model: a reply goes to the last
message's sender (never its ``Reply-To``), with the thread's other recipients
only on ``reply_all``.
"""
from __future__ import annotations

from typing import Any

MAX_RECIPIENTS = 10
THREAD_MESSAGES = 6
THREAD_MESSAGE_CHARS = 1500


def thread_text(thread: Any, own: str) -> str:
    """The last few messages of the thread, each only its sender's own words."""
    from openexecutive.delegation.ghostwriter import one_line
    from openexecutive.integrations.email_poller import sender_new_text

    shown = [m for m in thread.messages if "DRAFT" not in m.labels][-THREAD_MESSAGES:]
    parts = []
    for i, m in enumerate(shown, 1):
        who = one_line(m.from_name or m.from_addr, 120)
        if m.from_addr == own:
            who = f"{who} (the writer)"
        text = sender_new_text(m.text or "")[:THREAD_MESSAGE_CHARS]
        parts.append(f"[{i}] From: {who} — {one_line(m.date, 60)}\n{text}")
    return "\n\n".join(parts)


def plan_reply(thread: Any, own: str, reply_all: bool) -> dict[str, Any] | str:
    """Recipients, subject and threading headers for a reply, or why not."""
    from openexecutive.delegation.gmail import references_header

    received = [
        m for m in thread.messages
        if m.from_addr and m.from_addr != own and not {"SENT", "DRAFT"} & set(m.labels)
    ]
    if not received:
        return "There's no message from anyone else in that thread to reply to."
    last = received[-1]
    flags: list[str] = []
    if last.reply_to and last.reply_to != last.from_addr:
        flags.append("reply_to_ignored")
    if last.mailing_list:
        flags.append("mailing_list")
    newest = [m for m in thread.messages if "DRAFT" not in m.labels]
    if newest and newest[-1].from_addr == own:
        flags.append("you_replied_last")
    cc: list[str] = []
    if reply_all:
        cc = [a for a in dict.fromkeys([*last.to, *last.cc]) if a not in (own, last.from_addr)]
        if len(cc) > MAX_RECIPIENTS:
            cc = cc[:MAX_RECIPIENTS]
            flags.append("cc_trimmed")
    subject = last.subject or next((m.subject for m in thread.messages if m.subject), "")
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}".strip()
    return {
        "to": [last.from_addr],
        "cc": cc,
        "subject": subject,
        "in_reply_to": last.message_id_header or None,
        "references": references_header(last.references, last.message_id_header),
        "flags": flags,
        "last_text": last.text,
    }
