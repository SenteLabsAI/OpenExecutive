"""What the owner is told when a brief doesn't reach them, and why.

  * Email set up but the MCP gateway down is ``gateway_down`` — the
    Executive's fault, named as such — not ``no_channel`` (which told the
    owner to connect what they had already connected).
  * A send that fails records what happened, per channel, and the notice
    repeats it; a dead grant in it turns the fix into "reconnect".
  * A delivery asks the gateway supervisor for a start first.
  * A Google tool answering with an authorization failure marks the server
    as needing a reconnect: the prompt block stops saying "connected", the
    Setup page says so, and a working answer clears it.
"""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.setup_checks import Snapshot, check_brief, check_gmail
from openexecutive.briefing import brief_state, narrative_cache
from openexecutive.briefing.brief_state import DeliveryOutcome
from openexecutive.config import Settings
from openexecutive.orchestrator import mcp_gateway
from openexecutive.orchestrator.mcp_gateway import (
    MCPGateway,
    gateway_reconnect_needed,
    grant_failure,
)
from openexecutive.people.models import Person
from openexecutive.prompts.connected_systems import render_connected_systems
from openexecutive.scheduler import runner

NOW = datetime(2026, 10, 13, 8, 0, tzinfo=UTC)
MORNING = "principal_brief_morning"
DEAD_GRANT = json.dumps({
    "error": "RefreshError: ('invalid_grant: Token has been expired or revoked.', "
    "{'error': 'invalid_grant', 'error_description': 'Token has been expired or revoked.'})"
})


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("EXEC_EMAIL_ADDRESS", "exec@acme.io")
    monkeypatch.setattr(narrative_cache, "DB_PATH", tmp_path / "cache.db")
    monkeypatch.setattr(mcp_gateway, "_active_gateway", None)
    monkeypatch.setattr(mcp_gateway, "_supervisor", None)


def _owner(**fields: Any) -> Person:
    return Person(**{"id": 3, "full_name": "Ada", "is_principal": True, "email": "ada@acme.io", **fields})


def _mail_configured(monkeypatch: pytest.MonkeyPatch, configured: bool = True) -> None:
    monkeypatch.setattr(
        mcp_gateway, "configured_server_names",
        lambda _path: ["google_workspace"] if configured else [],
    )


# -- the record and the notice ---------------------------------------------------


def test_the_detail_round_trips_and_older_rows_still_read() -> None:
    brief_state.record_delivery_outcome(
        MORNING, reason="send_failed", channel=None, detail="email:  invalid_grant\n revoked",
    )
    latest = brief_state.last_delivery_outcome()
    assert latest is not None
    assert (latest.reason, latest.channel, latest.detail) == ("send_failed", None, "email: invalid_grant revoked")

    brief_state.record_delivery_outcome(MORNING, reason="delivered", channel="email")
    latest = brief_state.last_delivery_outcome()
    assert latest is not None and (latest.channel, latest.detail) == ("email", None)


def test_a_long_detail_is_cut_to_a_line() -> None:
    brief_state.record_delivery_outcome(MORNING, reason="send_failed", channel=None, detail="x" * 1000)
    latest = brief_state.last_delivery_outcome()
    assert latest is not None and latest.detail is not None
    assert len(latest.detail) == brief_state.DETAIL_MAX_CHARS


@pytest.mark.parametrize(
    ("reason", "detail", "problem", "fix_has"),
    [
        ("gateway_down", "the MCP gateway failed to start (3 attempts): RuntimeError: boom",
         "the Executive's mail connection wasn't running (the MCP gateway failed to start (3 attempts): "
         "RuntimeError: boom)", "retrying on its own"),
        ("send_failed", "email: invalid_grant: Token has been expired or revoked",
         "every way of sending it failed (email: invalid_grant: Token has been expired or revoked)",
         "connect it again"),
        ("send_failed", "slack_dm: channel_not_found",
         "every way of sending it failed (slack_dm: channel_not_found)", "Setup status page"),
        ("no_channel", "ignored", "nothing is set up to send it to you", "Connect Gmail"),
        ("send_failed", None, "every way of sending it failed", "Setup status page"),
    ],
)
def test_describe_problem(reason: str, detail: str | None, problem: str, fix_has: str) -> None:
    got_problem, got_fix = brief_state.describe_problem(reason, detail)  # type: ignore[arg-type]
    assert got_problem == problem
    assert fix_has in got_fix


