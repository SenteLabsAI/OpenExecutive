"""End-to-end tests for the skill repo + ChromaDB index using a tmp_path store."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from openexecutive.knowledge import skills_index, skills_repo
from openexecutive.knowledge.skills_index import (
    count_skills,
    search_skills,
    seed_builtin_skills,
)
from openexecutive.knowledge.skills_repo import (
    SkillConflictError,
    SkillNotFoundError,
    create_skill,
    delete_skill,
    get_skill,
    list_skills,
    restore_skill,
    update_skill,
)
from openexecutive.knowledge.store import ChromaDBStore


@pytest.fixture()
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ChromaDBStore]:
    """Point both skill trees at temp dirs and give us a fresh ChromaDB."""
    builtin_root = tmp_path / "builtin_skills"
    company_root = tmp_path / "company_skills"
    builtin_root.mkdir()
    company_root.mkdir()

    monkeypatch.setattr(skills_index, "BUILTIN_SKILLS_PATH", builtin_root)
    monkeypatch.setattr(skills_repo, "BUILTIN_SKILLS_PATH", builtin_root)
    monkeypatch.setattr(skills_index, "_company_skills_path", lambda: company_root)
    monkeypatch.setattr(skills_repo, "_company_skills_path", lambda: company_root)

    store = ChromaDBStore(persist_directory=str(tmp_path / "chroma"))
    yield store


def _write_builtin(root: Path, category: str, name: str) -> Path:
    target = root / category / f"{name}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f"---\nname: {name}\ndescription: built-in {name}\n"
        f"when_to_use: tests\ncategory: {category}\n---\n\n# body of {name}\n",
        encoding="utf-8",
    )
    return target


def test_create_search_get_delete_roundtrip(isolated: ChromaDBStore) -> None:
    skill = create_skill(
        name="monthly-revenue-review",
        description="A monthly revenue review template",
        when_to_use="Each month-end close",
        category="finance",
        body="# Revenue review\n\nSteps...",
        store=isolated,
    )
    assert skill.source == "company"
    assert skill.frontmatter.name == "monthly-revenue-review"

    fetched = get_skill("monthly-revenue-review")
    assert fetched.frontmatter.description == "A monthly revenue review template"
    assert "Revenue review" in fetched.body

    hits = search_skills("monthly revenue close", isolated, n_results=3)
    assert any(h["name"] == "monthly-revenue-review" for h in hits)
    assert all("body" not in h for h in hits), "search_skills must not include body"

    delete_skill("monthly-revenue-review", store=isolated)
    with pytest.raises(SkillNotFoundError):
        get_skill("monthly-revenue-review")
    hits_after = search_skills("monthly revenue close", isolated, n_results=3)
    assert not any(h["name"] == "monthly-revenue-review" for h in hits_after)


def test_duplicate_create_raises_conflict(isolated: ChromaDBStore) -> None:
    create_skill(
        name="x",
        description="d",
        when_to_use="w",
        category="general",
        body="b",
        store=isolated,
    )
    with pytest.raises(SkillConflictError):
        create_skill(
            name="x",
            description="d2",
            when_to_use="w2",
            category="general",
            body="b2",
            store=isolated,
        )


def _hit_sources(store: ChromaDBStore, name: str) -> set[str]:
    return {
        h["source"]
        for h in search_skills(name, store, n_results=10)
        if h["name"] == name
    }


def test_updating_builtin_customizes_and_delete_reverts(isolated: ChromaDBStore) -> None:
    import asyncio

    builtin_file = _write_builtin(skills_index.BUILTIN_SKILLS_PATH, "finance", "builtin-thing")
    asyncio.run(seed_builtin_skills(store=isolated, force=True))
    assert get_skill("builtin-thing").source == "builtin"

    custom = update_skill(
        name="builtin-thing",
        description="our version",
        when_to_use="y",
        category="finance",
        body="# ours",
        store=isolated,
    )
    assert custom.source == "company"
    assert custom.customized is True
    assert "built-in builtin-thing" in builtin_file.read_text(), "built-in file untouched"

    fetched = get_skill("builtin-thing")
    assert (fetched.source, fetched.body.strip()) == ("company", "# ours")
    listed = [s for s in list_skills() if s.frontmatter.name == "builtin-thing"]
    assert len(listed) == 1 and listed[0].customized
    assert _hit_sources(isolated, "builtin-thing") == {"company"}

    assert delete_skill("builtin-thing", store=isolated) == "reverted"
    assert get_skill("builtin-thing").source == "builtin"
    assert _hit_sources(isolated, "builtin-thing") == {"builtin"}


def test_deleting_builtin_hides_it_and_restore_brings_it_back(
    isolated: ChromaDBStore,
) -> None:
    import asyncio

    _write_builtin(skills_index.BUILTIN_SKILLS_PATH, "finance", "builtin-thing")
    asyncio.run(seed_builtin_skills(store=isolated, force=True))

    assert delete_skill("builtin-thing", store=isolated) == "hidden"
    with pytest.raises(SkillNotFoundError):
        get_skill("builtin-thing")
    assert get_skill("builtin-thing", include_hidden=True).hidden is True
    assert "builtin-thing" not in {s.frontmatter.name for s in list_skills()}
    hidden = [s for s in list_skills(include_hidden=True) if s.hidden]
    assert [s.frontmatter.name for s in hidden] == ["builtin-thing"]
    assert _hit_sources(isolated, "builtin-thing") == set()
    # Startup reconciliation must not resurrect a hidden built-in.
    asyncio.run(seed_builtin_skills(store=isolated))
    assert _hit_sources(isolated, "builtin-thing") == set()
    # A hidden name is still taken.
    with pytest.raises(SkillConflictError):
        create_skill(
            name="builtin-thing",
            description="d",
            when_to_use="w",
            category="general",
            body="b",
            store=isolated,
        )

    restored = restore_skill("builtin-thing", store=isolated)
    assert (restored.source, restored.hidden) == ("builtin", False)
    assert get_skill("builtin-thing").source == "builtin"
    assert _hit_sources(isolated, "builtin-thing") == {"builtin"}
    assert not (skills_repo._company_skills_path() / ".hidden.yaml").exists()
    with pytest.raises(SkillNotFoundError):
        restore_skill("builtin-thing", store=isolated)


def test_create_conflicts_with_builtin_name(isolated: ChromaDBStore) -> None:
    _write_builtin(skills_index.BUILTIN_SKILLS_PATH, "finance", "builtin-thing")
    with pytest.raises(SkillConflictError):
        create_skill(
            name="builtin-thing",
            description="d",
            when_to_use="w",
            category="general",
            body="b",
            store=isolated,
        )


def test_update_changes_category_moves_file(isolated: ChromaDBStore) -> None:
    create_skill(
        name="mover",
        description="d",
        when_to_use="w",
        category="finance",
        body="b",
        store=isolated,
    )
    original = skills_repo._company_skills_path() / "finance" / "mover.md"
    assert original.exists()

    update_skill(
        name="mover",
        description="d2",
        when_to_use="w2",
        category="strategy",
        body="b2",
        store=isolated,
    )
    moved = skills_repo._company_skills_path() / "strategy" / "mover.md"
    assert moved.exists()
    assert not original.exists()


def test_seed_is_idempotent(isolated: ChromaDBStore) -> None:
    import asyncio

    _write_builtin(skills_index.BUILTIN_SKILLS_PATH, "strategy", "alpha")
    _write_builtin(skills_index.BUILTIN_SKILLS_PATH, "finance", "beta")
    first = asyncio.run(seed_builtin_skills(store=isolated, force=True))
    second = asyncio.run(seed_builtin_skills(store=isolated))
    assert first == 2
    assert second == 0
    assert count_skills(isolated, source="builtin") == 2


def test_seed_indexes_new_builtin_on_populated_collection(isolated: ChromaDBStore) -> None:
    """A built-in shipped in a later release must reach an already-seeded index."""
    import asyncio

    _write_builtin(skills_index.BUILTIN_SKILLS_PATH, "strategy", "alpha")
    assert asyncio.run(seed_builtin_skills(store=isolated)) == 1
    _write_builtin(skills_index.BUILTIN_SKILLS_PATH, "finance", "beta")
    assert asyncio.run(seed_builtin_skills(store=isolated)) == 1
    assert count_skills(isolated, source="builtin") == 2

    # Removed from the release -> dropped from the index.
    (skills_index.BUILTIN_SKILLS_PATH / "strategy" / "alpha.md").unlink()
    assert asyncio.run(seed_builtin_skills(store=isolated)) == 0
    assert count_skills(isolated, source="builtin") == 1


def test_list_includes_both_sources(isolated: ChromaDBStore) -> None:
    import asyncio

    _write_builtin(skills_index.BUILTIN_SKILLS_PATH, "strategy", "alpha")
    asyncio.run(seed_builtin_skills(store=isolated, force=True))
    create_skill(
        name="user-skill",
        description="d",
        when_to_use="w",
        category="general",
        body="b",
        store=isolated,
    )

    all_skills = list_skills()
    sources = {s.source for s in all_skills}
    assert sources == {"builtin", "company"}
    names = {s.frontmatter.name for s in all_skills}
    assert names == {"alpha", "user-skill"}
