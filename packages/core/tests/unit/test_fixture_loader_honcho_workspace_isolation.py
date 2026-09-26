"""Fixture lifecycle must isolate Honcho traffic per demo.

- ``load_fixture`` switches Honcho's active workspace to a per-fixture
  id (``openexec-fixture-<name>-<uuid8>``) so demo turns sync to a
  sacrificial store rather than polluting the operator's real peer cards.
- ``unload_fixture`` deletes the demo workspace and clears the override
  so the operator's env-default workspace takes over again.
- ``reset_all_state`` deletes both the active workspace and the
  env-default workspace if they differ.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from openexecutive.cli import fixture_loader
from openexecutive.knowledge.store import ChromaDBStore
from openexecutive.memory import honcho_client


@pytest.fixture()
def settings_stub(tmp_path: Path) -> Any:
    profile_path = tmp_path / "company" / "profile.yaml"
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text("name: ''\n")
    return type(
        "S",
        (),
        {
            "vector_store_path": tmp_path / "chroma",
            "company_profile_path": profile_path,
            "honcho_workspace_id": "openexec-test",
        },
    )()


@pytest.fixture(autouse=True)
def _isolate_dbs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.alerts import store as alerts_store
    from openexecutive.audit.logger import AuditLogger
    from openexecutive.departments import store as dept_store
    from openexecutive.evals.persistence import initialize_eval_runs_db
    from openexecutive.memory import episodic
    from openexecutive.people import store as people_store
    from openexecutive.workflows.persistence import initialize_runs_db

    episodic_path = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", episodic_path)
    monkeypatch.setattr(people_store, "DB_PATH", tmp_path / "people.db")
    monkeypatch.setattr(dept_store, "DB_PATH", tmp_path / "depts.db")
    episodic.initialize_db()
    # The reset also wipes the alert, workflow-run, eval-run and audit tables
    # that share the episodic DB, and fails on one that doesn't exist.
    alerts_store.initialize_db(episodic_path)
    initialize_runs_db(episodic_path)
    initialize_eval_runs_db(episodic_path)
    AuditLogger(db_path=episodic_path)  # the constructor creates audit_log
    people_store.initialize_db()
    dept_store.initialize_db()
    # Point the Honcho state file at the test's company profile dir.
    monkeypatch.setenv("COMPANY_PROFILE_PATH", str(tmp_path / "company" / "profile.yaml"))
    monkeypatch.setenv("HONCHO_WORKSPACE_ID", "openexec-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("EXEC_EMAIL_ADDRESS", "exec@example.com")
    honcho_client.reset_client_for_tests()
    # Also clear any leftover state file from a prior test.
    state = (tmp_path / "company" / "_user_backup" / ".honcho_active_workspace.json")
    if state.exists():
        state.unlink()


def _stub_chroma() -> Any:
    """Skip ChromaDB heavy lifting."""
    return [
        patch.object(ChromaDBStore, "delete_company_docs", lambda self: None),
        patch.object(ChromaDBStore, "delete_documents", lambda self, **kw: None),
    ]


def test_load_fixture_switches_active_workspace(
    settings_stub: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """After load_fixture, the active workspace id is per-fixture."""
    # Stub the actual fixture-loading bits we don't care about here.
    monkeypatch.setattr(fixture_loader, "_apply_state_from_source",
                        lambda src, st: _async_return({"loaded_from": str(src)}))

    # The fixture name must exist in the registry; create a stub fixture dir.
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    (fixture_root / "bombas").mkdir()
    (fixture_root / "bombas" / "profile.yaml").write_text("name: Bombas\n")
    monkeypatch.setattr(fixture_loader, "FIXTURES_ROOT", fixture_root)

    delete_calls: list[str | None] = []

    async def _spy(workspace_id: str | None = None) -> None:
        delete_calls.append(workspace_id)

    monkeypatch.setattr(honcho_client, "delete_workspace_and_reset_client", _spy)

    asyncio.run(fixture_loader.load_fixture("bombas", settings_stub))

    active = honcho_client.get_active_workspace_id()
    assert active.startswith("openexec-fixture-bombas-")
    assert active != "openexec-test"
    # No previous demo workspace existed, so delete should NOT have fired.
    assert delete_calls == []


def test_unload_fixture_deletes_demo_workspace_and_clears_override(
    settings_stub: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Unload must delete the demo workspace and restore the env default."""
    # Seed an active override as if a fixture is loaded.
    honcho_client.set_active_workspace_id(
        "openexec-fixture-bombas-deadbeef", fixture_name="bombas"
    )
    # And a fake backup dir + profile so unload_fixture proceeds.
    backup = settings_stub.company_profile_path.parent / "_user_backup"
    backup.mkdir(parents=True, exist_ok=True)
    (backup / "profile.yaml").write_text("name: ''\n")

    monkeypatch.setattr(
        fixture_loader, "_apply_state_from_source",
        lambda src, st: _async_return({"restored": True}),
    )

    deletes: list[str | None] = []

    async def _spy(workspace_id: str | None = None) -> None:
        deletes.append(workspace_id)

    monkeypatch.setattr(honcho_client, "delete_workspace_and_reset_client", _spy)

    asyncio.run(fixture_loader.unload_fixture(settings_stub))

    assert deletes == ["openexec-fixture-bombas-deadbeef"]
    # Override removed → falls back to env default.
    assert honcho_client.get_active_workspace_id() == "openexec-test"


