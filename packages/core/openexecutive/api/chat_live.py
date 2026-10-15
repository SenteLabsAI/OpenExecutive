"""Live updates for a chat more than one person is in.

A chat its owner shared (``session_store.shared_with``) is read and written by
everyone in it at once. Each page that has it open holds one stream
(``GET /sessions/{id}/live``) and this module feeds those streams:

- ``turn_started``: someone sent a message; who, and what it says.
- the reply as it is written: ``chunk``, ``thinking``, ``activity``,
  ``action_taken``, as the sender's own stream carries them.
- ``turn_ended``: the turn is saved. Pages read the chat again, which brings
  the reply's id, its sources and anything added while it worked.
- ``typing``: someone is writing a message.
- ``people``: someone was added or left. ``removed`` ends the stream of
  whoever lost the chat, ``deleted`` everyone's.
- ``resync``: a page fell behind (its queue filled). Its stream ends; it
  connects again and reads the chat.

Every event names the chat's running turn by ``turn_key`` (the sender's own
client turn id), so the sender's page can skip the copy of what its own
stream already showed.

It costs nothing until a turn runs: a stream waits on its queue, and a chat
nobody shares publishes nothing (the chat route only calls in for shared
ones). One process holds every chat's watchers, as it holds every live
``Session`` (``chat._sessions``). Nothing here is stored: a page that
(re)connects reads the saved chat and gets the running turn so far in
``ready`` (``snapshot``).
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

# Events a page may be behind by before it is told to read the chat again. A
# long reply streams a few hundred chunks.
_QUEUE_MAX = 1024
# Open pages per chat and per process. Each is an idle connection, but
# without a cap one caller could open any number.
MAX_PER_CHAT = 40
MAX_TOTAL = 2000
# One `typing` event per person per chat at most this often.
TYPING_EVERY_S = 2.0
_TYPING_MAX = 4096


@dataclass(eq=False)
class Watcher:
    """One open page: who it is for and the events waiting for it."""

    session_id: str
    person_id: int | None
    queue: asyncio.Queue[dict[str, Any]] = field(
        default_factory=lambda: asyncio.Queue(maxsize=_QUEUE_MAX)
    )
    # Set once its last event (``removed``, ``deleted``, ``resync``) is
    # queued: nothing after it is sent.
    ended: bool = False


_watchers: dict[str, set[Watcher]] = {}
# The running turn of each shared chat, for a page that opens mid-turn.
_turns: dict[str, dict[str, Any]] = {}
_typing_at: dict[tuple[str, int], float] = {}


def subscribe(session_id: str, person_id: int | None) -> Watcher | None:
    """A new page watching ``session_id``. None when the caps are reached."""
    total = sum(len(w) for w in _watchers.values())
    current = _watchers.get(session_id, set())
    if total >= MAX_TOTAL or len(current) >= MAX_PER_CHAT:
        return None
    watcher = Watcher(session_id=session_id, person_id=person_id)
    _watchers.setdefault(session_id, set()).add(watcher)
    return watcher


def unsubscribe(watcher: Watcher) -> None:
    current = _watchers.get(watcher.session_id)
    if current is None:
        return
    current.discard(watcher)
    if not current:
        _watchers.pop(watcher.session_id, None)


def watchers(session_id: str) -> int:
    """How many pages have ``session_id`` open."""
    return len(_watchers.get(session_id, ()))


def _end(watcher: Watcher, event_type: str) -> None:
    while not watcher.queue.empty():
        watcher.queue.get_nowait()
    watcher.queue.put_nowait({"type": event_type, "session_id": watcher.session_id})
    watcher.ended = True


def _offer(watcher: Watcher, event: dict[str, Any]) -> None:
    if watcher.ended:
        return
    try:
        watcher.queue.put_nowait(event)
    except asyncio.QueueFull:
        # Too far behind to catch up event by event: drop what it missed and
        # have it connect again and read the saved chat instead.
        _end(watcher, "resync")


def publish(session_id: str, event: dict[str, Any]) -> None:
    """Send ``event`` to every page watching ``session_id``. Never blocks."""
    for watcher in list(_watchers.get(session_id, ())):
        _offer(watcher, {**event, "session_id": session_id})


def end_for(session_id: str, person_ids: set[int] | None, event_type: str) -> None:
    """End the streams of ``person_ids`` on ``session_id`` (everyone's when
    None) with a last ``event_type`` event (``removed``, ``deleted``)."""
    for watcher in list(_watchers.get(session_id, ())):
        if (person_ids is None or watcher.person_id in person_ids) and not watcher.ended:
            _end(watcher, event_type)


def turn_started(
    session_id: str,
    *,
    turn_key: str,
    sender_person_id: int | None,
    sender_name: str,
    content: str,
) -> None:
    """A turn began: kept for pages that open while it runs, and sent."""
    turn: dict[str, Any] = {
        "turn_key": turn_key,
        "sender_person_id": sender_person_id,
        "sender_name": sender_name,
        "content": content,
        "reply": "",
        "actions": [],
    }
    _turns[session_id] = turn
    publish(session_id, {"type": "turn_started", **{k: v for k, v in turn.items() if k not in ("reply", "actions")}})


def turn_event(session_id: str, turn_key: str, event: dict[str, Any]) -> None:
    """One step of the running turn's reply, as the sender's stream sent it."""
    turn = _turns.get(session_id)
    if turn is None or turn["turn_key"] != turn_key:
        return
    kind = event.get("type")
    if kind == "chunk":
        turn["reply"] += str(event.get("content") or "")
    elif kind == "action_taken":
        turn["actions"].append(event)
    publish(session_id, {**event, "turn_key": turn_key})


def turn_ended(session_id: str, turn_key: str) -> None:
    """The turn is over and saved (or saved nothing): pages read the chat."""
    turn = _turns.get(session_id)
    if turn is not None and turn["turn_key"] == turn_key:
        _turns.pop(session_id, None)
    publish(session_id, {"type": "turn_ended", "turn_key": turn_key})


def snapshot(session_id: str) -> dict[str, Any] | None:
    """The running turn so far, or None."""
    turn = _turns.get(session_id)
    if turn is None:
        return None
    return {**turn, "actions": list(turn["actions"])}


def typing(session_id: str, person_id: int, name: str) -> bool:
    """Tell the chat ``person_id`` is writing. False when they just did."""
    now = time.monotonic()
    key = (session_id, person_id)
    last = _typing_at.get(key)
    if last is not None and now - last < TYPING_EVERY_S:
        return False
    if len(_typing_at) >= _TYPING_MAX:
        _typing_at.clear()
    _typing_at[key] = now
    publish(session_id, {"type": "typing", "person_id": person_id, "name": name})
    return True


def reset() -> None:
    """Forget every watcher and turn (tests)."""
    _watchers.clear()
    _turns.clear()
    _typing_at.clear()
