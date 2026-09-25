"""Settings → Setup status: the checks behind GET /setup/status.

Every probe runs against a mocked transport; nothing here reaches a real
service. The recurring assertion besides the verdict itself: no token ever
comes back in a check's text.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import pytest
from dotenv import dotenv_values
from fastapi.testclient import TestClient

from openexecutive.api import setup_checks
from openexecutive.api.routes import setup_status as setup_route
from openexecutive.api.setup_checks import (
    EXAMPLE_VALUES,
    LABELS,
    SetupCheck,
    Snapshot,
    check_ai_model,
    check_api_protection,
    check_company,
    check_discord,
    check_exec_email,
    check_gmail,
    check_google_chat,
    check_memory,
    check_owner,
    check_scheduler,
    check_slack,
    check_telegram,
    is_example_email,
    run_checks,
)
from openexecutive.audit import AuditEvent
from openexecutive.config import Settings
from openexecutive.people.models import Person

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[4]

SLACK_BOT = "xoxb-111-222-realistic"
SLACK_APP = "xapp-1-A1-realistic"
TELEGRAM = "123456789:AAH" + "x" * 32
DISCORD = "discord-token-realistic"

# Every integration off, so a value in the developer's shell or .env can't
# leak into a check; each test turns on what it exercises.
_ALL_OFF: dict[str, Any] = {
    "ANTHROPIC_API_KEY": "sk-ant-api03-realistic",
    "ANTHROPIC_WORKSPACE_ID": None,
    "EXEC_EMAIL_ADDRESS": "exec@acme.io",
    "OPENROUTER_ENABLED": False,
    "LOCAL_MODELS_ENABLED": False,
    "SLACK_BOT_TOKEN": None,
    "SLACK_APP_TOKEN": None,
    "TELEGRAM_BOT_TOKEN": None,
    "TELEGRAM_WEBHOOK_SECRET": None,
    "DISCORD_BOT_TOKEN": None,
    "DISCORD_APP_ID": None,
    "GOOGLE_CHAT_PROJECT_NUMBER": None,
    "GOOGLE_CHAT_SERVICE_ACCOUNT_FILE": None,
    "GOOGLE_CHAT_SERVICE_ACCOUNT_EMAIL": None,
    "MCP_ENABLED": False,
    "HONCHO_ENABLED": False,
    "SCHEDULER_ENABLED": True,
    "SCHEDULER_POLL_INTERVAL_SECONDS": 30,
}


def make_settings(**overrides: Any) -> Settings:
    return Settings(**{**_ALL_OFF, **overrides})


def make_snap(settings: Settings | None = None, **fields: Any) -> Snapshot:
    values: dict[str, Any] = {
        "settings": settings or make_settings(),
        "now": NOW,
        "local_login": False,
        "people": [],
        "principal": None,
        "last_inbound": {},
    }
    values.update(fields)
    return Snapshot(**values)


def inbound(actor: str, summary: str, ts: str = "2026-09-25T11:50:00.000000Z") -> AuditEvent:
    return AuditEvent(
        id=1,
        ts=ts,
        event_type="integration_inbound",
        session_id=None,
        turn_id=None,
        actor=actor,
        summary=summary,
    )


class Recorder:
    """An httpx transport that answers from a routing function and keeps
    every request it saw."""

    def __init__(self, route: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self._route = route

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._route(request)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


def unreachable(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("no route to host", request=request)


def never_called(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected request to {request.url.host}")


def assert_no_secret(check: SetupCheck, *secrets: str) -> None:
    text = check.model_dump_json()
    for secret in secrets:
        assert secret not in text


# ---------------------------------------------------------------------------
# AI model
# ---------------------------------------------------------------------------


class FakeAnthropic:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.closed = False
        self.models = self

    def with_options(self, **_: Any) -> FakeAnthropic:
        return self

    async def list(self, **_: Any) -> list[Any]:
        if self.error is not None:
            raise self.error
        return []

    async def close(self) -> None:
        self.closed = True


def _status_error(cls: type[anthropic.APIStatusError], status: int) -> anthropic.APIStatusError:
    request = httpx.Request("GET", "https://api.anthropic.com/v1/models")
    return cls(message="refused", response=httpx.Response(status, request=request), body=None)


@pytest.fixture
def fake_anthropic(monkeypatch: pytest.MonkeyPatch) -> Callable[[BaseException | None], FakeAnthropic]:
    def install(error: BaseException | None = None) -> FakeAnthropic:
        client = FakeAnthropic(error)
        monkeypatch.setattr(
            "openexecutive.providers.anthropic_provider.configured_async_client",
            lambda **_: client,
        )
        return client

    return install


async def test_ai_model_connected(fake_anthropic: Callable[..., FakeAnthropic]) -> None:
    client = fake_anthropic()
    check = await check_ai_model(make_snap())
    assert (check.state, check.summary) == ("ok", "Connected to Anthropic.")
    assert client.closed


async def test_ai_model_sample_key_is_red_without_a_call(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(**_: Any) -> None:
        raise AssertionError("the sample key must not be sent anywhere")

    monkeypatch.setattr("openexecutive.providers.anthropic_provider.configured_async_client", boom)
    check = await check_ai_model(make_snap(make_settings(ANTHROPIC_API_KEY="sk-ant-your-key-here")))
    assert check.state == "error"
    assert "sample value" in check.summary


@pytest.mark.parametrize(
    ("error", "state", "phrase"),
    [
        (_status_error(anthropic.AuthenticationError, 401), "error", "turned down the key"),
        (_status_error(anthropic.BadRequestError, 400), "error", "ANTHROPIC_WORKSPACE_ID"),
        (_status_error(anthropic.PermissionDeniedError, 403), "error", "ANTHROPIC_WORKSPACE_ID"),
        (_status_error(anthropic.RateLimitError, 429), "warn", "rate limit"),
        (_status_error(anthropic.InternalServerError, 529), "warn", "HTTP 529"),
        (
            anthropic.APIConnectionError(request=httpx.Request("GET", "https://api.anthropic.com")),
            "warn",
            "Couldn't reach Anthropic",
        ),
        (TimeoutError(), "warn", "Couldn't reach Anthropic"),
    ],
)
async def test_ai_model_failures(
    fake_anthropic: Callable[..., FakeAnthropic], error: BaseException, state: str, phrase: str
) -> None:
    client = fake_anthropic(error)
    check = await check_ai_model(make_snap())
    assert check.state == state
    assert phrase in f"{check.summary} {check.fix}"
    assert client.closed
    assert_no_secret(check, "sk-ant-api03-realistic")


async def test_ai_model_bad_request_with_a_workspace_set_points_at_the_workspace(
    fake_anthropic: Callable[..., FakeAnthropic],
) -> None:
    fake_anthropic(_status_error(anthropic.BadRequestError, 400))
    check = await check_ai_model(make_snap(make_settings(ANTHROPIC_WORKSPACE_ID="wrk_1")))
    assert check.state == "error"
    assert "belongs to that workspace" in (check.fix or "")


async def test_ai_model_without_anthropic_names_the_provider_in_use() -> None:
    settings = make_settings(
        ANTHROPIC_API_KEY=None, OPENROUTER_ENABLED=True, OPENROUTER_API_KEY="sk-or-realistic"
    )
    check = await check_ai_model(make_snap(settings))
    assert check.state == "ok"
    assert "OpenRouter" in check.summary


# ---------------------------------------------------------------------------
# Company, owner, the Executive's address, API protection
# ---------------------------------------------------------------------------


def test_exec_email() -> None:
    assert check_exec_email(make_snap(make_settings(EXEC_EMAIL_ADDRESS="nobody"))).state == "error"
    sample = check_exec_email(make_snap(make_settings(EXEC_EMAIL_ADDRESS="exec@example.com")))
    assert sample.state == "warn" and "sample address" in sample.summary
    real = check_exec_email(make_snap())
    assert (real.state, real.summary) == ("ok", "The Executive sends email as exec@acme.io.")


def test_is_example_email() -> None:
    assert is_example_email("exec@example.com")
    assert is_example_email("A@Example.ORG")
    assert is_example_email("a@corp.example")
    assert not is_example_email("a@example.company.com")
    assert not is_example_email("a@acme.io")


def test_company(tmp_path: Path) -> None:
    profile = tmp_path / "profile.yaml"
    missing = check_company(make_snap(make_settings(COMPANY_PROFILE_PATH=str(profile))))
    assert (missing.state, missing.link) == ("warn", "/onboard")

    profile.write_text("company:\n  name: Acme Robotics\n", encoding="utf-8")
    done = check_company(make_snap(make_settings(COMPANY_PROFILE_PATH=str(profile))))
    assert (done.state, done.summary) == ("ok", "Set up for Acme Robotics.")


def test_owner() -> None:
    assert check_owner(make_snap()).link == "/onboard"

    unlinked = Person(id=1, full_name="Ada Park", is_principal=True)
    warn = check_owner(make_snap(principal=unlinked))
    assert (warn.state, warn.link) == ("warn", "/people")
    assert "owner-only actions will be refused" in warn.summary

    # Local login signs in as the owner, so no email is needed.
    assert check_owner(make_snap(principal=unlinked, local_login=True)).state == "ok"

    linked = Person(id=1, full_name="Ada Park", is_principal=True, email="ada@acme.io")
    assert check_owner(make_snap(principal=linked)).summary == "Ada Park (ada@acme.io) is the owner."


def test_api_protection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BACKEND_SHARED_SECRET", "s3cret-value")
    protected = check_api_protection(make_snap())
    assert protected.state == "ok"
    assert_no_secret(protected, "s3cret-value")

    monkeypatch.delenv("BACKEND_SHARED_SECRET")
    assert check_api_protection(make_snap(local_login=True)).state == "ok"
    assert check_api_protection(make_snap()).state == "warn"


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------


def slack_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "team": "Acme HQ", "user": "exec"})


def listening_handler(connected: bool = True) -> Any:
    async def is_connected() -> bool:
        return connected

    return SimpleNamespace(client=SimpleNamespace(is_connected=is_connected))


def slack_settings(**overrides: Any) -> Settings:
    return make_settings(**{"SLACK_BOT_TOKEN": SLACK_BOT, "SLACK_APP_TOKEN": SLACK_APP, **overrides})


async def test_slack_off_and_sample_tokens_make_no_call() -> None:
    http = Recorder(never_called)
    async with http.client() as client:
        off = await check_slack(make_snap(), client)
        sample = await check_slack(
            make_snap(make_settings(SLACK_BOT_TOKEN="xoxb-your-bot-token", SLACK_APP_TOKEN="xapp-your-app-token")),
            client,
        )
        wrong_kind = await check_slack(make_snap(slack_settings(SLACK_BOT_TOKEN="xoxp-user-token")), client)
        wrong_app = await check_slack(make_snap(slack_settings(SLACK_APP_TOKEN="xoxb-not-an-app-token")), client)
    assert off.state == "off"
    assert sample.state == "error" and "sample tokens" in sample.summary
    assert wrong_kind.state == "error" and "xoxb-" in wrong_kind.summary
    assert wrong_app.state == "error" and "xapp-" in wrong_app.summary
    assert http.requests == []


async def test_slack_connected_and_listening() -> None:
    http = Recorder(slack_ok)
    ada = Person(id=1, full_name="Ada", slack_user_id="U1")
    snap = make_snap(
        slack_settings(),
        slack_handler=listening_handler(),
        people=[ada],
        last_inbound={"slack": inbound("slack", "Inbound slack from user=U1 channel=D1: hello there")},
    )
    async with http.client() as client:
        check = await check_slack(snap, client)
    assert (check.state, check.summary) == ("ok", "Connected to Acme HQ and listening.")
    assert check.last_activity == "2026-09-25T11:50:00.000000Z"
    # Message content from the audit row never reaches the page.
    assert "hello there" not in check.model_dump_json()
    [request] = http.requests
    assert request.url == "https://slack.com/api/auth.test"
    assert request.headers["authorization"] == f"Bearer {SLACK_BOT}"
    assert_no_secret(check, SLACK_BOT, SLACK_APP)


async def test_slack_rejected_token_shows_only_a_clean_error_code() -> None:
    async with Recorder(lambda r: httpx.Response(200, json={"ok": False, "error": "invalid_auth"})).client() as c:
        check = await check_slack(make_snap(slack_settings()), c)
    assert check.state == "error"
    assert check.summary == "Slack turned down SLACK_BOT_TOKEN (invalid_auth)."

    odd = {"ok": False, "error": "<script>alert(1)</script>"}
    async with Recorder(lambda r: httpx.Response(200, json=odd)).client() as c:
        check = await check_slack(make_snap(slack_settings()), c)
    assert check.summary == "Slack turned down SLACK_BOT_TOKEN."


async def test_slack_states_after_the_token_works() -> None:
    async with Recorder(unreachable).client() as c:
        assert (await check_slack(make_snap(slack_settings()), c)).state == "warn"
    async with Recorder(slack_ok).client() as c:
        send_only = await check_slack(make_snap(make_settings(SLACK_BOT_TOKEN=SLACK_BOT)), c)
        not_started = await check_slack(make_snap(slack_settings()), c)
        not_listening = await check_slack(
            make_snap(slack_settings(), slack_handler=listening_handler(False)), c
        )
    assert send_only.state == "warn" and "won't hear messages" in send_only.summary
    assert not_started.state == "error" and "didn't start" in not_started.summary
    assert not_listening.state == "warn"


async def test_slack_nobody_on_the_team_list_can_reach_it() -> None:
    snap = make_snap(slack_settings(), slack_handler=listening_handler(), people=[Person(id=1, full_name="Ada")])
    async with Recorder(slack_ok).client() as c:
        check = await check_slack(snap, c)
    assert (check.state, check.link) == ("warn", "/people")
    assert "nobody on the team list has a Slack member ID" in check.summary


async def test_slack_last_message_was_turned_away() -> None:
    snap = make_snap(
        slack_settings(),
        slack_handler=listening_handler(),
        people=[Person(id=1, full_name="Ada", slack_user_id="U1")],
        last_inbound={"slack": inbound("slack", "Rejected: slack user=U999 not in People roster")},
    )
    async with Recorder(slack_ok).client() as c:
        check = await check_slack(snap, c)
    assert check.state == "warn"
    assert "(from Slack member ID U999) was ignored" in check.summary


@pytest.mark.parametrize(
    ("people_ids", "rejected_at"),
    [
        (["U1", "U999"], "2026-09-25T11:50:00.000000Z"),  # the sender has since been added
        (["U1"], "2026-09-23T11:50:00.000000Z"),  # turned away days ago
    ],
)
async def test_slack_turned_away_message_stops_warning(people_ids: list[str], rejected_at: str) -> None:
    snap = make_snap(
        slack_settings(),
        slack_handler=listening_handler(),
        people=[Person(id=i, full_name=f"P{i}", slack_user_id=uid) for i, uid in enumerate(people_ids, 1)],
        last_inbound={"slack": inbound("slack", "Rejected: slack user=U999 not in People roster", rejected_at)},
    )
    async with Recorder(slack_ok).client() as c:
        check = await check_slack(snap, c)
    assert check.state == "ok"


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------


def failed_task(exc_name: str) -> asyncio.Future[None]:
    future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    future.set_exception(type(exc_name, (Exception,), {})())
    return future


async def test_discord_connected() -> None:
    bot = SimpleNamespace(is_ready=lambda: True, user="exec-bot#0001", ws=SimpleNamespace(open=True))
    snap = make_snap(
        make_settings(DISCORD_BOT_TOKEN=DISCORD),
        discord_bot=bot,
        people=[Person(id=1, full_name="Ada", discord_user_id="42")],
    )
    async with Recorder(never_called).client() as c:
        check = await check_discord(snap, c)
    assert (check.state, check.summary) == ("ok", "Connected as exec-bot#0001.")


async def test_discord_ready_but_reconnecting_is_not_green() -> None:
    # discord.py keeps is_ready() through a dropped connection; the closed
    # gateway socket is what says no messages can arrive.
    bot = SimpleNamespace(is_ready=lambda: True, user="exec-bot#0001", ws=SimpleNamespace(open=False))
    snap = make_snap(make_settings(DISCORD_BOT_TOKEN=DISCORD), discord_bot=bot)
    async with Recorder(never_called).client() as c:
        check = await check_discord(snap, c)
    assert check.state == "warn" and "reconnecting" in check.summary


@pytest.mark.parametrize(
    ("exc_name", "phrase"),
    [
        ("LoginFailure", "turned down DISCORD_BOT_TOKEN"),
        ("PrivilegedIntentsRequired", "Message Content intent"),
        ("GatewayNotFound", "stopped (GatewayNotFound)"),
    ],
)
async def test_discord_stopped(exc_name: str, phrase: str) -> None:
    bot = SimpleNamespace(is_ready=lambda: False, user=None)
    snap = make_snap(
        make_settings(DISCORD_BOT_TOKEN=DISCORD), discord_bot=bot, discord_bot_task=failed_task(exc_name)
    )
    async with Recorder(never_called).client() as c:
        check = await check_discord(snap, c)
    assert check.state == "error"
    assert phrase in check.summary


async def test_discord_stopped_task_wins_over_a_stale_ready_flag() -> None:
    bot = SimpleNamespace(is_ready=lambda: True, user="exec-bot#0001", ws=SimpleNamespace(open=True))
    snap = make_snap(
        make_settings(DISCORD_BOT_TOKEN=DISCORD), discord_bot=bot, discord_bot_task=failed_task("LoginFailure")
    )
    async with Recorder(never_called).client() as c:
        check = await check_discord(snap, c)
    assert check.state == "error" and "turned down DISCORD_BOT_TOKEN" in check.summary


async def test_discord_task_that_ended_without_an_error_is_stopped() -> None:
    finished: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    finished.set_result(None)
    bot = SimpleNamespace(is_ready=lambda: False, user=None)
    snap = make_snap(make_settings(DISCORD_BOT_TOKEN=DISCORD), discord_bot=bot, discord_bot_task=finished)
    async with Recorder(never_called).client() as c:
        check = await check_discord(snap, c)
    assert (check.state, check.summary) == ("error", "The Discord bot stopped.")


async def test_discord_still_connecting_tests_the_token() -> None:
    settings = make_settings(DISCORD_BOT_TOKEN=DISCORD)
    bot = SimpleNamespace(is_ready=lambda: False, user=None)
    rejected = Recorder(lambda r: httpx.Response(401, json={"message": "401: Unauthorized"}))
    async with rejected.client() as c:
        check = await check_discord(make_snap(settings, discord_bot=bot), c)
    assert check.state == "error" and "turned down" in check.summary
    assert rejected.requests[0].headers["authorization"] == f"Bot {DISCORD}"
    assert_no_secret(check, DISCORD)

    async with Recorder(lambda r: httpx.Response(200, json={"id": "1"})).client() as c:
        assert (await check_discord(make_snap(settings, discord_bot=bot), c)).state == "warn"
    async with Recorder(unreachable).client() as c:
        assert (await check_discord(make_snap(settings, discord_bot=bot), c)).state == "warn"


async def test_discord_off_or_never_started() -> None:
    async with Recorder(never_called).client() as c:
        assert (await check_discord(make_snap(), c)).state == "off"
        never = await check_discord(make_snap(make_settings(DISCORD_BOT_TOKEN=DISCORD)), c)
    assert never.state == "error" and "didn't start" in never.summary


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


def telegram(webhook: dict[str, Any], me: dict[str, Any] | None = None) -> Recorder:
    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getMe"):
            return httpx.Response(200, json=me or {"ok": True, "result": {"username": "acme_exec_bot"}})
        return httpx.Response(200, json={"ok": True, "result": webhook})

    return Recorder(route)


GOOD_HOOK = {"url": "https://exec.acme.io/webhook/telegram", "pending_update_count": 0}


def telegram_settings(**overrides: Any) -> Settings:
    return make_settings(
        **{"TELEGRAM_BOT_TOKEN": TELEGRAM, "TELEGRAM_WEBHOOK_SECRET": "hook-secret", **overrides}
    )


async def test_telegram_receiving() -> None:
    http = telegram(GOOD_HOOK)
    snap = make_snap(telegram_settings(), people=[Person(id=1, full_name="Ada", telegram_chat_id="7")])
    async with http.client() as c:
        check = await check_telegram(snap, c)
    assert (check.state, check.summary) == ("ok", "@acme_exec_bot is receiving messages.")
    assert [r.url.path for r in http.requests] == [f"/bot{TELEGRAM}/getMe", f"/bot{TELEGRAM}/getWebhookInfo"]
    assert_no_secret(check, TELEGRAM, "hook-secret")


async def test_telegram_bad_token_is_never_put_in_a_url() -> None:
    http = Recorder(never_called)
    async with http.client() as c:
        malformed = await check_telegram(make_snap(make_settings(TELEGRAM_BOT_TOKEN="1:abc/../x")), c)
    assert malformed.state == "error" and "isn't a Telegram bot token" in malformed.summary
    assert http.requests == []


async def test_telegram_local_login_needs_the_secret() -> None:
    async with Recorder(never_called).client() as c:
        check = await check_telegram(
            make_snap(make_settings(TELEGRAM_BOT_TOKEN=TELEGRAM), local_login=True), c
        )
    assert check.state == "error" and "Local login" in check.summary


@pytest.mark.parametrize(
    ("hook", "state", "phrase"),
    [
        ({"url": ""}, "warn", "doesn't know where to deliver"),
        ({"url": "https://other.example/bot"}, "warn", "isn't this app's /webhook/telegram"),
        (
            {**GOOD_HOOK, "last_error_date": int(NOW.timestamp()) - 60, "last_error_message": "Wrong response from the webhook: 401 Unauthorized"},
            "warn",
            "couldn't deliver @acme_exec_bot's last message: Wrong response from the webhook: 401 Unauthorized",
        ),
    ],
)
async def test_telegram_delivery_problems(hook: dict[str, Any], state: str, phrase: str) -> None:
    snap = make_snap(telegram_settings(), people=[Person(id=1, full_name="Ada", telegram_chat_id="7")])
    async with telegram(hook).client() as c:
        check = await check_telegram(snap, c)
    assert check.state == state
    assert phrase in check.summary


async def test_telegram_odd_answers_do_not_crash_the_check() -> None:
    odd = Recorder(lambda r: httpx.Response(200, json={"ok": True, "result": "not an object"}))
    async with odd.client() as c:
        check = await check_telegram(make_snap(telegram_settings()), c)
    assert check.state == "warn" and "The bot works" in check.summary


async def test_telegram_old_delivery_error_is_ignored() -> None:
    old = {**GOOD_HOOK, "last_error_date": int((NOW - timedelta(days=3)).timestamp()), "last_error_message": "old"}
    snap = make_snap(telegram_settings(), people=[Person(id=1, full_name="Ada", telegram_chat_id="7")])
    async with telegram(old).client() as c:
        assert (await check_telegram(snap, c)).state == "ok"


async def test_telegram_rejected_token_and_missing_secret() -> None:
    async with telegram(GOOD_HOOK, me={"ok": False, "error_code": 401}).client() as c:
        rejected = await check_telegram(make_snap(telegram_settings()), c)
    assert rejected.state == "error" and "turned down" in rejected.summary

    snap = make_snap(make_settings(TELEGRAM_BOT_TOKEN=TELEGRAM))
    async with telegram(GOOD_HOOK).client() as c:
        open_hook = await check_telegram(snap, c)
    assert open_hook.state == "warn" and "fake ones" in open_hook.summary


# ---------------------------------------------------------------------------
# Google Chat and Gmail
# ---------------------------------------------------------------------------


def test_google_chat(tmp_path: Path) -> None:
    assert check_google_chat(make_snap()).state == "off"

    def chat(**overrides: Any) -> SetupCheck:
        return check_google_chat(make_snap(make_settings(**overrides)))

    assert "digits only" in chat(GOOGLE_CHAT_PROJECT_NUMBER="my-project").summary
    assert "no service account" in chat(GOOGLE_CHAT_PROJECT_NUMBER="1234").summary
    missing = chat(GOOGLE_CHAT_PROJECT_NUMBER="1234", GOOGLE_CHAT_SERVICE_ACCOUNT_FILE=str(tmp_path / "nope.json"))
    assert missing.state == "error"

    key = tmp_path / "sa.json"
    key.write_text(json.dumps({"type": "service_account", "client_email": "bot@p.iam.gserviceaccount.com"}))
    assert chat(GOOGLE_CHAT_PROJECT_NUMBER="1234", GOOGLE_CHAT_SERVICE_ACCOUNT_FILE=str(key)).state == "ok"
    assert chat(GOOGLE_CHAT_PROJECT_NUMBER="1234", GOOGLE_CHAT_SERVICE_ACCOUNT_EMAIL="bot@p.iam").state == "ok"

    key.write_text("not json")
    assert chat(GOOGLE_CHAT_PROJECT_NUMBER="1234", GOOGLE_CHAT_SERVICE_ACCOUNT_FILE=str(key)).state == "error"


@pytest.fixture
def gmail_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    config = tmp_path / "mcp_servers.json"
    config.write_text(json.dumps({"mcpServers": {"google_workspace": {"command": "true"}}}))
    for var in ("GWORKSPACE_AUTH_MODE", "GOOGLE_SERVICE_ACCOUNT_KEY_JSON", "GOOGLE_SERVICE_ACCOUNT_KEY_FILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "id.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "oauth-secret")
    return make_settings(MCP_ENABLED=True, MCP_SERVERS_CONFIG_PATH=str(config))


def test_gmail_connected(gmail_settings: Settings) -> None:
    fresh = check_gmail(make_snap(gmail_settings, mcp_gateway=object()))
    assert fresh.state == "ok"
    assert "exec@acme.io's inbox" in fresh.summary and "No email has come in yet" in fresh.summary

    seen = check_gmail(
        make_snap(gmail_settings, mcp_gateway=object(), last_inbound={"email": inbound("email", "Inbound email")})
    )
    assert "No email has come in yet" not in seen.summary
    assert seen.last_activity is not None
    assert_no_secret(seen, "oauth-secret")


def test_gmail_problems(gmail_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    assert check_gmail(make_snap()).state == "off"
    assert "didn't start" in check_gmail(make_snap(gmail_settings)).summary

    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET")
    no_oauth = check_gmail(make_snap(gmail_settings, mcp_gateway=object()))
    assert no_oauth.state == "error" and "GOOGLE_OAUTH_CLIENT_SECRET" in no_oauth.summary

    monkeypatch.setenv("GWORKSPACE_AUTH_MODE", "service_account")
    assert "GOOGLE_SERVICE_ACCOUNT_KEY_JSON" in check_gmail(make_snap(gmail_settings, mcp_gateway=object())).summary
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_KEY_FILE", "/keys/sa.json")
    assert check_gmail(make_snap(gmail_settings, mcp_gateway=object())).state == "ok"


# ---------------------------------------------------------------------------
# Scheduler and memory
# ---------------------------------------------------------------------------


@pytest.fixture
def heartbeat(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    monkeypatch.setattr(
        "openexecutive.scheduler.pause.get_pause_state", lambda: SimpleNamespace(paused=False)
    )

    def set_beat(started: datetime | None, tick: tuple[datetime, str] | None = None) -> None:
        monkeypatch.setattr("openexecutive.scheduler.runner.scheduler_heartbeat", lambda: (started, tick))

    return set_beat


@pytest.mark.parametrize(
    ("started", "tick", "state", "phrase"),
    [
        (None, None, "error", "isn't running"),
        (NOW - timedelta(seconds=20), None, "warn", "Starting up"),
        (NOW - timedelta(minutes=10), None, "error", "10 minutes ago but hasn't finished"),
        (NOW - timedelta(hours=1), (NOW - timedelta(minutes=5), "ran"), "error", "last check was 5 minutes ago"),
        (NOW - timedelta(hours=1), (NOW - timedelta(seconds=10), "failed"), "error", "hit an error"),
        (NOW - timedelta(hours=1), (NOW - timedelta(seconds=10), "waiting_for_company"), "warn", "Waiting for company"),
        (NOW - timedelta(hours=1), (NOW - timedelta(seconds=10), "ran"), "ok", "Running."),
        (NOW - timedelta(hours=1), (NOW - timedelta(seconds=10), "rotating"), "ok", "Running."),
    ],
)
def test_scheduler(
    heartbeat: Callable[..., None],
    started: datetime | None,
    tick: tuple[datetime, str] | None,
    state: str,
    phrase: str,
) -> None:
    heartbeat(started, tick)
    check = check_scheduler(make_snap())
    assert check.state == state
    assert phrase in check.summary


def test_scheduler_off_and_paused(heartbeat: Callable[..., None], monkeypatch: pytest.MonkeyPatch) -> None:
    heartbeat(NOW, (NOW, "paused"))
    assert check_scheduler(make_snap(make_settings(SCHEDULER_ENABLED=False))).state == "off"
    monkeypatch.setattr("openexecutive.scheduler.pause.get_pause_state", lambda: SimpleNamespace(paused=True))
    paused = check_scheduler(make_snap())
    assert paused.state == "warn" and "Resume" in (paused.fix or "")

    # Paused and dead: Resume wouldn't help, so say it has stopped.
    heartbeat(NOW - timedelta(hours=1), (NOW - timedelta(minutes=30), "paused"))
    dead = check_scheduler(make_snap())
    assert dead.state == "error" and "has stopped" in dead.summary


def test_scheduler_stale_window_follows_the_poll_interval(heartbeat: Callable[..., None]) -> None:
    # A 5-minute poll interval: four minutes of silence is just between ticks.
    heartbeat(NOW - timedelta(hours=1), (NOW - timedelta(minutes=4), "ran"))
    slow = make_settings(SCHEDULER_POLL_INTERVAL_SECONDS=300)
    assert check_scheduler(make_snap(slow)).state == "ok"


@pytest.mark.parametrize(
    ("probe", "state"),
    [
        ({"status": "disabled"}, "off"),
        ({"status": "ok", "latency_ms": 12}, "ok"),
        ({"status": "error", "error_type": "TimeoutError", "error_msg": "key=hk-secret expired"}, "error"),
    ],
)
async def test_memory(monkeypatch: pytest.MonkeyPatch, probe: dict[str, Any], state: str) -> None:
    async def honcho_health() -> dict[str, Any]:
        return probe

    monkeypatch.setattr("openexecutive.api.routes.health.honcho_health", honcho_health)
    check = await check_memory(make_snap())
    assert check.state == state
    # Only the error's class name is shown, never its message.
    assert_no_secret(check, "hk-secret")


# ---------------------------------------------------------------------------
# Running them all, and the route
# ---------------------------------------------------------------------------


@pytest.fixture
def check_logs() -> Iterator[list[str]]:
    """Messages logged by api/setup_checks.py. ``caplog`` listens on the
    root logger and would miss them once another test has run
    ``main._configure_logging``, which stops the ``openexecutive`` tree
    propagating — so this attaches to the module's logger directly."""
    messages: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    handler = Collect(level=logging.INFO)
    setup_logger = logging.getLogger(setup_checks.__name__)
    setup_logger.addHandler(handler)
    try:
        yield messages
    finally:
        setup_logger.removeHandler(handler)


