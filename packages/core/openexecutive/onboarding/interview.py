"""Conversational company setup — the interview loop behind ``/onboard``.

The user describes their business in their own words; this module asks
clarifying questions until it can draft a company profile, a leadership
roster, and a department list. Nothing is written anywhere: ``advance``
returns either a ``Question`` or a ``CompanyDraft``, and the route owns both
the session state and the single write (see ``onboarding/commit.py``).

Shape notes for anyone changing this file:

* **The tool array is CONSTANT.** Both tools are present on every call, sorted
  by name. Adding a tool partway through a conversation invalidates the cached
  tool prefix on every subsequent turn — the same invariant
  ``orchestrator/form_tools.py`` documents. ``tool_choice={"type": "any"}``
  lets the model pick which one to call without varying the array.

* **``tool_choice`` flips exactly once**, on the turn where the question budget
  is spent or the caller forces a draft. That single call misses the tools
  cache; one miss per onboarding session is the price of a bounded interview.
  This is deliberate — do not "fix" it by making the tools conditional.

* **The system block is a constant**, never f-stringed. An existing profile is
  rendered into the first USER turn via ``to_prompt_block()``, never into the
  cached system block. This module does not touch ``prompts/cache_manager.py``,
  so the "exactly 2 cache_control blocks" budget there is unaffected.

* **Errors never echo model or user input.** A ``ValidationError``'s ``str()``
  embeds the offending values, which here are the user's ARR, burn, and
  runway. ``InterviewError`` messages are fixed strings; the detail goes to the
  log as a type name only. (``fixtures/generator.py`` interpolates the
  exception into its ``GenerationError`` — do not copy that here.)
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from openexecutive.agents.onboarding_interviewer import (
    ONBOARDING_INTERVIEWER_AGENT_ID,
    OnboardingInterviewerAgent,
)
from openexecutive.config import get_settings
from openexecutive.departments.models import AuthorityLevel
from openexecutive.memory.company_profile import CompanyProfile
from openexecutive.utils.slug import DEPARTMENT_SLUG_FALLBACK, slugify

logger = logging.getLogger(__name__)

# Budgets. The user can always short-circuit with force_draft, so these only
# bound a runaway model.
MAX_QUESTIONS = 8
# Must stay above what a single legal /start can produce — a 20k description
# plus 8 attachments at 15k extracted chars each is ~140k. A lower ceiling
# locked the user out of the conversation on turn one for doing exactly what
# the UI invites ("you can also attach a deck").
MAX_TRANSCRIPT_CHARS = 250_000

# Field bounds. Enforced here rather than with Field(max_length=...) so a
# rejection reports a fixed string instead of echoing the offending value
# back in FastAPI's 422 body — these drafts carry the company's financials.
MAX_NAME_CHARS = 200
MAX_ROLE_CHARS = 200
MAX_MISSION_CHARS = 2000
# The profile is rendered into the CACHED system prompt on every Executive
# turn (see CLAUDE.md), so an unbounded field here is a permanent cost on
# every request, not just one big row.
MAX_PROFILE_TEXT_CHARS = 10_000

_MAX_TOKENS = 8000
# Truncation for the repair turn that echoes the model's own bad output back at
# it. Same value fixtures/generator.py uses inline; the two are independent, so
# changing this one does not change that one.
_REPAIR_ECHO_CHARS = 2000

ASK_TOOL_NAME = "ask_clarifying_question"
EMIT_TOOL_NAME = "emit_company_draft"

# The first thing the user sees. A constant, so /onboard/interview/start costs
# no model call and cannot fail.
# Sent when the transcript would otherwise end on an assistant turn.
_CONTINUE_PROMPT = (
    "Continue from what you already have: ask the next question, or draft "
    "the profile if you have enough."
)

OPENING_PROMPT = (
    "Tell me about your company — what you do, who you sell to, roughly how "
    "big you are, and what you're focused on this year. Write it however you "
    "like; I'll ask about anything I'm missing. You can also attach a deck, "
    "a one-pager, or anything else that describes the business."
)


class InterviewError(RuntimeError):
    """Interview could not produce a question or a draft.

    The message is always a fixed, input-free string safe to return to the
    client — see the module docstring.
    """


class InterviewTimeout(InterviewError):
    """The provider did not respond within the configured wall clock."""


class Turn(BaseModel):
    """One stored conversation turn.

    Stored as plain text rather than raw API blocks: the history is replayed
    as ordinary user/assistant text turns, so there are no tool_use /
    tool_result pairs to keep consistent across a restart or a repair retry.
    """

    role: str  # "user" | "assistant"
    text: str


class PersonDraft(BaseModel):
    # extra="ignore" is load-bearing: it drops any email address or chat handle
    # the model emits despite the system prompt forbidding them, so a contact
    # can never be auto-imported. Same invariant fixture/engagement drafts hold.
    model_config = ConfigDict(extra="ignore")

    full_name: str
    role: str = ""
    is_principal: bool = False


class DepartmentDraft(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str
    mission: str = ""
    head_person_name: str = ""
    authority_level: AuthorityLevel = AuthorityLevel.PROPOSE_ONLY


class CompanyDraft(BaseModel):
    profile: CompanyProfile
    people: list[PersonDraft] = Field(default_factory=list)
    departments: list[DepartmentDraft] = Field(default_factory=list)
    confidence_notes: list[str] = Field(default_factory=list)
    summary: str = ""


class Question(BaseModel):
    question: str
    hint: str = ""


@dataclass(frozen=True)
class DraftError:
    """One validation failure, in two renderings.

    ``detail`` quotes the offending value — useful to the model in the repair
    turn, and NEVER safe in an HTTP body, because a rejected name or head
    reference is client-supplied text sitting next to the company's
    financials. ``safe`` is the fixed string the route returns.
    """

    safe: str
    detail: str


_ASK_TOOL: dict[str, Any] = {
    "name": ASK_TOOL_NAME,
    "description": (
        "Ask the user ONE clarifying question about their company. Use this "
        "only while something material is still missing — the user reviews and "
        "edits the draft before it is saved, so an early draft beats a long "
        "interview."
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
        },
    },
}

_EMIT_TOOL: dict[str, Any] = {
    "name": EMIT_TOOL_NAME,
    "description": (
        "Emit the draft company profile, leadership roster, and department "
        "list for the user to review and edit. Exactly one person must have "
        "is_principal=true (the user). Every department's head_person_name "
        "must exactly match a person's full_name or be empty. Leave anything "
        "the user did not state null or empty and name it in confidence_notes "
        "— never guess a number."
    ),
    "input_schema": {
        "type": "object",
        "required": ["profile", "people", "summary"],
        "properties": {
            "profile": {
                "type": "object",
                "description": "The company profile that grounds every answer the Executive gives.",
                "required": ["name"],
                "properties": {
                    "name": {"type": "string"},
                    "industry": {"type": "string"},
                    "stage": {
                        "type": "string",
                        "description": "e.g. 'Bootstrapped', 'Seed', 'Series B', 'Private / PE-backed'.",
                    },
                    "founding_year": {"type": "integer"},
                    "headcount": {"type": "integer"},
                    "annual_revenue_arr": {
                        "type": "number",
                        "description": "Annual revenue / ARR in USD (a number, not a string). Omit if not stated.",
                    },
                    "mission": {"type": "string"},
                    "vision": {"type": "string"},
                    "target_customer": {
                        "type": "object",
                        "properties": {
                            "profile": {"type": "string"},
                            "pain_points": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                    "competitive_landscape": {
                        "type": "object",
                        "properties": {
                            "primary_competitors": {"type": "array", "items": {"type": "string"}},
                            "competitive_advantages": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                    "strategic_priorities": {
                        "type": "object",
                        "properties": {
                            "current_year": {"type": "array", "items": {"type": "string"}},
                            "north_star_metric": {"type": "string"},
                        },
                    },
                    "culture": {
                        "type": "object",
                        "properties": {
                            "values": {"type": "array", "items": {"type": "string"}},
                            "operating_principles": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                    "financials": {
                        "type": "object",
                        "properties": {
                            "burn_rate_monthly": {"type": "number"},
                            "runway_months": {"type": "number"},
                            "key_metrics": {
                                "type": "object",
                                "description": "Flat map of metric name -> value.",
                            },
                        },
                    },
                    "org_structure": {
                        "type": "object",
                        "description": (
                            "Narrative org summary. Leave empty — it is derived "
                            "from `people` and `departments` when the user saves."
                        ),
                        "properties": {
                            "departments": {"type": "array", "items": {"type": "string"}},
                            "leadership_team": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                    "vendors": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "External vendors/suppliers the company depends on. Listing "
                            "one authorizes the Executive to watch its public status and "
                            "news without asking, so include only real dependencies."
                        ),
                    },
                    "tickers": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Public company ticker symbols that matter to this business "
                            "(a listed competitor, customer, or supplier). Same watch "
                            "semantics as `vendors`."
                        ),
                    },
                },
            },
            "people": {
                "type": "array",
                "description": (
                    "The leadership team as named by the user. EXACTLY ONE must have "
                    "is_principal=true — the user themselves. Do NOT include email "
                    "addresses or chat handles."
                ),
                "items": {
                    "type": "object",
                    "required": ["full_name"],
                    "properties": {
                        "full_name": {"type": "string"},
                        "role": {"type": "string", "description": "e.g. 'CEO', 'Head of Sales'."},
                        "is_principal": {
                            "type": "boolean",
                            "description": "True for the user who is setting this up. Exactly one person.",
                        },
                    },
                },
            },
            "departments": {
                "type": "array",
                "description": (
                    "The functions this business actually runs. Titles must be distinct. "
                    "head_person_name must match a person's full_name exactly, or be empty."
                ),
                "items": {
                    "type": "object",
                    "required": ["title"],
                    "properties": {
                        "title": {"type": "string", "description": "e.g. 'Finance', 'Customer Success'."},
                        "mission": {"type": "string", "description": "One sentence on what this function owns."},
                        "head_person_name": {"type": "string"},
                        "authority_level": {
                            "type": "string",
                            "enum": [level.value for level in AuthorityLevel],
                            "description": "Default 'propose_only' unless the user says otherwise.",
                        },
                    },
                },
            },
            "confidence_notes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "One short line per field you could not determine, saying what is missing.",
            },
            "summary": {
                "type": "string",
                "description": "One short paragraph reading the company back to the user in plain language.",
            },
        },
    },
}

# Sorted by name so the cached tool prefix is stable. ask_ < emit_.
TOOLS: list[dict[str, Any]] = sorted([_ASK_TOOL, _EMIT_TOOL], key=lambda t: str(t["name"]))


def validate_draft(draft: CompanyDraft) -> list[DraftError]:
    """Referential-integrity and bounds errors (empty list = OK).

    Field-level validation already happened in ``model_validate``; this is the
    cross-object layer the commit step relies on, mirroring
    ``fixtures.generator.validate_bundle``. Every error carries both a
    model-facing ``detail`` and a client-facing ``safe`` string — see
    ``DraftError``.
    """
    errors: list[DraftError] = []

    def add(safe: str, detail: str | None = None) -> None:
        errors.append(DraftError(safe=safe, detail=detail or safe))

    if not draft.profile.name.strip():
        add("Your company needs a name.", "profile.name is required")
    if not draft.people:
        add(
            "Add at least one person, and mark which one is you.",
            "at least one person is required",
        )

    all_names = [p.full_name.strip() for p in draft.people]
    if any(not n for n in all_names):
        add("Every person needs a name.", "every person needs a non-empty full_name")
    # Case-INSENSITIVE, matching the key save_onboarding_people upserts on.
    # A case-sensitive check let "JANE DOE" and "Jane Doe" both through, and the
    # upsert then collapsed them onto one row — last write wins, which could
    # land on the non-principal spelling and leave the company with NO principal.
    folded = [n.lower() for n in all_names]
    if len(set(folded)) != len(folded):
        dupes = sorted({n for n in all_names if folded.count(n.lower()) > 1})
        add(
            "Two people have the same name — give them distinct names.",
            f"duplicate full_name(s) in roster: {dupes}",
        )

    principals = [p for p in draft.people if p.is_principal]
    if len(principals) != 1:
        # find_principal_person() backs caller resolution, alert routing, and
        # the scheduler's principal brief. Zero or two is a real breakage.
        add(
            "Mark exactly one person as you.",
            f"exactly one person must have is_principal=true (got {len(principals)})",
        )

    if any(len(n) > MAX_NAME_CHARS for n in all_names):
        add(f"A person's name is too long (limit {MAX_NAME_CHARS} characters).")
    if any(len(p.role) > MAX_ROLE_CHARS for p in draft.people):
        add(f"A person's role is too long (limit {MAX_ROLE_CHARS} characters).")

    # The profile lands in the cached system prompt on every Executive turn, so
    # an unbounded field here is a permanent per-request cost.
    for label, value in (
        ("name", draft.profile.name),
        ("mission", draft.profile.mission),
        ("vision", draft.profile.vision),
        ("industry", draft.profile.industry),
        ("stage", draft.profile.stage),
    ):
        if len(value) > MAX_PROFILE_TEXT_CHARS:
            add(
                f"The company {label} is too long "
                f"(limit {MAX_PROFILE_TEXT_CHARS:,} characters)."
            )

    names = {n.lower() for n in all_names}
    for d in draft.departments:
        if not d.title.strip():
            add("Every department needs a name.", "every department needs a non-empty title")
        if len(d.title) > MAX_NAME_CHARS:
            add(f"A department name is too long (limit {MAX_NAME_CHARS} characters).")
        if len(d.mission) > MAX_MISSION_CHARS:
            add(
                f"A department description is too long "
                f"(limit {MAX_MISSION_CHARS:,} characters)."
            )
        if d.head_person_name and d.head_person_name.strip().lower() not in names:
            add(
                "A department is led by someone who isn't on the team list.",
                f"department '{d.title}' head_person_name "
                f"'{d.head_person_name}' is not in the roster",
            )
    # Same fallback the store uses, so two titles that both slugify to
    # nothing collide here exactly as they would on insert.
    slugs = [
        slugify(d.title, fallback=DEPARTMENT_SLUG_FALLBACK)
        for d in draft.departments
        if d.title.strip()
    ]
    if len(set(slugs)) != len(slugs):
        dupes = sorted({s for s in slugs if slugs.count(s) > 1})
        add(
            "Two departments have the same name.",
            f"duplicate department title(s): {dupes}",
        )

    return errors


def _extract_tool_call(response: Any) -> tuple[str, dict[str, Any]]:
    """Pull (tool_name, tool_input) out of an Anthropic messages response."""
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) != "tool_use":
            continue
        name = getattr(block, "name", None)
        data = getattr(block, "input", None)
        if name in (ASK_TOOL_NAME, EMIT_TOOL_NAME) and isinstance(data, dict):
            return name, data
    raise InterviewError("The setup assistant did not return a usable response.")


def _build_messages(
    transcript: list[Turn], existing_profile: CompanyProfile | None
) -> list[dict[str, Any]]:
    """Replay the transcript as plain text turns.

    An existing profile is prepended to the FIRST user turn, never put in the
    cached system block (see module docstring).
    """
    # Coalesce consecutive same-role turns. The Anthropic API rejects a
    # non-alternating sequence, and two user turns in a row are reachable in
    # practice — a draft adds no assistant turn on its own, and a turn whose
    # text is blank drops out of the replay below.
    messages: list[dict[str, Any]] = []
    for t in transcript:
        if not t.text.strip():
            continue
        if messages and messages[-1]["role"] == t.role:
            messages[-1]["content"] = f"{messages[-1]['content']}\n\n{t.text}"
            continue
        messages.append({"role": t.role, "content": t.text})
    if not messages:
        raise InterviewError("The setup session has no conversation yet.")
    # A draft records itself as an assistant turn, so the transcript can end on
    # one — e.g. "ask me more questions" then "draft again" without typing.
    # Sending that as a trailing assistant message is a prefill, which the API
    # rejects alongside a forced tool_choice, and semantically asks the model to
    # continue its own summary rather than act.
    if messages[-1]["role"] == "assistant":
        messages.append({"role": "user", "content": _CONTINUE_PROMPT})
    if existing_profile is not None and not existing_profile.is_empty():
        block = existing_profile.to_prompt_block()
        if block:
            messages[0] = {
                "role": messages[0]["role"],
                "content": (
                    "The user is re-running setup. Their current saved profile "
                    "is below — confirm or update it rather than starting over, "
                    "and do not re-ask what it already answers.\n\n"
                    f"{block}\n\n---\n\n{messages[0]['content']}"
                ),
            }
    return messages


def transcript_chars(transcript: list[Turn]) -> int:
    return sum(len(t.text) for t in transcript)


async def advance(
    transcript: list[Turn],
    *,
    existing_profile: CompanyProfile | None = None,
    force_draft: bool = False,
    questions_asked: int = 0,
    model: str | None = None,
) -> Question | CompanyDraft:
    """Run one interview turn.

    Returns a ``Question`` to put to the user, or a ``CompanyDraft`` to review.
    Raises ``InterviewError`` / ``InterviewTimeout`` — both with fixed,
    input-free messages.
    """
    from openexecutive.audit.usage import log_model_usage
    from openexecutive.providers.registry import get_provider

    settings = get_settings()
    agent = OnboardingInterviewerAgent()
    resolved_model = model if model is not None else agent.effective_model()
    system_text = agent.effective_system_prompt()
    provider = get_provider(resolved_model)

    messages = _build_messages(transcript, existing_profile)

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
            response = await asyncio.wait_for(
                _call(), timeout=settings.chat_stream_timeout_s
            )
        except TimeoutError as exc:  # asyncio.TimeoutError is an alias since 3.11
            raise InterviewTimeout(
                "The setup assistant took too long to respond. Try again."
            ) from exc
        except InterviewError:
            raise
        except Exception as exc:
            # Type name only: a provider error can quote the request body,
            # which carries the user's financials.
            logger.error("onboarding interview: provider call failed (%s)", type(exc).__name__)
            raise InterviewError(
                "The setup assistant is unavailable right now. Try again."
            ) from exc

        log_model_usage(
            response,
            model=resolved_model,
            actor=ONBOARDING_INTERVIEWER_AGENT_ID,
            iteration=attempt,
        )

        name, raw = _extract_tool_call(response)

        if name == ASK_TOOL_NAME:
            if must_draft:
                # tool_choice forced the emit tool; a question here means the
                # provider ignored it. Retry once WITH feedback — replaying the
                # identical request would just reproduce the same output.
                if attempt == 0:
                    messages = [
                        *messages,
                        {"role": "assistant", "content": raw.get("question", "")},
                        {
                            "role": "user",
                            "content": (
                                "No more questions — draft from what you have "
                                "and leave anything you don't know empty. Call "
                                f"{EMIT_TOOL_NAME} now."
                            ),
                        },
                    ]
                    continue
                raise InterviewError(
                    "The setup assistant could not produce a draft. "
                    "Try adding a bit more detail, or use the step-by-step form."
                )
            try:
                question = Question.model_validate(raw)
                if not question.question.strip():
                    # An empty question would store a blank assistant turn that
                    # drops out of the replay, breaking role alternation.
                    raise ValueError("empty question")
                return question
            except (ValidationError, ValueError) as exc:
                logger.error(
                    "onboarding interview: malformed question (%s)", type(exc).__name__
                )
                if attempt == 0:
                    continue
                raise InterviewError(
                    "The setup assistant did not return a usable response."
                ) from exc

        errors: list[str]
        try:
            draft = CompanyDraft.model_validate(raw)
        except ValidationError as exc:
            # exc is NOT interpolated into any raised message — it embeds the
            # offending values, which include the user's financials.
            logger.error(
                "onboarding interview: draft failed schema validation (%s)",
                type(exc).__name__,
            )
            errors = [str(exc)]
        else:
            draft_errors = validate_draft(draft)
            if not draft_errors:
                return draft
            errors = [e.detail for e in draft_errors]
            logger.info("onboarding interview: draft has %d consistency error(s)", len(errors))

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

        raise InterviewError(
            "The setup assistant could not produce a valid draft. "
            "Try rephrasing, or use the step-by-step form instead."
        )

    # Unreachable: every branch above returns or raises on attempt 1. Kept
    # so the function has a provable return type.
    raise InterviewError("The setup assistant could not produce a valid draft.")
