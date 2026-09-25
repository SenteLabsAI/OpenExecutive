"""Data model + validation for user-created ("dynamic") workflows.

A dynamic workflow is a *stored definition* (JSON, persisted by
``dynamic_store``) that the generic ``DynamicWorkflow`` engine
(``workflows/dynamic.py``) interprets at runtime. It implements the same
``Workflow`` ABC the hand-written built-ins do, so the HTTP run path, SSE
streaming, run persistence, and the ``/jobs/[name]`` form renderer all work
unchanged.

The definition is intentionally *data, not code*: steps are a small
discriminated union (specialist consult / human-approval gate / synthesis),
input fields are free-text declarations, and an optional cadence string
re-uses the existing scheduler. No arbitrary code is ever stored or executed.

``validate_definition`` enforces the structural rules the engine relies on
(exactly one terminal synthesis step, valid specialists, placeholders that
reference declared fields only, rostered approval/cadence people, parseable
cadence). It returns a list of human-readable errors so both the chat
authoring tools and the HTTP CRUD routes can surface the same messages.
"""
from __future__ import annotations

import re
import string
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from openexecutive.workflows.base import WorkflowSection

# A dynamic workflow name / input-field name: snake_case, 3-49 chars, leads
# with a letter. Matches the convention built-in workflow registry keys use
# (e.g. "quarterly_plan") and keeps the value safe to interpolate into URLs,
# SQL parameters, and Pydantic field names.
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,48}$")

# The specialists a step may consult. Kept as a module constant (rather than
# importing SPECIALIST_REGISTRY at module load) to avoid an import cycle
# workflows -> orchestrator -> workflows; validate_definition cross-checks the
# live registry lazily so a newly added specialist is picked up automatically.
_FALLBACK_SPECIALISTS = frozenset(
    {"cso", "cfo", "chro", "gc", "coo", "cmo", "cpo", "sales", "board_comms", "triage"}
)

# Bounds that keep a single definition (and its runs) cheap to store, render,
# and reason about. A real executive job is a handful of steps over a handful
# of inputs; these caps are generous headroom, not expected limits, and exist
# mainly as a DoS / runaway-cost guard on user-authored definitions.
_MAX_STEPS = 12
_MAX_INPUT_FIELDS = 16
_MAX_GOAL_CHARS = 4000

# Action steps: the tool allowlist IS the user's approval, so it is kept short
# enough to read on the review card. Names are the exact tool names the engine
# will call — MCP tools are ``server__tool``; built-ins are ``oe__*``.
TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MAX_STEP_TOOLS = 16
MAX_TOOL_CALLS_CAP = 50
DEFAULT_MAX_TOOL_CALLS = 20
# The gateway's own meta-tools. A step naming one of these could reach every
# downstream tool (call_tool) or attach a new server (load_mcp_server), which
# would make the per-step allowlist meaningless.
FORBIDDEN_STEP_TOOLS = frozenset({"search_tools", "call_tool", "load_mcp_server"})


class InputFieldSpec(BaseModel):
    """One free-text input field on a dynamic workflow's form.

    All dynamic-workflow inputs are strings (matching the built-ins, which use
    free-text fields). ``required`` maps to a Pydantic required field;
    optional fields default to the empty string.
    """

    name: str = Field(description="snake_case identifier, referenced as {name} in step goals")
    label: str = Field(description="Human-readable label shown on the form")
    description: str = ""
    required: bool = True
    multiline: bool = False


class SpecialistStepSpec(BaseModel):
    """Consult one specialist with a goal prompt, optionally grounded by RAG."""

    kind: Literal["specialist"] = "specialist"
    id: str
    title: str
    description: str = ""
    specialist: str = Field(description="Specialist registry key, e.g. 'cso'")
    goal: str = Field(
        description=(
            "Prompt for the specialist. May reference input fields with "
            "{field_name} placeholders."
        ),
    )
    rag_query: str = Field(
        default="",
        description="Optional retrieval query; when set, RAG chunks are fetched and passed in.",
    )
    playbook: str = Field(
        default="",
        description=(
            "Optional name of an existing playbook (skill) the step follows — its "
            "steps are appended to the goal. Leave empty unless one clearly fits."
        ),
    )


