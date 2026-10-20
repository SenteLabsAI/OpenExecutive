"""An installed extension's check on calls to its own MCP server
(orchestrator/extensions.py, register_gateway_check; mcp_gateway.call_tool).

The check sees every call to its server and may refuse it, which is audited
as a blocked outbound call; otherwise ``outgoing`` builds what is sent, after
every gate and again for each send, from the model's own arguments. A check
that fails refuses the call. Calls to any other server never reach it.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import openexecutive.orchestrator.mcp_gateway as gw_module
from openexecutive.orchestrator import extensions
from openexecutive.orchestrator.extensions import ExtensionError, GatewayCall, GatewayCheck
from openexecutive.orchestrator.mcp_gateway import MCPGateway


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    extensions._reset_for_tests()
    # No OPENEXECUTIVE_EXTENSIONS here: count the registry as loaded.
    monkeypatch.setattr(extensions, "_loaded", True)
    yield
    extensions._reset_for_tests()


@pytest.fixture()
def audit_rows(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **k: rows.append((a, k)))
    return rows


def _gateway(*results: str) -> tuple[MCPGateway, AsyncMock]:
    gateway = MCPGateway()
    session = MagicMock()
    session.call_tool = AsyncMock(side_effect=[MagicMock(content=[MagicMock(text=t)]) for t in results])
    gateway._session = session
    return gateway, session.call_tool


def _call(gateway: MCPGateway, name: str, arguments: Any) -> dict[str, Any]:
    return json.loads(asyncio.run(gateway.call_tool({"name": name, "arguments": arguments})))


def _register(check: Any, server: str = "kiosk") -> None:
    extensions.register_gateway_check(GatewayCheck(server=server, check=check))


# --------------------------------------------------------------------- #
# Registering
# --------------------------------------------------------------------- #


@pytest.mark.parametrize("server", ["google_workspace", "microsoft_365", "fetch"])
def test_a_built_in_server_takes_no_check(server: str) -> None:
    with pytest.raises(ExtensionError, match="built in"):
        _register(lambda _t, _a: GatewayCall(), server=server)


@pytest.mark.parametrize("server", ["", "Kiosk", "kiosk-app", "a__b", "1kiosk", "kiosk_", "_kiosk", "k" * 40])
def test_a_server_name_must_be_one_the_gateway_can_split(server: str) -> None:
    with pytest.raises(ExtensionError, match="server name"):
        _register(lambda _t, _a: GatewayCall(), server=server)


def test_a_server_has_one_check() -> None:
    _register(lambda _t, _a: GatewayCall())
    with pytest.raises(ExtensionError, match="already"):
        _register(lambda _t, _a: GatewayCall())


def test_the_check_covers_its_own_server_only() -> None:
    _register(lambda _t, _a: GatewayCall())
    assert extensions.gateway_check_for("kiosk__show_menu") is not None
    for other in ("kiosk", "kiosks__show_menu", "kioskx__show_menu", "google_workspace__send_gmail_message"):
        assert extensions.gateway_check_for(other) is None


# --------------------------------------------------------------------- #
# In call_tool
# --------------------------------------------------------------------- #


def test_a_refused_call_never_goes_out_and_is_audited(audit_rows: list[tuple[Any, ...]]) -> None:
    _register(lambda _t, _a: GatewayCall(refusal="Not for this person."))
    gateway, sent = _gateway('{"ok": true}')
    out = _call(gateway, "kiosk__show_menu", {"table": 4})
    assert out == {"error": "Not for this person."}
    sent.assert_not_awaited()
    (args, kwargs), = audit_rows
    assert args[0] == "integration_outbound_blocked"
    assert kwargs["details"]["tool"] == "kiosk__show_menu"
    assert kwargs["details"]["field"] == "server_check"


def test_the_check_sees_the_normalized_name_and_a_copy_of_the_arguments() -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    def check(tool: str, arguments: dict[str, Any]) -> GatewayCall:
        seen.append((tool, arguments))
        arguments["table"] = 99  # the check's copy, not what the model sent
        return GatewayCall()

    _register(check)
    gateway, sent = _gateway('{"ok": true}')
    _call(gateway, "kiosk__Show-Menu", {"table": 4})
    assert seen == [("kiosk__show_menu", {"table": 99})]
    sent.assert_awaited_once_with("call_tool", {"tool_name": "kiosk__Show-Menu", "arguments": {"table": 4}})


def test_outgoing_builds_what_is_sent_from_the_model_arguments() -> None:
    def outgoing(arguments: dict[str, Any]) -> dict[str, Any]:
        return {**arguments, "_signed": "token-1"}

    _register(lambda _t, _a: GatewayCall(outgoing=outgoing))
    gateway, sent = _gateway('{"ok": true}')
    assert _call(gateway, "kiosk__show_menu", {"table": 4}) == {"ok": True}
    sent.assert_awaited_once_with(
        "call_tool", {"tool_name": "kiosk__show_menu", "arguments": {"table": 4, "_signed": "token-1"}}
    )


def test_outgoing_is_built_again_for_a_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    # A token good for one request: the discovery retry gets its own.
    tokens = iter(["token-1", "token-2"])
    _register(lambda _t, _a: GatewayCall(outgoing=lambda a: {**a, "_signed": next(tokens)}))
    monkeypatch.setattr(gw_module, "_is_undiscovered_refusal", lambda name, text: text == "undiscovered")
    gateway, sent = _gateway("undiscovered", '{"ok": true}')
    monkeypatch.setattr(gateway, "_discover", AsyncMock(return_value=True))
    assert _call(gateway, "kiosk__show_menu", {"table": 4}) == {"ok": True}
    assert [c.args[1]["arguments"]["_signed"] for c in sent.await_args_list] == ["token-1", "token-2"]


def test_a_check_that_raises_refuses_the_call(audit_rows: list[tuple[Any, ...]]) -> None:
    def check(_t: str, _a: dict[str, Any]) -> GatewayCall:
        raise RuntimeError("boom")

    _register(check)
    gateway, sent = _gateway('{"ok": true}')
    assert "check failed" in _call(gateway, "kiosk__show_menu", {})["error"]
    sent.assert_not_awaited()
    assert audit_rows and audit_rows[0][0][0] == "integration_outbound_blocked"


def test_a_check_that_returns_something_else_refuses_the_call() -> None:
    _register(lambda _t, _a: None)
    gateway, sent = _gateway('{"ok": true}')
    assert "check failed" in _call(gateway, "kiosk__show_menu", {})["error"]
    sent.assert_not_awaited()


@pytest.mark.parametrize("built", [RuntimeError("boom"), "not a dict"])
def test_an_outgoing_that_fails_refuses_the_call(built: Any) -> None:
    def outgoing(_a: dict[str, Any]) -> Any:
        if isinstance(built, Exception):
            raise built
        return built

    _register(lambda _t, _a: GatewayCall(outgoing=outgoing))
    gateway, sent = _gateway('{"ok": true}')
    assert "check failed" in _call(gateway, "kiosk__show_menu", {})["error"]
    sent.assert_not_awaited()


def test_arguments_that_are_not_an_object_never_reach_the_check() -> None:
    check = MagicMock(return_value=GatewayCall())
    _register(check)
    gateway, sent = _gateway('{"ok": true}')
    assert "JSON object" in _call(gateway, "kiosk__show_menu", ["table", 4])["error"]
    check.assert_not_called()
    sent.assert_not_awaited()


def test_another_server_is_untouched() -> None:
    check = MagicMock(return_value=GatewayCall(refusal="no"))
    _register(check)
    gateway, sent = _gateway('{"ok": true}')
    assert _call(gateway, "github__list_prs", {"repo": "a/b"}) == {"ok": True}
    check.assert_not_called()
    sent.assert_awaited_once_with("call_tool", {"tool_name": "github__list_prs", "arguments": {"repo": "a/b"}})


@pytest.mark.parametrize("name", ["Kiosk__show_menu", "KIOSK__show_menu", " kiosk__show_menu"])
def test_a_server_spelled_differently_is_refused_not_signed(name: str) -> None:
    # The proxy routes by the exact name: "Kiosk__x" would reach a server
    # loaded at runtime as "Kiosk", carrying what this check signed.
    outgoing = MagicMock(return_value={"_signed": "token-1"})
    _register(lambda _t, _a: GatewayCall(outgoing=outgoing))
    gateway, sent = _gateway('{"ok": true}')
    assert "exact name" in _call(gateway, name, {})["error"]
    outgoing.assert_not_called()
    sent.assert_not_awaited()


@pytest.mark.parametrize("name", ["kiosk", "Kiosk", "KIOSK", " kiosk "])
def test_a_runtime_server_cannot_take_a_checked_servers_name(name: str) -> None:
    _register(lambda _t, _a: GatewayCall())
    gateway, sent = _gateway('{"ok": true}')
    out = json.loads(asyncio.run(gateway.load_mcp_server({"name": name, "url": "https://evil.example/mcp"})))
    assert "belongs to an installed extension" in out["error"]
    sent.assert_not_awaited()


def test_an_answer_never_hands_back_what_outgoing_added() -> None:
    token = "v1.payload.signature-abc"
    _register(lambda _t, _a: GatewayCall(outgoing=lambda a: {**a, "_signed": token}))
    echo = json.dumps({"error": f"validation failed: input_value={{'_signed': '{token}', 'table': 'window seat'}}"})
    gateway, _ = _gateway(echo)
    out = _call(gateway, "kiosk__show_menu", {"table": "window seat"})
    assert token not in json.dumps(out) and "[redacted]" in out["error"]
    # The model's own words stay.
    assert "window seat" in out["error"]
