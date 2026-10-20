"""The MCP gateway supervisor: a failed start is retried with backoff, work
that needs the gateway can ask for a start now, a late success wires what
boot would have, and /health says where it stands."""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import health as health_route
from openexecutive.orchestrator import mcp_gateway
from openexecutive.orchestrator.gateway_boot import GatewaySupervisor

CONFIG = Path("/tmp/mcp_servers.json")


@pytest.fixture
def oe_logs() -> Iterator[list[str]]:
    """Rendered messages from the ``openexecutive`` logger tree (caplog
    would miss them once the app's logging config stops propagation)."""
    messages: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    logger = logging.getLogger("openexecutive")
    handler = _Collect()
    logger.addHandler(handler)
    try:
        yield messages
    finally:
        logger.removeHandler(handler)


@pytest.fixture(autouse=True)
def _required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("EXEC_EMAIL_ADDRESS", "exec@example.com")
    monkeypatch.setattr(mcp_gateway, "_active_gateway", None)
    monkeypatch.setattr(mcp_gateway, "_supervisor", None)


class _Flaky:
    """A gateway whose first ``fail_times`` starts raise."""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.starts = 0
        self.closed = 0
        self.server_names = ("google_workspace",)
        self.needs_reconnect: set[str] = set()
        self.prime_pinned_tools = AsyncMock(return_value=[])

    async def start(self, _path: Path) -> None:
        self.starts += 1
        if self.starts <= self.fail_times:
            raise RuntimeError(f"child died ({self.starts})")

    async def close(self) -> None:
        self.closed += 1


def _stub(monkeypatch: pytest.MonkeyPatch, gateway: Any) -> list[Any]:
    """One stub gateway for every MCPGateway() and a poller that records
    what it was handed."""
    monkeypatch.setattr(mcp_gateway, "MCPGateway", MagicMock(return_value=gateway))
    pollers: list[Any] = []

    async def _poller(gw: Any) -> None:
        pollers.append(gw)
        await asyncio.Event().wait()

    monkeypatch.setattr("openexecutive.integrations.email_poller.run_email_poller", _poller)
    return pollers


def _supervisor(app_state: Any = None, **kw: Any) -> GatewaySupervisor:
    return GatewaySupervisor(
        config_path=CONFIG, servers=("google_workspace",), app_state=app_state,
        start_timeout_s=1.0, backoff_s=(0.01, 0.02), ensure_cooldown_s=0.0, **kw,
    )


# -- retries ---------------------------------------------------------------------