@pytest.mark.parametrize(
    ("recorded", "gateway_down", "can_deliver", "problem"),
    [
        ("no_channel", True, False, "gateway_down"),  # it was the gateway all along
        ("gateway_down", True, False, "gateway_down"),
        ("gateway_down", False, False, "no_channel"),  # the gateway is up, nothing else is
        ("gateway_down", False, True, None),  # fixed since
        ("no_owner", True, False, "gateway_down"),
    ],
)
def test_current_problem_names_the_gateway(
    recorded: str, gateway_down: bool, can_deliver: bool, problem: str | None
) -> None:
    outcome = DeliveryOutcome(MORNING, recorded, None, NOW)  # type: ignore[arg-type]
    assert brief_state.current_problem(
        outcome, has_owner=True, can_deliver=can_deliver, gateway_down=gateway_down
    ) == problem


def test_no_owner_still_wins_over_the_gateway() -> None:
    outcome = DeliveryOutcome(MORNING, "no_channel", None, NOW)
    assert brief_state.current_problem(outcome, has_owner=False, can_deliver=False, gateway_down=True) == "no_owner"


@pytest.fixture
def notice_client(monkeypatch: pytest.MonkeyPatch) -> Any:
    from openexecutive.api.routes import chat as chat_route
    from openexecutive.api.routes import today as today_route

    state: dict[str, Any] = {"principal": _owner(), "plan": [], "problem": None}
    monkeypatch.setattr(chat_route, "_caller_is_principal_or_unclaimed", lambda _r: True)
    monkeypatch.setattr(runner, "principal_delivery_plan", lambda: (state["principal"], state["plan"]))
    monkeypatch.setattr(runner, "mail_gateway_problem", lambda _p, **_kw: state["problem"])
    app = FastAPI()
    app.include_router(today_route.router)
    return TestClient(app), state


def test_the_notice_blames_the_gateway_not_the_owner(notice_client: Any) -> None:
    client, state = notice_client
    state["problem"] = "the MCP gateway failed to start (2 attempts): RuntimeError: boom"
    brief_state.record_delivery_outcome(MORNING, reason="gateway_down", channel=None, detail="stale")
    body = client.get("/today/brief-delivery").json()
    assert body["problem"] == (
        "the Executive's mail connection wasn't running "
        "(the MCP gateway failed to start (2 attempts): RuntimeError: boom)"
    )
    assert "retrying on its own" in body["fix"]


def test_the_notice_repeats_what_the_send_said(notice_client: Any) -> None:
    client, state = notice_client
    state["plan"] = ["email"]
    brief_state.record_delivery_outcome(
        MORNING, reason="send_failed", channel=None, detail="email: invalid_grant: Token has been expired or revoked",
    )
    body = client.get("/today/brief-delivery").json()
    assert "invalid_grant" in body["problem"] and "connect it again" in body["fix"]


# -- the delivery path -----------------------------------------------------------


class _Supervisor:
    def __init__(self, gateway: Any = None, problem: str | None = "the MCP gateway failed to start (1 attempt)") -> None:
        self.gateway = gateway
        self.ensured = 0
        self._problem = problem

    async def ensure(self) -> Any:
        self.ensured += 1
        if self.gateway is not None:
            mcp_gateway.set_active_gateway(self.gateway)
        return self.gateway

    def problem(self, *, with_error: bool = True) -> str | None:
        return None if self.gateway is not None else self._problem


def test_email_only_with_the_gateway_down_is_gateway_down(monkeypatch: pytest.MonkeyPatch) -> None:
    _mail_configured(monkeypatch)
    sup = _Supervisor()
    mcp_gateway.set_gateway_supervisor(sup)

    result = asyncio.run(runner.deliver_to_person(_owner(), "BRIEF", label="Morning Brief"))

    assert sup.ensured == 1, "the delivery asked for a start first"
    assert result.reason == "gateway_down" and not result.ok
    assert result.detail == "email not available: the MCP gateway failed to start (1 attempt)"


def test_no_email_set_up_is_still_no_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    _mail_configured(monkeypatch, configured=False)
    sup = _Supervisor()
    mcp_gateway.set_gateway_supervisor(sup)

    result = asyncio.run(runner.deliver_to_person(_owner(), "BRIEF"))

    assert sup.ensured == 0, "nothing to start for"
    assert result.reason == "no_channel"


