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
- ``approval_gate`` → yield a ``WaitForHumanEvent`` carrying a
                    ``WorkflowResumeState``. The caller checkpoints and pauses;
                    once the human answers, ``resume()`` re-enters at the next
                    step (see "Pause and resume" below).
- ``synthesis``   → assemble prior outputs into the final Markdown artifact,
                    either by concatenation or via one synthesis consult.
- ``action``      → run the ``workflow_actor`` agent with ONLY the step's
                    approved tools (``workflows/action_step.py``); its report
                    of what it did becomes the step's output.

Pause and resume
----------------
``run()`` and ``resume()`` share one step loop, ``_run_steps``, which takes
the index to start at and the outputs accumulated so far. That is the whole
mechanism: nothing serialises a generator frame. At a gate the engine hands
the caller a ``WorkflowResumeState`` (workflow name, gate id and index, next
index, completed outputs); on resume the caller hands it back, the engine
re-validates it against the live definition, records the human's decision as
the gate step's own output, and runs the loop from the next index.

A second gate needs no special handling — it is simply another pause raised
from the same loop, with a fresh payload.
"""
from __future__ import annotations

import hashlib
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
    ActionStepSpec,
    ApprovalGateStepSpec,
    DynamicWorkflowDef,
    SpecialistStepSpec,
    SynthesisStepSpec,
)
from openexecutive.workflows.tool_catalog import unavailable_step_tools
from openexecutive.workflows.wait_for_human import (
    CONTINUE_DECISIONS,
    DECISION_VERBS,
    NON_APPROVAL_SHAPES,
    WaitForHumanEvent,
    WaitForHumanResolution,
    WorkflowResumeState,
)

# Synthetic first step every dynamic workflow runs — loads the company profile
# and surfaces a "Load context" row in the UI, mirroring the built-ins.
_CONTEXT_STEP_ID = "context"

# Cap on the approver's own text carried into the artifact — and therefore
# into the synthesis specialist's prompt. A decision needs a sentence or two
# of reasoning; anything longer is not a sign-off note.
_MAX_REPLY_CHARS = 500


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
            yield _stale_error(stale)
            return
        # Same promise for action steps: a tool that vanished (server removed,
        # deny-listed, gateway down) fails the run BEFORE any step acts, not
        # after earlier steps already sent or changed something.
        missing_tools = await unavailable_step_tools(self._defn)
        if missing_tools:
            yield _missing_tools_error(missing_tools)
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

        async for event in self._run_steps(
            start_index=0,
            values=values,
            company_block=company_block,
            outputs={},
            store=store,
        ):
            yield event

    async def resume(
        self,
        *,
        inputs: BaseModel,
        state: WorkflowResumeState,
        resolution: WaitForHumanResolution,
        store: ChromaDBStore,
    ) -> AsyncIterator[WorkflowEvent]:
        """Continue a run that paused at an approval gate, now answered.

        Called by ``resumer._execute_resume`` in a background task, long after
        the HTTP request that started the run is gone. Yields the same event
        types ``run()`` does, so every consumer of a workflow stream works
        unchanged — including a second ``WaitForHumanEvent`` if the definition
        has another gate further on.
        """
        values = inputs.model_dump()

        # Same pre-flight as run(): the pause may have outlived a specialist.
        # A resumed run has already been paid for up to the gate, but finishing
        # it with "Unknown specialist: …" as a section is worse than failing.
        stale = _stale_specialist_steps(self._defn)
        if stale:
            yield _stale_error(stale)
            return
        missing_tools = await unavailable_step_tools(self._defn)
        if missing_tools:
            yield _missing_tools_error(missing_tools)
            return

        gate = self._gate_for_resume(state)
        if gate is None:
            # `upsert_definition` overwrites by name, so the definition can
            # have been edited while the run sat at the gate — which silently
            # re-points the stored index at a different step. Resuming anyway
            # would skip or repeat work and produce a plausible-looking wrong
            # artifact, so refuse and say why.
            yield WorkflowEvent(
                type="error",
                message=(
                    "the workflow definition changed while this run was "
                    "awaiting approval, so it can no longer be resumed safely "
                    f"(expected an approval gate {state.gate_step_id!r} at "
                    f"step {state.gate_step_index}) — re-run the workflow"
                ),
            )
            return

        # Re-derive the company block rather than persisting it: it is a pure
        # function of the profile, and a profile edited during the pause should
        # be the one the remaining steps see.
        yield WorkflowEvent(
            type="step_start", step_id=_CONTEXT_STEP_ID, step_title="Load context"
        )
        profile = load_or_create_profile()
        company_block = _company_context_block(profile)
        yield WorkflowEvent(
            type="step_done",
            step_id=_CONTEXT_STEP_ID,
            summary=f"Reloaded profile for {profile.name or 'company'}.",
        )

        decision = str(resolution.parsed_decision.get("decision") or "")
        if not _may_continue(gate, decision):
            # Not a recognisable yes, so stop rather than produce the
            # deliverable they did not approve. Everything after the gate
            # exists to act on an approval. The full decision — including a
            # verdict we did not recognise — stays in `resolution_json`.
            note = str(resolution.parsed_decision.get("note") or "").strip()
            verb = DECISION_VERBS.get(decision) or (
                f"Stopped on an unrecognised decision {decision!r}"
                if decision
                else "Stopped with no decision recorded"
            )
            yield WorkflowEvent(
                type="error",
                message=(
                    f"{verb} at the {gate.title!r} approval gate"
                    + (f": {note}" if note else "")
                ),
            )
            return

        # Give the synthesis step the decision as a readable section, and
        # close the gate's step in the event stream for symmetry with a fresh
        # run. Nothing consumes these events on the resume path today — the
        # original SSE socket closed hours ago and the run-detail page polls
        # JSON — but `resume()` yields the same shape `run()` does so any
        # future consumer (a reconnecting stream) needs no special case.
        outputs: dict[str, tuple[str, str]] = dict(state.outputs)
        outputs[gate.id] = (gate.title, _format_resolution(gate, resolution))
        yield WorkflowEvent(
            type="step_done",
            step_id=gate.id,
            summary=_first_line(outputs[gate.id][1]),
        )

        async for event in self._run_steps(
            # DERIVED from the validated gate index, never read from the
            # payload: a stored cursor is a second source of truth, and the
            # copy an attacker can edit would be the one we obeyed.
            start_index=state.gate_step_index + 1,
            values=values,
            company_block=company_block,
            outputs=outputs,
            store=store,
        ):
            yield event

    # -- step loop ---------------------------------------------------------

    def _gate_for_resume(
        self, state: WorkflowResumeState
    ) -> ApprovalGateStepSpec | None:
        """The gate this payload paused at, or None if it no longer matches.

        Every field is checked against the LIVE definition because all of them
        can go stale during a pause: the payload may predate this build
        (`version`/`engine`), name a workflow this object isn't, or point at an
        index the definition no longer has — or has, but now holds a different
        step.
        """
        if state.version != 1 or state.engine != "dynamic":
            return None
        if state.workflow_name != self.name:
            return None
        if not 0 <= state.gate_step_index < len(self._defn.steps):
            return None
        step = self._defn.steps[state.gate_step_index]
        if not isinstance(step, ApprovalGateStepSpec) or step.id != state.gate_step_id:
            return None
        # The gate being where we left it is NOT enough. Someone can keep the
        # gate identical and replace every step after it while the run is
        # parked, so the approver's yes lands on work they never saw. Pin the
        # whole list. (An empty fingerprint is a payload written before this
        # check existed; refuse it rather than grandfather a gap in the very
        # control this is protecting.)
        if state.steps_fingerprint != _steps_fingerprint(self._defn):
            return None
        return step

    async def _run_steps(
        self,
        *,
        start_index: int,
        values: dict[str, Any],
        company_block: str,
        outputs: dict[str, tuple[str, str]],
        store: ChromaDBStore,
    ) -> AsyncIterator[WorkflowEvent]:
        """Interpret steps from ``start_index`` on.

        The single place step semantics live, so ``run()`` and ``resume()``
        cannot drift apart — and so a gate reached on a resumed run raises the
        same pause, with the same payload shape, as one reached on a fresh run.
        """
        for index in range(start_index, len(self._defn.steps)):
            step = self._defn.steps[index]

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
                # The caller delivers the question, checkpoints the run
                # (status='awaiting_human') and stops. `resume_state` is what
                # lets it be picked up again — without it the gate is
                # pause-only and the steps below never run.
                # WaitForHumanEvent is a sentinel the caller isinstance-checks;
                # it isn't a WorkflowEvent, hence the typed-yield ignore.
                gate = WaitForHumanEvent(
                    person_id=step.person_id,
                    question=question,
                    timeout_hours=step.timeout_hours,
                    on_timeout=step.on_timeout,
                    expected_reply_shape=step.expected_reply_shape,
                    context_summary=f"Approval gate in workflow {self.title!r}",
                    resume_state=WorkflowResumeState(
                        workflow_name=self.name,
                        gate_step_id=step.id,
                        gate_step_index=index,
                        steps_fingerprint=_steps_fingerprint(self._defn),
                        # Copy: the caller serialises this after we return, and
                        # a live reference would keep mutating under it if the
                        # loop ever continued.
                        outputs=dict(outputs),
                    ),
                )
                yield gate  # type: ignore[misc]
                return

            elif isinstance(step, ActionStepSpec):
                from openexecutive.workflows.action_step import run_action_step

                yield WorkflowEvent(
                    type="step_start", step_id=step.id, step_title=step.title
                )
                try:
                    goal = _render(step.goal, values)
                except (KeyError, IndexError) as exc:
                    yield WorkflowEvent(
                        type="error",
                        message=f"step {step.id!r} placeholder error: {exc}",
                    )
                    return
                async for kind, payload in run_action_step(
                    step,
                    workflow_name=self.name,
                    workflow_title=self.title,
                    goal=goal,
                    values=values,
                    company_block=company_block,
                    prior_outputs=dict(outputs),
                ):
                    if kind == "progress":
                        yield WorkflowEvent(
                            type="progress", step_id=step.id, summary=payload
                        )
                    elif kind == "error":
                        yield WorkflowEvent(type="error", message=payload)
                        return
                    else:
                        outputs[step.id] = (step.title, payload)
                        yield WorkflowEvent(
                            type="step_done", step_id=step.id, summary=_first_line(payload)
                        )

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


def _steps_fingerprint(defn: DynamicWorkflowDef) -> str:
    """A stable digest of a definition's step list.

    Covers the steps only — not the title, cadence or description — because
    those do not change what a resumed run will DO. Serialised through the
    Pydantic models so field order is fixed and a semantically identical
    definition fingerprints identically across processes.
    """
    body = "\n".join(step.model_dump_json() for step in defn.steps)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _may_continue(step: ApprovalGateStepSpec, decision: str) -> bool:
    """Whether a resumed run may proceed past this gate.

    Fails closed for approve/reject gates: only a recognised approval
    continues. The other reply shapes are questions, not permission, so their
    answer is data and the run always continues.
    """
    if step.expected_reply_shape in NON_APPROVAL_SHAPES:
        return True
    return decision in CONTINUE_DECISIONS


def _stale_error(stale: list[tuple[str, str]]) -> WorkflowEvent:
    """The error a run (or resume) fails with when a specialist has gone.

    Shared by both entry points so the message a resumed run dies with is the
    same one a fresh run would have shown.
    """
    return WorkflowEvent(
        type="error",
        message=(
            "workflow names a specialist that no longer exists — "
            + "; ".join(f"step {sid!r} uses {name!r}" for sid, name in stale)
            + " — edit the workflow to use a current specialist"
        ),
    )


def _format_resolution(
    step: ApprovalGateStepSpec, resolution: WaitForHumanResolution
) -> str:
    """Render the human's answer as the gate step's Markdown output.

    The gate is a step, so on resume it gets an output like any other and
    flows into synthesis — which is how the decision ends up IN the artifact
    rather than only in a database column. Shape-aware, because a `numeric`
    gate's answer is a number and rendering it as an approval would be a lie.
    """
    parsed = resolution.parsed_decision or {}
    shape = step.expected_reply_shape
    lines: list[str] = []

    def _quote(text: str) -> str:
        """Fence the human's own words before they reach a specialist prompt.

        This section becomes a "section draft" handed to the synthesis
        specialist, so anything the approver wrote arrives as prompt context.
        Label it as data and cap it: a `free_text` gate otherwise passes the
        whole reply through (Slack allows tens of thousands of characters),
        and an approver could reshape the artifact by writing instructions
        into their answer.
        """
        clean = " ".join(str(text).split())
        if len(clean) > _MAX_REPLY_CHARS:
            clean = clean[: _MAX_REPLY_CHARS - 1].rstrip() + "…"
        return f"> {clean}" if clean else ""

    if shape == "numeric":
        value = parsed.get("value")
        unit = str(parsed.get("unit") or "").strip()
        rendered = "no value given" if value is None else f"{value}{' ' + unit if unit else ''}"
        lines.append(f"**Answer:** {rendered}")
    elif shape == "free_text":
        lines.append(_quote(parsed.get("text") or resolution.reply_text or ""))
    elif shape == "document":
        preview = str(parsed.get("text_preview") or "").strip()
        lines.append("**Document received.**" if parsed.get("received") else "**No document received.**")
        if preview:
            lines.append("\n" + _quote(preview))
    else:  # approve_reject
        decision = str(parsed.get("decision") or "")
        lines.append(f"**{DECISION_VERBS.get(decision, 'Recorded')}**")
        note = str(parsed.get("note") or "").strip()
        if note:
            lines.append("\n" + _quote(note))

    if str(parsed.get("decision") or "") == "auto_proceed":
        lines.append(
            "\n_No human reply arrived before the deadline; the workflow's "
            "`on_timeout` policy auto-proceeded._"
        )

    provenance = f"person {resolution.person_id}"
    if resolution.source_channel:
        provenance += f" via {resolution.source_channel}"
    if resolution.resolved_at:
        provenance += f" at {resolution.resolved_at}"
    lines.append(f"\n_Recorded from {provenance}._")

    body = "\n".join(part for part in lines if part).strip()
    return f"## {step.title}\n\n{body}\n"


def _missing_tools_error(missing: list[str]) -> WorkflowEvent:
    return WorkflowEvent(
        type="error",
        message=(
            "these tools are not available right now, so the workflow did not "
            f"start: {', '.join(missing)}"
        ),
    )


def _stale_specialist_steps(defn: DynamicWorkflowDef) -> list[tuple[str, str]]:
    """(step_id, specialist) for every step that would consult a specialist
    missing from the live registry. A synthesis step only consults one when it
    has `instructions`."""
    stale: list[tuple[str, str]] = []
    for step in defn.steps:
        if isinstance(step, ApprovalGateStepSpec | ActionStepSpec):
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
