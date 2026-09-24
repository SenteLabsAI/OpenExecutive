"""Anthropic tool definitions + handlers for the skills library.

These tools are exposed to the Executive alongside `consult_specialist`. The
Executive uses them to discover, load, and curate reusable procedural skills.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from openexecutive.knowledge import skills_repo
from openexecutive.knowledge.skills import SKILL_CATEGORIES, SkillParseError
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
            "A hit's `workflows` lists runnable workflows that follow it: when the user "
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
            "Save a new reusable skill to the user's skills library. Use this when you've just "
            "completed a task that is worth doing the same way next time — a recurring report, "
            "a templated memo, a structured analysis. Pick a stable kebab-case name. Mention "
            "in your reply that you saved it."
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
            "Refine an existing user-created skill. All fields are required — this is a "
            "full replace. Built-in skills (and the company's customized copies of them) "
            "cannot be changed from chat: the user customizes them on the Playbooks tab."
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
            "Delete a user-created skill. Built-in skills (and customized copies of them) "
            "cannot be deleted or hidden from chat: the user does that on the Playbooks tab. "
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


async def handle_create_skill(input: dict[str, Any]) -> str:
    try:
        skill = skills_repo.create_skill(
            name=input["name"],
            description=input["description"],
            when_to_use=input["when_to_use"],
            category=input["category"],
            body=input["body"],
            store=_get_store(),
        )
    except KeyError as e:
        return json.dumps({"error": f"missing required field: {e.args[0]}"})
    except SkillConflictError as e:
        return json.dumps({"error": str(e), "code": "conflict"})
    except SkillParseError as e:
        return json.dumps({"error": str(e), "code": "invalid"})
    fm = skill.frontmatter
    return json.dumps({
        "saved": True,
        "name": fm.name,
        "category": fm.category,
        "path": f"company/skills/{fm.category}/{fm.name}.md",
    })


# Built-in playbooks feed automated workflows by fixed name (board_prep,
# quarterly_plan), and this tool loop also runs on inbound email and chat
# channels. So customizing or hiding one — which changes what those
# workflows follow — is left to a person on the Playbooks tab, never to a
# model that a crafted message could steer.
_BUILTIN_UI_ONLY = (
    "'{name}' is a built-in playbook. Built-ins and customized copies of them can "
    "only be customized, reverted or hidden by the user on the Playbooks tab "
    "(Workflows → Playbooks). Point the user there, or save a new playbook under "
    "a different name."
)


def _builtin_refusal(name: str) -> str | None:
    try:
        if skills_repo.is_builtin_name(name):
            return json.dumps({"error": _BUILTIN_UI_ONLY.format(name=name), "code": "builtin"})
    except SkillParseError as e:
        return json.dumps({"error": str(e), "code": "invalid"})
    return None


async def handle_update_skill(input: dict[str, Any]) -> str:
    if not input.get("name"):
        return json.dumps({"error": "missing required field: name"})
    refusal = _builtin_refusal(str(input["name"]))
    if refusal:
        return refusal
    try:
        skill = skills_repo.update_skill(
            name=input["name"],
            description=input["description"],
            when_to_use=input["when_to_use"],
            category=input["category"],
            body=input["body"],
            store=_get_store(),
        )
    except KeyError as e:
        return json.dumps({"error": f"missing required field: {e.args[0]}"})
    except SkillNotFoundError as e:
        return json.dumps({"error": str(e), "code": "not_found"})
    except SkillParseError as e:
        return json.dumps({"error": str(e), "code": "invalid"})
    return json.dumps({"updated": True, "name": skill.frontmatter.name})


async def handle_delete_skill(input: dict[str, Any]) -> str:
    name = input.get("name", "")
    if not name:
        return json.dumps({"error": "missing required field: name"})
    refusal = _builtin_refusal(name)
    if refusal:
        return refusal
    try:
        outcome = skills_repo.delete_skill(name, store=_get_store())
    except SkillNotFoundError as e:
        return json.dumps({"error": str(e), "code": "not_found"})
    except SkillParseError as e:
        return json.dumps({"error": str(e), "code": "invalid"})
    return json.dumps({"deleted": True, "name": name, "outcome": outcome})


SKILL_TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[str]]] = {
    "search_skills": handle_search_skills,
    "load_skill": handle_load_skill,
    "create_skill": handle_create_skill,
    "update_skill": handle_update_skill,
    "delete_skill": handle_delete_skill,
}
