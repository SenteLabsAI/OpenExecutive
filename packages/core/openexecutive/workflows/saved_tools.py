"""Saved tools: step scripts the Executive kept to run again by name.

When a ``run_script`` succeeds, the model may save it (``save_as``) as a named
tool; later ``run_script(tool=…, inputs=…)`` runs the saved script instead of
new code. Saving is approved automatically — a saved tool can never do more
than the context that runs it: its script calls tools only through that
context's own per-call path (a workflow step's allowlist, budget, target
check and audit; chat's gateway gates), and in a workflow step it runs only
when the step already allows every tool it used (``tools``).

Every save of changed content is a new version; ``current_version`` is the
one that runs, and an owner can switch it back (``rollback``) or turn the
tool off (``set_enabled``). Every run is recorded (``saved_tool_runs``).
Same DB file and conventions as ``workflows/dynamic_store.py``.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openexecutive.memory.episodic import DB_PATH, _get_conn

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,48}$")
MAX_DESCRIPTION_CHARS = 500
MAX_TOOLS_PER_SAVED_TOOL = 32
MAX_SAVED_TOOLS = 200
_MAX_RUNS_KEPT_PER_TOOL = 200


@dataclass(frozen=True)
class SavedTool:
    name: str
    description: str
    enabled: bool
    version: int
    script: str
    tools: list[str]
    origin: str
    created_at: str
    updated_at: str

    def summary(self) -> dict[str, Any]:
        """What a model or the Tools page needs to pick it — never the script."""
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "uses_tools": self.tools,
        }


class SavedToolError(ValueError):
    """A save or change refused, with a message safe to show."""


def _resolve(db_path: Path | None) -> Path:
    return db_path if db_path is not None else DB_PATH


def initialize(db_path: Path | None = None) -> None:
    with _get_conn(_resolve(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS saved_tools (
                name            TEXT PRIMARY KEY,
                description     TEXT NOT NULL,
                enabled         INTEGER NOT NULL DEFAULT 1,
                current_version INTEGER NOT NULL,
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS saved_tool_versions (
                name        TEXT NOT NULL,
                version     INTEGER NOT NULL,
                description TEXT NOT NULL,
                script      TEXT NOT NULL,
                tools       TEXT NOT NULL,
                origin      TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                PRIMARY KEY (name, version)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS saved_tool_runs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                version     INTEGER NOT NULL,
                ok          INTEGER NOT NULL,
                calls       INTEGER NOT NULL,
                duration_ms INTEGER NOT NULL,
                origin      TEXT NOT NULL,
                at          TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_saved_tool_runs_name ON saved_tool_runs(name, id)"
        )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _row_to_tool(row: Any) -> SavedTool:
    return SavedTool(
        name=row[0],
        description=row[1],
        enabled=bool(row[2]),
        version=int(row[3]),
        script=row[4],
        tools=list(json.loads(row[5])),
        origin=row[6],
        created_at=row[7],
        updated_at=row[8],
    )


_SELECT = """
    SELECT t.name, v.description, t.enabled, t.current_version, v.script, v.tools,
           v.origin, t.created_at, t.updated_at
    FROM saved_tools t
    JOIN saved_tool_versions v ON v.name = t.name AND v.version = t.current_version
"""


def get(name: str, db_path: Path | None = None) -> SavedTool | None:
    initialize(db_path)
    with _get_conn(_resolve(db_path)) as conn:
        row = conn.execute(_SELECT + " WHERE t.name = ?", (name,)).fetchone()
    return _row_to_tool(row) if row else None


def list_tools(*, enabled_only: bool = False, db_path: Path | None = None) -> list[SavedTool]:
    initialize(db_path)
    sql = _SELECT + (" WHERE t.enabled = 1" if enabled_only else "") + " ORDER BY t.name"
    with _get_conn(_resolve(db_path)) as conn:
        return [_row_to_tool(r) for r in conn.execute(sql).fetchall()]


def validate_save(name: str, description: str, script: str, tools: list[str]) -> None:
    """Raise SavedToolError if this can't be saved."""
    from openexecutive.workflows.step_script import MAX_SCRIPT_CHARS

    if not NAME_RE.fullmatch(name):
        raise SavedToolError(
            "save_as must be snake_case: 3-49 lowercase letters, digits or underscores, "
            "starting with a letter"
        )
    if not description.strip():
        raise SavedToolError("a saved tool needs a one-sentence description")
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise SavedToolError(f"the description is longer than {MAX_DESCRIPTION_CHARS} characters")
    if not script.strip() or len(script) > MAX_SCRIPT_CHARS:
        raise SavedToolError("the script is empty or too long to save")
    if len(tools) > MAX_TOOLS_PER_SAVED_TOOL:
        raise SavedToolError("the script uses too many different tools to save")


