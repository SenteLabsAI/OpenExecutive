"""HTTP tests for the /workflows/custom CRUD surface + catalog integration."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-used")

from fastapi.testclient import TestClient  # noqa: E402

from openexecutive.api.main import create_app  # noqa: E402
from openexecutive.workflows import dynamic_store  # noqa: E402


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    db_path = tmp_path / "episodic.db"
    dynamic_store.initialize_dynamic_workflows_db(db_path)
    monkeypatch.setattr(dynamic_store, "DB_PATH", db_path)
    # Match CI: no shared-secret gate so requests aren't 401'd.
    monkeypatch.delenv("BACKEND_SHARED_SECRET", raising=False)
    app = create_app()
    with TestClient(app) as c:
        yield c


def _definition(name: str = "weekly_watch", **overrides: Any) -> dict[str, Any]:
    base = {
        "name": name,
        "title": "Weekly Watch",
        "input_fields": [{"name": "topic", "label": "Topic", "required": True}],
        "steps": [
            {"kind": "specialist", "id": "research", "title": "Research",
             "specialist": "cso", "goal": "Analyze {topic}."},
            {"kind": "synthesis", "id": "assemble", "title": "Assemble"},
        ],
    }
    base.update(overrides)
    return base


def test_create_list_get_delete(client: TestClient) -> None:
    r = client.post("/workflows/custom", json=_definition())
    assert r.status_code == 201, r.text
    assert r.json()["name"] == "weekly_watch"

    r = client.get("/workflows/custom")
    assert [d["name"] for d in r.json()["definitions"]] == ["weekly_watch"]

    r = client.get("/workflows/custom/weekly_watch")
    assert r.status_code == 200
    assert r.json()["title"] == "Weekly Watch"

    r = client.delete("/workflows/custom/weekly_watch")
    assert r.status_code == 200
    assert client.get("/workflows/custom/weekly_watch").status_code == 404


def test_create_rejects_invalid(client: TestClient) -> None:
    bad = _definition()
    bad["steps"] = [bad["steps"][0]]  # missing synthesis
    r = client.post("/workflows/custom", json=bad)
    assert r.status_code == 422
    assert any("synthesis" in str(d) for d in r.json()["detail"])


def test_create_rejects_duplicate(client: TestClient) -> None:
    assert client.post("/workflows/custom", json=_definition()).status_code == 201
    r = client.post("/workflows/custom", json=_definition())
    assert r.status_code == 409


def test_update(client: TestClient) -> None:
    client.post("/workflows/custom", json=_definition())
    updated = _definition(title="Renamed Watch")
    r = client.put("/workflows/custom/weekly_watch", json=updated)
    assert r.status_code == 200
    assert r.json()["title"] == "Renamed Watch"


def test_update_name_mismatch_rejected(client: TestClient) -> None:
    client.post("/workflows/custom", json=_definition())
    r = client.put("/workflows/custom/weekly_watch", json=_definition(name="other_name"))
    assert r.status_code == 422


def test_activate_toggle(client: TestClient) -> None:
    client.post("/workflows/custom", json=_definition())
    r = client.post("/workflows/custom/weekly_watch/activate", json={"is_active": False})
    assert r.status_code == 200
    assert r.json()["is_active"] is False


def test_custom_workflow_appears_in_catalog_and_meta(client: TestClient) -> None:
    client.post("/workflows/custom", json=_definition())
    # Shows up in GET /workflows alongside built-ins, flagged is_custom.
    catalog = client.get("/workflows").json()["workflows"]
    entry = next((w for w in catalog if w["name"] == "weekly_watch"), None)
    assert entry is not None
    assert entry["is_custom"] is True
    # Its synthesized input schema is served by the per-workflow meta route.
    meta = client.get("/workflows/weekly_watch").json()
    assert "topic" in meta["input_schema"]["properties"]


def test_run_validates_dynamic_input_schema(client: TestClient) -> None:
    """Starting a run with a missing required field 422s — proving the
    synthesized input_model is wired into the run path."""
    client.post("/workflows/custom", json=_definition())
    r = client.post("/workflows/weekly_watch/runs", json={})  # missing 'topic'
    assert r.status_code == 422


def test_get_missing_custom_returns_404(client: TestClient) -> None:
    assert client.get("/workflows/custom/nope").status_code == 404
    assert client.delete("/workflows/custom/nope").status_code == 404


# --- action steps & tool search ---------------------------------------------


def _action_definition(tools: list[str]) -> dict[str, Any]:
    return _definition(
        name="file_bills",
        input_fields=[],
        steps=[
            {"kind": "action", "id": "file", "title": "File bills",
             "goal": "Add bills to the tracker.", "tools": tools},
            {"kind": "synthesis", "id": "assemble", "title": "Assemble"},
        ],
    )


def test_create_rejects_unknown_tools(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "openexecutive.orchestrator.mcp_gateway.get_active_gateway", lambda: None
    )
    r = client.post("/workflows/custom", json=_action_definition(["srv__ghost"]))
    assert r.status_code == 422
    assert "srv__ghost" in " ".join(r.json()["detail"])
    # Built-ins need no gateway.
    r = client.post("/workflows/custom", json=_action_definition(["oe__read_file"]))
    assert r.status_code == 201, r.text


def test_update_rejects_unknown_tools(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "openexecutive.orchestrator.mcp_gateway.get_active_gateway", lambda: None
    )
    assert client.post("/workflows/custom", json=_action_definition(["oe__read_file"])).status_code == 201
    r = client.put("/workflows/custom/file_bills", json=_action_definition(["srv__ghost"]))
    assert r.status_code == 422


def test_activate_revalidates_tools(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Turning a workflow on is the approval point for one chat saved off, so
    a tool that has gone away blocks activation; turning off never does."""
    from openexecutive.api.routes import workflows as routes

    real = routes.validate_definition_and_tools
    defn = _action_definition(["oe__read_file"])
    assert client.post("/workflows/custom", json=defn).status_code == 201
    assert client.post("/workflows/custom/file_bills/activate", json={"is_active": False}).status_code == 200

    async def _missing(_defn: Any) -> list[str]:
        return ["step 'file': tool 'oe__read_file' is not available"]

    monkeypatch.setattr(routes, "validate_definition_and_tools", _missing)
    r = client.post("/workflows/custom/file_bills/activate", json={"is_active": True, "definition": defn})
    assert r.status_code == 422
    assert "not available" in " ".join(r.json()["detail"])
    assert dynamic_store.get_definition("file_bills").is_active is False  # type: ignore[union-attr]
    # Turning off skips the check.
    assert client.post("/workflows/custom/file_bills/activate", json={"is_active": False}).status_code == 200

    monkeypatch.setattr(routes, "validate_definition_and_tools", real)
    r = client.post("/workflows/custom/file_bills/activate", json={"is_active": True, "definition": defn})
    assert r.status_code == 200, r.text
    assert r.json()["is_active"] is True


