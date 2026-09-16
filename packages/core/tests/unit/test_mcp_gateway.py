"""Unit tests for MCPGateway — mocked subprocess, no real extensible-mcp needed."""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from openexecutive.orchestrator.mcp_gateway import (
    _FORWARDED_ENV_VARS,
    MCP_TOOL_NAMES,
    MCP_TOOLS,
    MCPGateway,
)

# ---------------------------------------------------------------------------
# Tool schema correctness — no I/O needed
# ---------------------------------------------------------------------------


def test_mcp_tools_have_required_fields() -> None:
    for tool in MCP_TOOLS:
        assert "name" in tool
        assert "description" in tool
        assert "input_schema" in tool
        schema = tool["input_schema"]
        assert schema["type"] == "object"
        assert "properties" in schema
        assert "required" in schema


def test_mcp_tool_names_matches_tools() -> None:
    assert frozenset(t["name"] for t in MCP_TOOLS) == MCP_TOOL_NAMES


def test_mcp_tools_contain_three_meta_tools() -> None:
    assert {t["name"] for t in MCP_TOOLS} == {"search_tools", "call_tool", "load_mcp_server"}


def test_search_tools_requires_query() -> None:
    tool = next(t for t in MCP_TOOLS if t["name"] == "search_tools")
    assert "query" in tool["input_schema"]["required"]


def test_call_tool_requires_name() -> None:
    tool = next(t for t in MCP_TOOLS if t["name"] == "call_tool")
    assert "name" in tool["input_schema"]["required"]


def test_load_mcp_server_requires_name_and_url() -> None:
    tool = next(t for t in MCP_TOOLS if t["name"] == "load_mcp_server")
    assert "name" in tool["input_schema"]["required"]
    assert "url" in tool["input_schema"]["required"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_text_result(text: str) -> MagicMock:
    item = MagicMock()
    item.text = text
    result = MagicMock()
    result.content = [item]
    return result


def _make_mock_session() -> MagicMock:
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.initialize = AsyncMock()
    session.call_tool = AsyncMock()
    return session


def _make_mock_stdio_cm() -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock()))
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


class _FakeMcpModules:
    """Context manager that injects fake mcp modules into sys.modules."""

    def __init__(self, session: MagicMock, stdio_cm: MagicMock) -> None:
        self._session = session
        self._stdio_cm = stdio_cm
        self._saved: dict[str, Any] = {}

    def __enter__(self) -> _FakeMcpModules:
        fake_mcp = MagicMock()
        fake_mcp.ClientSession = MagicMock(return_value=self._session)
        fake_mcp.StdioServerParameters = MagicMock()

        fake_stdio = MagicMock()
        fake_stdio.stdio_client = MagicMock(return_value=self._stdio_cm)

        for key in ("mcp", "mcp.client", "mcp.client.stdio"):
            self._saved[key] = sys.modules.get(key)

        sys.modules["mcp"] = fake_mcp
        sys.modules["mcp.client"] = MagicMock()
        sys.modules["mcp.client.stdio"] = fake_stdio
        return self

    def __exit__(self, *_: Any) -> None:
        for key, val in self._saved.items():
            if val is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = val


# ---------------------------------------------------------------------------
# MCPGateway lifecycle
# ---------------------------------------------------------------------------


def test_gateway_start_calls_initialize(tmp_path: Path) -> None:
    session = _make_mock_session()
    config = tmp_path / "mcp_servers.json"
    config.write_text("{}")

    with _FakeMcpModules(session, _make_mock_stdio_cm()):
        asyncio.run(MCPGateway().start(config))

    session.initialize.assert_awaited_once()


def test_gateway_forwards_embedding_cache_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The subprocess must receive the embedding-cache/offline env explicitly —
    the MCP stdio client does not inherit it, so without forwarding fastembed
    re-downloads its model on every cold start."""
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", "/opt/fastembed-cache")
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.delenv("HF_HOME", raising=False)
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)
    session = _make_mock_session()
    config = tmp_path / "mcp_servers.json"
    config.write_text("{}")

    with _FakeMcpModules(session, _make_mock_stdio_cm()):
        asyncio.run(MCPGateway().start(config))
        params_cls = sys.modules["mcp"].StdioServerParameters
        env = params_cls.call_args.kwargs["env"]

    assert env["FASTEMBED_CACHE_PATH"] == "/opt/fastembed-cache"
    assert env["HF_HUB_OFFLINE"] == "1"
    # Only vars actually set in the parent env are forwarded.
    assert "HF_HOME" not in env
    assert "TRANSFORMERS_OFFLINE" not in env


def test_gateway_env_none_when_no_cache_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With none of the forwarded vars set (local/CI), env stays None so the MCP
    SDK keeps its prior default-environment behaviour — no regression. Clear the
    whole _FORWARDED_ENV_VARS tuple so the test is deterministic regardless of
    which of them happen to be exported in the ambient shell."""
    for var in _FORWARDED_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    session = _make_mock_session()
    config = tmp_path / "mcp_servers.json"
    config.write_text("{}")

    with _FakeMcpModules(session, _make_mock_stdio_cm()):
        asyncio.run(MCPGateway().start(config))
        params_cls = sys.modules["mcp"].StdioServerParameters
        env = params_cls.call_args.kwargs["env"]

    assert env is None