class ApprovalGateStepSpec(BaseModel):
    """Pause the run for a human decision (the wait_for_human primitive).

    The question is delivered to ``person_id`` on their preferred channel and
    the run parks at ``awaiting_human``. When they answer, the run CONTINUES
    from the next step — a gate may sit anywhere in the sequence, and a
    definition may have more than one.

    What the answer does:
    - ``approve`` (and the ``auto_proceed`` a timeout synthesises) → the
      remaining steps run, and the decision is recorded as this step's own
      output, so it appears as a section in the artifact.
    - ``reject`` / ``defer`` → the run ends at ``error`` with the person's
      note. Everything after a sign-off exists to act on a yes.

    ``on_timeout`` applies when nobody answers within ``timeout_hours``:
    ``escalate`` files a briefing alert and ends the run at ``timed_out``,
    ``auto_proceed`` continues as though approved, ``fail`` ends it at
    ``error``.

    Not allowed in a cadence-enabled workflow: a scheduled fire has no
    interactive human to ask (``validate_definition`` rejects it).
    """

    kind: Literal["approval_gate"] = "approval_gate"
    id: str
    title: str
    description: str = ""
    person_id: int = Field(description="Rostered Person.id to ask for the decision")
    question: str = Field(description="Approval question; may reference {field_name} placeholders")
    timeout_hours: int = 48
    on_timeout: Literal["escalate", "auto_proceed", "fail"] = "escalate"
    expected_reply_shape: Literal[
        "approve_reject", "free_text", "numeric", "document"
    ] = "approve_reject"


class SynthesisStepSpec(BaseModel):
    """Assemble prior step outputs into the final Markdown artifact.

    With no ``instructions``, prior step outputs are concatenated under their
    titles. With ``instructions``, the named specialist is consulted once over
    the joined drafts to produce a polished artifact.
    """

    kind: Literal["synthesis"] = "synthesis"
    id: str
    title: str
    description: str = ""
    instructions: str = ""
    specialist: str = "cso"


class ActionStepSpec(BaseModel):
    """Get something done with tools (send, update, file, look up…).

    An agent works toward ``goal`` using ONLY the tools named in ``tools`` —
    the allowlist the user approved when they created the workflow — for at
    most ``max_tool_calls`` calls. Its report of what it did becomes the
    step's output. See ``workflows/action_step.py``.
    """

    kind: Literal["action"] = "action"
    id: str
    title: str
    description: str = ""
    goal: str
    tools: list[str] = Field(default_factory=list)
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS


StepSpec = Annotated[
    SpecialistStepSpec | ApprovalGateStepSpec | SynthesisStepSpec | ActionStepSpec,
    Field(discriminator="kind"),
]


class DynamicWorkflowDef(BaseModel):
    """A complete, persisted user-created workflow definition."""

    name: str = Field(description="Registry key, snake_case, unique, not a built-in")
    title: str
    description: str = ""
    section: WorkflowSection = WorkflowSection.OPERATING
    estimated_minutes: int = 4
    input_fields: list[InputFieldSpec] = Field(default_factory=list)
    steps: list[StepSpec] = Field(default_factory=list)
    # Optional recurrence (cadence DSL: daily@HH:MM / weekly@DOW@HH:MM /
    # quarterly@DD-HH:MM). When set, the scheduler auto-runs the workflow and
    # DMs the artifact to cadence_person_id.
    cadence: str | None = None
    cadence_person_id: int | None = None
    is_active: bool = True
    created_at: str = ""
    updated_at: str = ""


def _placeholders(text: str) -> list[str]:
    """Return the raw ``{field_name}`` tokens referenced in text.

    Returns the field_name exactly as written — including any attribute
    (``topic.x``) or index (``topic[0]``) access and positional/empty forms
    (``0`` / ``""``) — so the validator can REJECT those rather than silently
    stripping them. Malformed format strings (e.g. an unbalanced brace) raise
    ValueError in the parser, which the caller treats as a validation error.
    """
    out: list[str] = []
    for _literal, field_name, _format_spec, _conversion in string.Formatter().parse(text):
        if field_name is not None:
            out.append(field_name)
    return out


def _valid_specialists() -> frozenset[str]:
    """Live specialist keys, falling back to the known set on import trouble."""
    try:
        from openexecutive.orchestrator.router import SPECIALIST_REGISTRY

        return frozenset(SPECIALIST_REGISTRY.keys())
    except Exception:  # pragma: no cover - defensive; registry import is stable
        return _FALLBACK_SPECIALISTS


def _person_exists(person_id: int) -> bool:
    """True if person_id resolves to a non-archived roster Person."""
    try:
        from openexecutive.people.store import get_person

        person = get_person(person_id)
        return person is not None and not getattr(person, "archived", False)
    except Exception:
        return False


