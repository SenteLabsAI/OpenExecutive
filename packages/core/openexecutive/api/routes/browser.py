"""The Open Executive browser extension's relay (``browser.relay``).

The extension's script in an open Open Executive tab calls these, signed in
as that person, through the UI's backend proxy:

  GET  /browser/status         — for Settings → Browser and the chat's card:
                                 {available, connected, stay_connected, store_url, protocol}: whether
                                 the browser is on here (``OE_BROWSER_OFF``), whether theirs
                                 is asking for steps now, whether "Stay connected" is ticked in it
                                 (null when not connected or too old to say), and the extension's
                                 install page (null for none: Settings shows how to load it)
  GET  /browser/starters       — the "Try one" pills on chat's card once it's connected:
                                 {starters: [str]} (``browser.starters``), [] while the
                                 browser is off here
  GET  /browser/hello          — who this is for, so the extension can ask them to connect this site:
                                 {exec_name, first_name, protocol}: ``protocol`` is the
                                 version of these calls this server speaks, so an extension
                                 that speaks another one can say it needs an update
  GET  /browser/steps?wait=20&stay=1
                               — the next step the Executive wants done in their browser:
                                 {step: {id, action, args} | null}; ``stay`` says whether
                                 "Stay connected" is ticked (1/0; left out by older extensions)
  POST /browser/steps/{id}     — {ok, error?, ...what happened}: the step's answer
  POST /browser/hand-over/{id} — {outcome: "done" | "stop" | "auto"}: they pressed Done or Stop
                                 on the "Waiting on you" bar (``browser.handover``), or
                                 it saw the sign-in form go ("auto"): {ok, carried_on, kept?,
                                 on_card?}; 404 when it is already finished

Only someone on the team, for their own steps. While the browser is off here
(``OE_BROWSER_OFF=1``), all but status and starters answer 404.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from openexecutive.browser import relay as browser_relay

router = APIRouter(prefix="/browser")

# The version of these calls (steps, answers, hand-overs) this server speaks.
# The extension says which one it speaks: one that differs tells the person
# to update it (or the server). Raise it only for a change an older
# extension can't follow.
PROTOCOL = 1

MAX_BODY = 400_000
# {"outcome": "done"} and nothing more.
MAX_HAND_OVER_BODY = 2_000
# Done or Stop on a workflow's bar looks for its waiting run this many times.
WORKFLOW_TRIES = 4
WORKFLOW_RETRY_S = 1.5


def _on() -> None:
    if not browser_relay.on():
        raise HTTPException(status_code=404, detail="The browser extension isn't turned on here.")


def _caller(request: Request) -> Any:
    from openexecutive.api.routes.chat import _resolve_caller_person_id
    from openexecutive.people.store import get_person

    person_id = _resolve_caller_person_id(request)
    person = get_person(person_id) if person_id is not None else None
    if person is None or person.id is None or person.archived or person.kind != "team":
        raise HTTPException(status_code=403, detail="The browser extension is for people on the team.")
    return person


@router.get("/hello")
async def hello(request: Request) -> dict[str, Any]:
    from openexecutive.config import get_settings

    _on()
    person = _caller(request)
    words = (person.full_name or "").split()
    return {
        "exec_name": get_settings().exec_display_name.strip() or "Open Executive",
        "first_name": words[0][:40] if words else "",
        "protocol": PROTOCOL,
    }


@router.get("/status")
async def status(request: Request) -> dict[str, Any]:
    from openexecutive.browser import tools as browser_tools

    person = _caller(request)
    if not browser_relay.on():
        return {"available": False, "connected": False, "stay_connected": None, "store_url": None, "protocol": PROTOCOL}
    return {
        "available": True,
        "connected": browser_relay.connected(person.id),
        "stay_connected": browser_relay.stay_connected(person.id),
        "store_url": browser_tools.store_url(),
        "protocol": PROTOCOL,
    }


async def _read_capped(request: Request, limit: int) -> bytes:
    """The request body, refused (413) past ``limit`` bytes before more of it
    is read: by its Content-Length first, then while it streams in."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413, detail="That answer is too big.")
    raw = bytearray()
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > limit:
            raise HTTPException(status_code=413, detail="That answer is too big.")
    return bytes(raw)


