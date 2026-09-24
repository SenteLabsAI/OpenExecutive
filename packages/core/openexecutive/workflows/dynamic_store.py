"""SQLite persistence for user-created ("dynamic") workflow definitions.

Mirrors ``workflows/persistence.py`` conventions: same DB file as
``memory/episodic.py`` (one backup covers everything), lazy idempotent table
creation, and a ``db_path`` override on every function so tests can point at a
temp DB.

The full definition is stored as JSON in the ``definition`` column; ``name``
and ``is_active`` are also denormalised into columns so the registry
fall-through (``workflows/__init__.py``) can list active definitions without
deserialising every row's body.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from openexecutive.memory.episodic import DB_PATH, _get_conn
from openexecutive.workflows.dynamic_models import DynamicWorkflowDef


def _resolve(db_path: Path | None) -> Path:
    """Dynamic DB_PATH resolution — lets tests monkeypatch the module DB_PATH."""
    return db_path if db_path is not None else DB_PATH


def initialize_dynamic_workflows_db(db_path: Path | None = None) -> None:
    """Create the dynamic_workflows table if it doesn't exist. Idempotent."""
    with _get_conn(_resolve(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dynamic_workflows (
                name        TEXT PRIMARY KEY,
                definition  TEXT NOT NULL,
                is_active   INTEGER NOT NULL DEFAULT 1,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
            """
        )


def upsert_definition(
    defn: DynamicWorkflowDef, db_path: Path | None = None
) -> DynamicWorkflowDef:
    """Insert or replace a definition by name. Stamps created_at/updated_at.

    Returns the stored definition (with timestamps applied). On update, the
    original created_at is preserved.
    """
    initialize_dynamic_workflows_db(db_path)
    now = datetime.now(UTC).isoformat()
    existing = get_definition(defn.name, db_path=db_path)
    created_at = existing.created_at if existing and existing.created_at else now
    defn = defn.model_copy(update={"created_at": created_at, "updated_at": now})
    with _get_conn(_resolve(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO dynamic_workflows (name, definition, is_active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                definition = excluded.definition,
                is_active  = excluded.is_active,
                updated_at = excluded.updated_at
            """,
            (
                defn.name,
                defn.model_dump_json(),
                1 if defn.is_active else 0,
                created_at,
                now,
            ),
        )
    return defn


def get_definition(
    name: str, db_path: Path | None = None
) -> DynamicWorkflowDef | None:
    if not _resolve(db_path).exists():
        return None
    with _get_conn(_resolve(db_path)) as conn:
        if not _table_exists(conn):
            return None
        row = conn.execute(
            "SELECT definition FROM dynamic_workflows WHERE name = ?", (name,)
        ).fetchone()
    if row is None:
        return None
    return DynamicWorkflowDef.model_validate_json(row[0])


def list_definitions(
    active_only: bool = True, db_path: Path | None = None
) -> list[DynamicWorkflowDef]:
    if not _resolve(db_path).exists():
        return []
    where = "WHERE is_active = 1 " if active_only else ""
    with _get_conn(_resolve(db_path)) as conn:
        if not _table_exists(conn):
            return []
        rows = conn.execute(
            f"SELECT definition FROM dynamic_workflows {where}ORDER BY name"
        ).fetchall()
    return [DynamicWorkflowDef.model_validate_json(r[0]) for r in rows]


def delete_definition(name: str, db_path: Path | None = None) -> bool:
    if not _resolve(db_path).exists():
        return False
    with _get_conn(_resolve(db_path)) as conn:
        if not _table_exists(conn):
            return False
        cur = conn.execute("DELETE FROM dynamic_workflows WHERE name = ?", (name,))
        return cur.rowcount > 0


def set_active(name: str, active: bool, db_path: Path | None = None) -> bool:
    """Flip the is_active flag (keeping is_active inside the JSON body in sync)."""
    defn = get_definition(name, db_path=db_path)
    if defn is None:
        return False
    upsert_definition(defn.model_copy(update={"is_active": active}), db_path=db_path)
    return True


def _table_exists(conn: object) -> bool:
    """True if the dynamic_workflows table is present in this connection's DB."""
    import sqlite3

    assert isinstance(conn, sqlite3.Connection)
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dynamic_workflows'"
    ).fetchone()
    return row is not None


def activate_if_unchanged(
    expected: DynamicWorkflowDef, db_path: Path | None = None
) -> bool | None:
    """Switch ``expected`` on only if its stored row is still that revision.

    One conditional UPDATE keyed on ``updated_at`` (every write stamps a new
    one), so a save from any process that lands after the caller read and
    checked ``expected`` makes this a no-op instead of switching on a revision
    nobody reviewed. Returns True if switched on, False if the row changed,
    None if it no longer exists.
    """
    now = datetime.now(UTC).isoformat()
    stored = expected.model_copy(update={"is_active": True, "updated_at": now})
    with _get_conn(_resolve(db_path)) as conn:
        cur = conn.execute(
            """
            UPDATE dynamic_workflows
               SET definition = ?, is_active = 1, updated_at = ?
             WHERE name = ? AND updated_at = ?
            """,
            (stored.model_dump_json(), now, expected.name, expected.updated_at),
        )
        if cur.rowcount:
            return True
        exists = conn.execute(
            "SELECT 1 FROM dynamic_workflows WHERE name = ?", (expected.name,)
        ).fetchone()
    return False if exists else None
