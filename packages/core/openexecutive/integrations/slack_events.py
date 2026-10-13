"""``POST /webhook/slack`` — Slack's Events API over HTTP.

The other way to hear Slack, next to Socket Mode: with ``SLACK_SIGNING_SECRET``
set and no ``SLACK_APP_TOKEN``, Slack posts each event to this address instead
of pushing it down a socket. The Bolt app ``slack_bot.create_slack_app`` builds
is the same either way; here its request handler checks Slack's signature
(Bolt refuses a missing, wrong or stale one before any listener runs), answers
Slack's one-time URL check, and acknowledges at once while the listener runs.

Kept apart from ``slack_bot`` so ``api/main.py`` can mount the route at import
without importing slack_bolt: the handler is only set once the app is built.

The route checks the signature itself before Bolt sees the request: Bolt logs
the whole body of a request that fails its check, and raises on a body or
timestamp it can't parse, so unsigned traffic would otherwise write whatever
it likes into the log. Bolt checks it again; that costs one HMAC.

A signed request is taken once. The signature alone only proves Slack sent it
within ``SIGNATURE_MAX_AGE_S``, so a captured request could be posted again in
that window, and Slack's own retries (``x-slack-retry-num``, re-signed with a
new timestamp) would run twice. The route remembers the exact signature header
of every request it hands to Bolt (an identical replay of any signed request
carries the same one) and the ``event_id`` of an ``event_callback`` body (the
same across Slack's retries), and answers a repeat 200 with an empty body
without reaching Bolt, so Slack stops retrying. The keys are recorded before
Bolt runs, so two copies arriving together are still caught, and forgotten if
Bolt raises or answers 5xx, so Slack's retry of a delivery that failed here
still runs. The seen-set is in memory and per process: it covers one machine,
and a restart forgets it (a replay after that is still bounded by the
signature window).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from collections import OrderedDict
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

logger = logging.getLogger(__name__)

router = APIRouter()

PATH = "/webhook/slack"
MAX_BODY_BYTES = 1024 * 1024
# Slack's own limit: a request signed longer ago than this is refused (replays).
SIGNATURE_MAX_AGE_S = 300

# A signature stays valid while now is within SIGNATURE_MAX_AGE_S of its
# timestamp on either side, so up to twice that from first sight; a minute more
# for clock slop. Slack's retries of one event come within about five minutes.
SEEN_TTL_S = 2 * SIGNATURE_MAX_AGE_S + 60
# Bound on remembered keys; past it the oldest go first. Only correctly signed
# requests add keys (two at most each), so only Slack's own volume fills it.
SEEN_MAX = 10_000

# Keys of requests already handed to Bolt -> monotonic time they were seen.
_seen: OrderedDict[str, float] = OrderedDict()

# The listener serving PATH while HTTP events mode is on, else None.
_listener: HttpEventsListener | None = None


class HttpEventsListener:
    """Stands where the Socket Mode handler does in the app's lifespan: there
    is no connection to open or close, Slack calls ``PATH`` instead."""

    mode = "http"

    def __init__(self, request_handler: Any, signing_secret: str) -> None:
        self.request_handler = request_handler
        self.signing_secret = signing_secret

    async def connect_async(self) -> None:
        global _listener
        _listener = self

    async def close_async(self) -> None:
        global _listener
        if _listener is self:
            _listener = None


def signature_valid(secret: str, body: bytes, timestamp: str, signature: str, *, now: float | None = None) -> bool:
    """Whether ``signature`` is Slack's v0 signature of ``body`` at ``timestamp``,
    signed within ``SIGNATURE_MAX_AGE_S`` of now. Compared in constant time."""
    # isascii: str.isdigit() also passes digits int() can't read ("²").
    if not secret or not signature or not (timestamp.isascii() and timestamp.isdigit() and len(timestamp) <= 20):
        return False
    if abs((time.time() if now is None else now) - int(timestamp)) > SIGNATURE_MAX_AGE_S:
        return False
    digest = hmac.new(secret.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"v0={digest}".encode(), signature.encode())


def _dedup_keys(body: bytes, signature: str) -> list[str]:
    """The keys a request is remembered by: its signature, and the event id of
    an ``event_callback`` JSON body."""
    keys = [f"sig:{signature}"]
    if body[:1] == b"{":
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            payload = None
        if isinstance(payload, dict) and payload.get("type") == "event_callback":
            event_id = payload.get("event_id")
            if isinstance(event_id, str) and event_id:
                keys.append(f"event:{event_id}")
    return keys


def _first_sight(keys: list[str], *, now: float | None = None) -> bool:
    """Record ``keys``; False (recording nothing) when any was seen within
    ``SEEN_TTL_S``."""
    now = time.monotonic() if now is None else now
    # Entries are kept in the order seen, so the expired ones are at the front.
    while _seen:
        oldest_key, seen_at = next(iter(_seen.items()))
        if now - seen_at < SEEN_TTL_S:
            break
        del _seen[oldest_key]
    if any(key in _seen for key in keys):
        return False
    for key in keys:
        _seen[key] = now
    while len(_seen) > SEEN_MAX:
        _seen.popitem(last=False)
    return True


def _forget(keys: list[str]) -> None:
    for key in keys:
        _seen.pop(key, None)


async def _read_capped(request: Request) -> bytes | None:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            return None
    return bytes(body)


@router.post(PATH)
async def slack_events(request: Request) -> Response:
    listener = _listener
    if listener is None:
        raise HTTPException(status_code=503, detail="Slack events over HTTP are not configured")
    body = await _read_capped(request)
    if body is None:
        return Response(status_code=413)
    signature = request.headers.get("x-slack-signature", "")
    if not signature_valid(
        listener.signing_secret,
        body,
        request.headers.get("x-slack-request-timestamp", ""),
        signature,
    ):
        logger.warning("Slack: refused a request to %s with a bad signature", PATH)
        return Response(status_code=401)
    keys = _dedup_keys(body, signature)
    if not _first_sight(keys):
        # The retry header isn't signed: log only whether it is a number.
        retry = request.headers.get("x-slack-retry-num", "")
        logger.info(
            "Slack: dropped a repeated request to %s (%s)",
            PATH, "a Slack retry" if retry.isascii() and retry.isdigit() else "a replay",
        )
        return Response(status_code=200)
    # Bolt reads the body again: hand it a request that replays the bytes read.
    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    replay = Request(request.scope, receive)
    try:
        response: Response = await listener.request_handler.handle(replay)
    except (ValueError, UnicodeDecodeError):
        # Signed, but not a body Bolt can read (JSON errors are ValueErrors).
        # The same bytes would fail the same way, so the keys stay recorded.
        logger.warning("Slack: couldn't read a signed request to %s", PATH)
        return Response(status_code=400)
    except BaseException:
        _forget(keys)
        raise
    if response.status_code >= 500:
        _forget(keys)
    return response
