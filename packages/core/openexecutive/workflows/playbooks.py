"""Playbooks that workflow steps follow.

A playbook is a skill (``knowledge/skills_repo.py``): *how* a piece of work is
done. A workflow step that follows one gets its body appended to the step's
prompt. The lookup resolves the playbook in effect for this company, so a
company's customized copy of a built-in is what the step follows, and a
hidden (or missing, or malformed) one falls back to the step's own prompt
plus RAG — a workflow never fails because a playbook is gone.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from openexecutive.knowledge.skills import SkillParseError
from openexecutive.knowledge.skills_repo import SkillNotFoundError, get_skill

logger = logging.getLogger(__name__)


def load_playbook(name: str) -> str:
    """The body of the playbook in effect for `name`, or "" when there is none."""
    try:
        return get_skill(name).body
    except (SkillNotFoundError, SkillParseError) as e:
        logger.info("Workflow playbook %r unavailable, step runs without it: %s", name, e)
        return ""


def playbook_clause(body: str, instruction: str) -> str:
    """Prompt suffix asking a step to follow `body`; "" when there is no playbook."""
    if not body.strip():
        return ""
    return f"\n\n{instruction}:\n\n{body}"


@dataclass(frozen=True)
class PlaybookUser:
    """A workflow whose steps follow a given playbook."""

    name: str
    title: str


def playbook_users() -> dict[str, list[PlaybookUser]]:
    """Playbook name -> the runnable workflows (built-in and active custom) following it.

    Best-effort: if the custom-workflow store can't be read, built-ins are
    still reported rather than failing the caller.
    """
    from openexecutive.workflows import WORKFLOW_REGISTRY, list_workflows

    try:
        workflows = list_workflows()
    except Exception:
        logger.exception("Could not list custom workflows; reporting built-ins only")
        workflows = list(WORKFLOW_REGISTRY.values())
    users: dict[str, list[PlaybookUser]] = {}
    for wf in workflows:
        for name in wf.followed_playbooks():
            users.setdefault(name, []).append(PlaybookUser(name=wf.name, title=wf.title))
    return users