def test_activate_requires_the_reviewed_version(client: TestClient) -> None:
    """The click can only switch on what the card showed: a tool workflow
    changed since (e.g. overwritten from chat) or activated blind is a 409."""
    shown = _action_definition(["oe__read_file"])
    assert client.post("/workflows/custom", json=shown).status_code == 201
    client.post("/workflows/custom/file_bills/activate", json={"is_active": False})
    changed = _action_definition(["oe__read_file", "oe__create_alert"])
    assert client.put("/workflows/custom/file_bills", json={**changed, "is_active": False}).status_code == 200

    for body in ({"is_active": True, "definition": shown}, {"is_active": True}, {"is_active": True, "definition": "x"}):
        r = client.post("/workflows/custom/file_bills/activate", json=body)
        assert r.status_code == 409, body
    assert dynamic_store.get_definition("file_bills").is_active is False  # type: ignore[union-attr]

    # The stored definition as fetched (timestamps, is_active) still matches.
    fetched = client.get("/workflows/custom/file_bills").json()
    r = client.post("/workflows/custom/file_bills/activate", json={"is_active": True, "definition": fetched})
    assert r.status_code == 200, r.text
    assert r.json()["is_active"] is True


def test_activate_rechecks_after_validation(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A save landing while the tool check awaits the gateway is not switched on."""
    from openexecutive.api.routes import workflows as routes
    from openexecutive.workflows.dynamic_models import DynamicWorkflowDef

    shown = _action_definition(["oe__read_file"])
    client.post("/workflows/custom", json=shown)
    client.post("/workflows/custom/file_bills/activate", json={"is_active": False})

    async def _swap_during_check(_defn: Any) -> list[str]:
        swapped = _action_definition(["oe__read_file", "oe__create_alert"])
        dynamic_store.upsert_definition(
            DynamicWorkflowDef.model_validate({**swapped, "is_active": False})
        )
        return []

    monkeypatch.setattr(routes, "validate_definition_and_tools", _swap_during_check)
    r = client.post("/workflows/custom/file_bills/activate", json={"is_active": True, "definition": shown})
    assert r.status_code == 409
    assert dynamic_store.get_definition("file_bills").is_active is False  # type: ignore[union-attr]


def test_activate_if_unchanged_is_compare_and_set(client: TestClient) -> None:
    """The store only switches on the exact revision the caller read, so a
    save from another process between check and write can't be switched on."""
    from openexecutive.workflows.dynamic_models import DynamicWorkflowDef

    shown = _action_definition(["oe__read_file"])
    client.post("/workflows/custom", json=shown)
    client.post("/workflows/custom/file_bills/activate", json={"is_active": False})
    read = dynamic_store.get_definition("file_bills")
    assert read is not None

    # Another writer lands after the read.
    swapped = _action_definition(["oe__read_file", "oe__create_alert"])
    dynamic_store.upsert_definition(DynamicWorkflowDef.model_validate({**swapped, "is_active": False}))
    assert dynamic_store.activate_if_unchanged(read) is False
    latest = dynamic_store.get_definition("file_bills")
    assert latest is not None and latest.is_active is False
    assert [s.tools for s in latest.steps if s.kind == "action"] == [["oe__read_file", "oe__create_alert"]]

    # The current revision switches on, keeping its body in sync.
    assert dynamic_store.activate_if_unchanged(latest) is True
    on = dynamic_store.get_definition("file_bills")
    assert on is not None and on.is_active is True
    assert [d.name for d in dynamic_store.list_definitions(active_only=True)] == ["file_bills"]
    # Gone -> None.
    dynamic_store.delete_definition("file_bills")
    assert dynamic_store.activate_if_unchanged(latest) is None


def test_activate_ignores_non_object_body(client: TestClient) -> None:
    """A non-object body falls back to the default (turn on) instead of a 500."""
    client.post("/workflows/custom", json=_definition())
    client.post("/workflows/custom/weekly_watch/activate", json={"is_active": False})
    r = client.post("/workflows/custom/weekly_watch/activate", json=[1, 2])
    assert r.status_code == 200
    assert r.json()["is_active"] is True


def test_tool_search_endpoint(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "openexecutive.orchestrator.mcp_gateway.get_active_gateway", lambda: None
    )
    assert client.get("/workflows/tools/search").json() == {"tools": []}
    tools = client.get("/workflows/tools/search", params={"q": "read a file"}).json()["tools"]
    assert tools[0] == {
        "name": "oe__read_file",
        "description": tools[0]["description"],
        "read_only": True,
        "source": "builtin",
    }


def test_tool_describe_endpoint(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "openexecutive.orchestrator.mcp_gateway.get_active_gateway", lambda: None
    )
    assert client.get("/workflows/tools/describe").json() == {"tools": []}
    tools = client.get(
        "/workflows/tools/describe", params={"names": "oe__message_person, srv__ghost,oe__read_file"}
    ).json()["tools"]
    assert [(t["name"], t["read_only"]) for t in tools] == [
        ("oe__message_person", None),
        ("oe__read_file", True),
    ]


def test_tool_describe_ignores_malformed_and_duplicate_names(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    searched: list[str] = []

    async def _resolve(names: list[str]) -> dict[str, Any]:
        searched.extend(names)
        return {}

    monkeypatch.setattr(
        "openexecutive.api.routes.workflows.resolve_tools_catalog", _resolve
    )
    client.get(
        "/workflows/tools/describe",
        params={"names": "srv__a,srv__a,has space,semi;colon," + "x" * 70},
    )
    assert searched == ["srv__a"]