def test_a_late_gateway_carries_the_brief(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gateway was down at boot; the re-check before the send brings it
    up, and the brief goes by email."""
    _mail_configured(monkeypatch)
    sent: list[dict[str, Any]] = []

    class _Gateway:
        async def call_tool(self, tool_input: dict[str, Any]) -> str:
            sent.append(tool_input)
            return "Email sent! Message ID: m-1"

    mcp_gateway.set_gateway_supervisor(_Supervisor(gateway=_Gateway()))

    result = asyncio.run(runner.deliver_to_person(_owner(), "BRIEF", label="Morning Brief"))

    assert result.ok and result.channel == "email" and len(sent) == 1


def test_chat_only_never_goes_by_email(monkeypatch: pytest.MonkeyPatch) -> None:
    """Email is set up and working, but a chat-only send has no chat app to
    go on: nothing goes, and the email gateway is never tried."""
    _mail_configured(monkeypatch)
    sup = _Supervisor(gateway=object())
    mcp_gateway.set_gateway_supervisor(sup)
    plans: list[list[str]] = []

    async def _send(person: Person, plan: list[str], text: str, *, label: str) -> runner.PrincipalDelivery:
        plans.append(plan)
        return runner.PrincipalDelivery(True, "sent", "delivered")

    monkeypatch.setattr(runner, "_send_on_plan", _send)

    result = asyncio.run(runner.deliver_to_person(_owner(), "PING", chat_only=True))

    assert not result.ok and result.reason == "no_channel"
    assert plans == [] and sup.ensured == 0


def test_chat_only_leaves_email_out_of_the_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    _mail_configured(monkeypatch)
    mcp_gateway.set_gateway_supervisor(_Supervisor(gateway=object()))
    plans: list[list[str]] = []

    async def _send(person: Person, plan: list[str], text: str, *, label: str) -> runner.PrincipalDelivery:
        plans.append(plan)
        return runner.PrincipalDelivery(True, "sent", "delivered")

    monkeypatch.setattr(runner, "_send_on_plan", _send)

    asyncio.run(runner.deliver_to_person(_owner(slack_user_id="U1"), "PING", chat_only=True))
    asyncio.run(runner.deliver_to_person(_owner(slack_user_id="U1"), "BRIEF"))

    assert plans[0] == ["slack_dm"]
    assert "email" in plans[1], "without chat_only the same person also gets email"


def test_every_channel_failing_says_what_each_said(monkeypatch: pytest.MonkeyPatch) -> None:
    _mail_configured(monkeypatch)

    class _Gateway:
        async def call_tool(self, tool_input: dict[str, Any]) -> str:
            return DEAD_GRANT

    mcp_gateway.set_active_gateway(_Gateway())  # type: ignore[arg-type]

    async def _slack(args: dict[str, Any]) -> str:
        return json.dumps({"error": "channel_not_found"})

    monkeypatch.setattr("openexecutive.orchestrator.schedule_tools.handle_send_slack_dm", _slack)

    result = asyncio.run(runner.deliver_to_person(_owner(slack_user_id="U1"), "BRIEF"))

    assert result.reason == "send_failed"
    assert result.detail.startswith("delivery failed on every channel: slack_dm: channel_not_found; email: ")
    assert "invalid_grant" in result.detail


def test_a_handler_that_raises_is_named_too(monkeypatch: pytest.MonkeyPatch) -> None:
    _mail_configured(monkeypatch, configured=False)

    async def _slack(args: dict[str, Any]) -> str:
        raise RuntimeError("Slack is down")

    monkeypatch.setattr("openexecutive.orchestrator.schedule_tools.handle_send_slack_dm", _slack)
    result = asyncio.run(runner.deliver_to_person(_owner(slack_user_id="U1"), "BRIEF"))
    assert result.detail == "delivery failed on every channel: slack_dm: Slack is down"


def test_mail_gateway_problem_only_when_email_is_the_executives_fault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mail_configured(monkeypatch)
    mcp_gateway.set_gateway_supervisor(_Supervisor(problem="boom"))
    assert runner.mail_gateway_problem(_owner()) == "boom"
    assert runner.mail_gateway_problem(_owner(email="")) is None
    assert runner.mail_gateway_problem(None) is None
    _mail_configured(monkeypatch, configured=False)
    assert runner.mail_gateway_problem(_owner()) is None


# -- a grant that died ------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        DEAD_GRANT,
        json.dumps({"error": {"code": 401, "message": "Request had invalid authentication credentials. "
                                                      "Expected OAuth 2 access token", "status": "UNAUTHENTICATED"}}),
        "Error: InvalidAuthenticationToken: Access token has expired or is not yet valid.",
        '{"error": "Reauthentication is required"}',
    ],
)
def test_grant_failure_reads_an_authorization_error(text: str) -> None:
    assert grant_failure(text) is not None


@pytest.mark.parametrize(
    "text",
    [
        "",
        json.dumps({"result": "3 messages", "messages": [{"subject": "Re: invalid_grant on the staging box"}]}),
        json.dumps({"error": "Rate limit exceeded; retry after 30s"}),
        json.dumps({"error": "403 Forbidden: the caller does not have permission on file X"}),
        "Email sent! Message ID: m-1",
        "Errors in Q3: the invalid_grant tickets were closed; token expired on the staging box",
        "failed to find a slot; the token expired note is in the thread",
    ],
)
def test_grant_failure_ignores_other_answers(text: str) -> None:
    assert grant_failure(text) is None


def test_a_dead_grant_marks_the_server_and_a_working_answer_clears_it() -> None:
    gateway = MCPGateway()
    gateway.server_names = ("google_workspace",)
    assert gateway_reconnect_needed(gateway) == ()

    gateway._note_grant_health("google_workspace__search_gmail_messages", DEAD_GRANT)
    assert gateway_reconnect_needed(gateway) == ("google_workspace",)
    assert gateway.mark_needs_reconnect("google_workspace") is False, "already known"

    gateway._note_grant_health("fetch__get", '{"error": "invalid_grant"}')
    assert gateway_reconnect_needed(gateway) == ("fetch", "google_workspace")

    gateway._note_grant_health("google_workspace__get_events", '{"result": []}')
    assert gateway_reconnect_needed(gateway) == ("fetch",)
    assert gateway_reconnect_needed(object()) == ()


def test_the_prompt_block_stops_saying_connected() -> None:
    from openexecutive.config import get_settings

    settings = get_settings()
    connected = render_connected_systems(mcp_servers=("google_workspace",), settings=settings)
    dead = render_connected_systems(
        mcp_servers=("google_workspace",), settings=settings, needs_reconnect=("google_workspace",),
    )
    assert "Google Workspace: connected" in connected
    assert "Google Workspace: connected" not in dead
    assert "needs reconnecting" in dead and "exec@acme.io" in dead
    assert "google_workspace__send_gmail_message" not in dead, "no tool names to reach for"
    # Another server dying says nothing about Google.
    other = render_connected_systems(
        mcp_servers=("google_workspace",), settings=settings, needs_reconnect=("fetch",),
    )
    assert other == connected


def test_the_executive_rerenders_block_0_from_the_gateway_state(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.prompts.cache_manager import build_system_blocks

    seen: list[tuple[str, ...]] = []
    real = render_connected_systems

    def _spy(**kw: Any) -> str:
        seen.append(tuple(kw.get("needs_reconnect", ())))
        return real(**kw)

    monkeypatch.setattr("openexecutive.prompts.cache_manager.render_connected_systems", _spy)
    build_system_blocks(None, mcp_servers=("google_workspace",), needs_reconnect=("google_workspace",))
    assert seen == [("google_workspace",)]


# -- the Setup status page ---------------------------------------------------------


def _settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    config = tmp_path / "mcp_servers.json"
    config.write_text(json.dumps({"mcpServers": {"google_workspace": {"command": "true"}}}))
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "id.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "oauth-secret")
    return Settings(
        ANTHROPIC_API_KEY="sk-ant-api03-realistic", EXEC_EMAIL_ADDRESS="exec@acme.io",
        SCHEDULER_ENABLED=True, MCP_ENABLED=True, MCP_SERVERS_CONFIG_PATH=str(config),
    )


def _snap(settings: Settings, **fields: Any) -> Snapshot:
    values: dict[str, Any] = {
        "settings": settings, "now": NOW, "local_login": False, "people": [], "principal": _owner(),
        "last_inbound": {}, "brief_zone": "UTC",
    }
    values.update(fields)
    return Snapshot(**values)


def test_the_gmail_light_says_why_and_that_it_retries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    check = check_gmail(_snap(
        _settings(tmp_path, monkeypatch), mcp_gateway=None,
        gateway_problem="the MCP gateway failed to start (2 attempts): TimeoutError",
    ))
    assert check.state == "error"
    assert "didn't start" in check.summary and "2 attempts" in check.summary
    assert "retrying" in (check.fix or "")


def test_the_gmail_light_names_a_dead_grant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    check = check_gmail(_snap(
        _settings(tmp_path, monkeypatch), mcp_gateway=object(), gateway_needs_reconnect=("google_workspace",),
    ))
    assert check.state == "error"
    assert "expired or was revoked" in check.summary and "exec@acme.io" in check.summary
    assert "Sign Google in again" in (check.fix or "")


def test_the_brief_light_blames_the_gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    monkeypatch.setattr(runner, "mail_gateway_problem", lambda _p, **_kw: "the MCP gateway failed to start (1 attempt)")
    check = check_brief(_snap(settings, brief_email_ready=False))
    assert check.state == "error"
    assert check.summary == (
        "Not sent by email: the Executive's mail connection wasn't running "
        "(the MCP gateway failed to start (1 attempt))."
    )


def test_the_brief_light_repeats_a_failed_sends_detail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    last = DeliveryOutcome(MORNING, "send_failed", None, NOW, detail="email: invalid_grant: revoked")
    check = check_brief(_snap(settings, brief_email_ready=True, brief_delivery=last))
    assert check.state == "error"
    assert "invalid_grant" not in check.summary, "every team member reads this page"
    assert "connect it again" in (check.fix or "")
