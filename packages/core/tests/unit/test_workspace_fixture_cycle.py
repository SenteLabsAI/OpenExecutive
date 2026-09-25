"""Fixture load → unload must hand the user back their own workspace settings,
including when ``_user_backup/`` is a stale snapshot from an earlier load
(unload keeps it) that predates ``workspace.yaml``."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from openexecutive.cli import fixture_loader
from openexecutive.departments import registry as dept_registry
from openexecutive.departments import store as dept_store
from openexecutive.memory import episodic, honcho_client
from openexecutive.memory import workspace_settings as ws
from openexecutive.people import store as people_store

USER = ws.WorkspaceSettings(mode="solo", timezone="America/Chicago")


class _FakeStore:
    RESEARCH_COLLECTION = "recent_research"
    COMPANY_COLLECTION = "company_docs"

    def __init__(self, **_kw: object) -> None: ...

    def delete_company_docs(self) -> None: ...

    def delete_documents(self, **_kw: object) -> None: ...

    def delete_notion_docs(self) -> None: ...

    def delete_attachment_docs(self) -> None: ...


@pytest.fixture()
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    db = tmp_path / "episodic.db"
    for mod in (episodic, people_store, dept_store):
        monkeypatch.setattr(mod, "DB_PATH", db)
    episodic.initialize_db(db)
    people_store.initialize_db(db)
    dept_store.initialize_db(db)
    dept_registry.invalidate()
    monkeypatch.setattr(ws, "_configured_timezone", lambda: ws.ZoneInfo("UTC"))
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **k: None)

    # Everything outside the SQLite state is irrelevant here.
    monkeypatch.setattr("openexecutive.knowledge.store.ChromaDBStore", _FakeStore)
    monkeypatch.setattr(
        "openexecutive.knowledge.notion_sync.reset_local_state", lambda **_kw: None
    )

    async def _noop(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(honcho_client, "delete_workspace_and_reset_client", _noop)
    monkeypatch.setattr(honcho_client, "set_active_workspace_id", lambda *a, **k: None)
    monkeypatch.setattr(honcho_client, "clear_active_workspace_id", lambda *a, **k: None)
    monkeypatch.setattr(honcho_client, "get_active_workspace_id", lambda: "openexec")

    profile = tmp_path / "company" / "profile.yaml"
    profile.parent.mkdir(parents=True)
    profile.write_text("name: My Co\n")
    fixture = tmp_path / "fixtures" / "demo"
    fixture.mkdir(parents=True)
    (fixture / "profile.yaml").write_text("name: Demo Co\n")
    monkeypatch.setattr(fixture_loader, "FIXTURES_ROOT", tmp_path / "fixtures")
    return type("S", (), {
        "vector_store_path": tmp_path / "chroma",
        "company_profile_path": profile,
        "honcho_workspace_id": "openexec",
    })()


def _backup(settings: Any) -> Path:
    return settings.company_profile_path.parent / "_user_backup"


def _load(settings: Any) -> None:
    asyncio.run(fixture_loader.load_fixture("demo", settings))


def _unload(settings: Any) -> None:
    asyncio.run(fixture_loader.unload_fixture(settings))


def test_first_cycle_restores_the_users_settings(settings: Any) -> None:
    ws.restore_workspace_settings(USER)
    _load(settings)
    assert ws.get_workspace() == ws.WorkspaceSettings()  # the fixture has no workspace.yaml
    _unload(settings)
    assert ws.get_workspace() == USER


def test_stale_backup_without_workspace_file_is_refreshed_on_load(settings: Any) -> None:
    # A backup left by an earlier load/unload, from before workspace.yaml.
    backup = _backup(settings)
    backup.mkdir(parents=True)
    (backup / "profile.yaml").write_text("name: My Co\n")
    ws.restore_workspace_settings(USER)

    _load(settings)
    assert (backup / "workspace.yaml").exists()
    _unload(settings)
    assert ws.get_workspace() == USER


def test_stale_backup_workspace_file_is_overwritten_with_todays_settings(settings: Any) -> None:
    ws.restore_workspace_settings(ws.WorkspaceSettings(mode="team", timezone="Asia/Seoul"))
    _load(settings)
    _unload(settings)
    # The user changes their settings; the backup (kept by unload) is stale.
    ws.restore_workspace_settings(USER)
    _load(settings)
    _unload(settings)
    assert ws.get_workspace() == USER


def test_fixture_to_fixture_switch_keeps_the_users_backup(settings: Any) -> None:
    """Loading a second fixture while one is active must not overwrite the
    backup with the first fixture's settings."""
    ws.restore_workspace_settings(USER)
    _load(settings)
    ws.restore_workspace_settings(ws.WorkspaceSettings(mode="team", timezone="Europe/Rome"))
    _load(settings)
    _unload(settings)
    assert ws.get_workspace() == USER


def test_unload_from_a_backup_without_workspace_file_keeps_current_settings(settings: Any) -> None:
    """A backup that predates workspace.yaml restores nothing — the current
    settings are left alone rather than reset to team / no zone."""
    backup = _backup(settings)
    backup.mkdir(parents=True)
    (backup / "profile.yaml").write_text("name: My Co\n")
    fixture_loader._fixture_active_sentinel(settings).write_text("demo")
    ws.restore_workspace_settings(USER)
    _unload(settings)
    assert ws.get_workspace() == USER


def test_bad_zone_in_fixture_file_does_not_abort_the_load(settings: Any, tmp_path: Path) -> None:
    (tmp_path / "fixtures" / "demo" / "workspace.yaml").write_text(
        "mode: solo\ntimezone: America\n"
    )
    _load(settings)
    assert ws.get_workspace() == ws.WorkspaceSettings(mode="solo", timezone=None)
    assert "Demo Co" in settings.company_profile_path.read_text()  # the load completed
