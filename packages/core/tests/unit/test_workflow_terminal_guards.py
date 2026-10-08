"""complete_run / fail_run must not clobber awaiting_human or terminal rows."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from openexecutive.workflows import persistence as wf


def test_fail_run_does_not_overwrite_awaiting_human(tmp_path: Path) -> None:
    db = tmp_path / "runs.db"
    wf.create_run("r1", "board_prep", "t", {}, db_path=db)
    # Simulate a successful checkpoint landing awaiting_human.
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE workflow_runs SET status = 'awaiting_human' WHERE run_id = ?",
            ("r1",),
        )
        conn.commit()
    assert wf.fail_run("r1", "late stream error", db_path=db) is False
    assert wf.get_run("r1", db_path=db)["status"] == "awaiting_human"


def test_complete_run_only_from_running(tmp_path: Path) -> None:
    db = tmp_path / "runs.db"
    wf.create_run("r2", "board_prep", "t", {}, db_path=db)
    assert wf.complete_run("r2", "# done", db_path=db) is True
    assert wf.get_run("r2", db_path=db)["status"] == "done"
    assert wf.complete_run("r2", "# again", db_path=db) is False
    assert wf.get_run("r2", db_path=db)["artifact"] == "# done"


def test_fail_run_from_running(tmp_path: Path) -> None:
    db = tmp_path / "runs.db"
    wf.create_run("r3", "board_prep", "t", {}, db_path=db)
    assert wf.fail_run("r3", "boom", db_path=db) is True
    assert wf.get_run("r3", db_path=db)["status"] == "error"


def test_fail_run_accepts_timed_out(tmp_path: Path) -> None:
    db = tmp_path / "runs.db"
    wf.create_run("r4", "board_prep", "t", {}, db_path=db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE workflow_runs SET status = 'timed_out' WHERE run_id = ?",
            ("r4",),
        )
        conn.commit()
    assert wf.fail_run("r4", "timeout fail", db_path=db) is True
    assert wf.get_run("r4", db_path=db)["status"] == "error"