async def test_run_checks_keeps_order_and_contains_a_crash(
    monkeypatch: pytest.MonkeyPatch,
    fake_anthropic: Callable[..., FakeAnthropic],
    heartbeat: Callable[..., None],
    check_logs: list[str],
) -> None:
    fake_anthropic()
    heartbeat(NOW - timedelta(minutes=5), (NOW, "ran"))

    async def honcho_health() -> dict[str, Any]:
        return {"status": "disabled"}

    monkeypatch.setattr("openexecutive.api.routes.health.honcho_health", honcho_health)

    def explode(snap: Snapshot) -> SetupCheck:
        raise RuntimeError("token=abc123 leaked into a message")

    monkeypatch.setattr(setup_checks, "check_company", explode)
    async with Recorder(never_called).client() as c:
        checks = await run_checks(make_snap(), c)
    assert [c.id for c in checks] == list(LABELS)
    company = checks[list(LABELS).index("company")]
    assert (company.state, company.summary) == ("error", "This check couldn't run.")
    assert check_logs == ["setup status: the company check crashed (RuntimeError)"]
    assert all(c.label == LABELS[c.id] for c in checks)


@pytest.fixture
def route_calls(monkeypatch: pytest.MonkeyPatch) -> list[Snapshot]:
    for var in ("OE_LOCAL_LOGIN", "OE_PUBLIC_DEPLOYMENT", "BACKEND_SHARED_SECRET"):
        monkeypatch.delenv(var, raising=False)
    calls: list[Snapshot] = []

    def gather(settings: Settings, *, local_login: bool, app_state: Any) -> Snapshot:
        return make_snap(settings, local_login=local_login)

    async def run(snap: Snapshot, http: httpx.AsyncClient) -> list[SetupCheck]:
        calls.append(snap)
        return [SetupCheck(id="ai_model", label="AI model", state="ok", summary="Connected to Anthropic.")]

    monkeypatch.setattr(setup_route, "gather_snapshot", gather)
    monkeypatch.setattr(setup_route, "run_checks", run)
    return calls


