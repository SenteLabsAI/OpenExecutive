"""Conversational workflow design — the loop behind the ``/jobs/new`` wizard.

The user describes a job in their own words; this module asks clarifying
questions until it can draft a complete ``DynamicWorkflowDef``. Nothing is
written anywhere: ``advance`` returns either a ``DesignerQuestion`` or a
``WorkflowDraft``, the route owns the session, and the UI saves the reviewed
draft through the existing ``POST /workflows/custom`` (which validates again).

Same shape as ``onboarding/interview.py`` — read its module docstring for the
reasoning; the invariants are repeated here because they are load-bearing:

* **The tool array is CONSTANT** (both tools on every call, sorted by name).
  ``tool_choice`` flips from ``any`` to the emit tool exactly once, when the
  question budget is spent or the user forces a draft.

* **The system block is a constant** with ``cache_control``. Everything that
  varies — the specialist list, the roster, the timezone, taken names, the
  previous draft — goes into USER turns, never the cached block.

* **Errors raised to the route are fixed strings.** Validation detail goes to
  the model in the repair turn and to the log as a type name only.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from openexecutive.agents.workflow_designer import (
    WORKFLOW_DESIGNER_AGENT_ID,
    WorkflowDesignerAgent,
)
from openexecutive.config import get_settings
from openexecutive.onboarding.interview import Turn, replay_transcript, transcript_chars
from openexecutive.orchestrator.workflow_authoring_tools import DEFINITION_SCHEMA
from openexecutive.workflows.base import WorkflowSection
from openexecutive.workflows.dynamic_models import DynamicWorkflowDef, validate_definition

logger = logging.getLogger(__name__)

# Budgets. The user can always short-circuit with force_draft; these only bound
# a runaway model and a runaway client.
MAX_QUESTIONS = 6
MAX_TRANSCRIPT_CHARS = 60_000
MAX_OPTIONS = 4
MAX_OPTION_CHARS = 80
# Roster fields are free text anyone with People access can set, and they are
# rendered into the prompt — bound them and keep each to one line so a crafted
# name cannot smuggle a block of instructions into the context.
MAX_ROSTER_NAME_CHARS = 80
MAX_ROSTER_ROLE_CHARS = 80

_MAX_TOKENS = 8000
_REPAIR_ECHO_CHARS = 4000

ASK_TOOL_NAME = "ask_clarifying_question"
EMIT_TOOL_NAME = "emit_workflow_draft"

# Sent when the transcript would otherwise end on an assistant turn.
_CONTINUE_PROMPT = (
    "Continue from what you already have: ask the next question, or draft "
    "the workflow if you have enough."
)

# Specialists offered to the designer. `triage` is registry-valid but scores
# inbound events rather than producing analysis, so it is not a useful step.
_EXCLUDED_SPECIALISTS = frozenset({"triage"})


class WorkflowDesignerError(RuntimeError):
    """The designer could not produce a question or a draft.

    The message is always a fixed, input-free string safe to return to the
    client.
    """


class WorkflowDesignerTimeout(WorkflowDesignerError):
    """The provider did not respond within the configured wall clock."""


class DesignerQuestion(BaseModel):
    question: str
    hint: str = ""
    options: list[str] = Field(default_factory=list)


class WorkflowDraft(BaseModel):
    definition: DynamicWorkflowDef
    summary: str = ""
    assumptions: list[str] = Field(default_factory=list)


_ASK_TOOL: dict[str, Any] = {
    "name": ASK_TOOL_NAME,
    "description": (
        "Ask the user ONE clarifying question about the workflow they want. "
        "Use this only while something that changes the workflow's shape is "
        "still unclear — the user reviews and can refine the draft, so an "
        "early draft beats a long interview."
    ),
    "input_schema": {
        "type": "object",
        "required": ["question"],
        "properties": {
            "question": {
                "type": "string",
                "description": "One question, in plain language, to the user.",
            },
            "hint": {
                "type": "string",
                "description": (
                    "Optional one-line nudge shown under the question — why "
                    "you're asking, or an example answer."
                ),
            },
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Up to four short suggested answers the user can click "
                    "instead of typing. Omit when the answer is open-ended."
                ),
            },
        },
    },
}

_EMIT_TOOL: dict[str, Any] = {
    "name": EMIT_TOOL_NAME,
    "description": (
        "Emit the complete workflow definition for the user to review. It is "
        "validated before the user sees it; if it breaks a structural rule "
        "you will be told exactly what to fix."
    ),
    "input_schema": {
        "type": "object",
        "required": ["definition", "summary"],
        "properties": {
            "definition": DEFINITION_SCHEMA,
            "summary": {
                "type": "string",
                "description": (
                    "Two or three plain sentences reading the workflow back "
                    "to the user."
                ),
            },
            "assumptions": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "One short line per decision you made that the user did "
                    "not state explicitly."
                ),
            },
        },
    },
}

# Sorted by name so the cached tool prefix is stable. ask_ < emit_.
TOOLS: list[dict[str, Any]] = sorted([_ASK_TOOL, _EMIT_TOOL], key=lambda t: str(t["name"]))


def _one_line(text: str, limit: int) -> str:
    """Collapse whitespace/control characters to single spaces and truncate."""
    flat = " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())
    return flat[:limit]


def build_context_block() -> str:
    """Render the live facts the designer needs, for the FIRST user turn.

    Read once per session by the route and stored, so the replayed first turn
    stays identical across the conversation. Every source degrades to an
    empty list rather than failing the session.
    """
    from openexecutive.orchestrator.router import SPECIALIST_DESCRIPTIONS, SPECIALIST_REGISTRY
    from openexecutive.people.store import list_people
    from openexecutive.workflows import WORKFLOW_REGISTRY
    from openexecutive.workflows.dynamic_store import list_definitions

    settings = get_settings()

    specialists = [
        f"- {key}: {SPECIALIST_DESCRIPTIONS.get(key, key)}"
        for key in sorted(SPECIALIST_REGISTRY)
        if key not in _EXCLUDED_SPECIALISTS
    ]

    try:
        people = list_people()
    except Exception as exc:  # the roster is optional context, not a hard dependency
        logger.warning("workflow designer: roster unavailable (%s)", type(exc).__name__)
        people = []
    roster = []
    for p in people:
        if p.id is None:
            continue
        name = _one_line(p.full_name, MAX_ROSTER_NAME_CHARS)
        role = _one_line(p.role, MAX_ROSTER_ROLE_CHARS)
        roster.append(
            f"- person_id {p.id}: {name}"
            + (f" — {role}" if role else "")
            + (" (the user)" if p.is_principal else "")
        )

    try:
        custom_names = [d.name for d in list_definitions(active_only=False)]
    except Exception as exc:
        logger.warning("workflow designer: custom workflows unavailable (%s)", type(exc).__name__)
        custom_names = []
    taken = sorted({*WORKFLOW_REGISTRY.keys(), *custom_names})

    sections = [f"- {s.value}" for s in WorkflowSection]

    return (
        "Context for designing this workflow (facts from the system, not "
        "from the user):\n\n"
        "Specialists you may use (the `specialist` value is the key before "
        "the colon):\n" + "\n".join(specialists) + "\n\n"
        "People on the roster (the only valid person_id values):\n"
        + ("\n".join(roster) if roster else "- (nobody yet — no approval gates or schedules)")
        + "\n\n"
        f"The user's timezone: {settings.user_timezone}. Cadences are in UTC.\n\n"
        "Sections:\n" + "\n".join(sections) + "\n\n"
        "Workflow names already taken (do not reuse): " + ", ".join(taken)
    )


def _name_taken(name: str) -> bool:
    """True if a custom workflow already uses ``name``.

    Built-in clashes are caught by ``validate_definition`` itself; this covers
    the custom ones, which only ``POST /workflows/custom`` would otherwise
    reject — after the user already reviewed the draft.
    """
    from openexecutive.workflows.dynamic_store import get_definition

    try:
        return get_definition(name) is not None
    except Exception as exc:
        logger.warning("workflow designer: name lookup failed (%s)", type(exc).__name__)
        return False


def _build_messages(
    transcript: list[Turn],
    context_block: str,
    previous_draft: DynamicWorkflowDef | None,
) -> list[dict[str, Any]]:
    messages = replay_transcript(transcript, _CONTINUE_PROMPT)
    if not messages:
        raise WorkflowDesignerError("Describe the workflow you want first.")
    if context_block:
        first = messages[0]
        messages[0] = {
            "role": first["role"],
            "content": f"{context_block}\n\n---\n\n{first['content']}",
        }
    if previous_draft is not None:
        # The transcript stores a draft only as its plain-language summary.
        # When the user is refining, hand the model the exact current
        # definition so it edits it instead of re-deriving it from memory.
        last = messages[-1]
        draft_json = previous_draft.model_dump_json(
            exclude={"is_active", "created_at", "updated_at"}
        )
        messages[-1] = {
            "role": last["role"],
            "content": (
                "The current draft of the workflow is below. Apply the "
                "user's latest message to it — keep everything they did not "
                "ask to change.\n\n"
                f"{draft_json}\n\n---\n\n{last['content']}"
            ),
        }
    return messages


def _extract_tool_call(response: Any) -> tuple[str, dict[str, Any]]:
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) != "tool_use":
            continue
        name = getattr(block, "name", None)
        data = getattr(block, "input", None)
        if name in (ASK_TOOL_NAME, EMIT_TOOL_NAME) and isinstance(data, dict):
            return name, data
    raise WorkflowDesignerError("The workflow assistant did not return a usable response.")


def _parse_question(raw: dict[str, Any]) -> DesignerQuestion:
    """Validate a question, clamping the suggested answers instead of failing."""
    options = raw.get("options")
    clean: list[str] = []
    if isinstance(options, list):
        for opt in options:
            if isinstance(opt, str) and opt.strip():
                clean.append(opt.strip()[:MAX_OPTION_CHARS])
            if len(clean) == MAX_OPTIONS:
                break
    question = DesignerQuestion.model_validate({**raw, "options": clean})
    if not question.question.strip():
        # A blank question would store a blank assistant turn that drops out
        # of the replay, breaking role alternation.
        raise ValueError("empty question")
    return question


def _check_draft(raw: dict[str, Any]) -> tuple[WorkflowDraft | None, list[str]]:
    """Parse and validate an emitted draft. Returns (draft, []) or (None, errors)."""
    try:
        draft = WorkflowDraft.model_validate(raw)
    except ValidationError as exc:
        logger.info("workflow designer: draft failed schema validation (%s)", type(exc).__name__)
        return None, [str(exc)]
    # The server owns these; whatever the model put there is meaningless.
    draft.definition = draft.definition.model_copy(
        update={"is_active": True, "created_at": "", "updated_at": ""}
    )
    errors = validate_definition(draft.definition)
    if _name_taken(draft.definition.name):
        errors.append(
            f"name {draft.definition.name!r} is already used by a saved custom "
            "workflow — pick a different name"
        )
    if errors:
        logger.info("workflow designer: draft has %d validation error(s)", len(errors))
        return None, errors
    return draft, []


async def advance(
    transcript: list[Turn],
    *,
    context_block: str = "",
    previous_draft: DynamicWorkflowDef | None = None,
    force_draft: bool = False,
    questions_asked: int = 0,
    model: str | None = None,
) -> DesignerQuestion | WorkflowDraft:
    """Run one designer turn.

    Returns a ``DesignerQuestion`` for the user, or a validated
    ``WorkflowDraft`` to review. Raises ``WorkflowDesignerError`` /
    ``WorkflowDesignerTimeout`` — both with fixed, input-free messages.
    """
    from openexecutive.audit.usage import log_model_usage
    from openexecutive.providers.registry import get_provider

    settings = get_settings()
    agent = WorkflowDesignerAgent()
    resolved_model = model if model is not None else agent.effective_model()
    system_text = agent.effective_system_prompt()
    provider = get_provider(resolved_model)

    messages = _build_messages(transcript, context_block, previous_draft)

    # The one place tool_choice varies — see the module docstring.
    must_draft = (
        force_draft
        or questions_asked >= MAX_QUESTIONS
        or transcript_chars(transcript) >= MAX_TRANSCRIPT_CHARS
    )
    tool_choice: dict[str, Any] = (
        {"type": "tool", "name": EMIT_TOOL_NAME} if must_draft else {"type": "any"}
    )

    async def _call() -> Any:
        return await provider.messages_create(
            model=resolved_model,
            max_tokens=_MAX_TOKENS,
            system=[
                {
                    "type": "text",
                    "text": system_text,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=TOOLS,
            tool_choice=tool_choice,
            messages=messages,
        )

    for attempt in range(2):
        try:
            response = await asyncio.wait_for(_call(), timeout=settings.interview_timeout_s)
        except TimeoutError as exc:
            raise WorkflowDesignerTimeout(
                "The workflow assistant took too long to respond. Try again."
            ) from exc
        except WorkflowDesignerError:
            raise
        except Exception as exc:
            logger.error("workflow designer: provider call failed (%s)", type(exc).__name__)
            raise WorkflowDesignerError(
                "The workflow assistant is unavailable right now. Try again."
            ) from exc

        log_model_usage(
            response,
            model=resolved_model,
            actor=WORKFLOW_DESIGNER_AGENT_ID,
            iteration=attempt,
        )

        name, raw = _extract_tool_call(response)

        if name == ASK_TOOL_NAME:
            if must_draft:
                # tool_choice forced the emit tool; a question here means the
                # provider ignored it. Retry once WITH feedback.
                if attempt == 0:
                    messages = [
                        *messages,
                        {
                            "role": "assistant",
                            # Never empty: the API rejects a blank assistant turn,
                            # which would turn this retry into a 502.
                            "content": str(raw.get("question") or "").strip()
                            or "(asked another question)",
                        },
                        {
                            "role": "user",
                            "content": (
                                "No more questions — draft the workflow from "
                                "what you have and list what you assumed. "
                                f"Call {EMIT_TOOL_NAME} now."
                            ),
                        },
                    ]
                    continue
                raise WorkflowDesignerError(
                    "The workflow assistant could not produce a draft. "
                    "Try adding a bit more detail, or use the advanced editor."
                )
            try:
                return _parse_question(raw)
            except (ValidationError, ValueError) as exc:
                logger.error("workflow designer: malformed question (%s)", type(exc).__name__)
                if attempt == 0:
                    continue
                raise WorkflowDesignerError(
                    "The workflow assistant did not return a usable response."
                ) from exc

        draft, errors = _check_draft(raw)
        if draft is not None:
            return draft

        if attempt == 0:
            messages = [
                *messages,
                {"role": "assistant", "content": json.dumps(raw)[:_REPAIR_ECHO_CHARS]},
                {
                    "role": "user",
                    "content": (
                        "That draft was not usable:\n- "
                        + "\n- ".join(errors)
                        + f"\n\nFix them and call {EMIT_TOOL_NAME} again."
                    ),
                },
            ]
            tool_choice = {"type": "tool", "name": EMIT_TOOL_NAME}
            must_draft = True
            continue

        raise WorkflowDesignerError(
            "The workflow assistant could not produce a valid workflow. "
            "Try rephrasing, or use the advanced editor instead."
        )

    # Unreachable: every branch above returns or raises on attempt 1.
    raise WorkflowDesignerError("The workflow assistant could not produce a valid workflow.")
