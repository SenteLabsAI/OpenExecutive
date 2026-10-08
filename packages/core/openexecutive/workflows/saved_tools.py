"""Saved tools: step scripts the Executive kept to run again by name.

When a ``run_script`` succeeds, the model may save it (``save_as``) as a named
tool; later ``run_script(tool=…, inputs=…)`` runs the saved script instead of
new code. Saving is approved automatically — a saved tool can never do more
than the context that runs it: its script calls tools only through that
context's own per-call path (a workflow step's allowlist, budget, target
check and audit; chat's gateway gates), and in a workflow step it runs only
when the step already allows every tool it used (``tools``).

Every save of changed content is a new version; ``current_version`` is the
one that runs in chat, and an owner can switch it back (``rollback``) or turn
the tool off (``set_enabled``). Every run is recorded (``saved_tool_runs``).

A tool is one of two kinds. A ``script`` (the default) is a ``run_script``
step script that calls the Executive's own tools. A ``python`` tool is a
``run_python_job`` job (workflows/python_job.py): Python with libraries on
files, run again with ``run_python_job(tool=…, inputs=…)``. Python jobs run
only on the principal's own chat turns, never in workflows, so a Python tool
is never turned on for workflows.

Workflows run only a version the owner turned on for them
(``workflow_version``, set by ``set_workflows``): a tool kept on a turn that
read outside content could otherwise carry injected code into an unattended
run. A newer save doesn't change it; the owner turns the new one on.
Same DB file and conventions as ``workflows/dynamic_store.py``.
"""
from __future__ import annotations

import json
import re
import sqlite3
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
KINDS = ("script", "python")
_MAX_RUNS_KEPT_PER_TOOL = 200
_MAX_VERSIONS_KEPT_PER_TOOL = 20


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
    # The version workflows may run (the owner turned it on), else None.
    workflow_version: int | None = None
    kind: str = "script"

    def summary(self) -> dict[str, Any]:
        """What a model or the Tools page needs to pick it — never the script."""
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "uses_tools": self.tools,
            "in_workflows": self.workflow_version is not None,
            "kind": self.kind,
            "run_with": "run_python_job" if self.kind == "python" else "run_script",
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
        columns = {r[1] for r in conn.execute("PRAGMA table_info(saved_tools)").fetchall()}
        for column, ddl in (
            ("workflow_version", "ALTER TABLE saved_tools ADD COLUMN workflow_version INTEGER"),
            ("kind", "ALTER TABLE saved_tools ADD COLUMN kind TEXT NOT NULL DEFAULT 'script'"),
        ):
            if column in columns:
                continue
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError as exc:
                # Another process added it between the check and the ALTER.
                if "duplicate column" not in str(exc):
                    raise


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
        workflow_version=row[9],
        kind=row[10] or "script",
    )


_SELECT = """
    SELECT t.name, v.description, t.enabled, v.version, v.script, v.tools,
           v.origin, t.created_at, t.updated_at, t.workflow_version, t.kind
    FROM saved_tools t
    JOIN saved_tool_versions v ON v.name = t.name AND v.version = t.current_version
"""

# The same, for the version workflows may run. Python tools never run there.
_SELECT_WORKFLOW = _SELECT.replace(
    "v.version = t.current_version", "v.version = t.workflow_version AND t.kind = 'script'"
)


def get(name: str, db_path: Path | None = None) -> SavedTool | None:
    initialize(db_path)
    with _get_conn(_resolve(db_path)) as conn:
        row = conn.execute(_SELECT + " WHERE t.name = ?", (name,)).fetchone()
    return _row_to_tool(row) if row else None


def get_for_workflows(name: str, db_path: Path | None = None) -> SavedTool | None:
    """The version of ``name`` workflows may run, or None when the owner
    hasn't turned it on for workflows."""
    initialize(db_path)
    with _get_conn(_resolve(db_path)) as conn:
        row = conn.execute(_SELECT_WORKFLOW + " WHERE t.name = ?", (name,)).fetchone()
    return _row_to_tool(row) if row else None


def list_for_workflows(db_path: Path | None = None) -> list[SavedTool]:
    """Enabled tools with a version turned on for workflows, at that version."""
    initialize(db_path)
    with _get_conn(_resolve(db_path)) as conn:
        rows = conn.execute(
            _SELECT_WORKFLOW + " WHERE t.enabled = 1 ORDER BY t.name"
        ).fetchall()
    return [_row_to_tool(r) for r in rows]


def set_workflows(name: str, version: int | None, db_path: Path | None = None) -> SavedTool:
    """Turn ``version`` on for workflows — the exact version the owner looked
    at, never "whatever is current now" — or workflows off (None)."""
    initialize(db_path)
    if version is not None and (tool := get(name, db_path)) is not None and tool.kind != "script":
        raise SavedToolError("Python tools run only in chat, never in workflows")
    with _get_conn(_resolve(db_path)) as conn:
        if version is None:
            cur = conn.execute(
                "UPDATE saved_tools SET workflow_version = NULL, updated_at = ? WHERE name = ?",
                (_now(), name),
            )
            if cur.rowcount == 0:
                raise SavedToolError("no saved tool by that name")
        else:
            cur = conn.execute(
                "UPDATE saved_tools SET workflow_version = ?, updated_at = ? WHERE name = ?"
                " AND EXISTS (SELECT 1 FROM saved_tool_versions WHERE name = ? AND version = ?)",
                (version, _now(), name, name, version),
            )
            if cur.rowcount == 0:
                raise SavedToolError("no such version of that saved tool")
    tool = get(name, db_path)
    assert tool is not None
    return tool


