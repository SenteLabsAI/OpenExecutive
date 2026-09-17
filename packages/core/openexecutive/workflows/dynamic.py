"""The generic engine that runs a user-created workflow definition.

``DynamicWorkflow`` wraps a stored :class:`DynamicWorkflowDef` and implements
the :class:`Workflow` ABC by *interpreting* the definition's steps — there is
no per-workflow code. It reuses the exact building blocks the hand-written
workflows use (``load_or_create_profile``, ``retrieve``,
``route_to_specialist``) so a dynamic workflow behaves like a built-in: same
SSE events, same artifact shape, same run persistence.

Step interpretation:
- ``specialist``  → consult the named specialist with the rendered goal
                    (and optional RAG), store the output.
- ``approval_gate`` → yield a ``WaitForHumanEvent`` (the HTTP runner
                    checkpoints and pauses; see the resume limitation note on
                    ``ApprovalGateStepSpec``).
- ``synthesis``   → assemble prior outputs into the final Markdown artifact,
                    either by concatenation or via one synthesis consult.
"""
from __future__ import annotations

import string
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel, Field, create_model

from openexecutive.knowledge.retriever import retrieve
from openexecutive.knowledge.store import ChromaDBStore
from openexecutive.memory.company_profile import CompanyProfile
from openexecutive.onboarding.profile_builder import load_or_create_profile
from openexecutive.orchestrator.router import SPECIALIST_REGISTRY, route_to_specialist
from openexecutive.workflows.base import (
    Workflow,
    WorkflowEvent,
    WorkflowMeta,
    WorkflowStepDef,
)
from openexecutive.workflows.dynamic_models import (
    ApprovalGateStepSpec,
    DynamicWorkflowDef,
    SpecialistStepSpec,
    SynthesisStepSpec,
)
from openexecutive.workflows.wait_for_human import WaitForHumanEvent

# Synthetic first step every dynamic workflow runs — loads the company profile
# and surfaces a "Load context" row in the UI, mirroring the built-ins.
_CONTEXT_STEP_ID = "context"


class _FlatFormatter(string.Formatter):
    """A Formatter that only substitutes flat ``{name}`` placeholders.

    Defense-in-depth alongside ``validate_definition``: it refuses attribute
    access (``{x.__class__}``), indexing (``{x[0]}``), and positional fields
    (``{0}`` / ``{}``), so a goal template can never walk object internals —
    even a definition that somehow bypassed validation. Disallowed forms raise
    KeyError, which the engine surfaces as a step error.
    """

    def get_field(self, field_name: str, args: Any, kwargs: Any) -> Any:
        if (
            not field_name
            or field_name[0].isdigit()
            or "." in field_name
            or "[" in field_name
        ):
            raise KeyError(field_name)
        return kwargs[field_name], field_name


_FLAT_FORMATTER = _FlatFormatter()


def _render(template: str, values: dict[str, Any]) -> str:
    """Substitute only flat declared-field placeholders into ``template``."""
    return _FLAT_FORMATTER.vformat(template, (), values)


