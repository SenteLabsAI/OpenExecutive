"""Anthropic tools that let the Executive author user-created workflows in chat.

Two fixed tools implement a **draft → confirm** handshake:

1. ``draft_workflow`` — the Executive proposes a complete definition. The
   handler validates it but does NOT persist; it returns a ``confirm_token``
   (a hash of the canonical definition) plus a human-readable summary the
   Executive shows the user for approval.
2. ``save_workflow`` — after the user approves, the Executive calls this with
   the same definition and the token. The handler recomputes the token and
   refuses on mismatch, so the saved workflow is exactly what the user saw.

Both tool schemas are fixed (no per-definition tools), so the cached tool list
stays stable. No new outbound capability is granted: approval-gate and cadence
recipients must be rostered people (enforced by ``validate_definition``), and
cadence delivery reuses the scheduler's existing guarded send.

A workflow with action (tool-using) steps is saved **inactive**. The chat
confirmation is a token this model holds itself, so it cannot stand in for a
person seeing the tools; the workflow runs (or schedules) only after someone
turns it on from its review card at ``/jobs/{name}``.
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import ValidationError

from openexecutive.orchestrator.schedule_tools import current_session

logger = logging.getLogger(__name__)

# Shared JSON-schema fragment describing a definition. Kept permissive on the
# step union (additionalProperties) — validate_definition does the real work
# and returns precise, model-readable errors.
DEFINITION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "The full workflow definition.",
    "properties": {
        "name": {
            "type": "string",
            "description": "snake_case registry key, 3-49 chars (e.g. 'weekly_competitor_watch').",
        },
        "title": {"type": "string", "description": "Human-readable name."},
        "description": {"type": "string"},
        "section": {
            "type": "string",
            "description": (
                "UI grouping. One of: 'Board', 'Capital & Investors', "
                "'Growth & GTM', 'Product', 'People', 'Risk, Legal & Crisis', "
                "'Operating Cadence'."
            ),
        },
        "estimated_minutes": {"type": "integer"},
        "input_fields": {
            "type": "array",
            "description": "Free-text form fields. Each: {name, label, description?, required?, multiline?}.",
            "items": {"type": "object", "additionalProperties": True},
        },
        "steps": {
            "type": "array",
            "description": (
                "Ordered steps. Each has a 'kind': "
                "'specialist' {id,title,specialist,goal,rag_query?,playbook?} (analysis by an "
                "advisor; playbook = name of an existing playbook the step follows), "
                "'action' {id,title,goal,tools,max_tool_calls?} (gets something done with "
                "tools — 'tools' is the exact list of tool names the step may call, e.g. "
                "'google_workspace__append_table_rows' or 'oe__read_file'; max_tool_calls "
                "is optional — omit it for the default budget), "
                "'approval_gate' {id,title,person_id,question,timeout_hours?,on_timeout?}, "
                "or 'synthesis' {id,title,instructions?,specialist?}. "
                "Exactly ONE synthesis step, and it MUST be last. Goals may use "
                "{field_name} placeholders referencing declared input fields."
            ),
            "items": {"type": "object", "additionalProperties": True},
        },
        "cadence": {
            "type": "string",
            "description": (
                "Optional recurrence: 'daily@HH:MM' / 'weekly@DOW@HH:MM' / "
                "'quarterly@DD-HH:MM' (UTC). Requires cadence_person_id and no "
                "required input fields."
            ),
        },
        "cadence_person_id": {
            "type": "integer",
            "description": "Rostered person who receives the artifact when the cadence fires.",
        },
    },
    "required": ["name", "title", "steps"],
    "additionalProperties": True,
}


DRAFT_WORKFLOW_TOOL: dict[str, Any] = {
    "name": "draft_workflow",
    "description": (
        "Draft a new reusable executive workflow (a structured, multi-step job "
        "the user can re-run from /jobs) WITHOUT saving it. Use when the user "
        "asks you to create, build, or set up a custom workflow. Returns a "
        "confirm_token and a summary of the plan. You MUST show the summary to "
        "the user and get explicit confirmation, then call save_workflow with "
        "the SAME definition and the confirm_token. Do NOT invent specialists, "
        "people, or metrics — only use the 8 specialist roles and people the "
        "user has identified. If validation fails, the response lists exactly "
        "what to fix; re-draft and try again. A workflow with action (tool) "
        "steps is saved switched OFF: after saving, give the user the link so "
        "they can review its tools and turn it on."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"definition": DEFINITION_SCHEMA},
        "required": ["definition"],
    },
}


SAVE_WORKFLOW_TOOL: dict[str, Any] = {
    "name": "save_workflow",
    "description": (
        "Persist a workflow definition that was previously returned by "
        "draft_workflow AND explicitly approved by the user. Pass the exact "
        "definition object and the confirm_token from the draft. The save is "
        "rejected if the definition was changed after drafting (token "
        "mismatch) — re-draft if you need to change anything. On success the "
        "workflow appears in /jobs and is immediately runnable — except one "
        "with action (tool) steps, which stays off until the user reviews its "
        "tools and turns it on at the returned deep_link."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "definition": DEFINITION_SCHEMA,
            "confirm_token": {
                "type": "string",
                "description": "The token returned by draft_workflow for this exact definition.",
            },
            "overwrite": {
                "type": "boolean",
                "description": "Set true to replace an existing custom workflow of the same name.",
            },
        },
        "required": ["definition", "confirm_token"],
    },
}


def _canonical_token(definition: dict[str, Any]) -> str:
    """Stable hash over the user-meaningful parts of a definition.

    Excludes only the server-managed timestamps. Everything the model drafted —
    including ``is_active`` — is covered, so a save can't silently differ from
    the definition shown in chat. (The one deliberate difference is the
    server's own: ``handle_save_workflow`` stores a tool workflow switched
    off, after this check.)
    """
    payload = {k: v for k, v in definition.items() if k not in {"created_at", "updated_at"}}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _approved_tool_workflow_error(name: str, existing: Any) -> str | None:
    """Refuse chat overwrites of a switched-on tool workflow (``existing``).

    A person approved its tools. Chat (including a turn steered by an inbound
    email) replacing it would switch it off and stage different tools under
    the same familiar name, so edits to it happen on the Workflows page instead.
    """
    from openexecutive.config import get_settings

    if existing is None or not existing.is_active or not _needs_review(existing):
        return None
    base = get_settings().ui_base_url.rstrip("/")
    return (
        f"{name!r} is a switched-on workflow whose tools the user approved; it "
        "can't be replaced from chat. Pick a different name for a new "
        f"workflow, or tell the user to edit it at {base}/jobs/new?edit={name}"
    )


def _session_caller_id() -> int | None:
    """The person chatting — recorded as a new workflow's owner."""
    from openexecutive.workflows.gate_delivery import session_caller_id

    session = current_session.get()
    return session_caller_id(session) if session is not None else None