def list_tools(*, enabled_only: bool = False, db_path: Path | None = None) -> list[SavedTool]:
    initialize(db_path)
    sql = _SELECT + (" WHERE t.enabled = 1" if enabled_only else "") + " ORDER BY t.name"
    with _get_conn(_resolve(db_path)) as conn:
        return [_row_to_tool(r) for r in conn.execute(sql).fetchall()]


def validate_save(
    name: str, description: str, script: str, tools: list[str], kind: str = "script"
) -> None:
    """Raise SavedToolError if this can't be saved."""
    from openexecutive.workflows.python_job import MAX_CODE_CHARS
    from openexecutive.workflows.step_script import MAX_SCRIPT_CHARS

    if kind not in KINDS:
        raise SavedToolError(f"a saved tool is one of {', '.join(KINDS)}")
    if not NAME_RE.fullmatch(name):
        raise SavedToolError(
            "save_as must be snake_case: 3-49 lowercase letters, digits or underscores, "
            "starting with a letter"
        )
    if not description.strip():
        raise SavedToolError("a saved tool needs a one-sentence description")
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise SavedToolError(f"the description is longer than {MAX_DESCRIPTION_CHARS} characters")
    if not script.strip() or len(script) > (MAX_CODE_CHARS if kind == "python" else MAX_SCRIPT_CHARS):
        raise SavedToolError("the script is empty or too long to save")
    if kind == "python" and tools:
        raise SavedToolError("a Python tool calls no other tools")
    if len(tools) > MAX_TOOLS_PER_SAVED_TOOL:
        raise SavedToolError("the script uses too many different tools to save")


def save(
    name: str,
    description: str,
    script: str,
    tools: list[str],
    *,
    origin: str,
    kind: str = "script",
    db_path: Path | None = None,
) -> SavedTool:
    """Save a new tool, or a new version of an existing one (of the same kind).

    A save whose description, script and tools match the current version
    changes nothing. A new version becomes current; a turned-off tool stays
    off (only the owner turns it back on).
    """
    tools = sorted(set(tools))
    # One line of printable text: it is shown to models in tool descriptions.
    description = " ".join(
        "".join(" " if unicodedata.category(ch) in ("Cc", "Cf") else ch for ch in description).split()
    )
    validate_save(name, description, script, tools, kind)
    initialize(db_path)
    now = _now()
    with _get_conn(_resolve(db_path)) as conn:
        # Hold the write lock from the read, so concurrent saves (another
        # process on the same file) can't pick the same version number.
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(_SELECT + " WHERE t.name = ?", (name,)).fetchone()
        previous = 0
        if current is None:
            count = conn.execute("SELECT COUNT(*) FROM saved_tools").fetchone()[0]
            if count >= MAX_SAVED_TOOLS:
                raise SavedToolError(
                    f"there are already {MAX_SAVED_TOOLS} saved tools; turn some off or reuse one"
                )
            version = 1
            conn.execute(
                "INSERT INTO saved_tools (name, description, enabled, current_version, created_at,"
                " updated_at, kind) VALUES (?, ?, 1, 1, ?, ?, ?)",
                (name, description, now, now, kind),
            )
        else:
            tool = _row_to_tool(current)
            if tool.kind != kind:
                raise SavedToolError(
                    f"there is already a {'Python' if tool.kind == 'python' else 'script'} tool "
                    "by that name; pick another name"
                )
            if (tool.description, tool.script, tool.tools) == (description, script, tools):
                return tool
            previous = tool.version
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
        # Keep the newest versions, and always the one that ran until now
        # (it may be an older one the owner rolled back to and trusts) and
        # the one turned on for workflows.
        conn.execute(
            "DELETE FROM saved_tool_versions WHERE name = ? AND version NOT IN ("
            " SELECT version FROM saved_tool_versions WHERE name = ? ORDER BY version DESC LIMIT ?)"
            " AND version != ? AND version IS NOT ("
            " SELECT workflow_version FROM saved_tools WHERE name = ?)",
            (name, name, _MAX_VERSIONS_KEPT_PER_TOOL, previous, name),
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
    """Turning a tool off also turns it off for workflows: turning it back on
    must not re-arm unattended runs without the owner's workflow switch."""
    initialize(db_path)
    with _get_conn(_resolve(db_path)) as conn:
        cur = conn.execute(
            "UPDATE saved_tools SET enabled = ?, updated_at = ?"
            + ("" if enabled else ", workflow_version = NULL")
            + " WHERE name = ?",
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
