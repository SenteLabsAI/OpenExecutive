"""Runtime overrides for specialist agent configuration.

Stores per-agent overrides for system prompt, additional instructions,
model, deep-reasoning flag, and role description. Persisted in the shared episodic_memory.db so it
survives restarts. Defaults flow through unchanged when no override exists.

History is append-only — every PATCH (or DELETE/reset) writes the prior
state to ``agent_override_history`` so prior versions can be restored.
"""
from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from openexecutive.memory.episodic import DB_PATH, _get_conn

# Synthetic agent_id for the Executive orchestrator. Shared across the
# API route, cache_manager, and orchestrator so the magic string lives in
# exactly one place.
EXECUTIVE_AGENT_ID = "executive"


class AgentOverride(BaseModel):
    agent_id: str
    prompt: str | None = None
    model: str | None = None
    use_deep_reasoning: bool | None = None
    role: str | None = None
    voice_persona_slug: str | None = None
    # Per-specialist research-mode focus tail (the domain-scope block appended
    # in research runs). When None, the code default in
    # monitoring.research.prompts._RESEARCH_FOCUS applies. Does NOT replace
    # the shared research contract, which stays code-only.
    research_focus: str | None = None
    # Text appended after the agent's system prompt (built-in or replaced),
    # so an admin can add guidance without freezing the agent on today's
    # built-in prompt.
    instructions: str | None = None
    updated_at: str | None = None


class AgentHistoryEntry(BaseModel):
    id: int
    agent_id: str
    prompt: str | None = None
    model: str | None = None
    use_deep_reasoning: bool | None = None
    role: str | None = None
    voice_persona_slug: str | None = None
    research_focus: str | None = None
    instructions: str | None = None
    created_at: str


def append_instructions(prompt: str, instructions: str | None) -> str:
    """Return ``prompt`` with the admin's additional instructions appended.

    Blank or missing instructions leave the prompt byte-identical, so an
    agent without them keeps its existing prompt cache.
    """
    if instructions is None or not instructions.strip():
        return prompt
    return (
        prompt
        + "\n\n<additional_instructions>\n"
        + instructions.strip()
        + "\n</additional_instructions>"
    )


_cache_lock = threading.Lock()
_cache: dict[str, AgentOverride] | None = None
_cache_db_path: Path | None = None


def _add_column_if_missing(conn: Any, table: str, column: str, col_type: str = "TEXT") -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


def _resolve_db(db_path: Path | None) -> Path:
    return db_path if db_path is not None else DB_PATH