def test_route_answers_and_reuses_a_recent_run(
    route_calls: list[Snapshot], monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.api.main import create_app

    client = TestClient(create_app())
    first = client.get("/setup/status")
    assert first.status_code == 200
    body = first.json()
    assert body["checks"] == [
        {
            "id": "ai_model",
            "label": "AI model",
            "state": "ok",
            "summary": "Connected to Anthropic.",
            "fix": None,
            "link": None,
            "last_activity": None,
        }
    ]
    assert datetime.fromisoformat(body["checked_at"]) == NOW
    assert client.get("/setup/status").json() == body
    assert len(route_calls) == 1

    monkeypatch.setattr(setup_route, "_REUSE_FOR_S", 0.0)
    client.get("/setup/status")
    assert len(route_calls) == 2


def test_route_runs_are_kept_per_app(route_calls: list[Snapshot]) -> None:
    from openexecutive.api.main import create_app

    TestClient(create_app()).get("/setup/status")
    TestClient(create_app()).get("/setup/status")
    assert len(route_calls) == 2


def test_route_needs_the_shared_secret_when_one_is_set(
    route_calls: list[Snapshot], monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.api.main import create_app

    monkeypatch.setenv("BACKEND_SHARED_SECRET", "s3cret")
    client = TestClient(create_app())
    assert client.get("/setup/status").status_code == 401
    assert client.get("/setup/status", headers={"x-api-key": "s3cret"}).status_code == 200
    assert len(route_calls) == 1


def test_route_passes_local_login_through(route_calls: list[Snapshot], monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.api.main import create_app

    monkeypatch.setenv("OE_LOCAL_LOGIN", "1")
    TestClient(create_app()).get("/setup/status", headers={"host": "localhost:8000"})
    assert route_calls[0].local_login is True


# ---------------------------------------------------------------------------
# .env.example and the settings that read it
# ---------------------------------------------------------------------------


def test_env_example_has_no_value_that_is_really_a_comment() -> None:
    values = dotenv_values(REPO_ROOT / ".env.example")
    assert values, ".env.example not found"
    comments = {key: value for key, value in values.items() if value and value.lstrip().startswith("#")}
    assert comments == {}


def test_env_example_samples_are_recognised() -> None:
    values = dotenv_values(REPO_ROOT / ".env.example")
    for key in ("ANTHROPIC_API_KEY", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"):
        sample = values.get(key)
        assert not sample or sample in EXAMPLE_VALUES, f"{key}={sample!r} is a sample the checks don't know"
    assert is_example_email(values["EXEC_EMAIL_ADDRESS"] or "")


@pytest.mark.parametrize(
    "field",
    [
        "TELEGRAM_BOT_TOKEN",
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
        "DISCORD_BOT_TOKEN",
        "DISCORD_APP_ID",
        "GOOGLE_CHAT_PROJECT_NUMBER",
        "GOOGLE_CHAT_SERVICE_ACCOUNT_FILE",
        "GOOGLE_CHAT_SERVICE_ACCOUNT_EMAIL",
    ],
)
def test_a_comment_left_as_a_value_means_unset(field: str) -> None:
    settings = make_settings(**{field: "# from @BotFather"})
    assert getattr(settings, field.lower()) is None
    assert getattr(make_settings(**{field: "real-value"}), field.lower()) == "real-value"


async def test_telegram_secret_telegram_would_refuse_is_red() -> None:
    settings = make_settings(TELEGRAM_BOT_TOKEN=TELEGRAM, TELEGRAM_WEBHOOK_SECRET="# from step 2")
    async with Recorder(never_called).client() as c:
        check = await check_telegram(make_snap(settings), c)
    assert check.state == "error" and "characters Telegram won't accept" in check.summary
    assert_no_secret(check, "# from step 2")


def test_a_comment_value_read_from_a_dotenv_file_means_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=          # from @BotFather\n", encoding="utf-8")
    others = {k: v for k, v in _ALL_OFF.items() if k != "TELEGRAM_BOT_TOKEN"}
    settings = Settings(_env_file=str(env_file), **others)  # type: ignore[call-arg]
    assert settings.telegram_bot_token is None


# ---------------------------------------------------------------------------
# The scheduler's heartbeat
# ---------------------------------------------------------------------------


@pytest.fixture
def scheduler_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """run_scheduler against a throwaway DB, with its tick reduced to the
    parts the heartbeat reports on. Returns knobs the test can turn."""
    from openexecutive.memory import episodic
    from openexecutive.scheduler import pause, runner

    db = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", db)
    episodic.initialize_db(db)
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **k: None)
    monkeypatch.setattr(pause, "_read_failing", False)
    monkeypatch.setattr(runner, "_started_at", None)
    monkeypatch.setattr(runner, "_last_tick", None)
    knobs: dict[str, Any] = {"company": True, "claim_error": None}

    def claim(now: datetime) -> list[Any]:
        if knobs["claim_error"] is not None:
            raise knobs["claim_error"]
        return []

    monkeypatch.setattr(runner, "claim_due_actions", claim)
    monkeypatch.setattr(runner, "_maybe_sweep_alerts", lambda now: 0)
    monkeypatch.setattr(runner, "_company_profile_active", lambda: knobs["company"])
    monkeypatch.setattr(runner, "seed_principal_briefs", lambda: 0)
    return knobs


async def _one_tick() -> tuple[datetime | None, tuple[datetime, str] | None]:
    from openexecutive.scheduler.runner import run_scheduler, scheduler_heartbeat

    task = asyncio.create_task(run_scheduler(gateway=None, poll_interval_seconds=60))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    return scheduler_heartbeat()


@pytest.mark.parametrize(
    ("knob", "value", "outcome"),
    [
        (None, None, "ran"),
        ("company", False, "waiting_for_company"),
        ("claim_error", RuntimeError("db locked"), "failed"),
    ],
)
async def test_every_tick_records_itself(
    scheduler_loop: dict[str, Any], knob: str | None, value: Any, outcome: str
) -> None:
    if knob is not None:
        scheduler_loop[knob] = value
    started, tick = await _one_tick()
    assert started is not None
    assert tick is not None and tick[1] == outcome
    assert started <= tick[0] <= datetime.now(UTC)


async def test_a_restart_forgets_the_last_run_s_tick(
    scheduler_loop: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.scheduler import runner

    monkeypatch.setattr(runner, "_last_tick", (datetime(2020, 1, 1, tzinfo=UTC), "ran"))
    seen_at_boot: list[Any] = []

    def requeue() -> int:
        seen_at_boot.append(runner.scheduler_heartbeat()[1])
        return 0

    monkeypatch.setattr(runner, "requeue_orphaned_running", requeue)
    await _one_tick()
    assert seen_at_boot == [None]


async def test_a_paused_tick_records_itself(scheduler_loop: dict[str, Any]) -> None:
    from openexecutive.scheduler import pause

    pause.pause("ceo@example.com")
    _, tick = await _one_tick()
    assert tick is not None and tick[1] == "paused"
