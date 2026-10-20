"""Skill domain model + YAML frontmatter parsing.

A skill is a Markdown file with YAML frontmatter describing a reusable
procedure the Executive can search for, load on demand, or create itself.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

# Categories a skill can live under. Mirrors knowledge/loader.DOMAIN_MAP plus
# "general" for cross-cutting playbooks.
SKILL_CATEGORIES: tuple[str, ...] = (
    "strategy",
    "finance",
    "hr",
    "legal",
    "operations",
    "marketing",
    "product",
    "board",
    "sales",
    "general",
)

SkillSource = Literal["builtin", "company"]

_VALID_SKILL_NAME = re.compile(r"^[a-zA-Z0-9_\-]+$")
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


class SkillParseError(ValueError):
    """Raised when a skill file's frontmatter is missing or malformed."""


class SkillFrontmatter(BaseModel):
    name: str
    description: str
    when_to_use: str
    category: str
    # Set on a company skill saved by customizing a built-in, so a company
    # skill that merely shares a name with a later-shipped built-in is not
    # mistaken for (and "reverted" as) a customization.
    customizes_builtin: bool = False


class Skill(BaseModel):
    frontmatter: SkillFrontmatter
    body: str
    source: SkillSource
    path: str = Field(..., description="Absolute filesystem path to the skill file")
    # A company skill that shadows a built-in of the same name.
    customized: bool = False
    # A built-in this company hid; only surfaced when explicitly asked for.
    hidden: bool = False


def validate_skill_name(name: str) -> None:
    """Raises SkillParseError if name isn't a valid identifier."""
    if not _VALID_SKILL_NAME.match(name):
        raise SkillParseError(
            f"Invalid skill name '{name}'. "
            "Must be alphanumeric with dashes or underscores."
        )


def parse_skill_text(text: str, path: Path, source: SkillSource) -> Skill:
    """Parse a raw skill file body. Path is used for metadata; errors name only the file."""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise SkillParseError(
            f"Skill {path.name} is missing YAML frontmatter (expected leading '---' fence)."
        )
    raw_yaml, body = match.group(1), match.group(2)
    try:
        data = yaml.safe_load(raw_yaml) or {}
    except yaml.YAMLError as e:
        raise SkillParseError(f"Skill {path.name} has malformed YAML frontmatter: {e}") from e
    if not isinstance(data, dict):
        raise SkillParseError(f"Skill {path.name} frontmatter must be a YAML mapping.")

    required = ("name", "description", "when_to_use", "category")
    missing = [k for k in required if not data.get(k)]
    if missing:
        raise SkillParseError(
            f"Skill {path.name} is missing required frontmatter field(s): {', '.join(missing)}"
        )
    # YAML reads `description: 2024` as an int and `yes` as a bool; reject
    # those here so every malformed file surfaces as SkillParseError.
    not_text = [k for k in required if not isinstance(data[k], str)]
    if not_text:
        raise SkillParseError(
            f"Skill {path.name}: frontmatter field(s) must be text: {', '.join(not_text)}"
        )

    validate_skill_name(data["name"])
    if data["category"] not in SKILL_CATEGORIES:
        raise SkillParseError(
            f"Skill {path.name}: unknown category '{data['category']}'. "
            f"Valid categories: {', '.join(SKILL_CATEGORIES)}"
        )

    # Filename stem must match the frontmatter name — keeps lookup by name unambiguous.
    if path.stem != data["name"]:
        raise SkillParseError(
            f"Skill {path.name}: frontmatter name '{data['name']}' "
            f"does not match filename stem '{path.stem}'."
        )

    return Skill(
        frontmatter=SkillFrontmatter(
            **{k: data[k] for k in required},
            customizes_builtin=data.get("customizes_builtin") is True,
        ),
        body=body.lstrip("\n"),
        source=source,
        path=str(path),
    )


def parse_skill_file(path: Path, source: SkillSource) -> Skill:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise SkillParseError(f"Skill {path.name} could not be read: {e}") from e
    return parse_skill_text(text, path, source)


def serialize_skill(frontmatter: SkillFrontmatter, body: str) -> str:
    """Render a skill back to a Markdown file with YAML frontmatter."""
    fm = yaml.safe_dump(
        frontmatter.model_dump(exclude_defaults=True),
        sort_keys=False,
        default_flow_style=False,
    ).strip()
    body_clean = body.rstrip() + "\n"
    return f"---\n{fm}\n---\n\n{body_clean}"
