"""Filesystem CRUD for skills, shared by API routes and Executive tool handlers."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

from openexecutive.knowledge.skills import (
    SKILL_CATEGORIES,
    Skill,
    SkillFrontmatter,
    SkillParseError,
    SkillSource,
    parse_skill_file,
    serialize_skill,
    validate_skill_name,
)
from openexecutive.knowledge.skills_index import (
    BUILTIN_SKILLS_PATH,
    _company_skills_path,
    delete_skill_index,
    hidden_builtin_names,
    index_skill,
    write_hidden_builtin_names,
)
from openexecutive.knowledge.store import ChromaDBStore

logger = logging.getLogger(__name__)


class SkillNotFoundError(LookupError):
    pass


class SkillConflictError(ValueError):
    pass


# What `delete_skill` did: removed a company skill, removed a company
# override (the built-in is back in effect), or hid a built-in.
DeleteOutcome = Literal["deleted", "reverted", "hidden"]


def _skill_path(source: SkillSource, category: str, name: str) -> Path:
    base = BUILTIN_SKILLS_PATH if source == "builtin" else _company_skills_path()
    return base / category / f"{name}.md"


def _find_in(root: Path, name: str) -> Path | None:
    if not root.exists():
        return None
    return next(iter(root.rglob(f"{name}.md")), None)


def _company_file(name: str) -> Path | None:
    """The valid company file for `name`. Malformed files shadow nothing."""
    root = _company_skills_path()
    if not root.exists():
        return None
    for path in sorted(root.rglob(f"{name}.md")):
        try:
            parse_skill_file(path, source="company")
        except SkillParseError as e:
            logger.warning("Ignoring malformed skill %s: %s", path, e)
            continue
        return path
    return None


def _find_skill_on_disk(name: str, include_hidden: bool = False) -> tuple[Path, SkillSource] | None:
    """Return the (path, source) of the skill in effect for `name`.

    A company skill wins over a built-in of the same name. A hidden built-in
    is only found with `include_hidden`.
    """
    company = _company_file(name)
    if company is not None:
        return company, "company"
    builtin = _find_in(BUILTIN_SKILLS_PATH, name)
    if builtin is not None and (include_hidden or name not in hidden_builtin_names()):
        return builtin, "builtin"
    return None


def is_builtin_name(name: str) -> bool:
    """True when a built-in ships under `name` (hidden or not)."""
    validate_skill_name(name)
    return _find_in(BUILTIN_SKILLS_PATH, name) is not None


def _builtin_in_effect(name: str, hidden: set[str]) -> bool:
    return name not in hidden and _find_in(BUILTIN_SKILLS_PATH, name) is not None


def _parse(path: Path, source: SkillSource, hidden: set[str]) -> Skill:
    skill = parse_skill_file(path, source=source)
    fm = skill.frontmatter
    if source == "company":
        # Only a deliberate customization of a built-in still in effect;
        # deleting it would bring that built-in back.
        skill.customized = fm.customizes_builtin and _builtin_in_effect(fm.name, hidden)
    else:
        skill.hidden = fm.name in hidden
    return skill


def list_skills(include_hidden: bool = False) -> list[Skill]:
    """Every skill in effect, sorted by (category, name). Malformed files are skipped.

    Company skills shadow built-ins of the same name; hidden built-ins are
    left out unless `include_hidden`.
    """
    hidden = hidden_builtin_names()
    by_name: dict[str, Skill] = {}
    for source, root in (("company", _company_skills_path()), ("builtin", BUILTIN_SKILLS_PATH)):
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.md")):
            if path.stem in by_name:
                continue
            try:
                skill = _parse(path, source, hidden)  # type: ignore[arg-type]
            except SkillParseError as e:
                logger.warning("Skipping malformed skill %s: %s", path, e)
                continue
            if skill.hidden and not include_hidden:
                continue
            by_name[skill.frontmatter.name] = skill
    return sorted(by_name.values(), key=lambda s: (s.frontmatter.category, s.frontmatter.name))


def get_skill(name: str, include_hidden: bool = False) -> Skill:
    validate_skill_name(name)
    found = _find_skill_on_disk(name, include_hidden=include_hidden)
    if found is None:
        raise SkillNotFoundError(f"Skill '{name}' not found")
    path, source = found
    return _parse(path, source, hidden_builtin_names())


def _validate_category(category: str) -> None:
    if category not in SKILL_CATEGORIES:
        raise SkillParseError(
            f"Unknown category '{category}'. Valid: {', '.join(SKILL_CATEGORIES)}"
        )


def _write_company_skill(
    name: str,
    description: str,
    when_to_use: str,
    category: str,
    body: str,
    customizes_builtin: bool = False,
) -> Skill:
    """Write the company file for `name`, removing any other company file with that stem.

    Clearing the others (a category move, or a malformed leftover) keeps one
    company file per name, so lookups stay unambiguous.
    """
    frontmatter = SkillFrontmatter(
        name=name,
        description=description,
        when_to_use=when_to_use,
        category=category,
        customizes_builtin=customizes_builtin,
    )
    path = _skill_path("company", category, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialize_skill(frontmatter, body), encoding="utf-8")
    for other in _company_skills_path().rglob(f"{name}.md"):
        if other != path:
            other.unlink()
    return Skill(frontmatter=frontmatter, body=body, source="company", path=str(path))


def create_skill(
    name: str,
    description: str,
    when_to_use: str,
    category: str,
    body: str,
    store: ChromaDBStore,
) -> Skill:
    """Create a new company skill. Conflicts with any existing name, hidden built-ins included.

    Customizing a built-in goes through `update_skill`, not here.
    """
    validate_skill_name(name)
    _validate_category(category)
    if _find_skill_on_disk(name, include_hidden=True) is not None:
        raise SkillConflictError(f"Skill '{name}' already exists")

    skill = _write_company_skill(name, description, when_to_use, category, body)
    index_skill(skill, store)
    return skill


def update_skill(
    name: str,
    description: str,
    when_to_use: str,
    category: str,
    body: str,
    store: ChromaDBStore,
) -> Skill:
    """Full replace of a skill.

    Updating a built-in customizes it: the edit is saved as a company skill of
    the same name, flagged `customizes_builtin`, that shadows the built-in
    (whose file is never touched). Deleting it later reverts to the built-in.
    """
    validate_skill_name(name)
    found = _find_skill_on_disk(name)
    if found is None:
        raise SkillNotFoundError(f"Skill '{name}' not found")
    _validate_category(category)

    path, source = found
    customizes = (
        source == "builtin"
        or parse_skill_file(path, source="company").frontmatter.customizes_builtin
    )
    written = _write_company_skill(
        name, description, when_to_use, category, body, customizes_builtin=customizes
    )
    skill = _parse(Path(written.path), "company", hidden_builtin_names())

    index_skill(skill, store)
    if source == "builtin":
        delete_skill_index(name, "builtin", store)
    return skill


def delete_skill(name: str, store: ChromaDBStore) -> DeleteOutcome:
    """Remove a skill from effect.

    - company skill: file deleted. "reverted" when it was a customization of
      a built-in still in effect, else "deleted". Either way a built-in of
      that name that isn't hidden is re-indexed, since nothing shadows it now.
    - built-in: its file lives in git, so it is hidden for this company
      instead ("hidden"); `restore_skill` brings it back.
    """
    validate_skill_name(name)
    found = _find_skill_on_disk(name)
    if found is None:
        raise SkillNotFoundError(f"Skill '{name}' not found")
    path, source = found

    if source == "builtin":
        write_hidden_builtin_names(hidden_builtin_names() | {name})
        delete_skill_index(name, "builtin", store)
        return "hidden"

    hidden = hidden_builtin_names()
    customized = _parse(path, "company", hidden).customized
    path.unlink()
    delete_skill_index(name, "company", store)
    builtin = _find_in(BUILTIN_SKILLS_PATH, name)
    if builtin is not None and name not in hidden:
        try:
            index_skill(parse_skill_file(builtin, source="builtin"), store)
        except SkillParseError as e:
            logger.warning("Built-in skill %s is malformed; not re-indexed: %s", builtin, e)
    return "reverted" if customized else "deleted"


def restore_skill(name: str, store: ChromaDBStore) -> Skill:
    """Un-hide a built-in skill and put it back in the index.

    Validates the built-in before touching the hidden list, so a failed
    restore leaves state unchanged.
    """
    validate_skill_name(name)
    hidden = hidden_builtin_names()
    builtin = _find_in(BUILTIN_SKILLS_PATH, name)
    if name not in hidden or builtin is None:
        raise SkillNotFoundError(f"Skill '{name}' is not a hidden built-in")
    parse_skill_file(builtin, source="builtin")  # raises SkillParseError before any write

    write_hidden_builtin_names(hidden - {name})
    skill = get_skill(name)
    if skill.source == "builtin":
        index_skill(skill, store)
    return skill


def skill_to_dict(skill: Skill, include_body: bool = True) -> dict[str, Any]:
    fm = skill.frontmatter
    out: dict[str, Any] = {
        "name": fm.name,
        "category": fm.category,
        "description": fm.description,
        "when_to_use": fm.when_to_use,
        "source": skill.source,
        "filename": Path(skill.path).name,
        "customized": skill.customized,
        "hidden": skill.hidden,
    }
    if include_body:
        out["body"] = skill.body
    return out
