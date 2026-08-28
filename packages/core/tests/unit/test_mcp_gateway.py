"""Unit tests for MCPGateway — mocked subprocess, no real extensible-mcp needed."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from openexecutive.orchestrator.mcp_gateway import (
    _FORWARDED_ENV_VARS,
    _GITLAB_ALLOWED_TOOLS,
    _GITLAB_READ_TOOLS,
    _GITLAB_WRITE_TOOLS,
    MCP_TOOL_NAMES,
    MCP_TOOLS,
    MCPGateway,
)


@pytest.fixture(autouse=True)
def _configure_gitlab_write_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITLAB_WRITE_NAMESPACES", "acme/platform")


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


def test_gitlab_read_tool_passes_through(tmp_path: Path) -> None:
    session = _make_mock_session()
    session.call_tool.return_value = _make_text_result('{"result": "ok"}')
    config = tmp_path / "mcp_servers.json"
    config.write_text("{}")

    async def _run() -> str:
        with _FakeMcpModules(session, _make_mock_stdio_cm()):
            gw = MCPGateway()
            await gw.start(config)
            return await gw.call_tool(
                {
                    "name": "gitlab__list_merge_requests",
                    "arguments": {"project_id": "acme/platform/org/branding"},
                }
            )

    result = asyncio.run(_run())
    assert json.loads(result)["result"] == "ok"
    session.call_tool.assert_awaited_once()


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        (
            "gitlab__create_issue",
            {
                "id": "acme/platform/org/branding",
                "title": "Test issue",
            },
        ),
        (
            "gitlab__save_work_item",
            {
                "group_id": "acme/platform",
                "title": "Test epic",
                "type_name": "Epic",
            },
        ),
        (
            "gitlab__manage_pipeline",
            {
                "id": "acme/platform/org/branding",
                "pipeline_id": 123,
                "name": "release",
            },
        ),
    ],
)
def test_gitlab_scoped_write_tools_pass_through(
    tmp_path: Path, tool_name: str, arguments: dict[str, object]
) -> None:
    session = _make_mock_session()
    session.call_tool.return_value = _make_text_result('{"result": "ok"}')
    config = tmp_path / "mcp_servers.json"
    config.write_text("{}")

    async def _run() -> str:
        with _FakeMcpModules(session, _make_mock_stdio_cm()):
            gw = MCPGateway()
            await gw.start(config)
            return await gw.call_tool({"name": tool_name, "arguments": arguments})

    result = asyncio.run(_run())
    assert json.loads(result)["result"] == "ok"
    session.call_tool.assert_awaited_once()


@pytest.mark.parametrize(
    ("tool_name", "arguments", "error_fragment"),
    [
        (
            "gitlab__create_issue",
            {"id": "another-company/private", "title": "No"},
            "outside",
        ),
        (
            "gitlab__create_issue",
            {"id": 123, "title": "No"},
            "outside",
        ),
        (
            "gitlab__save_work_item",
            {"title": "Missing target", "type_name": "Epic"},
            "must include",
        ),
        (
            "gitlab__fork_repository",
            {"id": "acme/platform/org/branding"},
            "must include an authorized namespace_path",
        ),
        (
            "gitlab__fork_repository",
            {
                "id": "acme/platform/org/branding",
                "namespace_path": "acme/platform",
                "namespace_id": 123,
            },
            "numeric destination",
        ),
        (
            "gitlab__attach_scan_profile",
            {"security_scan_profile_id": 1, "project_ids": [2]},
            "only accepts numeric targets",
        ),
        ("gitlab__future_tool", {}, "reviewed allowlist"),
    ],
)
def test_gitlab_unscoped_and_unknown_tools_fail_closed(
    tmp_path: Path,
    tool_name: str,
    arguments: dict[str, object],
    error_fragment: str,
) -> None:
    session = _make_mock_session()
    config = tmp_path / "mcp_servers.json"
    config.write_text("{}")

    async def _run() -> str:
        with _FakeMcpModules(session, _make_mock_stdio_cm()):
            gw = MCPGateway()
            await gw.start(config)
            return await gw.call_tool({"name": tool_name, "arguments": arguments})

    result = asyncio.run(_run())
    assert error_fragment in json.loads(result)["error"]
    session.call_tool.assert_not_awaited()


def test_gitlab_catalog_is_explicit_and_disjoint() -> None:
    assert len(_GITLAB_READ_TOOLS) == 28
    assert len(_GITLAB_WRITE_TOOLS) == 14
    assert _GITLAB_READ_TOOLS.isdisjoint(_GITLAB_WRITE_TOOLS)
    assert _GITLAB_ALLOWED_TOOLS == _GITLAB_READ_TOOLS | _GITLAB_WRITE_TOOLS


def test_gitlab_writes_disabled_without_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITLAB_WRITE_NAMESPACES")
    session = _make_mock_session()
    config = tmp_path / "mcp_servers.json"
    config.write_text("{}")

    async def _run() -> str:
        with _FakeMcpModules(session, _make_mock_stdio_cm()):
            gw = MCPGateway()
            await gw.start(config)
            return await gw.call_tool({
                "name": "gitlab__create_issue",
                "arguments": {
                    "id": "acme/platform/org/branding",
                    "title": "Blocked",
                },
            })

    result = asyncio.run(_run())
    assert "GitLab writes are disabled" in json.loads(result)["error"]
    session.call_tool.assert_not_awaited()


@pytest.mark.parametrize(
    "target",
    (
        "acme/platform",
        "acme/platform/org/branding",
        "acme%2Fplatform%2Fbranding",
        "https://gitlab.com/acme/platform/org/branding/-/issues/1",
    ),
)
def test_gitlab_write_accepts_configured_path_forms(tmp_path: Path, target: str) -> None:
    session = _make_mock_session()
    session.call_tool.return_value = _make_text_result('{"result": "ok"}')
    config = tmp_path / "mcp_servers.json"
    config.write_text("{}")

    async def _run() -> str:
        with _FakeMcpModules(session, _make_mock_stdio_cm()):
            gw = MCPGateway()
            await gw.start(config)
            return await gw.call_tool(
                {
                    "name": "gitlab__create_issue",
                    "arguments": {"id": target, "title": "Scoped"},
                }
            )

    result = asyncio.run(_run())
    assert json.loads(result)["result"] == "ok"
    session.call_tool.assert_awaited_once()


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
