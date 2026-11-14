"""The steps the Executive takes in a person's own web browser.

The Open Executive browser extension (``packages/extension``) runs in the
person's Chrome. While an Open Executive tab is open there, the
extension's script in that tab asks this machine for the next step, signed in
as that person (``GET /browser/steps``, ``api/routes/browser.py``), hands it to
the extension, which does it in the tab they are looking at, and posts what
happened back (``POST /browser/steps/{id}``). The Executive's browser tools
(``browser.tools``) put a step here and wait for that answer.

Steps are only ever handed to the person whose turn asked for them, and only
while their extension is asking (``connected``). A workflow run that paused
because their browser wasn't connected (``browser.tools``,
``WAITING_KEY``) is picked up the moment it asks again. Everything is held in this
process: an install runs one API process, and a step left over from before a
restart is one nobody is waiting on.
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

# Set to "1" to turn the browser off for this install: the Executive doesn't
# use anyone's browser, and the extension's calls are refused.
ENV_OFF = "OE_BROWSER_OFF"


def on() -> bool:
    """Whether the browser extension is on for this install."""
    return os.environ.get(ENV_OFF, "").strip() != "1"


# The extension asks again at least this often while an Open Executive tab is
# open (a wait is at most MAX_WAIT_S); past it, nobody is there to take a step.
CONNECTED_S = 45.0
# How long a turn waits for one step (a page load can be slow).
STEP_TIMEOUT_S = 45.0
# The longest one ask for a step waits for one to come.
MAX_WAIT_S = 20.0
# Steps waiting for one person at once: a turn runs its steps one by one, so
# more means something is stuck.
MAX_QUEUED = 8
# The largest answer kept from the extension (a page's text is cut there).
MAX_RESULT_CHARS = 60_000

ACTIONS = frozenset({
    "look", "tabs", "click", "click_at", "type", "select", "key", "scroll", "navigate", "open_tab", "switch_tab",
    "show_page", "point",
    # Hand the task over to the person (``browser.handover``), and
    # take the bar down when they said Done somewhere else.
    "hand_over", "hand_over_end",
})


@dataclass
class _Step:
    id: str
    person_id: int
    action: str
    args: dict[str, Any]
    future: asyncio.Future[dict[str, Any]]
    taken: bool = False


@dataclass
class _Person:
    queue: deque[_Step] = field(default_factory=deque)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    seen: float = 0.0
    # When their waiting workflow runs were last looked for.
    picked: float = 0.0
    # Whether "Stay connected" is ticked in their extension, as it last said
    # when asking for a step (None: an extension too old to say).
    stay: bool | None = None


_people: dict[int, _Person] = {}
_steps: dict[str, _Step] = {}
# A run is saved as waiting only after its step reports, so a browser that
# connected in that gap is checked again this often while it stays connected.
PICKUP_EVERY_S = 60.0
# The background tasks picking up runs that waited for a browser.
_PICKUPS: set[asyncio.Task[Any]] = set()


def waiting_key(person_id: int) -> str:
    """What a workflow run that waits for this person's browser waits on
    (``extensions.workflow_wait``)."""
    return f"browser:{person_id}"


def _pick_up_waiting(person_id: int) -> None:
    """Their browser is connected: pick up the workflow runs waiting for it."""
    from openexecutive.workflows import resumer

    try:
        task = asyncio.get_running_loop().create_task(resumer.resume_waiting(waiting_key(person_id)))
    except RuntimeError:  # pragma: no cover - always asked from a request
        return
    _PICKUPS.add(task)
    task.add_done_callback(_PICKUPS.discard)


def _person(person_id: int) -> _Person:
    entry = _people.get(person_id)
    if entry is None:
        entry = _people[person_id] = _Person()
    return entry


def connected(person_id: int, *, now: float | None = None) -> bool:
    """Whether this person's extension asked for steps lately."""
    entry = _people.get(person_id)
    if entry is None or not entry.seen:
        return False
    return ((now if now is not None else time.monotonic()) - entry.seen) <= CONNECTED_S


class NotConnected(Exception):
    """Nobody's extension is there to take the step."""


