"""Anthropic tool definitions + handlers for the skills library.

These tools are exposed to the Executive alongside `consult_specialist`. The
Executive uses them to find and read playbooks (how a piece of work is done)
and the workflows that follow them. Chat does not create or change playbooks:
something worth repeating is saved as a workflow (`draft_workflow`), and a
playbook's method is changed by a person on the workflow that follows it.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from openexecutive.knowledge import skills_repo
from openexecutive.knowledge.skills import SkillParseError
from openexecutive.knowledge.skills_index import search_skills as _search_skills
from openexecutive.knowledge.skills_repo import SkillNotFoundError
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


SKILL_TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[str]]] = {
    "search_skills": handle_search_skills,
    "load_skill": handle_load_skill,
}
