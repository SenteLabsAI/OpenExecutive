"""``reset_all_state`` must trigger Honcho workspace deletion alongside the
local wipe, and must keep going if the Honcho call raises.

The reset endpoint presents itself as a factory reset; without the Honcho
hook, peer cards keyed by old Person.id values are orphaned in the hosted
service forever. The regression-protective test here is the second one:
even if the Honcho call blows up (network, permission, missing SDK), the
rest of the reset still completes.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from openexecutive.cli import fixture_loader
from openexecutive.knowledge.store import ChromaDBStore


@pytest.fixture()
def settings_stub(tmp_path: Path) -> Any:
    """Minimal Settings-shape object that reset_all_state needs."""
    profile_path = tmp_path / "company" / "profile.yaml"
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text("name: ''\n")
    return type(
        "S",
        (),
        {
            "vector_store_path": tmp_path / "chroma",
            "company_profile_path": profile_path,
            "honcho_workspace_id": "openexec",
        },
    )()


@pytest.fixture(autouse=True)
def _isolate_dbs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the DBs the reset touches at tmp_path so the host's data isn't wiped.

    Also initialize each DB's schema, otherwise ``reset_all_state``'s seed
    step hits ``no such table: departments`` against the empty tmp files.
    """
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


def test_reset_calls_honcho_workspace_delete(
    settings_stub: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reset path must invoke delete_workspace_and_reset_client exactly once."""
    calls: list[str | None] = []

    async def _spy(workspace_id: str | None = None) -> None:
        calls.append(workspace_id)

    from openexecutive.memory import honcho_client
    monkeypatch.setattr(honcho_client, "delete_workspace_and_reset_client", _spy)

    # ChromaDB is heavy to construct; stub it so the test stays fast.
    with patch.object(ChromaDBStore, "delete_company_docs", lambda self: None), \
         patch.object(ChromaDBStore, "delete_documents", lambda self, **kw: None):
        import asyncio
        result = asyncio.run(fixture_loader.reset_all_state(settings_stub))

    assert len(calls) == 1, "expected exactly one Honcho workspace-delete call"
    assert result["reset"] is True


def test_reset_continues_when_honcho_delete_raises(
    settings_stub: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Honcho failure must NOT prevent the rest of reset_all_state from completing."""
    async def _boom(workspace_id: str | None = None) -> None:
        raise RuntimeError("honcho down")

    from openexecutive.memory import honcho_client
    monkeypatch.setattr(honcho_client, "delete_workspace_and_reset_client", _boom)

    with patch.object(ChromaDBStore, "delete_company_docs", lambda self: None), \
         patch.object(ChromaDBStore, "delete_documents", lambda self, **kw: None):
        import asyncio
        # Must not raise — the wrapper's broad except inside reset_all_state
        # swallows the failure.
        result = asyncio.run(fixture_loader.reset_all_state(settings_stub))

    assert result["reset"] is True
    # Departments still got re-seeded, proving step 5 ran AND step 6/7 ran
    # AFTER the Honcho failure (the hook is at 5b, so the early steps were
    # already done; the assert is that the function returned cleanly with
    # the post-step-5 work observable).
    assert "departments_seeded" in result


def test_reset_clears_active_override_between_two_workspace_deletes(
    settings_stub: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two-workspace path: when a demo fixture is active, reset deletes
    BOTH the demo workspace and the env-default. The active-workspace
    override must be cleared BETWEEN the two deletes, otherwise (in the
    pre-fix code) the second delete would rebuild a client bound to the
    just-deleted demo workspace and emit NotFoundError.

    Regression: the original ordering called clear_active_workspace_id()
    after both deletes, producing one NotFoundError audit row per reset
    when a demo fixture was active.
    """
    from openexecutive.memory import honcho_client

    # Stage an active demo override that differs from the env default
    # so the two-workspace branch is exercised.
    monkeypatch.setattr(
        honcho_client,
        "get_active_workspace_id",
        lambda: "openexec-fixture-demo-abcd1234",
    )

    call_order: list[str] = []
    cleared_at: list[int] = []

    def _spy_clear() -> None:
        cleared_at.append(len(call_order))

    monkeypatch.setattr(honcho_client, "clear_active_workspace_id", _spy_clear)

    async def _spy_delete(workspace_id: str | None = None) -> None:
        call_order.append(workspace_id or "<none>")

    monkeypatch.setattr(
        honcho_client, "delete_workspace_and_reset_client", _spy_delete
    )

    with patch.object(ChromaDBStore, "delete_company_docs", lambda self: None), \
         patch.object(ChromaDBStore, "delete_documents", lambda self, **kw: None):
        import asyncio
        asyncio.run(fixture_loader.reset_all_state(settings_stub))

    # Both workspaces got deleted: demo first, default second.
    assert call_order == [
        "openexec-fixture-demo-abcd1234",
        "openexec",
    ], f"unexpected delete order: {call_order}"
    # clear_active_workspace_id() ran exactly once, BETWEEN the two
    # deletes — i.e. after the first delete (len==1), before the second.
    assert cleared_at == [1], (
        f"clear_active_workspace_id must fire between the two deletes, "
        f"got snapshot positions {cleared_at}"
    )
