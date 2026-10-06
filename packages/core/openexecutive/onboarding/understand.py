"""First read of a new user's own description of their work.

Setup now starts with the free-text box. Before the interview begins, this one
model call reads that text (and any attached files, already extracted to text)
and reports what it can already tell: personal or team, the user's role, who
they report to, the company and this year's focus. The UI shows it for
confirmation and the interview then asks only about what is missing.

Same epistemics as the interviewer: extract, never invent. Anything the text
does not say comes back null and the UI asks for it. Nothing here writes
anything; the UI saves the confirmed values through PUT /workspace.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from openexecutive.agents.onboarding_interviewer import (
    ONBOARDING_INTERVIEWER_AGENT_ID,
    OnboardingInterviewerAgent,
)
from openexecutive.config import get_settings
from openexecutive.memory.workspace_settings import ROLE_TEXT_MAX
from openexecutive.onboarding.interview import InterviewError, InterviewTimeout

logger = logging.getLogger(__name__)

TOOL_NAME = "report_understanding"
_MAX_TOKENS = 1000
_COMPANY_MAX = 200
_FOCUS_MAX = 300

# A constant, not an f-string, and not cache-controlled: this call is made
# once per setup and its input is the user's own text (see CLAUDE.md).
SYSTEM = (
    "A new user is setting up Open Executive and has described their work in "
    "their own words. Report what the text already tells you by calling "
    f"{TOOL_NAME} exactly once.\n\n"
    "Rules:\n"
    "- Extract, never invent. A field the text does not state is null.\n"
    "- mode: 'solo' when the user is setting this up for their own work "
    "(an individual, a manager, an advisor, a founder describing themselves); "
    "'team' when they describe setting it up for a whole company with "
    "several people or departments. null when unclear.\n"
    "- role_kind: 'owner' (their own business), 'in_house' (leads a function "
    "or team in an organisation they do not own), 'independent' (advises or "
    "leads for clients), 'other', or null.\n"
    "- role_title, reports_to, company, focus: short phrases in the user's "
    "own words (company may include a size if stated, e.g. 'Northwind "
    "Software, about 300 people'; focus is what they want to achieve this "
    "year).\n"
    "- Never include email addresses, phone numbers or chat handles."
)

TOOL = {
    "name": TOOL_NAME,
    "description": "Report what the user's description already says.",
    "input_schema": {
        "type": "object",
        "properties": {
            "mode": {"type": ["string", "null"], "enum": ["solo", "team", None]},
            "role_kind": {
                "type": ["string", "null"],
                "enum": ["owner", "in_house", "independent", "other", None],
            },
            "role_title": {"type": ["string", "null"]},
            "reports_to": {"type": ["string", "null"]},
            "company": {"type": ["string", "null"]},
            "focus": {"type": ["string", "null"]},
        },
        "required": [],
    },
}


class Understanding(BaseModel):
    model_config = ConfigDict(extra="ignore")

    mode: Literal["solo", "team"] | None = None
    role_kind: Literal["owner", "in_house", "independent", "other"] | None = None
    role_title: str | None = None
    reports_to: str | None = None
    company: str | None = None
    focus: str | None = None


def _clip(value: str | None, limit: int) -> str | None:
    text = (value or "").strip()
    return text[:limit] if text else None


def _tidy(raw: Understanding) -> Understanding:
    """Trim blanks and cap lengths so the UI can save the values as they are."""
    return Understanding(
        mode=raw.mode,
        role_kind=raw.role_kind,
        role_title=_clip(raw.role_title, ROLE_TEXT_MAX["role_title"]),
        reports_to=_clip(raw.reports_to, ROLE_TEXT_MAX["reports_to"]),
        company=_clip(raw.company, _COMPANY_MAX),
        focus=_clip(raw.focus, _FOCUS_MAX),
    )


_MODES = {"solo", "team"}
_ROLE_KINDS = {"owner", "in_house", "independent", "other"}


def _coerce(data: dict[str, Any]) -> dict[str, Any]:
    """Null an out-of-enum mode or role_kind so one bad value cannot discard
    the valid fields beside it (the tool schema's enum is not enforced)."""
    out = dict(data)
    if out.get("mode") not in _MODES:
        out["mode"] = None
    if out.get("role_kind") not in _ROLE_KINDS:
        out["role_kind"] = None
    return out


def _extract(response: Any) -> dict[str, Any]:
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == TOOL_NAME:
            data = getattr(block, "input", None)
            if isinstance(data, dict):
                return data
    raise InterviewError("The setup assistant did not return a usable response.")


async def understand(text: str) -> Understanding:
    """Read ``text`` once. Raises ``InterviewError`` / ``InterviewTimeout``
    with fixed, input-free messages, like the interview itself."""
    from openexecutive.audit.usage import log_model_usage
    from openexecutive.providers.registry import get_provider

    agent = OnboardingInterviewerAgent()
    model = agent.effective_model()
    provider = get_provider(model)
    try:
        response = await asyncio.wait_for(
            provider.messages_create(
                model=model,
                max_tokens=_MAX_TOKENS,
                system=[{"type": "text", "text": SYSTEM}],
                tools=[TOOL],
                tool_choice={"type": "tool", "name": TOOL_NAME},
                messages=[{"role": "user", "content": text}],
            ),
            timeout=get_settings().interview_timeout_s,
        )
    except TimeoutError as exc:
        raise InterviewTimeout("The setup assistant took too long to respond.") from exc
    except Exception as exc:
        logger.error("onboarding understand: provider call failed (%s)", type(exc).__name__)
        raise InterviewError("The setup assistant is unavailable right now.") from exc

    log_model_usage(response, model=model, actor=ONBOARDING_INTERVIEWER_AGENT_ID)
    try:
        return _tidy(Understanding.model_validate(_coerce(_extract(response))))
    except ValidationError as exc:
        logger.error("onboarding understand: malformed result (%s)", type(exc).__name__)
        raise InterviewError("The setup assistant did not return a usable response.") from exc