def _needs_review(defn: Any) -> bool:
    """True when the workflow has tool-using steps, which a person must approve.

    Their approval is turning the workflow on from its review card on the Workflows
    page — a chat confirmation (a token this model holds itself) is not a
    human seeing the tools.
    """
    from openexecutive.workflows.dynamic_models import ActionStepSpec

    return any(isinstance(step, ActionStepSpec) for step in defn.steps)


async def _build_def(definition: Any) -> tuple[Any, str | None]:
    """Validate shape + rules. Returns (DynamicWorkflowDef, None) or (None, error)."""
    from openexecutive.workflows.dynamic_models import DynamicWorkflowDef
    from openexecutive.workflows.tool_catalog import validate_definition_and_tools

    if not isinstance(definition, dict):
        return None, "definition must be an object"
    try:
        defn = DynamicWorkflowDef.model_validate(definition)
    except ValidationError as exc:
        return None, f"definition shape invalid: {exc.errors()}"
    errors = await validate_definition_and_tools(defn)
    if errors:
        return None, "validation failed: " + "; ".join(errors)
    return defn, None


def _summarize(defn: Any) -> str:
    """One-line-per-step plan the Executive shows the user before saving."""
    lines = [f"**{defn.title}** ({defn.section.value}) — {len(defn.steps)} steps"]
    if defn.input_fields:
        lines.append(
            "Inputs: " + ", ".join(f"{f.label}{'' if f.required else ' (optional)'}" for f in defn.input_fields)
        )
    for i, step in enumerate(defn.steps, 1):
        kind = getattr(step, "kind", "?")
        if kind == "specialist":
            lines.append(f"{i}. [{step.specialist}] {step.title}")
        elif kind == "approval_gate":
            lines.append(f"{i}. [approval gate → person {step.person_id}] {step.title}")
        elif kind == "action":
            lines.append(f"{i}. [action · tools: {', '.join(step.tools)}] {step.title}")
        else:
            lines.append(f"{i}. [synthesis] {step.title}")
    if defn.cadence:
        lines.append(f"Recurs: {defn.cadence} → person {defn.cadence_person_id}")
    return "\n".join(lines)