@router.get("/starters")
async def starters(request: Request) -> dict[str, list[str]]:
    from openexecutive.browser import starters as browser_starters

    _caller(request)
    if not browser_relay.on():
        return {"starters": []}
    return {"starters": browser_starters.for_company()}


@router.get("/steps")
async def steps(
    request: Request,
    wait: float = Query(default=browser_relay.MAX_WAIT_S, ge=0),
    stay: bool | None = Query(default=None),
) -> dict[str, Any]:
    _on()
    person = _caller(request)
    return {"step": await browser_relay.next_step(person.id, wait=wait, stay=stay)}


@router.post("/steps/{step_id}")
async def answer(step_id: str, request: Request) -> dict[str, bool]:
    _on()
    person = _caller(request)
    raw = await _read_capped(request, MAX_BODY)
    try:
        body = json.loads(raw or b"{}")
    except (ValueError, RecursionError) as exc:
        raise HTTPException(status_code=400, detail="Send JSON.") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Send a JSON object.")
    if not browser_relay.finish(person.id, step_id, body):
        raise HTTPException(status_code=404, detail="No such step waiting.")
    return {"ok": True}


@router.post("/hand-over/{handover_id}")
async def hand_over(handover_id: str, request: Request) -> dict[str, bool]:
    """Done or Stop on a hand-over's bar, or "auto" when the extension saw
    the sign-in page left. A turn waiting for it gets the answer; a paused
    workflow run carries on or stops. When nothing waits any more, Done only
    takes the bar down: the work carries on from Done on its card, where the
    person sees what it will do, never from the bar. "auto" is a hint from
    the page, so it only ever answers a turn or run that waits, and is kept
    open otherwise (``kept``)."""
    from openexecutive.browser import handover as browser_handover

    _on()
    person = _caller(request)
    try:
        body = json.loads(await _read_capped(request, MAX_HAND_OVER_BODY) or b"{}")
    except (ValueError, RecursionError) as exc:
        raise HTTPException(status_code=400, detail="Send JSON.") from exc
    outcome = body.get("outcome") if isinstance(body, dict) else None
    if outcome not in browser_handover.OUTCOMES and outcome != browser_handover.AUTO:
        raise HTTPException(status_code=422, detail='Say "done" or "stop".')
    open_one = browser_handover.get(handover_id)
    if open_one is None or open_one.person_id != person.id:
        raise HTTPException(status_code=404, detail="That's already finished.")
    if outcome == browser_handover.AUTO:
        if not open_one.sign_in or not (browser_handover.waited_on(open_one) or open_one.workflow_key):
            return {"ok": True, "carried_on": False, "kept": True}
        outcome = "done"
    handover, waited = browser_handover.finish(handover_id, person.id, str(outcome))
    if handover is None:
        raise HTTPException(status_code=404, detail="That's already finished.")
    if waited:
        return {"ok": True, "carried_on": outcome == "done"}
    if handover.workflow_key:
        from openexecutive.workflows import resumer

        # A run is saved as waiting just after its step reports: a press in
        # that moment finds nothing yet, so it looks again for a few seconds.
        picked = 0
        for attempt in range(WORKFLOW_TRIES):
            if attempt:
                await asyncio.sleep(WORKFLOW_RETRY_S)
            if outcome == "done":
                picked = await resumer.resume_waiting(handover.workflow_key)
            else:
                picked = await resumer.stop_waiting(handover.workflow_key)
            if picked:
                break
        if outcome == "done" and not picked and browser_handover.reopen(handover):
            # Nothing to carry on yet: keep it open so Done can be pressed
            # again, rather than leave the run paused with no way back. An
            # older one's run has ended, so the bar comes down.
            return {"ok": True, "carried_on": False, "kept": True}
        return {"ok": True, "carried_on": outcome == "done" and picked > 0}
    if handover.card_id is not None and outcome == "stop":
        from openexecutive.alerts import store

        store.set_status(handover.card_id, "dismissed")
    # The turn ended: Done on the card carries the work on.
    return {"ok": True, "carried_on": False, "on_card": outcome == "done" and handover.card_id is not None}
