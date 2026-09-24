"""ChromaDB integration for the skills library.

Each skill is one document (no chunking — frontmatter + 100s of words fit easily).
The search corpus is the concatenation of name, description, and when_to_use;
the body is fetched separately via `load_skill`.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from openexecutive.knowledge.loader import BUILTIN_KNOWLEDGE_PATH
from openexecutive.knowledge.skills import (
    Skill,
    SkillParseError,
    SkillSource,
    parse_skill_file,
)
from openexecutive.knowledge.store import ChromaDBStore

logger = logging.getLogger(__name__)

SKILLS_COLLECTION = "skills"
BUILTIN_SKILLS_PATH = BUILTIN_KNOWLEDGE_PATH / "skills"
# Company-level list of built-in skills the user has hidden. Lives inside the
# company skills dir so client slots and fixture resets carry/clear it with
# the company's own skills. Not a `*.md`, so skill walks never parse it.
HIDDEN_SKILLS_FILENAME = ".hidden.yaml"


def _company_skills_path() -> Path:
    from openexecutive.config import get_settings

    return get_settings().company_profile_path.parent / "skills"


def hidden_builtin_names() -> set[str]:
    """Names of built-in skills this company has hidden. Unreadable file -> none."""
    path = _company_skills_path() / HIDDEN_SKILLS_FILENAME
    if not path.exists():
        return set()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        logger.warning("Ignoring unreadable hidden-skills file %s: %s", path, e)
        return set()
    names = data.get("hidden") if isinstance(data, dict) else None
    if not isinstance(names, list):
        return set()
    return {n for n in names if isinstance(n, str)}


def write_hidden_builtin_names(names: set[str]) -> None:
    """Persist the hidden set; an empty set removes the file."""
    path = _company_skills_path() / HIDDEN_SKILLS_FILENAME
    if not names:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({"hidden": sorted(names)}, default_flow_style=False),
        encoding="utf-8",
    )


def company_skill_names() -> set[str]:
    """Names (filename stems) of every company-authored skill on disk."""
    root = _company_skills_path()
    if not root.exists():
        return set()
    return {p.stem for p in root.rglob("*.md")}


def _skill_id(name: str, source: SkillSource) -> str:
    return f"skill::{source}::{name}"


def _skill_doc_text(skill: Skill) -> str:
    fm = skill.frontmatter
    return f"{fm.name}\n{fm.description}\n{fm.when_to_use}"


def _index_metadata(skill: Skill) -> dict[str, Any]:
    fm = skill.frontmatter
    return {
        "name": fm.name,
        "category": fm.category,
        "source": skill.source,
        "description": fm.description,
        "when_to_use": fm.when_to_use,
        "filename": Path(skill.path).name,
        "path": skill.path,
    }


def index_skill(skill: Skill, store: ChromaDBStore) -> None:
    """Upsert a single skill into the skills collection. Idempotent."""
    store.add_documents(
        texts=[_skill_doc_text(skill)],
        metadatas=[_index_metadata(skill)],
        ids=[_skill_id(skill.frontmatter.name, skill.source)],
        collection=SKILLS_COLLECTION,
    )


def delete_skill_index(name: str, source: SkillSource, store: ChromaDBStore) -> None:
    """Remove a single skill row from the index."""
    store.delete_documents(
        collection=SKILLS_COLLECTION,
        where={"$and": [{"name": name}, {"source": source}]},
    )


def search_skills(
    query: str,
    store: ChromaDBStore,
    n_results: int = 5,
    source_filter: SkillSource | None = None,
) -> list[dict[str, Any]]:
    """Semantic search across the skill index.

    Returns a list of `{name, category, description, when_to_use, source, score}`
    — no body. The Executive must call `load_skill` to fetch the procedure.
    """
    if n_results <= 0:
        return []

    col = store._get_or_create_collection(SKILLS_COLLECTION)
    count = col.count()
    if count == 0:
        return []

    where: dict[str, Any] | None = {"source": source_filter} if source_filter else None
    query_kwargs: dict[str, Any] = {
        "query_texts": [query],
        "n_results": min(n_results, count),
        "include": ["metadatas", "distances"],
    }
    if where:
        query_kwargs["where"] = where

    results = col.query(**query_kwargs)
    hits: list[dict[str, Any]] = []
    if results["metadatas"] and results["metadatas"][0]:
        for meta, dist in zip(
            results["metadatas"][0],
            results["distances"][0],
            strict=False,
        ):
            # Cosine distance -> similarity score for human readability.
            score = max(0.0, 1.0 - float(dist))
            hits.append({
                "name": meta.get("name", ""),
                "category": meta.get("category", ""),
                "description": meta.get("description", ""),
                "when_to_use": meta.get("when_to_use", ""),
                "source": meta.get("source", ""),
                "score": round(score, 4),
            })
    return hits


def _iter_skill_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(root.rglob("*.md"))


def sync_builtin_skill_index(store: ChromaDBStore, force: bool = False) -> int:
    """Make the index's built-in rows match the built-in skills in effect.

    A built-in is in effect unless the company hid it or overrides it with a
    company skill of the same name. Rows for built-ins no longer in effect are
    dropped; missing or changed ones are (re)indexed, so a built-in added in a
    release reaches installs whose collection is already populated. `force`
    re-embeds every in-effect built-in. Returns how many rows were indexed.
    """
    shadowed = hidden_builtin_names() | company_skill_names()
    wanted: dict[str, Skill] = {}
    for path in _iter_skill_files(BUILTIN_SKILLS_PATH):
        try:
            skill = parse_skill_file(path, source="builtin")
        except SkillParseError as e:
            logger.warning("Skipping malformed skill %s: %s", path, e)
            continue
        if skill.frontmatter.name not in shadowed:
            wanted[_skill_id(skill.frontmatter.name, "builtin")] = skill

    existing = {
        sid: meta
        for sid, meta in store.iter_chunk_metadata(SKILLS_COLLECTION)
        if meta.get("source") == "builtin"
    }
    store.delete_by_ids(SKILLS_COLLECTION, [sid for sid in existing if sid not in wanted])

    count = 0
    for sid, skill in wanted.items():
        meta = existing.get(sid)
        stale = meta is None or any(
            meta.get(k) != v for k, v in _index_metadata(skill).items()
        )
        if force or stale:
            index_skill(skill, store)
            count += 1
    return count


async def seed_builtin_skills(store: ChromaDBStore | None = None, force: bool = False) -> int:
    """Startup hook: reconcile built-in skill rows. Idempotent (0 when in sync)."""
    if store is None:
        from openexecutive.config import get_settings

        store = ChromaDBStore(persist_directory=get_settings().vector_store_path)
    return sync_builtin_skill_index(store, force=force)


def count_skills(store: ChromaDBStore, source: SkillSource | None = None) -> int:
    """Count indexed skills, optionally filtered by source."""
    col = store._get_or_create_collection(SKILLS_COLLECTION)
    if source is None:
        return col.count()
    try:
        result = col.get(where={"source": source}, include=[])
        return len(result.get("ids", []))
    except Exception:
        return 0
