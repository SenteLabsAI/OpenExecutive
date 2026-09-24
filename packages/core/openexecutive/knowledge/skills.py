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
    """Parse a raw skill file body. Path is used only for error context and metadata."""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise SkillParseError(
            f"Skill {path} is missing YAML frontmatter (expected leading '---' fence)."
        )
    raw_yaml, body = match.group(1), match.group(2)
    try:
        data = yaml.safe_load(raw_yaml) or {}
    except yaml.YAMLError as e:
        raise SkillParseError(f"Skill {path} has malformed YAML frontmatter: {e}") from e
    if not isinstance(data, dict):
        raise SkillParseError(f"Skill {path} frontmatter must be a YAML mapping.")

    required = ("name", "description", "when_to_use", "category")
    missing = [k for k in required if not data.get(k)]
    if missing:
        raise SkillParseError(
            f"Skill {path} is missing required frontmatter field(s): {', '.join(missing)}"
        )

    validate_skill_name(data["name"])
    if data["category"] not in SKILL_CATEGORIES:
        raise SkillParseError(
            f"Skill {path}: unknown category '{data['category']}'. "
            f"Valid categories: {', '.join(SKILL_CATEGORIES)}"
        )

    # Filename stem must match the frontmatter name — keeps lookup by name unambiguous.
    if path.stem != data["name"]:
        raise SkillParseError(
            f"Skill {path}: frontmatter name '{data['name']}' "
            f"does not match filename stem '{path.stem}'."
        )

    return Skill(
        frontmatter=SkillFrontmatter(**{k: data[k] for k in required}),
        body=body.lstrip("\n"),
        source=source,
        path=str(path),
    )


def parse_skill_file(path: Path, source: SkillSource) -> Skill:
    text = path.read_text(encoding="utf-8")
    return parse_skill_text(text, path, source)


def serialize_skill(frontmatter: SkillFrontmatter, body: str) -> str:
    """Render a skill back to a Markdown file with YAML frontmatter."""
    fm = yaml.safe_dump(
        frontmatter.model_dump(),
        sort_keys=False,
        default_flow_style=False,
    ).strip()
    body_clean = body.rstrip() + "\n"
    return f"---\n{fm}\n---\n\n{body_clean}"
