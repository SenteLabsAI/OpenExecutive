"""``reset_all_state`` must also wipe the run/audit history tables.

Before this change, the reset endpoint cleared chat/alerts/scheduled-actions
but left ``workflow_runs``, ``audit_log`` and ``eval_runs`` intact. The /jobs
"Runs" tab and /audit page then kept showing the previous fixture's executions
and the demo-page reset looked broken.

This test reaches into the ``_delete_all_rows`` helper that
``reset_all_state`` delegates to, seeds one row in each of the three tables
via their real persistence helpers, and confirms they're empty afterwards.
We invoke ``_delete_all_rows`` directly (not the async ``reset_all_state``)
to avoid pulling ChromaDB, Honcho, and an ``app.state`` shim into a unit
test — the assertion that matters is that the new table names appear in the
wipe tuple and that the deletion actually works against the live schemas.
"""
from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path

import pytest

from openexecutive.cli.fixture_loader import _delete_all_rows, reset_all_state


@pytest.fixture
def _episodic_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the episodic DB at tmp and initialize the base schema."""
    from openexecutive.memory import episodic

    db_path = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", db_path)
    episodic.initialize_db(db_path)
    return db_path


def test_reset_clears_workflow_runs_audit_log_and_eval_runs(
    _episodic_db: Path,
) -> None:
    """Seeding one row into each run/audit table and then running the same
    deletion ``reset_all_state`` uses must leave all three tables empty.
    """
    from openexecutive.audit.logger import AuditLogger
    from openexecutive.evals.persistence import create_eval_run
    from openexecutive.workflows.persistence import create_run

    # --- seed ---
    create_run(
        run_id="run-1",
        workflow_name="board_strategy",
        title="Board strategy run",
        inputs={"foo": "bar"},
        db_path=_episodic_db,
    )

    logger = AuditLogger(db_path=_episodic_db)
    logger.log(
        event_type="chat",
        actor="user",
        summary="seeded audit row",
    )

    create_eval_run(
        run_id="eval-1",
        kind="smoke",
        scenario_ids=["scenario-a"],
        db_path=_episodic_db,
    )

    # sanity: rows landed
    with sqlite3.connect(str(_episodic_db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM workflow_runs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM eval_runs").fetchone()[0] == 1

    # --- act: invoke the same helper reset_all_state uses ---
    cleared = _delete_all_rows(
        _episodic_db,
        ("workflow_runs", "audit_log", "eval_runs"),
    )

    # --- assert: counts returned + tables actually empty ---
    assert cleared == {"workflow_runs": 1, "audit_log": 1, "eval_runs": 1}
    with sqlite3.connect(str(_episodic_db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM workflow_runs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM eval_runs").fetchone()[0] == 0


def test_reset_all_state_wipe_list_includes_run_history_tables() -> None:
    """Guard against silently dropping a table from the wipe list.

    ``reset_all_state`` is a long async function with side effects (ChromaDB,
    Honcho, file I/O) that makes it expensive to invoke from a unit test.
    To make sure a future refactor doesn't quietly drop one of the run/audit
    table names from the tuple passed to ``_delete_all_rows``, assert their
    presence in the function's source.
    """
    src = inspect.getsource(reset_all_state)
    for table in ("workflow_runs", "audit_log", "eval_runs"):
        assert f'"{table}"' in src, (
            f"reset_all_state no longer mentions {table!r} — the demo-page "
            "reset will stop clearing it and previously-run jobs/audit/eval "
            "rows will reappear after reset."
        )


def test_reset_all_state_wipes_the_decision_ledger_and_act_as_me() -> None:
    """A reset box has proposed nothing, and keeps nobody's Act as me state.
    Act as me's tables come from its own list, so a new one is wiped too."""
    src = inspect.getsource(reset_all_state)
    for table in ("decision_instances", "decision_class_state"):
        assert f'"{table}"' in src
    assert "*DELEGATION_TABLES" in src