def test_unload_with_no_active_override_skips_delete(
    settings_stub: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If no per-fixture override is active, unload doesn't try to delete anything."""
    backup = settings_stub.company_profile_path.parent / "_user_backup"
    backup.mkdir(parents=True, exist_ok=True)
    (backup / "profile.yaml").write_text("name: ''\n")
    monkeypatch.setattr(
        fixture_loader, "_apply_state_from_source",
        lambda src, st: _async_return({"restored": True}),
    )

    deletes: list[str | None] = []

    async def _spy(workspace_id: str | None = None) -> None:
        deletes.append(workspace_id)

    monkeypatch.setattr(honcho_client, "delete_workspace_and_reset_client", _spy)

    asyncio.run(fixture_loader.unload_fixture(settings_stub))

    # No override → no per-workspace delete invoked.
    assert deletes == []


def test_reset_deletes_both_active_and_default_when_different(
    settings_stub: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Reset with an active per-fixture workspace must delete BOTH."""
    honcho_client.set_active_workspace_id(
        "openexec-fixture-bombas-deadbeef", fixture_name="bombas"
    )

    deletes: list[str | None] = []

    async def _spy(workspace_id: str | None = None) -> None:
        deletes.append(workspace_id)

    monkeypatch.setattr(honcho_client, "delete_workspace_and_reset_client", _spy)

    with _stub_chroma()[0], _stub_chroma()[1]:
        asyncio.run(fixture_loader.reset_all_state(settings_stub))

    assert deletes == ["openexec-fixture-bombas-deadbeef", "openexec-test"]
    # Override cleared.
    assert honcho_client.get_active_workspace_id() == "openexec-test"


# --------------------------------------------------------------------------- #
# Startup reconcile
# --------------------------------------------------------------------------- #


def test_reconcile_clears_orphaned_override(
    settings_stub: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Override file exists but no .fixture_active sentinel → clear it.

    Simulates a process that crashed between load_fixture and unload_fixture.
    """
    honcho_client.set_active_workspace_id("openexec-fixture-bombas-deadbeef")
    assert honcho_client.get_active_workspace_id() == "openexec-fixture-bombas-deadbeef"
    # No sentinel — that's the orphaned-state signal.
    sentinel = fixture_loader._fixture_active_sentinel(settings_stub)
    assert not sentinel.exists()

    fixture_loader.reconcile_honcho_workspace_on_startup(settings_stub)

    assert honcho_client.get_active_workspace_id() == "openexec-test"


def test_reconcile_preserves_override_when_sentinel_present(
    settings_stub: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both override AND sentinel exist → normal in-progress demo, leave it alone."""
    honcho_client.set_active_workspace_id("openexec-fixture-bombas-deadbeef")
    sentinel = fixture_loader._fixture_active_sentinel(settings_stub)
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("bombas")

    fixture_loader.reconcile_honcho_workspace_on_startup(settings_stub)

    # Still active — not a crash recovery situation.
    assert honcho_client.get_active_workspace_id() == "openexec-fixture-bombas-deadbeef"


def test_reconcile_no_override_is_a_noop(
    settings_stub: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No state file → reconcile does nothing."""
    fixture_loader.reconcile_honcho_workspace_on_startup(settings_stub)
    assert honcho_client.get_active_workspace_id() == "openexec-test"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


async def _async_return(value: Any) -> Any:
    return value
