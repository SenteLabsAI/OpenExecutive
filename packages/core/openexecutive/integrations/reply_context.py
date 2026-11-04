"""The message a chat-app message answers, carried into the Executive's turn.

Every chat app lets a person answer one particular message: Telegram's and
Discord's Reply, a quote in Google Chat, a reply in a Slack thread under a
message. Without that message, "what is
she wanting to send?" reaches the Executive with nothing to say which
message "she" is in, and a reply under an older message is read as if it
answered the newest one. Each adapter reads what its app says was answered
and puts it in front of the words with ``with_context``, for the Executive's
turn and the stored history (not for a workflow's waiting question or alert
triage, which read the person's own words).
"""
from __future__ import annotations

# The most of an answered message carried into the reply, in characters.
QUOTE_MAX = 1500

EXECUTIVE = "your earlier message"
SENDER = "their own earlier message"


def whose(*, by_executive: bool, by_sender: bool, name: str = "") -> str:
    """Who wrote the answered message, as the context line names them."""
    if by_executive:
        return EXECUTIVE
    if by_sender:
        return SENDER
    name = " ".join(name.split())
    return f"an earlier message from {name}" if name else "an earlier message"


def quote(writer: str, text: str) -> str:
    """The answered message as a quoted block, or "" when it has no words."""
    text = (text or "").strip()
    if not text:
        return ""
    if len(text) > QUOTE_MAX:
        text = text[:QUOTE_MAX].rstrip() + "…"
    lines = "\n".join(f"> {line}" for line in text.splitlines())
    return f"(Replying to {writer}:)\n{lines}"


def with_context(context: str, text: str) -> str:
    """``text`` with ``context`` (from ``quote``) in front of it."""
    if not context:
        return text
    return f"{context}\n\n{text}" if text else context


def google_chat(message: dict, own: str = "") -> str:
    """A Google Chat message's quote of an earlier one (its
    ``quotedMessageMetadata``), or "". ``own`` is the Executive's own
    ``users/{id}`` in the space, when the caller knows it. A forward is not
    a reply."""
    meta = message.get("quotedMessageMetadata")
    if not isinstance(meta, dict) or str(meta.get("quoteType") or "REPLY") != "REPLY":
        return ""
    snapshot = meta.get("quotedMessageSnapshot")
    if not isinstance(snapshot, dict):
        return ""
    raw_sender = message.get("sender")
    sender = str(raw_sender.get("name") or "") if isinstance(raw_sender, dict) else ""
    quoted_by = str(snapshot.get("sender") or "")
    writer = whose(
        by_executive=bool(own) and quoted_by == own,
        by_sender=bool(sender) and quoted_by == sender,
    )
    return quote(writer, str(snapshot.get("text") or ""))
