"""HTTP-level tests for /saved-tools: the owner sees the saved tools and can
turn one off or switch it back to an earlier version; nobody else can."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import saved_tools as route
from openexecutive.workflows import saved_tools


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "openexecutive.audit.log_event",
        lambda event_type, summary, **kw: rows.append({"type": event_type, "summary": summary, **kw}),
    )
    return rows


@pytest.fixture
def principal(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    state = {"is": True}
    monkeypatch.setattr("openexecutive.api.routes.people.caller_is_principal", lambda _r: state["is"])
    return state


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, audit: Any, principal: Any) -> TestClient:
    monkeypatch.setattr(saved_tools, "DB_PATH", tmp_path / "saved.db")
    monkeypatch.delenv("SAVED_TOOLS_ENABLED", raising=False)
    saved_tools.save("count_files", "Count files.", "1", ["drive__list_items"], origin="chat")
    saved_tools.save("count_files", "Count the files.", "2", ["drive__list_items"], origin="chat")
    saved_tools.record_run("count_files", 2, ok=True, calls=3, duration_ms=40, origin="chat")
    app = FastAPI()
    app.include_router(route.router)
    return TestClient(app)


def test_list_and_detail(client: TestClient) -> None:
    body = client.get("/saved-tools").json()
    assert body["enabled"] is True
    assert [(t["name"], t["version"], t["enabled"]) for t in body["tools"]] == [("count_files", 2, True)]
    assert "script" not in body["tools"][0]
    detail = client.get("/saved-tools/count_files").json()
    assert detail["script"] == "2"
    assert [v["version"] for v in detail["versions"]] == [2, 1]
    assert detail["runs"][0]["calls"] == 3


def test_turn_off_and_roll_back_are_audited(client: TestClient, audit: list[dict[str, Any]]) -> None:
    off = client.put("/saved-tools/count_files", json={"enabled": False}).json()
    assert off["enabled"] is False
    back = client.post("/saved-tools/count_files/rollback", json={"version": 1}).json()
    assert (back["version"], back["script"], back["enabled"]) == (1, "1", False)
    assert [r["type"] for r in audit] == ["saved_tool_changed", "saved_tool_changed"]
    assert audit[1]["details"] == {"name": "count_files", "version": 1}


def test_unknown_names_and_versions_are_404(client: TestClient) -> None:
    assert client.get("/saved-tools/nope_tool").status_code == 404
    assert client.put("/saved-tools/nope_tool", json={"enabled": True}).status_code == 404
    assert client.post("/saved-tools/count_files/rollback", json={"version": 7}).status_code == 404


def test_only_the_owner(client: TestClient, principal: dict[str, bool]) -> None:
    principal["is"] = False
    assert client.get("/saved-tools").status_code == 403
    assert client.get("/saved-tools/count_files").status_code == 403
    assert client.put("/saved-tools/count_files", json={"enabled": False}).status_code == 403
    assert client.post("/saved-tools/count_files/rollback", json={"version": 1}).status_code == 403
    assert saved_tools.get("count_files").enabled  # type: ignore[union-attr]


def test_the_switch_shows(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAVED_TOOLS_ENABLED", "false")
    assert client.get("/saved-tools").json()["enabled"] is False


def test_turning_workflows_on_needs_a_request_tied_to_the_owner(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, audit: list[dict[str, Any]]
) -> None:
    tied = {"is": False}
    monkeypatch.setattr(route, "_tied_to_the_owner", lambda _r: tied["is"])
    assert client.put("/saved-tools/count_files", json={"workflows": True}).status_code == 409
    assert saved_tools.get("count_files").workflow_version is None  # type: ignore[union-attr]
    tied["is"] = True
    body = client.put("/saved-tools/count_files", json={"workflows": True}).json()
    assert body["workflow_version"] == 2
    # Turning it off never needs more than the owner.
    tied["is"] = False
    assert client.put("/saved-tools/count_files", json={"workflows": False}).json()["workflow_version"] is None
    assert client.put("/saved-tools/count_files", json={}).status_code == 422
    assert [r["details"].get("workflow_version") for r in audit] == [2, None]