def initialize_overrides_db(db_path: Path | None = None) -> None:
    db_path = _resolve_db(db_path)
    with _get_conn(db_path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS agent_overrides (
                agent_id TEXT PRIMARY KEY,
                prompt TEXT,
                model TEXT,
                use_deep_reasoning INTEGER,
                role TEXT,
                voice_persona_slug TEXT,
                research_focus TEXT,
                instructions TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agent_override_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                prompt TEXT,
                model TEXT,
                use_deep_reasoning INTEGER,
                role TEXT,
                voice_persona_slug TEXT,
                research_focus TEXT,
                instructions TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_override_history_agent
                ON agent_override_history(agent_id, id DESC);
        """)
        # Migrate existing DBs that predate voice_persona_slug / research_focus /
        # instructions.
        _add_column_if_missing(conn, "agent_overrides", "voice_persona_slug")
        _add_column_if_missing(conn, "agent_override_history", "voice_persona_slug")
        _add_column_if_missing(conn, "agent_overrides", "research_focus")
        _add_column_if_missing(conn, "agent_override_history", "research_focus")
        _add_column_if_missing(conn, "agent_overrides", "instructions")
        _add_column_if_missing(conn, "agent_override_history", "instructions")


def _row_to_override(row: Any) -> AgentOverride:
    deep = row["use_deep_reasoning"]
    return AgentOverride(
        agent_id=row["agent_id"],
        prompt=row["prompt"],
        model=row["model"],
        use_deep_reasoning=bool(deep) if deep is not None else None,
        role=row["role"],
        voice_persona_slug=row["voice_persona_slug"],
        research_focus=row["research_focus"],
        instructions=row["instructions"],
        updated_at=row["updated_at"],
    )


def _load_cache(db_path: Path) -> dict[str, AgentOverride]:
    global _cache, _cache_db_path
    if not db_path.exists():
        _cache = {}
        _cache_db_path = db_path
        return _cache
    initialize_overrides_db(db_path)
    out: dict[str, AgentOverride] = {}
    with _get_conn(db_path) as conn:
        rows = conn.execute("SELECT * FROM agent_overrides").fetchall()
    for row in rows:
        out[row["agent_id"]] = _row_to_override(row)
    _cache = out
    _cache_db_path = db_path
    return out


def _ensure_cache(db_path: Path) -> dict[str, AgentOverride]:
    global _cache, _cache_db_path
    with _cache_lock:
        if _cache is None or _cache_db_path != db_path:
            return _load_cache(db_path)
        return _cache


def invalidate_cache() -> None:
    """Force the cache to reload on next read. Useful for tests."""
    global _cache, _cache_db_path
    with _cache_lock:
        _cache = None
        _cache_db_path = None


def get_override(agent_id: str, db_path: Path | None = None) -> AgentOverride | None:
    return _ensure_cache(_resolve_db(db_path)).get(agent_id)


def list_overrides(db_path: Path | None = None) -> dict[str, AgentOverride]:
    return dict(_ensure_cache(_resolve_db(db_path)))


def set_override(
    agent_id: str,
    *,
    prompt: str | None = None,
    model: str | None = None,
    use_deep_reasoning: bool | None = None,
    role: str | None = None,
    voice_persona_slug: str | None = None,
    research_focus: str | None = None,
    instructions: str | None = None,
    prompt_set: bool = False,
    model_set: bool = False,
    deep_set: bool = False,
    role_set: bool = False,
    voice_persona_slug_set: bool = False,
    research_focus_set: bool = False,
    instructions_set: bool = False,
    db_path: Path | None = None,
) -> AgentOverride:
    """Update one or more override fields for an agent.

    The ``*_set`` booleans indicate which fields the caller actually wants
    to update — distinguishing "leave unchanged" from "set to NULL/clear".
    All flags False is a no-op that still bumps ``updated_at``.

    Writes prior state to history before mutating.
    """
    db_path = _resolve_db(db_path)
    initialize_overrides_db(db_path)
    now = datetime.now(UTC).isoformat()
    with _get_conn(db_path) as conn:
        existing_row = conn.execute(
            "SELECT * FROM agent_overrides WHERE agent_id = ?", (agent_id,)
        ).fetchone()

        if existing_row is not None:
            # Snapshot prior state into history before mutating.
            conn.execute(
                "INSERT INTO agent_override_history "
                "(agent_id, prompt, model, use_deep_reasoning, role, voice_persona_slug, research_focus, "
                "instructions, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    agent_id,
                    existing_row["prompt"],
                    existing_row["model"],
                    existing_row["use_deep_reasoning"],
                    existing_row["role"],
                    existing_row["voice_persona_slug"],
                    existing_row["research_focus"],
                    existing_row["instructions"],
                    existing_row["updated_at"],
                ),
            )
            new_prompt = prompt if prompt_set else existing_row["prompt"]
            new_model = model if model_set else existing_row["model"]
            new_deep = (
                (None if use_deep_reasoning is None else int(use_deep_reasoning))
                if deep_set
                else existing_row["use_deep_reasoning"]
            )
            new_role = role if role_set else existing_row["role"]
            new_vp = voice_persona_slug if voice_persona_slug_set else existing_row["voice_persona_slug"]
            new_rf = research_focus if research_focus_set else existing_row["research_focus"]
            new_instr = instructions if instructions_set else existing_row["instructions"]
            conn.execute(
                "UPDATE agent_overrides SET prompt = ?, model = ?, "
                "use_deep_reasoning = ?, role = ?, voice_persona_slug = ?, "
                "research_focus = ?, instructions = ?, updated_at = ? "
                "WHERE agent_id = ?",
                (new_prompt, new_model, new_deep, new_role, new_vp, new_rf, new_instr, now, agent_id),
            )
        else:
            new_prompt = prompt if prompt_set else None
            new_model = model if model_set else None
            new_deep = (
                (None if use_deep_reasoning is None else int(use_deep_reasoning))
                if deep_set
                else None
            )
            new_role = role if role_set else None
            new_vp = voice_persona_slug if voice_persona_slug_set else None
            new_rf = research_focus if research_focus_set else None
            new_instr = instructions if instructions_set else None
            conn.execute(
                "INSERT INTO agent_overrides "
                "(agent_id, prompt, model, use_deep_reasoning, role, voice_persona_slug, research_focus, "
                "instructions, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (agent_id, new_prompt, new_model, new_deep, new_role, new_vp, new_rf, new_instr, now),
            )

    invalidate_cache()
    result = get_override(agent_id, db_path=db_path)
    assert result is not None
    return result


def clear_override(agent_id: str, db_path: Path | None = None) -> bool:
    """Delete an agent's override row, snapshotting prior state to history.

    Returns True if a row was deleted, False if none existed.
    """
    db_path = _resolve_db(db_path)
    initialize_overrides_db(db_path)
    with _get_conn(db_path) as conn:
        existing_row = conn.execute(
            "SELECT * FROM agent_overrides WHERE agent_id = ?", (agent_id,)
        ).fetchone()
        if existing_row is None:
            return False
        conn.execute(
            "INSERT INTO agent_override_history "
            "(agent_id, prompt, model, use_deep_reasoning, role, voice_persona_slug, research_focus, "
            "instructions, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                agent_id,
                existing_row["prompt"],
                existing_row["model"],
                existing_row["use_deep_reasoning"],
                existing_row["role"],
                existing_row["voice_persona_slug"],
                existing_row["research_focus"],
                existing_row["instructions"],
                existing_row["updated_at"],
            ),
        )
        conn.execute("DELETE FROM agent_overrides WHERE agent_id = ?", (agent_id,))
    invalidate_cache()
    return True


def list_history(
    agent_id: str, limit: int = 50, db_path: Path | None = None
) -> list[AgentHistoryEntry]:
    db_path = _resolve_db(db_path)
    if not db_path.exists():
        return []
    initialize_overrides_db(db_path)
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM agent_override_history WHERE agent_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (agent_id, limit),
        ).fetchall()
    out: list[AgentHistoryEntry] = []
    for row in rows:
        deep = row["use_deep_reasoning"]
        out.append(
            AgentHistoryEntry(
                id=row["id"],
                agent_id=row["agent_id"],
                prompt=row["prompt"],
                model=row["model"],
                use_deep_reasoning=bool(deep) if deep is not None else None,
                role=row["role"],
                voice_persona_slug=row["voice_persona_slug"],
                research_focus=row["research_focus"],
                instructions=row["instructions"],
                created_at=row["created_at"],
            )
        )
    return out


def get_history_entry(
    history_id: int, db_path: Path | None = None
) -> AgentHistoryEntry | None:
    db_path = _resolve_db(db_path)
    if not db_path.exists():
        return None
    initialize_overrides_db(db_path)
    with _get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM agent_override_history WHERE id = ?", (history_id,)
        ).fetchone()
    if row is None:
        return None
    deep = row["use_deep_reasoning"]
    return AgentHistoryEntry(
        id=row["id"],
        agent_id=row["agent_id"],
        prompt=row["prompt"],
        model=row["model"],
        use_deep_reasoning=bool(deep) if deep is not None else None,
        role=row["role"],
        voice_persona_slug=row["voice_persona_slug"],
        research_focus=row["research_focus"],
        instructions=row["instructions"],
        created_at=row["created_at"],
    )


def rollback_to(
    history_id: int, db_path: Path | None = None
) -> AgentOverride | None:
    """Restore the override row to the state captured in the given history
    entry. Returns the new override, or None if the history id is unknown."""
    entry = get_history_entry(history_id, db_path=db_path)
    if entry is None:
        return None
    return set_override(
        entry.agent_id,
        prompt=entry.prompt,
        model=entry.model,
        use_deep_reasoning=entry.use_deep_reasoning,
        role=entry.role,
        voice_persona_slug=entry.voice_persona_slug,
        research_focus=entry.research_focus,
        instructions=entry.instructions,
        prompt_set=True,
        model_set=True,
        deep_set=True,
        role_set=True,
        voice_persona_slug_set=True,
        research_focus_set=True,
        instructions_set=True,
        db_path=db_path,
    )