class DynamicWorkflow(Workflow):
    """A ``Workflow`` backed by a stored :class:`DynamicWorkflowDef`."""

    def __init__(self, defn: DynamicWorkflowDef) -> None:
        self._defn = defn
        self.name = defn.name
        self.title = defn.title
        self.description = defn.description
        self.section = defn.section
        self.estimated_minutes = defn.estimated_minutes
        self._input_model: type[BaseModel] | None = None

    # -- ABC ---------------------------------------------------------------

    def input_model(self) -> type[BaseModel]:
        """Build a Pydantic model from the declared input fields (cached).

        Every field is a string (dynamic workflows take free-text inputs).
        Required fields use ``...``; optional fields default to ``""``.
        """
        if self._input_model is not None:
            return self._input_model
        field_defs: dict[str, Any] = {}
        for f in self._defn.input_fields:
            default = ... if f.required else ""
            field_defs[f.name] = (
                str,
                Field(default, title=f.label, description=f.description),
            )
        model: type[BaseModel] = create_model(  # type: ignore[call-overload]
            f"DynamicInput_{self.name}", **field_defs
        )
        self._input_model = model
        return model

    def steps(self) -> list[WorkflowStepDef]:
        out = [
            WorkflowStepDef(
                id=_CONTEXT_STEP_ID,
                title="Load context",
                description="Pull the company profile and prepare the run.",
            )
        ]
        for step in self._defn.steps:
            out.append(
                WorkflowStepDef(id=step.id, title=step.title, description=step.description)
            )
        return out

    def meta(self) -> WorkflowMeta:
        m = super().meta()
        return m.model_copy(update={"is_custom": True})

    def sample_inputs(self) -> dict[str, Any] | None:
        if not self._defn.input_fields:
            return None
        # No synthetic content — just blank scaffolding the user fills in.
        return {f.name: "" for f in self._defn.input_fields}

    async def run(
        self,
        inputs: BaseModel,
        store: ChromaDBStore,
    ) -> AsyncIterator[WorkflowEvent]:
        values = inputs.model_dump()

        # --- pre-flight ---
        # Definitions are validated on create/update, not on load, so a stored
        # definition can outlive a specialist (the `talent` key was removed).
        # `route_to_specialist` would return the string "Unknown specialist: …"
        # as a step's OUTPUT — or, via a synthesis step's own `specialist`, as
        # the whole artifact. Check every step up front so the run fails
        # before any approval gate is raised or any specialist call is paid for.
        stale = _stale_specialist_steps(self._defn)
        if stale:
            yield WorkflowEvent(
                type="error",
                message=(
                    "workflow names a specialist that no longer exists — "
                    + "; ".join(
                        f"step {sid!r} uses {name!r}" for sid, name in stale
                    )
                    + " — edit the workflow to use a current specialist"
                ),
            )
            return

        # --- context ---
        yield WorkflowEvent(
            type="step_start", step_id=_CONTEXT_STEP_ID, step_title="Load context"
        )
        profile = load_or_create_profile()
        company_block = _company_context_block(profile)
        yield WorkflowEvent(
            type="step_done",
            step_id=_CONTEXT_STEP_ID,
            summary=f"Loaded profile for {profile.name or 'company'}.",
        )

        outputs: dict[str, tuple[str, str]] = {}  # step_id -> (title, output)

        for step in self._defn.steps:
            if isinstance(step, SpecialistStepSpec):
                yield WorkflowEvent(
                    type="step_start", step_id=step.id, step_title=step.title
                )
                try:
                    goal = _render(step.goal, values)
                    rag = ""
                    if step.rag_query:
                        rag = retrieve(
                            query=_render(step.rag_query, values),
                            specialist_name=step.specialist,
                            n_builtin=5,
                            n_company=3,
                            store=store,
                        )
                except (KeyError, IndexError) as exc:
                    yield WorkflowEvent(
                        type="error",
                        message=f"step {step.id!r} placeholder error: {exc}",
                    )
                    return
                result = await route_to_specialist(
                    specialist_name=step.specialist,
                    query=goal,
                    context=company_block,
                    retrieved_knowledge=rag,
                )
                outputs[step.id] = (step.title, result)
                yield WorkflowEvent(
                    type="step_done", step_id=step.id, summary=_first_line(result)
                )

            elif isinstance(step, ApprovalGateStepSpec):
                yield WorkflowEvent(
                    type="step_start", step_id=step.id, step_title=step.title
                )
                try:
                    question = _render(step.question, values)
                except (KeyError, IndexError) as exc:
                    yield WorkflowEvent(
                        type="error",
                        message=f"step {step.id!r} placeholder error: {exc}",
                    )
                    return
                # The HTTP runner persists this as a checkpoint and pauses the
                # run (status='awaiting_human'). The generator does not
                # auto-resume past the gate (deferred upstream).
                # WaitForHumanEvent is a sentinel the runner isinstance-checks;
                # it isn't a WorkflowEvent, hence the typed-yield ignore.
                gate = WaitForHumanEvent(
                    person_id=step.person_id,
                    question=question,
                    timeout_hours=step.timeout_hours,
                    on_timeout=step.on_timeout,
                    expected_reply_shape=step.expected_reply_shape,
                    context_summary=f"Approval gate in workflow {self.title!r}",
                )
                yield gate  # type: ignore[misc]
                return

            elif isinstance(step, SynthesisStepSpec):
                yield WorkflowEvent(
                    type="step_start", step_id=step.id, step_title=step.title
                )
                artifact = await _synthesize(step, self.title, outputs, company_block)
                yield WorkflowEvent(
                    type="step_done",
                    step_id=step.id,
                    summary=f"Assembled {len(artifact)} characters.",
                )
                yield WorkflowEvent(type="artifact", content=artifact)
                return


# ---------------------------------------------------------------------------
# Internal helpers (mirror the private helpers in the hand-written workflows)
# ---------------------------------------------------------------------------


def _stale_specialist_steps(defn: DynamicWorkflowDef) -> list[tuple[str, str]]:
    """(step_id, specialist) for every step that would consult a specialist
    missing from the live registry. A synthesis step only consults one when it
    has `instructions`."""
    stale: list[tuple[str, str]] = []
    for step in defn.steps:
        if isinstance(step, ApprovalGateStepSpec):
            continue
        if isinstance(step, SynthesisStepSpec) and not step.instructions:
            continue
        if step.specialist not in SPECIALIST_REGISTRY:
            stale.append((step.id, step.specialist))
    return stale


async def _synthesize(
    step: SynthesisStepSpec,
    workflow_title: str,
    outputs: dict[str, tuple[str, str]],
    company_block: str,
) -> str:
    """Build the final Markdown artifact from prior step outputs."""
    joined = "\n\n".join(
        _ensure_heading(text, f"## {title}") for title, text in outputs.values()
    )
    if not step.instructions:
        body = joined or "_(No content was generated.)_"
        return f"# {workflow_title}\n\n{body}\n"
    # Run one synthesis consult over the drafts.
    result = await route_to_specialist(
        specialist_name=step.specialist,
        query=(
            f"{step.instructions}\n\n"
            "Assemble the following section drafts into one coherent Markdown "
            f"document titled '{workflow_title}'. Preserve substance; remove "
            "redundancy.\n\nSection drafts:\n\n" + joined
        ),
        context=company_block,
    )
    return result.strip() + "\n"


def _company_context_block(profile: CompanyProfile | None) -> str:
    if profile is None or profile.is_empty():
        return ""
    return profile.to_prompt_block()


def _first_line(text: str, max_len: int = 140) -> str:
    if not text:
        return "(empty)"
    line = text.strip().split("\n", 1)[0].lstrip("# ").strip()
    return line[: max_len - 1] + "…" if len(line) > max_len else line


def _ensure_heading(text: str, expected_heading: str) -> str:
    stripped = text.strip()
    if not stripped:
        return f"{expected_heading}\n\n_(No content generated for this section.)_"
    first_line = stripped.split("\n", 1)[0]
    # Any Markdown heading (#, ##, ###…) means the section already self-labels;
    # don't prepend our own and invert the hierarchy (e.g. ## above a #).
    if first_line.lstrip().startswith("#"):
        return stripped
    return f"{expected_heading}\n\n{stripped}"
