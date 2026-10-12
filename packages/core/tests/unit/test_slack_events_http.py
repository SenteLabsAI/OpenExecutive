"""Slack's Events API over HTTP at /webhook/slack (SLACK_SIGNING_SECRET, no
SLACK_APP_TOKEN), and the once-per-event / own-workspace filter both modes share.

The real `create_app()` lifespan and the real Bolt app run; only Slack's
`auth.test` is stubbed, so nothing reaches slack.com.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from openexecutive.api.main import create_app
from openexecutive.integrations import slack_bot, slack_events

SECRET = "test-signing-secret"
AUTH_TEST = {"ok": True, "user_id": "UBOT", "bot_id": "BBOT", "team_id": "T1", "team": "Acme"}


def _settings(monkeypatch: pytest.MonkeyPatch, **update: Any) -> None:
    from openexecutive.config import get_settings

    fields = {"slack_bot_token": "xoxb-test", "slack_app_token": None, "slack_signing_secret": SECRET}
    stub = get_settings().model_copy(update={**fields, **update})
    monkeypatch.setattr("openexecutive.config.get_settings", lambda: stub)


@pytest.fixture(autouse=True)
def _no_slack(monkeypatch: pytest.MonkeyPatch) -> None:
    from slack_sdk.web.async_slack_response import AsyncSlackResponse

    # Bolt's authorization reads the response's headers, so a plain dict won't do.
    response = AsyncSlackResponse(
        client=None, http_verb="POST", api_url="https://slack.com/api/auth.test",
        req_args={}, data=AUTH_TEST, headers={}, status_code=200,
    )
    monkeypatch.setattr(
        "slack_sdk.web.async_client.AsyncWebClient.auth_test", AsyncMock(return_value=response)
    )
    monkeypatch.setattr(slack_bot, "_bot_user_id", None)
    monkeypatch.setattr(slack_bot, "_bot_id_last_attempt", None)
    monkeypatch.setattr(slack_events, "_request_handler", None)


def _signed(body: dict[str, Any], *, secret: str = SECRET, age_s: int = 0) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(body).encode()
    ts = str(int(time.time()) - age_s)
    digest = hmac.new(secret.encode(), f"v0:{ts}:".encode() + raw, hashlib.sha256).hexdigest()
    return raw, {
        "content-type": "application/json",
        "x-slack-request-timestamp": ts,
        "x-slack-signature": f"v0={digest}",
    }


def _event(event_id: str = "Ev1", team_id: str = "T1") -> dict[str, Any]:
    return {
        "type": "event_callback",
        "team_id": team_id,
        "api_app_id": "A1",
        "event_id": event_id,
        "event_time": int(time.time()),
        "authorizations": [{"team_id": team_id, "user_id": "UBOT", "is_bot": True}],
        "event": {"type": "reaction_added", "user": "U1", "reaction": "tada", "item": {"type": "message"}},
    }


CHALLENGE = {"type": "url_verification", "token": "t", "challenge": "abc123"}


def test_url_check_is_answered_with_a_valid_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch)
    with TestClient(create_app()) as client:
        raw, headers = _signed(CHALLENGE)
        response = client.post("/webhook/slack", content=raw, headers=headers)
    assert response.status_code == 200
    assert response.json()["challenge"] == "abc123"


@pytest.mark.parametrize(("secret", "age_s"), [("someone-elses-secret", 0), (SECRET, 600)])
def test_wrong_or_stale_signature_is_refused(monkeypatch: pytest.MonkeyPatch, secret: str, age_s: int) -> None:
    _settings(monkeypatch)
    with TestClient(create_app()) as client:
        raw, headers = _signed(CHALLENGE, secret=secret, age_s=age_s)
        response = client.post("/webhook/slack", content=raw, headers=headers)
    assert response.status_code == 401
    assert "abc123" not in response.text


def test_unsigned_request_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch)
    with TestClient(create_app()) as client:
        response = client.post("/webhook/slack", json=CHALLENGE)
    assert response.status_code == 401


def test_route_is_off_without_a_signing_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch, slack_signing_secret=None)
    with TestClient(create_app()) as client:
        raw, headers = _signed(CHALLENGE)
        response = client.post("/webhook/slack", content=raw, headers=headers)
    assert response.status_code == 503


def test_socket_mode_wins_when_both_are_set(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock

    _settings(monkeypatch, slack_app_token="xapp-test")
    handler = MagicMock()
    handler.connect_async = AsyncMock()
    handler.close_async = AsyncMock()
    monkeypatch.setattr(slack_bot, "create_slack_app", AsyncMock(return_value=(MagicMock(), handler)))
    with TestClient(create_app()) as client:
        raw, headers = _signed(CHALLENGE)
        response = client.post("/webhook/slack", content=raw, headers=headers)
        handler.connect_async.assert_called_once()
    assert response.status_code == 503


def test_route_closes_on_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch)
    with TestClient(create_app()):
        assert slack_events._request_handler is not None
    assert slack_events._request_handler is None


async def _app_with_probe() -> tuple[Any, AsyncMock]:
    app, listener = await slack_bot.create_slack_app()
    assert isinstance(listener, slack_events.HttpEventsListener)
    probe = AsyncMock()

    @app.event("reaction_added")
    async def _probe(event: dict) -> None:
        await probe(event)

    return app, probe


async def _dispatch(app: Any, body: dict[str, Any]) -> int:
    from slack_bolt.request.async_request import AsyncBoltRequest

    raw, headers = _signed(body)
    response = await app.async_dispatch(AsyncBoltRequest(body=raw.decode(), headers=headers))
    return int(response.status)


@pytest.mark.asyncio
async def test_a_repeated_event_is_handled_once(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch)
    app, probe = await _app_with_probe()
    # Process the listener inline so the assertion sees it ran.
    app._process_before_response = True
    app._async_listener_runner.process_before_response = True

    assert await _dispatch(app, _event("Ev1")) == 200
    assert await _dispatch(app, _event("Ev1")) == 200
    assert await _dispatch(app, _event("Ev2")) == 200
    assert probe.await_count == 2


@pytest.mark.asyncio
async def test_an_event_for_another_workspace_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch)
    app, probe = await _app_with_probe()
    app._process_before_response = True
    app._async_listener_runner.process_before_response = True

    assert await _dispatch(app, _event("Ev9", team_id="T-OTHER")) == 200
    probe.assert_not_awaited()
    assert await _dispatch(app, _event("Ev10")) == 200
    probe.assert_awaited_once()


def test_first_delivery_forgets_the_oldest_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(slack_bot, "_SEEN_EVENTS_MAX", 2)
    slack_bot._seen_event_ids.clear()
    assert slack_bot._first_delivery("a") and slack_bot._first_delivery("b")
    assert not slack_bot._first_delivery("a")
    assert slack_bot._first_delivery("c")  # drops "b", the least recently seen
    assert slack_bot._first_delivery("b")
    assert slack_bot._first_delivery("")  # no id: never treated as a repeat
    assert slack_bot._first_delivery("")
    slack_bot._seen_event_ids.clear()
