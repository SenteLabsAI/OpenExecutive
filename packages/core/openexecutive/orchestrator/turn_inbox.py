"""Messages a person sends while the Executive is still working on a turn.

The web chat keeps its box open during a turn. A message typed then is handed
to the running turn's inbox (POST /chat/add) instead of starting a second
turn. The agent loop takes whatever has arrived at each step boundary (after a
round of tool results, before the next model call) and adds it to that round's
user message, so the answer it is writing takes the new message into account.

A message that arrives after the last step boundary (while the final answer is
being written) is never taken. The turn closes its inbox when it ends, and the
client sends anything not taken as the next turn, so nothing is lost and
nothing is answered twice.

Single-process, like the stop registry in ``api/routes/chat.py`` that owns
each inbox.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Bounds on what one turn will hold. A person adding a line or two is the use;
# these only stop a runaway client from growing the prompt without limit.
MAX_PENDING_MESSAGES = 10
MAX_MESSAGE_CHARS = 32000


@dataclass
class AddedMessage:
    # Client-minted, so the client can tell which of its queued bubbles the
    # turn took (the `message_added` event names them).
    id: str
    text: str


@dataclass
class TurnInbox:
    _pending: list[AddedMessage] = field(default_factory=list)
    # Everything the turn took, in arrival order: persisted after the turn's
    # own message and mirrored into the session history.
    taken: list[AddedMessage] = field(default_factory=list)
    closed: bool = False

    def add(self, message_id: str, text: str) -> bool:
        """Queue a message for the running turn. False once the turn has
        closed its inbox, for a duplicate id, or when the inbox is full; the
        client then sends the message as the next turn instead."""
        if self.closed or not text.strip() or len(text) > MAX_MESSAGE_CHARS:
            return False
        if len(self._pending) + len(self.taken) >= MAX_PENDING_MESSAGES:
            return False
        if any(m.id == message_id for m in (*self._pending, *self.taken)):
            return False
        self._pending.append(AddedMessage(message_id, text))
        return True

    def take(self) -> list[AddedMessage]:
        """Hand the loop everything that arrived since the last take."""
        if self.closed or not self._pending:
            return []
        out, self._pending = self._pending, []
        self.taken.extend(out)
        return out

    def close(self) -> None:
        """Refuse further messages. Anything still pending was never seen by
        the model; the client resends it as the next turn."""
        self.closed = True
        self._pending = []

    def taken_texts(self) -> list[str]:
        return [m.text for m in self.taken]


def render_added_messages(added: list[AddedMessage]) -> str:
    """The text block the loop adds after a round's tool results."""
    lines = [
        "While you were working on this, they sent another message. It is "
        "from the same person as their message above. Take it into account "
        "in this answer: if it changes or corrects what they asked, follow "
        "the newer message.",
    ]
    for m in added:
        lines.append(f"<added_message>\n{m.text}\n</added_message>")
    return "\n\n".join(lines)


def with_added(text: str, inbox: TurnInbox | None) -> str:
    """``text`` followed by the messages the turn took, for the passes that
    read the person's own words for the turn (memory, open loops)."""
    if inbox is None or not inbox.taken:
        return text
    return "\n\n".join([text, *inbox.taken_texts()]) if text else text
