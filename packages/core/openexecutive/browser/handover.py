"""The Executive hands a browser task over to the person.

Some steps on a website are the person's alone: signing in, a code from their
phone, a passkey, a password or card field (the extension never types into
those), a "prove you're human" check, or a choice only they should make. The
Executive calls ``browser_hand_over`` (``browser.tools``) with what it
needs from them. Then:

- the extension brings that tab to the front with a bar at the top, "Waiting
  on you: <what>", with Done and Stop (``packages/extension``);
- on a chat turn, a Needs you card says the same (``SOURCE``), and reaches
  them wherever their cards do;
  in a workflow, the run pauses on it (``extensions.workflow_wait``) and its
  creator is told, as for any pause;
- they do it, then press Done on the bar or the card. When the page they
  were handed was a sign-in form, the extension also sees it go and says
  Done by itself.

The Executive looks at the page again only after Done. Password, card and
code fields never show their values to it, in the page's text or its
screenshot (the extension blanks them for the picture). A turn waits ``WAIT_S`` for it; past that the turn ends
saying so. Then Done on the bar only takes it down: Done on the card
carries the work on in a new chat (the card's approve path), where the person
sees what it will do; a paused workflow run is picked up by either. Stop ends
it: the turn is told to stop, a waiting run stops (``resumer.stop_waiting``).
The extension's own Done (a sign-in page left) is a hint from the page, so it
only answers a turn or run that is waiting, on a hand-over whose page had a
sign-in form when it was handed over (``Handover.sign_in``).

This is not a check: Approvals, Your rules and In training apply to what the
Executive does next exactly as before. Hand-overs are held in this process,
like the relay's steps: after a restart, Done on an old bar says it's gone.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Literal

Outcome = Literal["done", "stop"]
OUTCOMES: frozenset[str] = frozenset({"done", "stop"})
# The extension saw a sign-in page left: a hint from the page, so it only
# answers a turn or run that is waiting (``api/routes/browser.py``).
AUTO = "auto"

# The card's source and tag (the web card says Done, not Approve).
SOURCE = "browser_handover"
TAG = "browser-handover"
# How long a chat turn waits for Done before it ends saying so.
WAIT_S = 300.0
# What a hand-over says it needs, at most.
MAX_NEED = 140
# What the Executive was in the middle of, kept for carrying on later.
MAX_TASK = 600
# Open hand-overs per person: more are left over (oldest go first).
MAX_PER_PERSON = 4
# How long after a workflow's hand-over Done may find its run not yet saved
# as waiting (``reopen``).
FRESH_S = 120.0
# A hand-over nobody finished is forgotten after this (a run waits 24 h).
TTL_S = 25 * 3600.0


@dataclass
class Handover:
    id: str
    person_id: int
    need: str
    card_id: int | None = None
    # The paused workflow run's wait key; "" on a chat turn.
    workflow_key: str = ""
    # The page had a sign-in form when it was handed over (the extension's
    # answer to the hand_over step). Only then is its own Done ("auto")
    # taken: a page left later can't say Done for any other kind of step.
    sign_in: bool = False
    created: float = field(default_factory=time.monotonic)
    # Set while a turn waits for the answer.
    future: asyncio.Future[str] | None = None
    # The loop it was started on: a sync route that closes its card runs in
    # a worker thread and hands the answer back through it.
    loop: asyncio.AbstractEventLoop | None = None


_open: dict[str, Handover] = {}


def one_line(text: object, cap: int) -> str:
    return " ".join(str(text or "").split())[:cap].strip()


def _prune(now: float | None = None) -> None:
    now = time.monotonic() if now is None else now
    for key in [k for k, h in _open.items() if now - h.created >= TTL_S]:
        _open.pop(key, None)


def start(person_id: int, need: str, *, card_id: int | None = None, workflow_key: str = "") -> Handover:
    """A new open hand-over for ``person_id``."""
    _prune()
    mine = sorted((h for h in _open.values() if h.person_id == person_id), key=lambda h: h.created)
    for old in mine[: max(0, len(mine) - MAX_PER_PERSON + 1)]:
        _open.pop(old.id, None)
    try:
        loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    handover = Handover(
        id=uuid.uuid4().hex, person_id=person_id, need=one_line(need, MAX_NEED),
        card_id=card_id, workflow_key=workflow_key, loop=loop,
    )
    _open[handover.id] = handover
    return handover


def get(handover_id: str) -> Handover | None:
    _prune()
    return _open.get(str(handover_id or ""))


def by_card(card_id: int) -> Handover | None:
    _prune()
    return next((h for h in _open.values() if h.card_id == card_id), None)


def waited_on(handover: Handover) -> bool:
    """Whether a turn is waiting for this hand-over right now."""
    return handover.future is not None and not handover.future.done()


def workflow_key(person_id: int, handover_id: str) -> str:
    """What a workflow run paused on this hand-over waits on."""
    return f"handover:{person_id}:{handover_id}"


def arm(handover: Handover) -> asyncio.Future[str]:
    """Get ready for Done or Stop before the bar is shown, so one pressed
    straight away (or the page's own Done) isn't lost before ``wait`` starts:
    ``finish`` then answers it, and ``wait`` returns it at once."""
    handover.future = asyncio.get_running_loop().create_future()
    return handover.future


async def wait(handover: Handover, timeout: float | None = None) -> Outcome | None:
    """Wait for Done or Stop on ``handover`` (``WAIT_S`` at most, unless
    ``timeout`` says); None when it didn't come in time (it stays open, for
    a Done later)."""
    timeout = WAIT_S if timeout is None else timeout
    future = handover.future if handover.future is not None else arm(handover)
    try:
        got = await asyncio.wait_for(asyncio.shield(future), timeout)
    except TimeoutError:
        return None
    finally:
        handover.future = None
    return "stop" if got == "stop" else "done"


def finish(handover_id: str, person_id: int, outcome: str) -> tuple[Handover | None, bool]:
    """Close ``person_id``'s hand-over with ``outcome``: (it, whether a turn
    was waiting for it, which now has the answer). (None, False) when there
    is no such open hand-over of theirs."""
    handover = get(handover_id)
    if handover is None or handover.person_id != person_id or outcome not in OUTCOMES:
        return None, False
    _open.pop(handover.id, None)
    future = handover.future
    if future is not None and not future.done():
        future.set_result(outcome)
        return handover, True
    return handover, False


def reopen(handover: Handover) -> bool:
    """Put back a hand-over ``finish`` closed when Done found nothing to carry
    on yet (a workflow run not saved as waiting): the bar stays, and a later
    Done tries again. Only while it is new (``FRESH_S``): past that the run
    has ended (stopped from its card, or timed out), so it stays closed.
    Whether it was put back."""
    if time.monotonic() - handover.created >= FRESH_S:
        return False
    _open[handover.id] = handover
    return True


def end_bar(handover: Handover) -> None:
    """Take the bar down in their browser: they said Done or Stop somewhere
    else (the card). Best effort, in the background; never raises."""
    from openexecutive.browser import relay as browser_relay

    async def _end() -> None:
        try:
            await browser_relay.run(handover.person_id, "hand_over_end", {"id": handover.id}, timeout=15.0)
        except Exception:
            # Not connected, or gone: the bar's own Done finds it closed.
            return

    try:
        task = asyncio.get_running_loop().create_task(_end())
    except RuntimeError:
        return
    _ending.add(task)
    task.add_done_callback(_ending.discard)


_ending: set[asyncio.Task[None]] = set()


def card_closed(card_id: int) -> bool:
    """Its card was dismissed or cleared without Done: the same as Stop. A
    turn waiting for it is told to stop and the bar comes down. Safe from a
    worker thread (the sync ack routes). False when no hand-over is open on
    that card."""
    handover = by_card(card_id)
    if handover is None:
        return False
    _open.pop(handover.id, None)
    loop = handover.loop

    def _close() -> None:
        future = handover.future
        if future is not None and not future.done():
            future.set_result("stop")
        end_bar(handover)

    if loop is None or loop.is_closed():
        return True
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        _close()
    else:
        loop.call_soon_threadsafe(_close)
    return True


def _reset_for_tests() -> None:
    _open.clear()