def _check_action_tools(step: ActionStepSpec) -> list[str]:
    """Shape rules for an action step's tool allowlist (existence is async —
    see ``tool_catalog.validate_tools_available``)."""
    errors: list[str] = []
    if not step.tools:
        errors.append(f"action step {step.id!r} must name at least one tool")
    if len(step.tools) > _MAX_STEP_TOOLS:
        errors.append(f"action step {step.id!r} names too many tools (max {_MAX_STEP_TOOLS})")
    if len(set(step.tools)) != len(step.tools):
        errors.append(f"action step {step.id!r} lists a tool more than once")
    for tool in step.tools:
        if not TOOL_NAME_RE.match(tool):
            errors.append(f"action step {step.id!r} has an invalid tool name {tool!r}")
        elif tool in FORBIDDEN_STEP_TOOLS:
            errors.append(
                f"action step {step.id!r} may not use {tool!r} — name the specific "
                "tools the step needs instead"
            )
    if not 1 <= step.max_tool_calls <= MAX_TOOL_CALLS_CAP:
        errors.append(
            f"action step {step.id!r} max_tool_calls must be 1-{MAX_TOOL_CALLS_CAP}"
        )
    return errors


def _playbook_exists(name: str) -> bool:
    """True when `name` is a playbook in effect (hidden built-ins don't count)."""
    from openexecutive.knowledge.skills import SkillParseError
    from openexecutive.knowledge.skills_repo import SkillNotFoundError, get_skill

    try:
        get_skill(name)
    except (SkillNotFoundError, SkillParseError):
        return False
    return True


