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
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

logger = logging.getLogger(__name__)

router = APIRouter()

PATH = "/webhook/slack"
MAX_BODY_BYTES = 1024 * 1024
# Slack's own limit: a request signed longer ago than this is refused (replays).
SIGNATURE_MAX_AGE_S = 300

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
    if not signature_valid(
        listener.signing_secret,
        body,
        request.headers.get("x-slack-request-timestamp", ""),
        request.headers.get("x-slack-signature", ""),
    ):
        logger.warning("Slack: refused a request to %s with a bad signature", PATH)
        return Response(status_code=401)
    # Bolt reads the body again: hand it a request that replays the bytes read.
    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    replay = Request(request.scope, receive)
    try:
        response: Response = await listener.request_handler.handle(replay)
    except (ValueError, UnicodeDecodeError):
        # Signed, but not a body Bolt can read (JSON errors are ValueErrors).
        logger.warning("Slack: couldn't read a signed request to %s", PATH)
        return Response(status_code=400)
    return response
