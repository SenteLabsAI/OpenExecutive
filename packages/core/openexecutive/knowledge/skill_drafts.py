"""Playbook changes the Executive proposes from chat, held for a person to review.

The Executive's chat tools (`create_skill`, `update_skill`, `delete_skill`)
run on inbound email and chat channels too, so a crafted message could steer
them. Instead of changing the library directly they save a *draft*; nothing
takes effect until a person approves it on the Playbooks tab — the same
draft → human approval shape custom workflows use.

Drafts are JSON files in ``company/skills/.drafts/`` (one per playbook name;
a newer proposal replaces an older one). They are not ``*.md``, so the
library's walks never see them: a draft is never listed, searched, loaded or
followed by a workflow. Living under ``company/skills/`` means client slots
carry them and a fixture reset clears them with the rest of the company's
skills.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from openexecutive.knowledge import skills_index, skills_repo
from openexecutive.knowledge.skills import Skill, SkillParseError, validate_skill_name
from openexecutive.knowledge.store import ChromaDBStore

logger = logging.getLogger(__name__)

DRAFTS_DIRNAME = ".drafts"

DraftAction = Literal["create", "update", "delete"]


class SkillDraftNotFoundError(LookupError):
    pass


class SkillDraft(BaseModel):
    action: DraftAction
    name: str
    # Empty for a proposed delete.
    category: str = ""
    description: str = ""
    when_to_use: str = ""
    body: str = ""
    proposed_at: str = ""


def _drafts_dir() -> Path:
    # Looked up on the module at call time, so it follows the same company
    # dir as the rest of the skills code (and any test that redirects it).
    return skills_index._company_skills_path() / DRAFTS_DIRNAME


def _draft_path(name: str) -> Path:
    validate_skill_name(name)
    return _drafts_dir() / f"{name}.json"


def save_draft(draft: SkillDraft) -> SkillDraft:
    """Store `draft`, replacing any pending draft for the same playbook."""
    stamped = draft.model_copy(update={"proposed_at": datetime.now(UTC).isoformat()})
    path = _draft_path(stamped.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(stamped.model_dump_json(indent=2), encoding="utf-8")
    return stamped


def _read(path: Path) -> SkillDraft:
    try:
        draft = SkillDraft.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValidationError) as e:
        raise SkillParseError(f"Playbook draft {path.name} is unreadable: {e}") from e
    if draft.name != path.stem:
        raise SkillParseError(f"Playbook draft {path.name} names '{draft.name}'")
    return draft


def list_drafts() -> list[SkillDraft]:
    """Pending drafts, newest first. Unreadable files are skipped."""
    root = _drafts_dir()
    if not root.exists():
        return []
    drafts: list[SkillDraft] = []
    for path in root.glob("*.json"):
        try:
            drafts.append(_read(path))
        except SkillParseError as e:
            logger.warning("Skipping playbook draft: %s", e)
    return sorted(drafts, key=lambda d: d.proposed_at, reverse=True)


def get_draft(name: str) -> SkillDraft:
    path = _draft_path(name)
    if not path.exists():
        raise SkillDraftNotFoundError(f"No pending draft for playbook '{name}'")
    return _read(path)


def discard_draft(name: str) -> None:
    path = _draft_path(name)
    if not path.exists():
        raise SkillDraftNotFoundError(f"No pending draft for playbook '{name}'")
    path.unlink()


def approve_draft(name: str, store: ChromaDBStore) -> dict[str, Any]:
    """Apply a draft to the library, then remove it.

    Goes through the ordinary repo operations, so their rules still hold (a
    create conflicts with any existing name; an update or delete needs the
    playbook to exist). On any error the draft is kept for the person to
    edit or discard.
    """
    draft = get_draft(name)
    result: dict[str, Any] = {"action": draft.action, "name": name}
    skill: Skill | None = None
    if draft.action == "delete":
        result["outcome"] = skills_repo.delete_skill(name, store=store)
    else:
        op = skills_repo.create_skill if draft.action == "create" else skills_repo.update_skill
        skill = op(
            name=name,
            description=draft.description,
            when_to_use=draft.when_to_use,
            category=draft.category,
            body=draft.body,
            store=store,
        )
    result["skill"] = skill
    discard_draft(name)
    return result
