"""/mcp answers as the caller the API verified, through the real mounted app.

Drives MCP's streamable HTTP JSON-RPC over the FastAPI app ``create_app``
builds (shared-secret gate, ``caller_gate``, the ``/mcp`` mount), so what
``caller_gate`` puts on ``request.state`` is what the MCP handlers read.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings

from openexecutive.api import caller_signing
from openexecutive.memory import episodic
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store

VECTORS = json.loads(
    (Path(__file__).parents[1] / "unit" / "caller_assertion_vectors.json").read_text()
)
OWNER = "olivia@co.example"
TEAMMATE = "ben@co.example"


@pytest.fixture
def roster(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, int]]:
    db = tmp_path / "mcp_http.db"
    monkeypatch.setattr(episodic, "DB_PATH", db)
    monkeypatch.setattr(people_store, "DB_PATH", db)
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **kw: None)
    episodic.initialize_db(db)
    people_store.initialize_db()
    people_registry.invalidate()
    ids = {
        "principal": people_store.upsert_person(
            full_name="Olivia Owner", is_principal=True, email=OWNER
        ),
        "teammate": people_store.upsert_person(full_name="Ben Teammate", email=TEAMMATE),
    }
    people_registry.invalidate()
    yield ids
    people_registry.invalidate()


def _app(monkeypatch: pytest.MonkeyPatch, *, signed: bool) -> Any:
    """The API app, and the /mcp endpoint whose session manager each request
    replaces: a manager runs once per instance, and FastMCP's cached one is
    the process's (``run_session_manager``)."""
    from openexecutive.api.main import create_app
    from openexecutive.mcp_server import server as mcp_server

    monkeypatch.delenv("BACKEND_SHARED_SECRET", raising=False)
    if signed:
        monkeypatch.setenv("CALLER_ASSERTION_PUBLIC_KEYS", VECTORS["public_keys"])
    else:
        monkeypatch.delenv("CALLER_ASSERTION_PUBLIC_KEYS", raising=False)
    app = create_app()
    endpoint = next(
        getattr(r, "endpoint", None) or getattr(r, "app", None)
        for r in mcp_server._http_app.routes
        if hasattr(getattr(r, "endpoint", None) or getattr(r, "app", None), "session_manager")
    )
    # Restored after the test; each request installs a fresh manager.
    monkeypatch.setattr(endpoint, "session_manager", endpoint.session_manager)
    return app, endpoint


def _fresh_manager(endpoint: Any) -> StreamableHTTPSessionManager:
    from openexecutive.mcp_server import server as mcp_server

    manager = StreamableHTTPSessionManager(
        app=mcp_server.mcp._mcp_server,
        stateless=True,
        security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    endpoint.session_manager = manager
    return manager


def _signed_as(kind: str, email: str = "") -> dict[str, str]:
    token = caller_signing.mint_assertion(
        VECTORS["test_signing_key"], kind=kind, email=email, method="POST", target="/mcp/"
    )
    return {"x-caller-assertion": str(token)}


def _rpc(app: Any, endpoint: Any, method: str, params: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    manager = _fresh_manager(endpoint)

    async def _go() -> dict[str, Any]:
        async with manager.run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/mcp/",
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                    headers={
                        "accept": "application/json, text/event-stream",
                        "mcp-protocol-version": "2025-06-18",
                        **headers,
                    },
                )
        assert response.status_code == 200, response.text
        data = [line[5:].strip() for line in response.text.splitlines() if line.startswith("data:")]
        return dict(json.loads(data[-1] if data else response.text))

    return asyncio.run(_go())


def _read(app: Any, endpoint: Any, uri: str, headers: dict[str, str]) -> dict[str, Any]:
    return _rpc(app, endpoint, "resources/read", {"uri": uri}, headers)


@pytest.mark.parametrize(
    ("headers", "allowed"),
    [
        (lambda: _signed_as("user", OWNER), True),
        (lambda: _signed_as("operator"), True),
        (lambda: _signed_as("user", TEAMMATE), False),
        (lambda: {}, False),  # a service: signed callers on, no assertion
    ],
)
def test_signed_callers_reach_the_mcp_handlers(
    roster: dict[str, int], monkeypatch: pytest.MonkeyPatch, headers: Any, allowed: bool
) -> None:
    app, endpoint = _app(monkeypatch, signed=True)
    with patch("openexecutive.memory.episodic.list_decisions", return_value=[]):
        out = _read(app, endpoint, "oe://memory/decisions", headers())
    if allowed:
        assert json.loads(out["result"]["contents"][0]["text"]) == []
    else:
        assert "Only the owner can read this." in out["error"]["message"]


def test_a_signed_in_teammate_reads_the_roster_and_a_service_does_not(
    roster: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, endpoint = _app(monkeypatch, signed=True)
    out = _read(app, endpoint, "oe://people/roster", _signed_as("user", TEAMMATE))
    names = {p["full_name"] for p in json.loads(out["result"]["contents"][0]["text"])}
    assert names == {"Olivia Owner", "Ben Teammate"}
    refused = _read(app, endpoint, "oe://people/roster", {})
    assert "Only someone on the team can read this." in refused["error"]["message"]


def test_open_mode_takes_the_caller_header_at_its_word(
    roster: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, endpoint = _app(monkeypatch, signed=False)
    with patch("openexecutive.memory.episodic.list_advice", return_value=[]):
        mine = _read(app, endpoint, "oe://memory/advice", {})
        theirs = _read(app, endpoint, "oe://memory/advice", {"x-caller-email": TEAMMATE})
    assert "result" in mine
    assert "Only the owner can read this." in theirs["error"]["message"]


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        (lambda: _signed_as("user", TEAMMATE), "teammate"),
        (lambda: _signed_as("operator"), "principal"),
        (lambda: {}, None),
    ],
)
def test_ask_executive_asks_as_the_signed_caller(
    roster: dict[str, int], monkeypatch: pytest.MonkeyPatch, headers: Any, expected: str | None
) -> None:
    app, endpoint = _app(monkeypatch, signed=True)
    chat = AsyncMock(return_value="An answer")
    with (
        patch("openexecutive.orchestrator.executive.Executive.chat", new=chat),
        patch("openexecutive.orchestrator.mcp_gateway.get_active_gateway", return_value=None),
    ):
        out = _rpc(
            app, endpoint, "tools/call",
            {"name": "ask_executive", "arguments": {"message": "Top risk?"}}, headers(),
        )
    assert out["result"]["content"][0]["text"] == "An answer"
    kwargs = chat.await_args.kwargs
    person_id = roster[expected] if expected else None
    assert kwargs["person_id"] == person_id
    assert kwargs["session"].from_mcp_client is True
    assert kwargs["session"].caller_person_id == person_id