def validate_definition(defn: DynamicWorkflowDef) -> list[str]:
    """Return a list of human-readable validation errors (empty == valid).

    Checks every structural invariant the ``DynamicWorkflow`` engine depends
    on. Person/registry/cadence checks query live state, so this is also the
    save-time gate (not just a shape check).
    """
    errors: list[str] = []

    # --- name ---
    if not _NAME_RE.match(defn.name):
        errors.append(
            f"name {defn.name!r} must be snake_case, 3-49 chars, starting with a letter"
        )
    else:
        from openexecutive.workflows import WORKFLOW_REGISTRY

        if defn.name in WORKFLOW_REGISTRY:
            errors.append(f"name {defn.name!r} collides with a built-in workflow")

    if not defn.title.strip():
        errors.append("title must not be empty")
    if defn.estimated_minutes < 1 or defn.estimated_minutes > 120:
        errors.append("estimated_minutes must be between 1 and 120")

    # --- input fields ---
    if len(defn.input_fields) > _MAX_INPUT_FIELDS:
        errors.append(f"too many input fields (max {_MAX_INPUT_FIELDS})")
    field_names: set[str] = set()
    for f in defn.input_fields:
        if not _NAME_RE.match(f.name):
            errors.append(f"input field name {f.name!r} must be snake_case (3-49 chars)")
        if f.name in field_names:
            errors.append(f"duplicate input field name {f.name!r}")
        field_names.add(f.name)
        if not f.label.strip():
            errors.append(f"input field {f.name!r} must have a label")

    # --- steps ---
    if not defn.steps:
        errors.append("workflow must have at least one step")
        return errors  # nothing more to check without steps
    if len(defn.steps) > _MAX_STEPS:
        errors.append(f"too many steps (max {_MAX_STEPS})")

    step_ids: set[str] = set()
    synthesis_count = 0
    specialist_step_count = 0
    valid_specialists = _valid_specialists()

    for idx, step in enumerate(defn.steps):
        if not _NAME_RE.match(step.id):
            errors.append(f"step id {step.id!r} must be snake_case (3-49 chars)")
        if step.id in step_ids:
            errors.append(f"duplicate step id {step.id!r}")
        step_ids.add(step.id)
        if not step.title.strip():
            errors.append(f"step {step.id!r} must have a title")

        if isinstance(step, SpecialistStepSpec):
            specialist_step_count += 1
            if step.specialist not in valid_specialists:
                errors.append(
                    f"step {step.id!r} names unknown specialist {step.specialist!r} "
                    f"(valid: {sorted(valid_specialists)})"
                )
            if not step.goal.strip():
                errors.append(f"step {step.id!r} must have a goal")
            if len(step.goal) > _MAX_GOAL_CHARS:
                errors.append(f"step {step.id!r} goal exceeds {_MAX_GOAL_CHARS} chars")
            errors.extend(_check_placeholders(step.id, step.goal, field_names))
            if step.rag_query:
                errors.extend(_check_placeholders(step.id, step.rag_query, field_names))
            if step.playbook and not _playbook_exists(step.playbook):
                errors.append(
                    f"step {step.id!r} names unknown playbook {step.playbook!r} "
                    "(see the Playbooks tab for names)"
                )

        elif isinstance(step, ApprovalGateStepSpec):
            if not step.question.strip():
                errors.append(f"approval gate {step.id!r} must have a question")
            errors.extend(_check_placeholders(step.id, step.question, field_names))
            if step.timeout_hours < 1 or step.timeout_hours > 720:
                errors.append(f"approval gate {step.id!r} timeout_hours must be 1-720")
            if not _person_exists(step.person_id):
                errors.append(
                    f"approval gate {step.id!r} person_id {step.person_id} is not a "
                    "non-archived roster person"
                )

        elif isinstance(step, SynthesisStepSpec):
            synthesis_count += 1
            if idx != len(defn.steps) - 1:
                errors.append("the synthesis step must be the last step")
            if step.specialist not in valid_specialists:
                errors.append(
                    f"synthesis step {step.id!r} names unknown specialist "
                    f"{step.specialist!r}"
                )
            if step.instructions:
                errors.extend(_check_placeholders(step.id, step.instructions, field_names))

        elif isinstance(step, ActionStepSpec):
            specialist_step_count += 1
            if not step.goal.strip():
                errors.append(f"step {step.id!r} must have a goal")
            if len(step.goal) > _MAX_GOAL_CHARS:
                errors.append(f"step {step.id!r} goal exceeds {_MAX_GOAL_CHARS} chars")
            errors.extend(_check_placeholders(step.id, step.goal, field_names))
            errors.extend(_check_action_tools(step))

    if synthesis_count != 1:
        errors.append("workflow must have exactly one synthesis step (and it must be last)")
    if specialist_step_count < 1:
        errors.append(
            "workflow must have at least one specialist or action step before synthesis"
        )

    # --- cadence ---
    if defn.cadence:
        from datetime import UTC, datetime

        from openexecutive.departments.cadence import _parse_cadence_spec

        if _parse_cadence_spec(defn.cadence, datetime.now(UTC)) is None:
            errors.append(
                f"cadence {defn.cadence!r} is not a valid spec "
                "(daily@HH:MM / weekly@DOW@HH:MM / quarterly@DD-HH:MM)"
            )
        if defn.cadence_person_id is None:
            errors.append("cadence requires cadence_person_id (who receives the artifact)")
        elif not _person_exists(defn.cadence_person_id):
            errors.append(
                f"cadence_person_id {defn.cadence_person_id} is not a non-archived "
                "roster person"
            )
        # A cadence run builds an all-empty input instance, so required fields
        # would fail validation at fire time.
        if any(f.required for f in defn.input_fields):
            errors.append(
                "a cadence-enabled workflow must not have required input fields "
                "(cadence runs supply no inputs)"
            )
        # A scheduled run cannot pause for a human: the scheduler ignores the
        # gate and the synthesis step after it never runs, so the run would
        # silently re-fire and bill specialists with no artifact. Forbid gates
        # in cadence-enabled workflows outright.
        if any(isinstance(s, ApprovalGateStepSpec) for s in defn.steps):
            errors.append(
                "a cadence-enabled workflow must not contain approval-gate steps "
                "(scheduled runs cannot wait for a human)"
            )
    elif defn.cadence_person_id is not None:
        errors.append("cadence_person_id is set but cadence is not")

    return errors


def _check_placeholders(step_id: str, text: str, field_names: set[str]) -> list[str]:
    """Validate every ``{placeholder}`` in text names a declared input field.

    Rejects attribute/index access (``{topic.__class__}``, ``{topic[0]}``) and
    positional/empty placeholders (``{0}`` / ``{}``). This keeps the engine's
    ``str.format`` rendering from being abused to walk object internals — the
    placeholder set is the security boundary, so it must be honest about what
    it allows.
    """
    try:
        used = _placeholders(text)
    except (ValueError, IndexError) as exc:
        return [f"step {step_id!r} has a malformed placeholder: {exc}"]
    errors: list[str] = []
    for name in used:
        if name == "" or name.isdigit():
            errors.append(
                f"step {step_id!r} uses a positional/empty placeholder "
                f"{{{name}}}; use named {{field}} placeholders only"
            )
        elif "." in name or "[" in name:
            errors.append(
                f"step {step_id!r} placeholder {{{name}}} uses attribute/index "
                "access, which is not allowed — reference a declared field directly"
            )
        elif name not in field_names:
            errors.append(
                f"step {step_id!r} references unknown input field {name!r} "
                f"(declared: {sorted(field_names)})"
            )
    return errors