def test_a_failed_boot_is_retried_until_it_comes_up(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = _Flaky(fail_times=2)
    pollers = _stub(monkeypatch, gateway)
    state = MagicMock()

    async def _run() -> GatewaySupervisor:
        sup = _supervisor(state)
        assert await sup.start() is False
        assert sup.status().state == "down"
        assert sup.status().attempts == 1
        assert sup.status().next_retry_at is not None
        for _ in range(200):
            if sup.is_up:
                break
            await asyncio.sleep(0.005)
        assert sup.is_up
        await asyncio.sleep(0)
        return sup

    sup = asyncio.run(_run())

    assert gateway.starts == 3
    assert gateway.closed == 2, "each failed start is reaped"
    # A late success does what a first-time success does.
    assert state.mcp_gateway is gateway
    assert mcp_gateway.get_active_gateway() is gateway
    assert pollers == [gateway]
    gateway.prime_pinned_tools.assert_awaited_once()
    status = sup.status()
    assert status.state == "up" and status.attempts == 3
    assert status.last_error == "RuntimeError: child died (2)", "kept after the recovery"
    assert status.next_retry_at is None


def test_the_backoff_grows_and_caps() -> None:
    sup = GatewaySupervisor(config_path=CONFIG, servers=("x",), backoff_s=(30, 60, 600))
    assert [sup._delay_for(n) for n in (1, 2, 3, 4, 9)] == [30, 60, 600, 600, 600]


def test_the_log_names_the_attempt_and_the_config(
    monkeypatch: pytest.MonkeyPatch, oe_logs: list[str]
) -> None:
    _stub(monkeypatch, _Flaky(fail_times=1))

    async def _run() -> None:
        sup = _supervisor(why="MCP_ENABLED was set explicitly")
        await sup.attempt()
        await sup.close()

    asyncio.run(_run())
    logged = "\n".join(oe_logs)
    assert "attempt 1" in logged and str(CONFIG) in logged and "google_workspace" in logged
    assert "MCP_ENABLED was set explicitly" in logged


# -- the lazy re-check -----------------------------------------------------------


def test_ensure_starts_now_instead_of_waiting_for_the_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = _Flaky(fail_times=1)
    _stub(monkeypatch, gateway)

    async def _run() -> tuple[Any, int]:
        sup = GatewaySupervisor(
            config_path=CONFIG, servers=("google_workspace",), start_timeout_s=1.0,
            backoff_s=(3600.0,), ensure_cooldown_s=0.0,  # the retry is an hour away
        )
        assert await sup.start() is False
        got = await sup.ensure()
        await sup.close()
        return got, gateway.starts

    got, starts = asyncio.run(_run())
    assert got is gateway and starts == 2


def test_ensure_waits_for_an_attempt_already_running(monkeypatch: pytest.MonkeyPatch) -> None:
    release = asyncio.Event()
    gateway = MagicMock()
    gateway.server_names = ("google_workspace",)
    gateway.prime_pinned_tools = AsyncMock(return_value=[])
    gateway.close = AsyncMock()
    starts = 0

    async def _slow(_path: Path) -> None:
        nonlocal starts
        starts += 1
        await release.wait()

    gateway.start = _slow
    _stub(monkeypatch, gateway)

    async def _run() -> None:
        sup = _supervisor()
        first = asyncio.ensure_future(sup.attempt())
        await asyncio.sleep(0.01)
        assert sup.status().state == "starting"
        second = asyncio.ensure_future(sup.ensure())
        await asyncio.sleep(0.01)
        release.set()
        assert await first is True
        assert await second is gateway
        await sup.close()

    asyncio.run(_run())
    assert starts == 1, "a second caller never starts a second child"


def test_ensure_waits_out_a_fresh_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = _Flaky(fail_times=1)
    _stub(monkeypatch, gateway)

    async def _run() -> tuple[Any, int, str | None, str | None]:
        sup = GatewaySupervisor(
            config_path=CONFIG, servers=("google_workspace",), start_timeout_s=1.0,
            backoff_s=(3600.0,), ensure_cooldown_s=3600.0,
        )
        assert await sup.start() is False
        got = await sup.ensure()
        out = (got, gateway.starts, sup.problem(), sup.problem(with_error=False))
        await sup.close()
        return out

    got, starts, with_error, without = asyncio.run(_run())
    assert got is None and starts == 1, "a start that just failed is not repeated on demand"
    assert with_error == "the MCP gateway failed to start (1 attempt): RuntimeError: child died (1)"
    assert without == "the MCP gateway failed to start (1 attempt)"


def test_module_hooks_reach_the_supervisor(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = _Flaky(fail_times=1)
    _stub(monkeypatch, gateway)

    async def _run() -> list[Any]:
        assert await mcp_gateway.ensure_active_gateway() is None, "no supervisor: nothing to try"
        assert mcp_gateway.gateway_problem() == "the MCP gateway is not running"
        sup = GatewaySupervisor(
            config_path=CONFIG, servers=("google_workspace",), start_timeout_s=1.0, backoff_s=(3600.0,),
            ensure_cooldown_s=0.0,
        )
        mcp_gateway.set_gateway_supervisor(sup)
        out: list[Any] = [mcp_gateway.gateway_problem()]
        await sup.start()
        out.append(mcp_gateway.gateway_problem())
        out.append(await mcp_gateway.ensure_active_gateway())
        out.append(mcp_gateway.gateway_problem())
        await sup.close()
        return out

    before, after_failure, ensured, after = asyncio.run(_run())
    assert before == "the MCP gateway has not started yet"
    assert after_failure == "the MCP gateway failed to start (1 attempt): RuntimeError: child died (1)"
    assert ensured is gateway and after is None


# -- shutdown --------------------------------------------------------------------


def test_close_stops_the_retries_the_poller_and_the_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = _Flaky(fail_times=0)
    pollers = _stub(monkeypatch, gateway)
    state = MagicMock()

    async def _run() -> GatewaySupervisor:
        sup = _supervisor(state)
        assert await sup.start() is True
        await asyncio.sleep(0)
        mcp_gateway.set_gateway_supervisor(sup)
        await sup.close()
        return sup

    sup = asyncio.run(_run())
    assert pollers == [gateway] and sup.poller_task is not None and sup.poller_task.cancelled()
    assert gateway.closed == 1
    assert state.mcp_gateway is None and mcp_gateway.get_active_gateway() is None


def test_a_real_cancellation_is_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = MagicMock()
    gateway.start = AsyncMock(side_effect=asyncio.CancelledError())
    gateway.close = AsyncMock()
    _stub(monkeypatch, gateway)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_supervisor().attempt())


# -- /health ---------------------------------------------------------------------


def _health(app_state_fields: dict[str, Any], *, mcp_enabled: bool = True) -> dict[str, Any]:
    from openexecutive.onboarding import profile_builder

    app = FastAPI()
    app.include_router(health_route.router)
    for key, value in app_state_fields.items():
        setattr(app.state, key, value)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            profile_builder, "load_or_create_profile",
            lambda *a, **k: MagicMock(is_empty=lambda: True, name=None),
        )
        mp.setenv("MCP_ENABLED", "true" if mcp_enabled else "false")
        body = TestClient(app).get("/health").json()
    return dict(body["gateway"])


def test_health_reports_the_supervisor_state() -> None:
    sup = _supervisor()
    sup.attempts = 2
    sup.last_error = "RuntimeError: child died"
    sup.last_failed_at = datetime(2026, 10, 13, 8, 0, tzinfo=UTC)
    sup.next_retry_at = datetime(2026, 10, 13, 8, 5, tzinfo=UTC)
    body = _health({"mcp_supervisor": sup, "mcp_gateway": None})
    assert body == {
        "state": "down", "attempts": 2,
        "last_failed_at": "2026-10-13T08:00:00+00:00", "up_since": None,
        "next_retry_at": "2026-10-13T08:05:00+00:00", "email_poller": False,
    }


def test_health_without_a_supervisor() -> None:
    assert _health({"mcp_gateway": None}, mcp_enabled=False)["state"] == "off"
    assert _health({"mcp_gateway": None}, mcp_enabled=True)["state"] == "no_servers"
    assert _health({"mcp_gateway": MagicMock(pinned_tools=None)})["state"] == "up"


def test_status_serializes_to_json() -> None:
    sup = _supervisor()
    json.dumps(sup.status().as_dict())
