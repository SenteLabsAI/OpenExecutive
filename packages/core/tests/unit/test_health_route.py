"""GET /health, and the pinned-Google-tools drift signal it reports.

`google_tools_missing` has three readings, and the point of the tests is that
only one of them asserts every pinned tool is present: `[]`. A probe that never
finished or that failed reports `null`, so neither can be mistaken for healthy.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import health as health_route
from openexecutive.orchestrator.mcp_gateway import PinnedToolsProbe


@pytest.fixture(autouse=True)
def _required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("EXEC_EMAIL_ADDRESS", "exec@example.com")
    # The route reads the company profile unguarded; keep it off the filesystem.
    from openexecutive.onboarding import profile_builder

    monkeypatch.setattr(
        profile_builder,
        "load_or_create_profile",
        lambda *a, **k: SimpleNamespace(is_empty=lambda: True, name=None),
    )


def _body(gateway: Any = None, *, set_state: bool = True) -> dict[str, Any]:
    app = FastAPI()
    app.include_router(health_route.router)
    if set_state:
        app.state.mcp_gateway = gateway
    resp = TestClient(app).get("/health")
    assert resp.status_code == 200
    return dict(resp.json())


def test_every_pinned_tool_found_reports_an_empty_list() -> None:
    gateway = SimpleNamespace(pinned_tools=PinnedToolsProbe(missing=()))
    assert _body(gateway)["google_tools_missing"] == []


def test_missing_tools_are_named() -> None:
    gateway = SimpleNamespace(
        pinned_tools=PinnedToolsProbe(missing=("google_workspace__create_doc", "x__y"))
    )
    assert _body(gateway)["google_tools_missing"] == [
        "google_workspace__create_doc",
        "x__y",
    ]


def test_priming_not_finished_reports_null_not_an_empty_list() -> None:
    gateway = SimpleNamespace(pinned_tools=None)
    assert _body(gateway)["google_tools_missing"] is None


def test_a_failed_probe_reports_null_not_an_empty_list() -> None:
    gateway = SimpleNamespace(pinned_tools=PinnedToolsProbe(missing=None))
    assert _body(gateway)["google_tools_missing"] is None


def test_no_gateway_reports_null() -> None:
    assert _body(None)["google_tools_missing"] is None


def test_an_app_without_the_mcp_lifespan_reports_null() -> None:
    # No mcp_gateway attribute on app.state at all, which is the shape every
    # bare FastAPI() test app and any embedder that skips the MCP lifespan has.
    assert _body(set_state=False)["google_tools_missing"] is None


def test_each_app_reads_its_own_gateway() -> None:
    # The route reads request.app.state, not a module-level singleton, so two
    # apps in one process do not see each other's priming result.
    first = SimpleNamespace(pinned_tools=PinnedToolsProbe(missing=("only_mine",)))
    second = SimpleNamespace(pinned_tools=PinnedToolsProbe(missing=()))
    assert _body(first)["google_tools_missing"] == ["only_mine"]
    assert _body(second)["google_tools_missing"] == []