async def run(person_id: int, action: str, args: dict[str, Any], *, timeout: float = STEP_TIMEOUT_S) -> dict[str, Any]:
    """Hand one step to the person's extension and wait for what happened:
    ``{"ok": True, ...}`` or ``{"ok": False, "error": ...}``."""
    if action not in ACTIONS:
        raise ValueError(f"unknown browser action {action!r}")
    if not connected(person_id):
        raise NotConnected
    entry = _person(person_id)
    if len(entry.queue) >= MAX_QUEUED:
        return {"ok": False, "error": "The browser is still busy with earlier steps. Wait, then look again."}
    loop = asyncio.get_running_loop()
    step = _Step(id=uuid.uuid4().hex, person_id=person_id, action=action, args=dict(args), future=loop.create_future())
    _steps[step.id] = step
    entry.queue.append(step)
    entry.ready.set()
    try:
        return await asyncio.wait_for(asyncio.shield(step.future), timeout)
    except TimeoutError:
        return {"ok": False, "error": "The browser didn't answer in time. Look at the page again before going on."}
    finally:
        _steps.pop(step.id, None)
        if step in entry.queue:
            entry.queue.remove(step)
        if not entry.queue:
            entry.ready.clear()


def stay_connected(person_id: int) -> bool | None:
    """Whether their connected extension has "Stay connected" ticked, so their
    workflows can use it with no Open Executive tab open. None when
    it isn't connected, or is too old to say."""
    if not connected(person_id):
        return None
    return _people[person_id].stay


async def next_step(
    person_id: int, *, wait: float = MAX_WAIT_S, stay: bool | None = None
) -> dict[str, Any] | None:
    """The person's next step for their extension, waiting up to ``wait``
    seconds for one; None when none came. Asking marks them connected, and
    ``stay`` is what it says about "Stay connected"."""
    was_connected = connected(person_id)
    entry = _person(person_id)
    entry.stay = stay
    deadline = time.monotonic() + max(0.0, min(wait, MAX_WAIT_S))
    if not was_connected or time.monotonic() - entry.picked >= PICKUP_EVERY_S:
        entry.seen = entry.picked = time.monotonic()
        _pick_up_waiting(person_id)
    while True:
        entry.seen = time.monotonic()
        while entry.queue:
            step = entry.queue.popleft()
            if step.future.done():
                continue
            step.taken = True
            if not entry.queue:
                entry.ready.clear()
            return {"id": step.id, "action": step.action, "args": step.args}
        entry.ready.clear()
        left = deadline - time.monotonic()
        if left <= 0:
            return None
        try:
            await asyncio.wait_for(entry.ready.wait(), left)
        except TimeoutError:
            entry.seen = time.monotonic()
            return None


def _clip(value: Any, depth: int = 0) -> Any:
    if isinstance(value, str):
        return value[:MAX_RESULT_CHARS]
    if isinstance(value, list | dict) and depth >= 8:
        return "…"
    if isinstance(value, list):
        return [_clip(v, depth + 1) for v in value[:400]]
    if isinstance(value, dict):
        return {str(k)[:64]: _clip(v, depth + 1) for k, v in list(value.items())[:40]}
    if isinstance(value, bool | int | float) or value is None:
        return value
    return str(value)[:MAX_RESULT_CHARS]


def finish(person_id: int, step_id: str, result: dict[str, Any]) -> bool:
    """What the extension did with a step it took. False when it is no step
    of theirs waiting for an answer."""
    step = _steps.get(step_id)
    if step is None or step.person_id != person_id or not step.taken or step.future.done():
        return False
    shot = result.get("screenshot")
    out = _clip({k: v for k, v in result.items() if k != "screenshot"})
    if isinstance(shot, dict):
        # The page's screenshot keeps its full size (``_clip`` would cut it):
        # only a well-formed picture goes on (``orchestrator.tool_images``).
        from openexecutive.orchestrator import tool_images

        media, data = shot.get("media_type"), shot.get("data")
        width, height = shot.get("width"), shot.get("height")
        if tool_images.valid(media, data) and all(
            isinstance(n, int) and not isinstance(n, bool) and 0 < n <= 4000 for n in (width, height)
        ):
            out["screenshot"] = {"media_type": media, "data": data, "width": width, "height": height}
    out["ok"] = bool(result.get("ok"))
    if not out["ok"] and not str(out.get("error") or "").strip():
        out["error"] = "That didn't work in the browser."
    step.future.set_result(out)
    return True


def _reset_for_tests() -> None:
    _people.clear()
    _steps.clear()