async def handle_draft_workflow(tool_input: dict[str, Any]) -> str:
    from openexecutive.workflows.dynamic_store import get_definition

    defn, error = await _build_def(tool_input.get("definition"))
    if error is None:
        error = _approved_tool_workflow_error(defn.name, get_definition(defn.name))
    if error is not None:
        return json.dumps({"error": error})
    token = _canonical_token(tool_input["definition"])
    note = (
        "Show this summary to the user. After they confirm, call "
        "save_workflow with the same definition and this confirm_token."
    )
    if _needs_review(defn):
        note += (
            " It uses tools, so it will be saved switched OFF: tell the user "
            "that after saving they review its tools and turn it on from the "
            "link save_workflow returns."
        )
    result: dict[str, Any] = {
        "status": "drafted",
        "confirm_token": token,
        "summary": _summarize(defn),
        "note": note,
    }
    if _needs_review(defn):
        result["requires_review"] = True
    # Surface an existing-name clash now (not only at save time) so the
    # Executive can warn the user before they approve a plan that would
    # otherwise need overwrite=true to save.
    if get_definition(defn.name) is not None:
        result["warning"] = (
            f"a custom workflow named {defn.name!r} already exists; saving will "
            "replace it and requires overwrite=true. Confirm with the user or "
            "pick a different name."
        )
    return json.dumps(result)


async def handle_save_workflow(tool_input: dict[str, Any]) -> str:
    from openexecutive.audit import log_event as audit_log
    from openexecutive.config import get_settings
    from openexecutive.workflows.dynamic_store import get_definition, save_if_unchanged

    definition = tool_input.get("definition")
    provided_token = str(tool_input.get("confirm_token", ""))

    defn, error = await _build_def(definition)
    if error is not None:
        return json.dumps({"error": error})

    expected_token = _canonical_token(definition)  # type: ignore[arg-type]
    if provided_token != expected_token:
        return json.dumps({
            "error": (
                "confirm_token does not match the definition — the definition "
                "changed since drafting. Call draft_workflow again to get a "
                "fresh token, show the user, and save that."
            )
        })

    existing = get_definition(defn.name)
    approved_error = _approved_tool_workflow_error(defn.name, existing)
    if approved_error is not None:
        return json.dumps({"error": approved_error})

    overwrite = bool(tool_input.get("overwrite", False))
    if existing is not None and not overwrite:
        return json.dumps({
            "error": (
                f"a custom workflow named {defn.name!r} already exists; pass "
                "overwrite=true to replace it."
            )
        })

    # A tool-using workflow waits for a person to turn it on from its review
    # card. Applied after the token check so the token still covers exactly
    # what was drafted. (Replacing an approved, switched-on one was refused above.)
    if _needs_review(defn):
        defn = defn.model_copy(update={"is_active": False})

    try:
        # Conditional on the row read above, so an activation (from any
        # process) landing after the approved-workflow check can't be
        # overwritten by this save.
        saved = save_if_unchanged(defn, existing, owner_person_id=_session_caller_id())
    except Exception as exc:
        logger.exception("save_workflow: persist failed")
        return json.dumps({"error": f"failed to save: {exc}"})
    if saved is None:
        return json.dumps({
            "error": (
                f"{defn.name!r} changed while saving (e.g. the user just turned "
                "it on). Nothing was saved; check its current state before "
                "drafting again."
            )
        })
    stored = saved

    # Reconcile any cadence into the scheduler.
    try:
        from openexecutive.workflows.dynamic_cadence import (
            cancel_cadence_rows,
            schedule_dynamic_workflow_cadence,
        )

        cancel_cadence_rows(stored.name)
        if stored.is_active and stored.cadence:
            schedule_dynamic_workflow_cadence(stored)
    except Exception:
        logger.exception("save_workflow: cadence scheduling failed (non-fatal)")

    session = current_session.get()
    session_id = getattr(session, "session_id", None) if session is not None else None
    audit_log(
        "dynamic_workflow",
        f"Saved custom workflow {stored.name!r} ({len(stored.steps)} steps)",
        session_id=session_id,
        actor="executive",
        details={
            "name": stored.name,
            "title": stored.title,
            "cadence": stored.cadence,
            "pending_review": not stored.is_active,
        },
    )

    base = get_settings().ui_base_url.rstrip("/")
    deep_link = f"{base}/jobs/{stored.name}"
    if not stored.is_active:
        # Tool workflows always; any other workflow only if drafted off.
        return json.dumps({
            "status": "saved_pending_review",
            "name": stored.name,
            "deep_link": deep_link,
            "note": (
                "Saved switched OFF. It won't run or follow its schedule until "
                "the user reviews it and turns it on at deep_link — give them "
                "the link."
            ),
        })
    return json.dumps({"status": "saved", "name": stored.name, "deep_link": deep_link})


WORKFLOW_AUTHORING_TOOLS: list[dict[str, Any]] = [
    DRAFT_WORKFLOW_TOOL,
    SAVE_WORKFLOW_TOOL,
]

WORKFLOW_AUTHORING_TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[str]]] = {
    "draft_workflow": handle_draft_workflow,
    "save_workflow": handle_save_workflow,
}
