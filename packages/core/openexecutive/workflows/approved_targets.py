"""Targets a workflow's tool steps have been approved to write to.

Approving a workflow approves its *tools*; which sheet, doc, recipient or URL
each call hits is chosen at run time. The first time a workflow writes to a
target it has never used, that write is held and its owner is asked (see
``action_step`` / ``dynamic``). A yes lands here, so later runs write to the
same target without asking again.

Approval is by **value** — a spreadsheet id, an address, a URL — not by tool:
the tools themselves were approved when the workflow was switched on.

Same DB file and conventions as ``dynamic_store``: lazy idempotent table
creation, and a ``db_path`` override on every function for tests.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openexecutive.memory.episodic import DB_PATH, _get_conn


def _resolve(db_path: Path | None) -> Path:
    """Dynamic DB_PATH resolution — lets tests monkeypatch the module DB_PATH."""
    return db_path if db_path is not None else DB_PATH


def normalize_value(value: str) -> str:
    """Canonical form a target is stored and compared in.

    Addresses are case-insensitive in practice, so an approved
    ``Ops@Example.com`` also covers ``ops@example.com``; everything else
    (ids, paths, URLs) is compared exactly.
    """
    text = str(value).strip()
    if "@" in text and " " not in text and "/" not in text:
        return text.lower()
    return text


def initialize_approved_targets_db(db_path: Path | None = None) -> None:
    with _get_conn(_resolve(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS workflow_approved_targets (
                workflow_name TEXT NOT NULL,
                value         TEXT NOT NULL,
                key           TEXT NOT NULL,
                approved_at   TEXT NOT NULL,
                run_id        TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (workflow_name, value)
            )
            """
        )


def _table_exists(conn: Any) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_approved_targets'"
        ).fetchone()
        is not None
    )


def approved_values(workflow_name: str, db_path: Path | None = None) -> set[str]:
    if not _resolve(db_path).exists():
        return set()
    with _get_conn(_resolve(db_path)) as conn:
        if not _table_exists(conn):
            return set()
        rows = conn.execute(
            "SELECT value FROM workflow_approved_targets WHERE workflow_name = ?",
            (workflow_name,),
        ).fetchall()
    return {str(r[0]) for r in rows}


def remember(
    workflow_name: str,
    targets: Iterable[tuple[str, str]],
    *,
    run_id: str = "",
    db_path: Path | None = None,
) -> None:
    """Record ``(key, value)`` targets as approved. Re-approving is a no-op."""
    rows = [
        (workflow_name, normalize_value(value), str(key), run_id)
        for key, value in targets
        if normalize_value(value)
    ]
    if not rows:
        return
    initialize_approved_targets_db(db_path)
    now = datetime.now(UTC).isoformat()
    with _get_conn(_resolve(db_path)) as conn:
        conn.executemany(
            """
            INSERT INTO workflow_approved_targets (workflow_name, value, key, approved_at, run_id)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(workflow_name, value) DO NOTHING
            """,
            [(name, value, key, now, run) for name, value, key, run in rows],
        )


def list_targets(workflow_name: str, db_path: Path | None = None) -> list[dict[str, str]]:
    if not _resolve(db_path).exists():
        return []
    with _get_conn(_resolve(db_path)) as conn:
        if not _table_exists(conn):
            return []
        rows = conn.execute(
            "SELECT value, key, approved_at, run_id FROM workflow_approved_targets "
            "WHERE workflow_name = ? ORDER BY approved_at, value",
            (workflow_name,),
        ).fetchall()
    return [
        {"value": r[0], "key": r[1], "approved_at": r[2], "run_id": r[3]} for r in rows
    ]


def forget(workflow_name: str, value: str, db_path: Path | None = None) -> bool:
    if not _resolve(db_path).exists():
        return False
    with _get_conn(_resolve(db_path)) as conn:
        if not _table_exists(conn):
            return False
        cur = conn.execute(
            "DELETE FROM workflow_approved_targets WHERE workflow_name = ? AND value = ?",
            (workflow_name, normalize_value(value)),
        )
        return cur.rowcount > 0


def forget_all(workflow_name: str, db_path: Path | None = None) -> None:
    """Drop every approval for ``workflow_name`` — a deleted workflow's approvals
    must not carry over to a new one saved under the same name."""
    if not _resolve(db_path).exists():
        return
    with _get_conn(_resolve(db_path)) as conn:
        if _table_exists(conn):
            conn.execute(
                "DELETE FROM workflow_approved_targets WHERE workflow_name = ?", (workflow_name,)
            )


def approver_for(workflow_name: str) -> int | None:
    """Who is asked to approve a new target: the workflow's creator.

    Falls back to the principal when the creator is unknown (workflows saved
    before owners were recorded, or created without a resolvable caller) or
    no longer an active roster member. None only when neither exists.
    """
    from openexecutive.people.store import find_principal_person, get_person
    from openexecutive.workflows.dynamic_store import get_owner

    owner_id = get_owner(workflow_name)
    if owner_id is not None:
        owner = get_person(owner_id)
        # A creator since moved to the principal's contacts is no longer on
        # the team: they are never asked to approve anything.
        if (
            owner is not None
            and not getattr(owner, "archived", False)
            and getattr(owner, "kind", "team") == "team"
        ):
            return owner_id
    principal = find_principal_person()
    return principal.id if principal is not None and principal.id is not None else None