def save(
    name: str,
    description: str,
    script: str,
    tools: list[str],
    *,
    origin: str,
    db_path: Path | None = None,
) -> SavedTool:
    """Save a new tool, or a new version of an existing one.

    A save whose description, script and tools match the current version
    changes nothing. A new version becomes current; a turned-off tool stays
    off (only the owner turns it back on).
    """
    tools = sorted(set(tools))
    # One line of printable text: it is shown to models in tool descriptions.
    description = " ".join(
        "".join(" " if unicodedata.category(ch) in ("Cc", "Cf") else ch for ch in description).split()
    )
    validate_save(name, description, script, tools)
    initialize(db_path)
    now = _now()
    with _get_conn(_resolve(db_path)) as conn:
        # Hold the write lock from the read, so concurrent saves (another
        # process on the same file) can't pick the same version number.
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(_SELECT + " WHERE t.name = ?", (name,)).fetchone()
        if current is None:
            count = conn.execute("SELECT COUNT(*) FROM saved_tools").fetchone()[0]
            if count >= MAX_SAVED_TOOLS:
                raise SavedToolError(
                    f"there are already {MAX_SAVED_TOOLS} saved tools; turn some off or reuse one"
                )
            version = 1
            conn.execute(
                "INSERT INTO saved_tools (name, description, enabled, current_version, created_at, updated_at)"
                " VALUES (?, ?, 1, 1, ?, ?)",
                (name, description, now, now),
            )
        else:
            tool = _row_to_tool(current)
            if (tool.description, tool.script, tool.tools) == (description, script, tools):
                return tool
            version = int(
                conn.execute(
                    "SELECT MAX(version) FROM saved_tool_versions WHERE name = ?", (name,)
                ).fetchone()[0]
            ) + 1
            conn.execute(
                "UPDATE saved_tools SET description = ?, current_version = ?, updated_at = ? WHERE name = ?",
                (description, version, now, name),
            )
        conn.execute(
            "INSERT INTO saved_tool_versions (name, version, description, script, tools, origin, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, version, description, script, json.dumps(tools), origin[:200], now),
        )
    saved = get(name, db_path)
    assert saved is not None
    return saved


def versions(name: str, db_path: Path | None = None) -> list[dict[str, Any]]:
    initialize(db_path)
    with _get_conn(_resolve(db_path)) as conn:
        rows = conn.execute(
            "SELECT version, description, script, tools, origin, created_at"
            " FROM saved_tool_versions WHERE name = ? ORDER BY version DESC",
            (name,),
        ).fetchall()
    return [
        {
            "version": r[0], "description": r[1], "script": r[2], "uses_tools": json.loads(r[3]),
            "origin": r[4], "created_at": r[5],
        }
        for r in rows
    ]


def set_enabled(name: str, enabled: bool, db_path: Path | None = None) -> SavedTool:
    initialize(db_path)
    with _get_conn(_resolve(db_path)) as conn:
        cur = conn.execute(
            "UPDATE saved_tools SET enabled = ?, updated_at = ? WHERE name = ?",
            (1 if enabled else 0, _now(), name),
        )
        if cur.rowcount == 0:
            raise SavedToolError("no saved tool by that name")
    tool = get(name, db_path)
    assert tool is not None
    return tool


def rollback(name: str, version: int, db_path: Path | None = None) -> SavedTool:
    """Make an earlier (or any existing) version the one that runs."""
    initialize(db_path)
    with _get_conn(_resolve(db_path)) as conn:
        row = conn.execute(
            "SELECT description FROM saved_tool_versions WHERE name = ? AND version = ?",
            (name, version),
        ).fetchone()
        if row is None:
            raise SavedToolError("no such version of that saved tool")
        conn.execute(
            "UPDATE saved_tools SET current_version = ?, description = ?, updated_at = ? WHERE name = ?",
            (version, row[0], _now(), name),
        )
    tool = get(name, db_path)
    assert tool is not None
    return tool


def record_run(
    name: str,
    version: int,
    *,
    ok: bool,
    calls: int,
    duration_ms: int,
    origin: str,
    db_path: Path | None = None,
) -> None:
    initialize(db_path)
    with _get_conn(_resolve(db_path)) as conn:
        conn.execute(
            "INSERT INTO saved_tool_runs (name, version, ok, calls, duration_ms, origin, at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, version, 1 if ok else 0, calls, duration_ms, origin[:200], _now()),
        )
        # Keep the newest runs per tool; the audit log keeps the rest.
        conn.execute(
            "DELETE FROM saved_tool_runs WHERE name = ? AND id NOT IN ("
            " SELECT id FROM saved_tool_runs WHERE name = ? ORDER BY id DESC LIMIT ?)",
            (name, name, _MAX_RUNS_KEPT_PER_TOOL),
        )


def runs(name: str, limit: int = 20, db_path: Path | None = None) -> list[dict[str, Any]]:
    initialize(db_path)
    with _get_conn(_resolve(db_path)) as conn:
        rows = conn.execute(
            "SELECT version, ok, calls, duration_ms, origin, at FROM saved_tool_runs"
            " WHERE name = ? ORDER BY id DESC LIMIT ?",
            (name, max(1, min(limit, 100))),
        ).fetchall()
    return [
        {"version": r[0], "ok": bool(r[1]), "calls": r[2], "duration_ms": r[3], "origin": r[4], "at": r[5]}
        for r in rows
    ]
