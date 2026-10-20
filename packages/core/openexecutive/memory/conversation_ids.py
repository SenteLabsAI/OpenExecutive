"""How a chat app's conversations are named.

A chat app hands the Executive one endless stream per person (a Telegram chat,
a Slack or Discord DM, a person's @mentions in one channel). The adapters give
that stream a fixed *base* id (``telegram:123``, ``slack:dm:U1``, …), and
``memory.conversations`` cuts it into conversations: the first keeps the base
id, later ones add ``~2``, ``~3``, … to it. Everything that has to treat the
stream as one (an approval answered after the cut, the decisions it made)
compares base ids.

No package imports: ``memory.episodic`` and ``workflows.inbound_resolver`` use these.
"""
from __future__ import annotations

from collections.abc import Callable

SEPARATOR = "~"

# The chat-app streams that are cut into conversations. Threads (Slack and
# Discord) are already one conversation each, and email has its own threads.
ROLLING_PREFIXES: tuple[str, ...] = (
    "telegram:",
    "slack:dm:",
    "slack:channel:",
    "discord:dm:",
    "discord:channel:",
)


# Further streams an installed adapter adds (`register_rolling`).
_extra_streams: list[Callable[[str], bool]] = []


def register_rolling(is_stream: Callable[[str], bool]) -> None:
    """Let an adapter outside this package have its streams cut too.
    ``is_stream`` is given a base id (never one with a ``~n`` suffix)."""
    if is_stream not in _extra_streams:
        _extra_streams.append(is_stream)


def _is_stream(base_id: str) -> bool:
    return base_id.startswith(ROLLING_PREFIXES) or any(f(base_id) for f in _extra_streams)


def is_rolling(session_id: str) -> bool:
    """Whether ``session_id`` belongs to a chat-app stream that is cut into
    conversations."""
    if _is_stream(session_id):
        return True
    base, sep, seq = session_id.rpartition(SEPARATOR)
    return bool(sep and seq.isdigit() and base and _is_stream(base))


def base_session_id(session_id: str) -> str:
    """The stream a conversation belongs to: ``telegram:123~3`` →
    ``telegram:123``. Any other id comes back unchanged."""
    base, sep, seq = session_id.rpartition(SEPARATOR)
    if sep and seq.isdigit() and base and _is_stream(base):
        return base
    return session_id


def conversation_number(session_id: str) -> int:
    """1 for a stream's first conversation (the bare base id), else its
    ``~n`` suffix."""
    if base_session_id(session_id) == session_id:
        return 1
    return int(session_id.rpartition(SEPARATOR)[2])


def conversation_id(base_id: str, number: int) -> str:
    """The id of the ``number``-th conversation in ``base_id``'s stream."""
    return base_id if number <= 1 else f"{base_id}{SEPARATOR}{number}"


def family_clause(column: str = "session_id") -> str:
    """A SQL condition matching every conversation of one stream. Bind it
    with ``family_params(base_id)``."""
    return f"({column} = ? OR substr({column}, 1, ?) = ?)"


def family_params(session_id: str) -> tuple[str, int, str]:
    base = base_session_id(session_id)
    prefix = base + SEPARATOR
    return (base, len(prefix), prefix)
