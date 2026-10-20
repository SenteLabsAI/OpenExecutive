"""Voice persona loader — merges built-in MD files with DB overrides.

Built-in personas live in personas/builtin/*.md and ship with the repo.
User edits are stored in the voice_personas SQLite table (episodic_memory.db),
keyed by slug. A DB row shadows the built-in with the same slug; deleting the
DB row restores the built-in. Pure-custom slugs (no built-in) are fully deleted
when their DB row is removed.

Active persona selection is stored on the agent_overrides row for "executive"
as voice_persona_slug. NULL resolves to "default" (shown as "Direct").

Three built-in voices are offered in the voice picker: Direct (``default``),
Supportive and Analytical. The named-executive voices carry ``legacy: true``
in their frontmatter: the picker hides them, but they still load, so an
install that picked one keeps its voice. A built-in voice's frontmatter may
also carry a one-line ``description`` and a short ``sample`` reply for the
picker. Voice files hold disposition only; response-length rules live in
``prompts/executive_persona.py``.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from openexecutive.memory.episodic import DB_PATH, _get_conn

_BUILTIN_DIR = Path(__file__).parent / "builtin"
_DEFAULT_SLUG = "default"

_cache_lock = threading.Lock()
_builtin_cache: dict[str, Persona] | None = None


class PersonaMeta(BaseModel):
    slug: str
    display_name: str
    is_builtin: bool
    is_customized: bool
    # Hidden from the voice picker but still loadable (built-in only).
    is_legacy: bool = False
    # Picker copy from the built-in's frontmatter ("" for custom voices).
    description: str = ""
    sample: str = ""


class Persona(BaseModel):
    slug: str
    display_name: str
    body: str
    is_builtin: bool
    is_customized: bool
    source_notes: str = ""
    is_legacy: bool = False
    description: str = ""
    sample: str = ""


def _parse_md(path: Path) -> dict[str, Any]:
    """Parse frontmatter + body from a markdown file."""
    text = path.read_text(encoding="utf-8")
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            fm = yaml.safe_load(parts[1]) or {}
            body = parts[2].strip()
            return {**fm, "body": body}
    return {"slug": path.stem, "display_name": path.stem, "body": text.strip()}


def _load_builtins() -> dict[str, Persona]:
    out: dict[str, Persona] = {}
    for md_path in sorted(_BUILTIN_DIR.glob("*.md")):
        data = _parse_md(md_path)
        slug = data.get("slug", md_path.stem)
        out[slug] = Persona(
            slug=slug,
            display_name=data.get("display_name", slug),
            body=data.get("body", ""),
            is_builtin=True,
            is_customized=False,
            source_notes=data.get("source_notes", ""),
            is_legacy=bool(data.get("legacy", False)),
            description=str(data.get("description", "")).strip(),
            sample=str(data.get("sample", "")).strip(),
        )
    return out


def _get_builtins() -> dict[str, Persona]:
    global _builtin_cache
    with _cache_lock:
        if _builtin_cache is None:
            _builtin_cache = _load_builtins()
        return _builtin_cache


def invalidate_builtin_cache() -> None:
    """Force builtins to reload on next read. Useful for tests."""
    global _builtin_cache
    with _cache_lock:
        _builtin_cache = None


def _initialize_voice_personas_table(db_path: Path) -> None:
    with _get_conn(db_path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS voice_personas (
                slug TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                body TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)


def _get_db_personas(db_path: Path) -> dict[str, dict[str, str]]:
    _initialize_voice_personas_table(db_path)
    with _get_conn(db_path) as conn:
        rows = conn.execute("SELECT slug, display_name, body FROM voice_personas").fetchall()
    return {row["slug"]: {"display_name": row["display_name"], "body": row["body"]} for row in rows}


def list_personas(db_path: Path | None = None) -> list[PersonaMeta]:
    db_path = db_path or DB_PATH
    builtins = _get_builtins()
    db_rows = _get_db_personas(db_path)
    seen: set[str] = set()
    result: list[PersonaMeta] = []

    for slug, builtin in builtins.items():
        seen.add(slug)
        result.append(PersonaMeta(
            slug=slug,
            display_name=db_rows[slug]["display_name"] if slug in db_rows else builtin.display_name,
            is_builtin=True,
            is_customized=slug in db_rows,
            is_legacy=builtin.is_legacy,
            description=builtin.description,
            sample=builtin.sample,
        ))

    for slug, row in db_rows.items():
        if slug not in seen:
            result.append(PersonaMeta(
                slug=slug,
                display_name=row["display_name"],
                is_builtin=False,
                is_customized=True,
            ))

    return result


def get_persona(slug: str, db_path: Path | None = None) -> Persona | None:
    db_path = db_path or DB_PATH
    builtins = _get_builtins()
    _initialize_voice_personas_table(db_path)
    with _get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT display_name, body FROM voice_personas WHERE slug = ?", (slug,)
        ).fetchone()

    if row is not None:
        builtin = builtins.get(slug)
        return Persona(
            slug=slug,
            display_name=row["display_name"],
            body=row["body"],
            is_builtin=slug in builtins,
            is_customized=True,
            source_notes=builtin.source_notes if builtin else "",
            is_legacy=builtin.is_legacy if builtin else False,
            description=builtin.description if builtin else "",
            sample=builtin.sample if builtin else "",
        )

    if slug in builtins:
        return builtins[slug]

    return None


def get_active_body(slug: str | None, db_path: Path | None = None) -> str:
    """Resolve slug (or None) to the persona body string.

    Falls back to 'default' if the slug is missing or unknown.
    """
    resolved = slug or _DEFAULT_SLUG
    persona = get_persona(resolved, db_path=db_path)
    if persona is None:
        persona = get_persona(_DEFAULT_SLUG, db_path=db_path)
    if persona is None:
        return ""
    return persona.body


def upsert_persona(
    slug: str,
    display_name: str,
    body: str,
    db_path: Path | None = None,
) -> Persona:
    db_path = db_path or DB_PATH
    _initialize_voice_personas_table(db_path)
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    with _get_conn(db_path) as conn:
        conn.execute(
            """INSERT INTO voice_personas (slug, display_name, body, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(slug) DO UPDATE SET
                 display_name = excluded.display_name,
                 body = excluded.body,
                 updated_at = excluded.updated_at""",
            (slug, display_name, body, now),
        )
    persona = get_persona(slug, db_path=db_path)
    assert persona is not None
    return persona


def delete_persona(slug: str, db_path: Path | None = None) -> bool:
    """Delete the DB row for slug. Returns True if a row was deleted."""
    db_path = db_path or DB_PATH
    _initialize_voice_personas_table(db_path)
    with _get_conn(db_path) as conn:
        rows_affected = conn.execute(
            "DELETE FROM voice_personas WHERE slug = ?", (slug,)
        ).rowcount
    return rows_affected > 0


def reset_persona(slug: str, db_path: Path | None = None) -> Persona | None:
    """Delete the DB row (restoring the built-in). Returns the built-in, or None if no built-in."""
    builtins = _get_builtins()
    if slug not in builtins:
        return None
    delete_persona(slug, db_path=db_path)
    return builtins[slug]


def create_persona(
    display_name: str,
    body: str,
    db_path: Path | None = None,
) -> Persona:
    """Create a new custom persona. Slug is derived from display_name."""
    db_path = db_path or DB_PATH
    slug = re.sub(r"[^a-z0-9]+", "-", display_name.lower()).strip("-")
    if not slug:
        slug = "custom"

    existing = get_persona(slug, db_path=db_path)
    if existing is not None:
        base = slug
        n = 2
        while get_persona(f"{base}-{n}", db_path=db_path) is not None:
            n += 1
        slug = f"{base}-{n}"

    return upsert_persona(slug, display_name, body, db_path=db_path)


def persona_exists(slug: str, db_path: Path | None = None) -> bool:
    return get_persona(slug, db_path=db_path) is not None
