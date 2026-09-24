"""Anthropic tool definitions + handlers for the skills library.

These tools are exposed to the Executive alongside `consult_specialist`. The
Executive uses them to discover, load, and curate reusable procedural skills.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import ValidationError

from openexecutive.knowledge import skills_repo
from openexecutive.knowledge.skill_drafts import DraftAction, SkillDraft, save_draft
from openexecutive.knowledge.skills import (
    SKILL_CATEGORIES,
    SkillParseError,
    validate_skill_name,
)
from openexecutive.knowledge.skills_index import search_skills as _search_skills
from openexecutive.knowledge.skills_repo import (
    SkillConflictError,
    SkillNotFoundError,
)
from openexecutive.knowledge.store import ChromaDBStore
from openexecutive.workflows.playbooks import playbook_users

logger = logging.getLogger(__name__)


def _get_store() -> ChromaDBStore:
    from openexecutive.config import get_settings

    return ChromaDBStore(persist_directory=get_settings().vector_store_path)


SKILL_TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_skills",
        "description": (
            "Search the skills library for reusable procedures relevant to the current task. "
            "Returns up to N matches with name, description, and when_to_use — but NOT the body. "
            "A hit's `workflows` lists workflows that follow it: when the user "
            "wants that full deliverable, offer or run the workflow instead. "
            "Use this when you suspect a task is something you've codified before, or when a "
            "user request looks repeatable. Follow up with `load_skill` to read the chosen procedure."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What kind of skill you're looking for, in natural language.",
                },
                "n_results": {
                    "type": "integer",
                    "description": "Max number of results (default 5).",
                    "minimum": 1,
                    "maximum": 20,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "load_skill",
        "description": (
            "Load the full Markdown body of a skill by name (as returned by `search_skills`). "
            "Use the loaded procedure to guide your work."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The skill's name (filename stem, e.g. 'quarterly-forecast').",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "create_skill",
        "description": (
            "Propose a new reusable skill (a playbook) for the user's library. Use this when "
            "you've just completed a task that is worth doing the same way next time — a "
            "recurring report, a templated memo, a structured analysis. Pick a stable "
            "kebab-case name. It is saved as a DRAFT: nothing changes until the user approves "
            "it on the Playbooks tab, so tell them and include the returned review_link."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Kebab-case identifier, e.g. 'monthly-revenue-review'.",
                },
                "description": {
                    "type": "string",
                    "description": "One-line summary of what this skill produces.",
                },
                "when_to_use": {
                    "type": "string",
                    "description": "When you should invoke this skill — the trigger pattern.",
                },
                "category": {
                    "type": "string",
                    "enum": list(SKILL_CATEGORIES),
                    "description": "Which domain this skill belongs to.",
                },
                "body": {
                    "type": "string",
                    "description": "The full procedure as Markdown. Steps, templates, examples.",
                },
            },
            "required": ["name", "description", "when_to_use", "category", "body"],
        },
    },
    {
        "name": "update_skill",
        "description": (
            "Propose a refinement to an existing user-created skill. All fields are required — "
            "this is a full replace. Saved as a DRAFT the user approves on the Playbooks tab "
            "(tell them, with the review_link); the current version stays in effect until "
            "then. Built-in skills, customized copies of them, and any skill a workflow "
            "follows cannot be changed from chat: the user edits those on the Playbooks tab."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"},
                "when_to_use": {"type": "string"},
                "category": {"type": "string", "enum": list(SKILL_CATEGORIES)},
                "body": {"type": "string"},
            },
            "required": ["name", "description", "when_to_use", "category", "body"],
        },
    },
    {
        "name": "delete_skill",
        "description": (
            "Propose deleting a user-created skill. Saved as a DRAFT the user approves on the "
            "Playbooks tab (tell them, with the review_link). Built-in skills, customized "
            "copies of them, and any skill a workflow follows cannot be deleted or hidden "
            "from chat: the user does that on the Playbooks tab. "
            "Use sparingly — only when the user explicitly asks or the skill is clearly obsolete."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
            },
            "required": ["name"],
        },
    },
]


async def handle_search_skills(input: dict[str, Any]) -> str:
    query = input.get("query", "")
    n = int(input.get("n_results") or 5)
    if not query:
        return json.dumps({"error": "missing required field: query"})
    hits = _search_skills(query=query, store=_get_store(), n_results=n)
    users = playbook_users()
    for hit in hits:
        hit["workflows"] = [u.name for u in users.get(hit["name"], [])]
    return json.dumps({"results": hits}, ensure_ascii=False)


async def handle_load_skill(input: dict[str, Any]) -> str:
    name = input.get("name", "")
    if not name:
        return json.dumps({"error": "missing required field: name"})
    try:
        skill = skills_repo.get_skill(name)
    except SkillNotFoundError as e:
        return json.dumps({"error": str(e)})
    except SkillParseError as e:
        return json.dumps({"error": str(e)})
    fm = skill.frontmatter
    return (
        f"# {fm.name}\n\n"
        f"**Category**: {fm.category}  \n"
        f"**Source**: {skill.source}\n\n"
        f"**Description**: {fm.description}\n\n"
        f"**When to use**: {fm.when_to_use}\n\n"
        f"---\n\n{skill.body}"
    )


def _invalid(e: Exception) -> str:
    if isinstance(e, ValidationError):
        return "Every field must be text: " + "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()
        )
    return str(e)


def _review_link(name: str) -> str:
    return f"/jobs?tab=playbooks&draft={name}"


def _drafted(draft: SkillDraft) -> str:
    return json.dumps({
        "drafted": True,
        "action": draft.action,
        "name": draft.name,
        "review_link": _review_link(draft.name),
        "note": (
            "Saved as a draft for the user to review. Nothing changes until they "
            "approve it on the Playbooks tab — tell them, with the link."
        ),
    })


def _draft_fields(input: dict[str, Any], action: DraftAction) -> SkillDraft:
    """Build a create/update draft; KeyError on a missing field, SkillParseError if invalid."""
    category = input["category"]
    if category not in SKILL_CATEGORIES:
        raise SkillParseError(
            f"Unknown category '{category}'. Valid: {', '.join(SKILL_CATEGORIES)}"
        )
    return SkillDraft(
        action=action,
        name=input["name"],
        category=category,
        description=input["description"],
        when_to_use=input["when_to_use"],
        body=input["body"],
    )


async def handle_create_skill(input: dict[str, Any]) -> str:
    # A workflow may still name a playbook that was deleted (it runs without
    # it); chat must not be able to fill that name with new instructions.
    refusal = _followed_refusal(str(input.get("name", "")))
    if refusal:
        return refusal
    try:
        draft = _draft_fields(input, "create")
        validate_skill_name(draft.name)
        if skills_repo.name_taken(draft.name):
            raise SkillConflictError(f"Skill '{draft.name}' already exists")
        return _drafted(save_draft(draft))
    except KeyError as e:
        return json.dumps({"error": f"missing required field: {e.args[0]}"})
    except SkillConflictError as e:
        return json.dumps({"error": str(e), "code": "conflict"})
    except (SkillParseError, ValidationError) as e:
        return json.dumps({"error": _invalid(e), "code": "invalid"})


# Playbooks that workflows follow are read at run time — including by
# scheduled custom workflows a person approved, whose later action steps can
# write to already-approved targets unattended. This tool loop also runs on
# inbound email and chat channels, so changing what such a workflow follows
# (customizing, hiding, editing or deleting its playbook) is left to a
# person on the Playbooks tab, never to a model a crafted message could steer.
# That covers every built-in (workflows read them by fixed name) and any
# company playbook any workflow follows (switched-off custom ones included).
_BUILTIN_UI_ONLY = (
    "'{name}' is a built-in playbook. Built-ins and customized copies of them can "
    "only be customized, reverted or hidden by the user on the Playbooks tab "
    "(Workflows → Playbooks). Point the user there, or save a new playbook under "
    "a different name."
)
_FOLLOWED_UI_ONLY = (
    "'{name}' is followed by the workflow(s) {workflows}, so only the user can "
    "change or delete it, on the Playbooks tab (Workflows → Playbooks). Point the "
    "user there, or save a new playbook under a different name."
)


def _followed_refusal(name: str) -> str | None:
    """Refusal JSON when a workflow follows `name` (or that can't be checked)."""
    try:
        followers = playbook_users(strict=True).get(name, [])
    except Exception:
        logger.exception("Could not check which workflows follow playbook %r", name)
        return json.dumps({
            "error": (
                f"Couldn't check whether a workflow follows '{name}', so it can't be "
                "changed from chat right now. The user can change it on the Playbooks tab."
            ),
            "code": "unverifiable",
        })
    if followers:
        titles = ", ".join(u.title for u in followers)
        return json.dumps({
            "error": _FOLLOWED_UI_ONLY.format(name=name, workflows=titles),
            "code": "followed_by_workflow",
        })
    return None


def _protected_refusal(name: str) -> str | None:
    """Refusal JSON when chat may not change `name`, else None."""
    try:
        if skills_repo.is_builtin_name(name):
            return json.dumps({"error": _BUILTIN_UI_ONLY.format(name=name), "code": "builtin"})
    except SkillParseError as e:
        return json.dumps({"error": str(e), "code": "invalid"})
    return _followed_refusal(name)


async def handle_update_skill(input: dict[str, Any]) -> str:
    if not input.get("name"):
        return json.dumps({"error": "missing required field: name"})
    refusal = _protected_refusal(str(input["name"]))
    if refusal:
        return refusal
    try:
        draft = _draft_fields(input, "update")
        skills_repo.get_skill(draft.name)  # must exist (and not be hidden)
        return _drafted(save_draft(draft))
    except KeyError as e:
        return json.dumps({"error": f"missing required field: {e.args[0]}"})
    except SkillNotFoundError as e:
        return json.dumps({"error": str(e), "code": "not_found"})
    except (SkillParseError, ValidationError) as e:
        return json.dumps({"error": _invalid(e), "code": "invalid"})


async def handle_delete_skill(input: dict[str, Any]) -> str:
    name = input.get("name", "")
    if not name:
        return json.dumps({"error": "missing required field: name"})
    if not isinstance(name, str):
        return json.dumps({"error": "name must be text", "code": "invalid"})
    refusal = _protected_refusal(name)
    if refusal:
        return refusal
    try:
        skills_repo.get_skill(name)
        return _drafted(save_draft(SkillDraft(action="delete", name=name)))
    except SkillNotFoundError as e:
        return json.dumps({"error": str(e), "code": "not_found"})
    except SkillParseError as e:
        return json.dumps({"error": str(e), "code": "invalid"})


SKILL_TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[str]]] = {
    "search_skills": handle_search_skills,
    "load_skill": handle_load_skill,
    "create_skill": handle_create_skill,
    "update_skill": handle_update_skill,
    "delete_skill": handle_delete_skill,
}
