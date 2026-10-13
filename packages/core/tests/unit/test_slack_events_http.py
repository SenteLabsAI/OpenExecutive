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
from collections import OrderedDict
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
    monkeypatch.setattr(slack_events, "_listener", None)
    monkeypatch.setattr(slack_events, "_seen", OrderedDict())


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


def test_an_unsigned_body_never_reaches_the_log(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """Bolt logs the whole body of a request that fails its signature check;
    the route refuses it before Bolt sees it."""
    import logging

    _settings(monkeypatch)
    caplog.set_level(logging.DEBUG)
    with TestClient(create_app()) as client:
        res = client.post(
            "/webhook/slack",
            content=b"x=1\nFORGED_LINE the secret is hunter2",
            headers={"x-slack-request-timestamp": str(int(time.time())), "x-slack-signature": "v0=00"},
        )
    assert res.status_code == 401
    assert "FORGED_LINE" not in caplog.text


@pytest.mark.parametrize("timestamp", ["abc", "\u00b2", "9" * 5000])
def test_an_unreadable_timestamp_is_a_bad_signature(monkeypatch: pytest.MonkeyPatch, timestamp: str) -> None:
    _settings(monkeypatch)
    assert not slack_events.signature_valid(SECRET, b"{}", timestamp, "v0=00")
    if timestamp.isascii():
        with TestClient(create_app()) as client:
            res = client.post(
                "/webhook/slack", content=b"{}",
                headers={"x-slack-request-timestamp": timestamp, "x-slack-signature": "v0=00"},
            )
        assert res.status_code == 401


def test_a_signed_body_that_is_not_json_is_a_bad_request(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch)
    with TestClient(create_app()) as client:
        raw = b"{"
        ts = str(int(time.time()))
        digest = hmac.new(SECRET.encode(), f"v0:{ts}:".encode() + raw, hashlib.sha256).hexdigest()
        res = client.post(
            "/webhook/slack", content=raw,
            headers={"content-type": "application/json", "x-slack-request-timestamp": ts, "x-slack-signature": f"v0={digest}"},
        )
    assert res.status_code == 400


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
        assert slack_events._listener is not None
    assert slack_events._listener is None


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


def _counting_handler(monkeypatch: pytest.MonkeyPatch, client: TestClient, *, fail: list[Any] | None = None) -> list[bytes]:
    """Wrap the live listener's Bolt handler, recording each body it is handed.
    ``fail`` holds what each call does first: an exception to raise or a status."""
    from starlette.responses import Response

    listener = slack_events._listener
    assert listener is not None
    real = listener.request_handler
    seen: list[bytes] = []
    outcomes = list(fail or [])

    class Wrapped:
        async def handle(self, request: Any) -> Any:
            seen.append(await request.body())
            if outcomes:
                outcome = outcomes.pop(0)
                if isinstance(outcome, BaseException):
                    raise outcome
                return Response(status_code=outcome)
            return await real.handle(request)

    monkeypatch.setattr(listener, "request_handler", Wrapped())
    return seen


def test_an_identical_replay_never_reaches_bolt(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch)
    logged: list[str] = []
    monkeypatch.setattr(slack_events.logger, "info", lambda msg, *args: logged.append(msg % args))
    with TestClient(create_app()) as client:
        handled = _counting_handler(monkeypatch, client)
        raw, headers = _signed(CHALLENGE)
        first = client.post("/webhook/slack", content=raw, headers=headers)
        again = client.post("/webhook/slack", content=raw, headers=headers)
    assert first.status_code == 200 and first.json()["challenge"] == "abc123"
    assert again.status_code == 200 and again.content == b""
    assert len(handled) == 1
    assert logged == ["Slack: dropped a repeated request to /webhook/slack (a replay)"]


def test_a_replay_of_a_form_payload_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not only JSON events: any signed body is remembered by its signature."""
    _settings(monkeypatch)
    raw = b"payload=%7B%22type%22%3A%22block_actions%22%7D"
    ts = str(int(time.time()))
    digest = hmac.new(SECRET.encode(), f"v0:{ts}:".encode() + raw, hashlib.sha256).hexdigest()
    headers = {
        "content-type": "application/x-www-form-urlencoded",
        "x-slack-request-timestamp": ts,
        "x-slack-signature": f"v0={digest}",
    }
    with TestClient(create_app()) as client:
        handled = _counting_handler(monkeypatch, client)
        client.post("/webhook/slack", content=raw, headers=headers)
        again = client.post("/webhook/slack", content=raw, headers=headers)
    assert again.status_code == 200 and again.content == b""
    assert len(handled) == 1


def test_a_resigned_slack_retry_of_an_event_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch)
    with TestClient(create_app()) as client:
        handled = _counting_handler(monkeypatch, client)
        # No listener takes reaction_added in the real app, so Bolt answers 404.
        raw, headers = _signed(_event("EvRetry"))
        assert client.post("/webhook/slack", content=raw, headers=headers).status_code < 500
        # Slack's retry: same event, a later timestamp, so a new signature.
        raw2, headers2 = _signed({**_event("EvRetry"), "event_time": 1}, age_s=-1)
        assert headers2["x-slack-signature"] != headers["x-slack-signature"]
        retry = client.post("/webhook/slack", content=raw2, headers={**headers2, "x-slack-retry-num": "1"})
        other = _signed(_event("EvOther"))
        assert client.post("/webhook/slack", content=other[0], headers=other[1]).status_code < 500
    assert retry.status_code == 200 and retry.content == b""
    assert len(handled) == 2


@pytest.mark.parametrize("failure", [RuntimeError("bolt broke"), 500])
def test_a_delivery_bolt_failed_can_be_retried(monkeypatch: pytest.MonkeyPatch, failure: Any) -> None:
    _settings(monkeypatch)
    with TestClient(create_app(), raise_server_exceptions=False) as client:
        handled = _counting_handler(monkeypatch, client, fail=[failure])
        raw, headers = _signed(_event("EvFail"))
        first = client.post("/webhook/slack", content=raw, headers=headers)
        retry = client.post("/webhook/slack", content=raw, headers={**headers, "x-slack-retry-num": "1"})
    assert first.status_code == 500
    assert retry.status_code < 500 and retry.content != b""
    assert len(handled) == 2


def test_seen_keys_expire_and_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(slack_events, "SEEN_MAX", 3)
    assert slack_events._first_sight(["a"], now=0.0)
    assert not slack_events._first_sight(["a"], now=10.0)
    # A repeat records nothing, even for a key it carried that was new.
    assert not slack_events._first_sight(["a", "b"], now=10.0)
    assert slack_events._first_sight(["b"], now=10.0)
    assert slack_events._first_sight(["a"], now=slack_events.SEEN_TTL_S + 1)  # "a" expired
    for key in ("c", "d", "e"):
        assert slack_events._first_sight([key], now=slack_events.SEEN_TTL_S + 2)
    assert len(slack_events._seen) == 3
    assert "b" not in slack_events._seen  # the oldest went first


def test_ttl_covers_the_whole_signature_window() -> None:
    assert slack_events.SEEN_TTL_S >= 2 * slack_events.SIGNATURE_MAX_AGE_S


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (json.dumps({"type": "event_callback", "event_id": "Ev7"}).encode(), ["sig:s", "event:Ev7"]),
        (json.dumps({"type": "url_verification", "event_id": "Ev7"}).encode(), ["sig:s"]),
        (json.dumps({"type": "event_callback", "event_id": 7}).encode(), ["sig:s"]),
        (b"{not json", ["sig:s"]),
        (b"payload=x", ["sig:s"]),
    ],
)
def test_dedup_keys(body: bytes, expected: list[str]) -> None:
    assert slack_events._dedup_keys(body, "s") == expected