def test_gateway_close_is_idempotent() -> None:
    gw = MCPGateway()
    asyncio.run(gw.close())
    asyncio.run(gw.close())


def test_gateway_close_after_start(tmp_path: Path) -> None:
    session = _make_mock_session()
    stdio_cm = _make_mock_stdio_cm()
    config = tmp_path / "mcp_servers.json"
    config.write_text("{}")

    async def _run() -> None:
        with _FakeMcpModules(session, stdio_cm):
            gw = MCPGateway()
            await gw.start(config)
            await gw.close()

    asyncio.run(_run())
    session.__aexit__.assert_awaited_once()
    stdio_cm.__aexit__.assert_awaited_once()


# ---------------------------------------------------------------------------
# Method dispatch
# ---------------------------------------------------------------------------


def test_search_tools_serializes_query(tmp_path: Path) -> None:
    session = _make_mock_session()
    session.call_tool.return_value = _make_text_result('{"tools": ["github_list_prs"]}')
    config = tmp_path / "mcp_servers.json"
    config.write_text("{}")

    async def _run() -> str:
        with _FakeMcpModules(session, _make_mock_stdio_cm()):
            gw = MCPGateway()
            await gw.start(config)
            return await gw.search_tools({"query": "list pull requests"})

    result = asyncio.run(_run())
    session.call_tool.assert_awaited_with("search_tools", {"query": "list pull requests"})
    assert json.loads(result)["tools"] == ["github_list_prs"]


def test_call_tool_passes_arguments(tmp_path: Path) -> None:
    session = _make_mock_session()
    session.call_tool.return_value = _make_text_result('{"result": "ok"}')
    config = tmp_path / "mcp_servers.json"
    config.write_text("{}")

    async def _run() -> str:
        with _FakeMcpModules(session, _make_mock_stdio_cm()):
            gw = MCPGateway()
            await gw.start(config)
            return await gw.call_tool({"name": "github_list_prs", "arguments": {"repo": "foo/bar"}})

    result = asyncio.run(_run())
    session.call_tool.assert_awaited_with(
        "call_tool", {"tool_name": "github_list_prs", "arguments": {"repo": "foo/bar"}}
    )
    assert json.loads(result)["result"] == "ok"


def test_call_tool_defaults_empty_arguments(tmp_path: Path) -> None:
    session = _make_mock_session()
    session.call_tool.return_value = _make_text_result("{}")
    config = tmp_path / "mcp_servers.json"
    config.write_text("{}")

    async def _run() -> None:
        with _FakeMcpModules(session, _make_mock_stdio_cm()):
            gw = MCPGateway()
            await gw.start(config)
            await gw.call_tool({"name": "some_tool"})

    asyncio.run(_run())
    called_args = session.call_tool.await_args[0]
    assert called_args[1]["arguments"] == {}


def test_load_mcp_server_passes_name_and_url(tmp_path: Path) -> None:
    session = _make_mock_session()
    session.call_tool.return_value = _make_text_result('{"ok": true}')
    config = tmp_path / "mcp_servers.json"
    config.write_text("{}")

    async def _run() -> str:
        with _FakeMcpModules(session, _make_mock_stdio_cm()):
            gw = MCPGateway()
            await gw.start(config)
            return await gw.load_mcp_server({"name": "hubspot", "url": "https://mcp.hubspot.com"})

    result = asyncio.run(_run())
    session.call_tool.assert_awaited_with(
        "load_mcp_server", {"name": "hubspot", "url": "https://mcp.hubspot.com"}
    )
    assert json.loads(result)["ok"] is True


def test_load_mcp_server_rejects_non_https(tmp_path: Path) -> None:
    session = _make_mock_session()
    config = tmp_path / "mcp_servers.json"
    config.write_text("{}")

    async def _run() -> str:
        with _FakeMcpModules(session, _make_mock_stdio_cm()):
            gw = MCPGateway()
            await gw.start(config)
            return await gw.load_mcp_server({"name": "evil", "url": "http://internal.corp/mcp"})

    result = asyncio.run(_run())
    assert "error" in json.loads(result)
    session.call_tool.assert_not_awaited()


