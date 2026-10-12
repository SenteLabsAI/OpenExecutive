"""``POST /webhook/slack`` — Slack's Events API over HTTP.

The other way to hear Slack, next to Socket Mode: with ``SLACK_SIGNING_SECRET``
set and no ``SLACK_APP_TOKEN``, Slack posts each event to this address instead
of pushing it down a socket. The Bolt app ``slack_bot.create_slack_app`` builds
is the same either way; here its request handler checks Slack's signature
(Bolt refuses a missing, wrong or stale one before any listener runs), answers
Slack's one-time URL check, and acknowledges at once while the listener runs.

Kept apart from ``slack_bot`` so ``api/main.py`` can mount the route at import
without importing slack_bolt: the handler is only set once the app is built.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

logger = logging.getLogger(__name__)

router = APIRouter()

PATH = "/webhook/slack"

# Bolt's AsyncSlackRequestHandler while HTTP events mode is on, else None.
_request_handler: Any = None


def set_request_handler(handler: Any) -> None:
    global _request_handler
    _request_handler = handler


class HttpEventsListener:
    """Stands where the Socket Mode handler does in the app's lifespan: there
    is no connection to open or close, Slack calls ``PATH`` instead."""

    mode = "http"

    def __init__(self, request_handler: Any) -> None:
        self.request_handler = request_handler

    async def connect_async(self) -> None:
        set_request_handler(self.request_handler)

    async def close_async(self) -> None:
        if _request_handler is self.request_handler:
            set_request_handler(None)


@router.post(PATH)
async def slack_events(request: Request) -> Response:
    handler = _request_handler
    if handler is None:
        raise HTTPException(status_code=503, detail="Slack events over HTTP are not configured")
    response: Response = await handler.handle(request)
    return response
