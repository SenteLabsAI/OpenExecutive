"""A closed connection to the tools child is started again once, in place."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest

from openexecutive.orchestrator import mcp_gateway as gw_module
from openexecutive.orchestrator.mcp_gateway import MCPGateway


def _session(*results: Any) -> MagicMock:
    session = MagicMock()
    session.call_tool = AsyncMock(side_effect=list(results))
    session.__aexit__ = AsyncMock(return_value=None)
    return session


def _gateway(dead: MagicMock, fresh: MagicMock) -> tuple[MCPGateway, list[Path]]:
    gateway = MCPGateway()
    gateway._session = dead
    gateway._config_path = Path("company/mcp_servers.json")
    started: list[Path] = []

    async def start(config_path: Path) -> None:
        started.append(config_path)
        gateway._session = fresh

    gateway.start = start  # type: ignore[method-assign]
    return gateway, started


def test_a_closed_connection_is_started_again_and_the_call_retried() -> None:
    dead = _session(anyio.ClosedResourceError())
    fresh = _session("found")
    gateway, started = _gateway(dead, fresh)
    result = asyncio.run(gw_module._session_call(gateway._require_session(), "search_tools", {"query": "q"}))
    assert result == "found"
    assert started == [Path("company/mcp_servers.json")]
    assert gateway._session is fresh
    dead.__aexit__.assert_awaited()


def test_calls_that_find_it_closed_together_start_it_once() -> None:
    dead = MagicMock()
    dead.call_tool = AsyncMock(side_effect=anyio.ClosedResourceError())
    dead.__aexit__ = AsyncMock(return_value=None)
    fresh = _session("a", "b")
    gateway, started = _gateway(dead, fresh)

    async def both() -> list[Any]:
        return list(await asyncio.gather(
            gateway._require_session().call_tool("search_tools", {}),
            gateway._require_session().call_tool("search_tools", {}),
        ))

    assert sorted(asyncio.run(both())) == ["a", "b"]
    assert len(started) == 1


def test_any_other_failure_is_not_retried() -> None:
    dead = _session(RuntimeError("boom"))
    gateway, started = _gateway(dead, _session("never"))
    with pytest.raises(RuntimeError):
        asyncio.run(gateway._require_session().call_tool("search_tools", {}))
    assert started == []


def test_a_failed_revival_is_tried_again_by_the_next_call() -> None:
    dead = _session(anyio.ClosedResourceError())
    fresh = _session("found")
    gateway, started = _gateway(dead, fresh)
    attempts = {"n": 0}

    async def start(config_path: Path) -> None:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise TimeoutError("slow start")
        gateway._session = fresh

    gateway.start = start  # type: ignore[method-assign]
    with pytest.raises(TimeoutError):
        asyncio.run(gateway._require_session().call_tool("search_tools", {}))
    assert gateway._session is None
    assert asyncio.run(gateway._require_session().call_tool("search_tools", {})) == "found"
    assert attempts["n"] == 2


def test_a_closed_gateway_is_not_started_again() -> None:
    gateway, _ = _gateway(_session(), _session())
    asyncio.run(gateway.close())
    with pytest.raises(RuntimeError):
        gateway._require_session()