def test_gateway_close_clears_state(tmp_path: Path) -> None:
    session = _make_mock_session()
    stdio_cm = _make_mock_stdio_cm()
    config = tmp_path / "mcp_servers.json"
    config.write_text("{}")

    async def _run() -> MCPGateway:
        with _FakeMcpModules(session, stdio_cm):
            gw = MCPGateway()
            await gw.start(config)
            await gw.close()
            return gw

    gw = asyncio.run(_run())
    assert gw._session is None
    assert gw._stdio_cm is None


def test_not_started_raises_runtime_error() -> None:
    gw = MCPGateway()

    async def _run() -> None:
        await gw.search_tools({"query": "test"})

    with pytest.raises(RuntimeError, match="start()"):
        asyncio.run(_run())


def test_empty_content_returns_fallback(tmp_path: Path) -> None:
    session = _make_mock_session()
    empty = MagicMock()
    empty.content = []
    session.call_tool.return_value = empty
    config = tmp_path / "mcp_servers.json"
    config.write_text("{}")

    async def _run() -> str:
        with _FakeMcpModules(session, _make_mock_stdio_cm()):
            gw = MCPGateway()
            await gw.start(config)
            return await gw.search_tools({"query": "anything"})

    result = asyncio.run(_run())
    assert json.loads(result) == {"tools": []}


# ---------------------------------------------------------------------------
# mcp_servers.json.example — the shipped example must describe servers that can
# actually start. Issue #115: the example documented
# `npx -y @github/github-mcp-server stdio`; that npm package does not exist and
# the API image has no Node, so extensible-mcp logged "Failed to connect to
# 'github', skipping" and anyone copying the example got a toolless gateway.
# ---------------------------------------------------------------------------

_CORE_DIR = Path(__file__).resolve().parents[2]
_EXAMPLE_CONFIG = _CORE_DIR / "mcp_servers.json.example"
_DOCKERFILE = _CORE_DIR.parents[1] / "docker" / "Dockerfile"

# Bare (non-absolute) commands the API image can actually resolve on PATH.
# `uvx` arrives with `pip install uv` in docker/Dockerfile. Node is NOT
# installed — the image apt-installs only git — so `npx` is not on this list
# and never should be without a Dockerfile change to match.
_IMAGE_INTERPRETERS = frozenset({"uvx"})


def _example_servers() -> dict[str, dict[str, Any]]:
    raw: dict[str, Any] = json.loads(_EXAMPLE_CONFIG.read_text())
    servers: dict[str, dict[str, Any]] = raw["mcpServers"]
    return servers


def test_example_config_parses_and_every_server_is_launchable() -> None:
    """Mirrors extensible-mcp's own load_config validation.

    `config.load_config` raises ValueError on a server that defines neither
    `command` nor `url`, which would take the whole gateway down at startup.
    """
    servers = _example_servers()
    assert servers, "example config must define at least one server"
    for name, server in servers.items():
        assert isinstance(server, dict), f"server '{name}' must be an object"
        assert "command" in server or "url" in server, (
            f"server '{name}' has neither 'command' nor 'url' — "
            "extensible-mcp's load_config rejects this config outright"
        )


def test_example_config_commands_are_present_in_the_api_image() -> None:
    """Every documented `command` must exist in the image that runs it.

    An absolute path has to be one docker/Dockerfile installs; a bare command
    has to be an interpreter the image provides. This is the regression test
    for #115 — an `npx` command fails it, because the image has no Node.
    """
    dockerfile = _DOCKERFILE.read_text()
    for name, server in _example_servers().items():
        command = server.get("command")
        if command is None:
            continue  # url-only server, nothing to launch locally
        if command.startswith("/"):
            # Whole-path match, not bare substring: a plain `in` check would let
            # `/usr/local/bin/work` pass on the strength of the unrelated
            # `/usr/local/bin/workspace-mcp-launch.sh` already in the Dockerfile.
            assert re.search(re.escape(command) + r"(?![\w./-])", dockerfile), (
                f"server '{name}' launches '{command}', which docker/Dockerfile "
                "never installs — it cannot start in the API image"
            )
        else:
            assert command in _IMAGE_INTERPRETERS, (
                f"server '{name}' launches '{command}', which the API image does "
                f"not provide (known interpreters: {sorted(_IMAGE_INTERPRETERS)}). "
                "Install it in docker/Dockerfile or use an absolute path."
            )
