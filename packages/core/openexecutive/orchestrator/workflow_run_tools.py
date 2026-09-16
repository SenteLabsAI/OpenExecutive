"""Anthropic tool definitions + handlers for launching workflows from chat.

Before these tools existed the Executive could only fire the research council
(``run_executive_research``) from a chat turn — every other built-in workflow
and every user-created custom workflow was reachable only through the ``/jobs``
UI. These two tools close that gap so the principal can ask the Executive to run
any workflow conversationally and get the artifact back in the same turn.

- ``list_workflows`` (read) surfaces the launchable catalog with each
  workflow's input fields, mirroring ``GET /workflows``.
- ``run_workflow`` (write) runs one workflow to completion using the same
  engine as the HTTP route (``openexecutive.api.routes.workflows``):
  validate inputs, ``create_run``, stream events,
  ``complete_run`` / ``fail_run``. It also handles the approval-gate case — a
  workflow that yields a ``WaitForHumanEvent`` is checkpointed
  (``save_checkpoint``) and reported as ``awaiting_human`` (exactly as the route
  does, ``api/routes/workflows.py``), rather than hanging or erroring.

JSON-in / JSON-out, audited, matching the other orchestrator tools.
"""
from __future__ import annotations

import contextlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from openexecutive.audit import log_event as audit_log

logger = logging.getLogger(__name__)

# Workflows that must NOT be launched through the generic ``run_workflow`` path.
# ``executive_research`` already has the dedicated ``run_executive_research``
# tool with its own routing/synthesis affordances; exposing a second entry point
# would let the model run it two different ways. ``list_workflows`` hides these
# and ``run_workflow`` refuses them, pointing the model at the dedicated tool.
_CHAT_LAUNCH_BLOCKLIST = frozenset({"executive_research"})

# Cap error text stored in the audit detail (kept short so audit rows stay
# scannable) and the exception snippet surfaced back to the model.
_AUDIT_ERR_MAXLEN = 300
_EXC_SNIPPET_MAXLEN = 200


# --------------------------------------------------------------------------- #
# Tool definitions
# --------------------------------------------------------------------------- #

LIST_WORKFLOWS_TOOL: dict[str, Any] = {
    "name": "list_workflows",
    "description": (
        "List every workflow you can launch with run_workflow — the built-in "
        "executive jobs (board prep, quarterly plan, performance review, "
        "competitive teardown, fundraising prep, etc.) plus any custom "
        "workflows the company has authored. Returns each workflow's name, "
        "title, description, section, estimated_minutes, and the input fields "
        "it expects (name → type/description/required). Call this when the "
        "principal asks what you can run, or to discover a workflow's name and "
        "required inputs before calling run_workflow."
    ),
    "input_schema": {"type": "object", "properties": {}},
}


RUN_WORKFLOW_TOOL: dict[str, Any] = {
    "name": "run_workflow",
    "description": (
        "Run one workflow to completion and return its Markdown artifact. Use "
        "this to act on a request like 'put together a board prep deck' or "
        "'run a competitive teardown of Acme'. Resolve the exact `workflow` "
        "name and its required `inputs` with list_workflows first, then pass "
        "`inputs` as an object matching that workflow's fields.\n"
        "Notes:\n"
        "- A few workflows dispatch real messages when run (morning_brief and "
        "end_of_day_digest DM the principal; executive_reflection can DM heads "
        "and post broadcasts). Confirm the principal actually wants an ad-hoc "
        "run before firing those — don't trigger them speculatively.\n"
        "- If a workflow pauses for a human sign-off, this returns "
        "status='awaiting_human' with who it's waiting on; relay that and do "
        "not re-run it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "workflow": {
                "type": "string",
                "description": "The workflow name (registry key) to run, e.g. 'board_prep'.",
            },
            "inputs": {
                "type": "object",
                "description": (
                    "The workflow's input fields, matching the schema from "
                    "list_workflows. Pass an empty object if it takes none."
                ),
            },
        },
        "required": ["workflow"],
    },
}


WORKFLOW_RUN_TOOLS: list[dict[str, Any]] = [
    LIST_WORKFLOWS_TOOL,
    RUN_WORKFLOW_TOOL,
]


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #


def _audit(tool: str, kind: str, ok: bool, summary: str, details: dict[str, Any]) -> None:
    audit_log(
        "tool_invocation",
        summary,
        actor="executive",
        details={"tool": tool, "kind": kind, "ok": ok, **details},
    )


def _err(tool: str, msg: str, kind: str = "read") -> str:
    _audit(tool, kind, False, f"{tool}: {msg}", {"error": msg[:_AUDIT_ERR_MAXLEN]})
    return json.dumps({"error": msg})


def _input_fields(input_schema: dict[str, Any]) -> dict[str, Any]:
    """Flatten a workflow's JSON-schema into a compact name → field map.

    Keeps only what the model needs to fill `inputs`: each property's type,
    description, and whether it's required. Drops the schema machinery
    (``$defs``, ``title``, validators) that would bloat the tool result.
    """
    props = input_schema.get("properties", {}) or {}
    required = set(input_schema.get("required", []) or [])
    fields: dict[str, Any] = {}
    for name, spec in props.items():
        spec = spec if isinstance(spec, dict) else {}
        fields[name] = {
            "type": spec.get("type"),
            "description": spec.get("description", ""),
            "required": name in required,
        }
    return fields


async def handle_list_workflows(tool_input: dict[str, Any]) -> str:
    from openexecutive.workflows import list_workflows as _list_workflows

    try:
        workflows = _list_workflows()
    except Exception as exc:
        logger.exception("list_workflows: failed")
        return _err("list_workflows", str(exc))

    out: list[dict[str, Any]] = []
    for wf in workflows:
        if wf.name in _CHAT_LAUNCH_BLOCKLIST:
            continue
        try:
            meta = wf.meta()
        except Exception:
            logger.exception("list_workflows: meta() failed for %s", getattr(wf, "name", "?"))
            continue
        out.append({
            "name": meta.name,
            "title": meta.title,
            "description": meta.description,
            "section": meta.section.value,
            "estimated_minutes": meta.estimated_minutes,
            "is_custom": meta.is_custom,
            "inputs": _input_fields(meta.input_schema),
        })

    _audit("list_workflows", "read", True, f"list_workflows returned {len(out)}", {"count": len(out)})
    return json.dumps({"workflows": out, "count": len(out)})


async def handle_run_workflow(tool_input: dict[str, Any]) -> str:
    from openexecutive.config import get_settings
    from openexecutive.knowledge.store import ChromaDBStore
    from openexecutive.workflows import get_workflow
    from openexecutive.workflows.persistence import (
        complete_run,
        create_run,
        fail_run,
        save_checkpoint,
    )
    from openexecutive.workflows.wait_for_human import WaitForHumanEvent

    name = str(tool_input.get("workflow", "")).strip()
    if not name:
        return _err("run_workflow", "workflow name is required", kind="write")
    if name in _CHAT_LAUNCH_BLOCKLIST:
        return _err(
            "run_workflow",
            f"{name!r} can't be launched here — use the run_executive_research tool instead.",
            kind="write",
        )

    try:
        workflow = get_workflow(name)
    except KeyError:
        return _err(
            "run_workflow",
            f"unknown workflow: {name!r}. Call list_workflows to see what's available.",
            kind="write",
        )

    raw_inputs = tool_input.get("inputs")
    if raw_inputs is None:
        raw_inputs = {}
    if not isinstance(raw_inputs, dict):
        return _err("run_workflow", "inputs must be an object", kind="write")

    input_cls = workflow.input_model()
    try:
        wf_inputs = input_cls.model_validate(raw_inputs)
    except Exception as exc:
        # Surface the validation error so the model can correct its inputs.
        return _err("run_workflow", f"invalid inputs for {name}: {exc}", kind="write")

    run_id = uuid.uuid4().hex
    try:
        create_run(run_id, name, f"{workflow.title} (chat-tool fire)", wf_inputs.model_dump())
    except Exception as exc:
        # Don't run an untracked workflow: without the run row, a later
        # save_checkpoint would UPDATE nothing (SQLite reports 0 rows, no error)
        # and we'd falsely claim awaiting_human for a run the resumer can never
        # find. Fail fast instead.
        logger.exception("run_workflow: create_run failed")
        return _err("run_workflow", f"could not start run: {exc}", kind="write")

    store = ChromaDBStore(persist_directory=get_settings().vector_store_path)
    artifact = ""
    last_error = ""
    awaiting: dict[str, Any] | None = None
    try:
        async for event in workflow.run(inputs=wf_inputs, store=store):
            # An approval-gate step yields a WaitForHumanEvent (not a
            # WorkflowEvent): checkpoint the run and stop. Mirrors
            # api/routes/workflows.py — the resumer applies the timeout policy
            # and the inbound resolver records the human's reply.
            if isinstance(event, WaitForHumanEvent):
                until = datetime.now(UTC) + timedelta(hours=event.timeout_hours)
                # Deliberately NOT suppressed: if the checkpoint can't be
                # written the run must NOT be reported as awaiting_human — a
                # silently-dropped checkpoint would orphan the run in 'running'
                # where the resumer/inbound resolver can never find it. Let the
                # failure fall through to the outer handler, which fails the run.
                save_checkpoint(
                    run_id=run_id,
                    state_json=event.model_dump_json(),
                    awaiting_person_id=event.person_id,
                    awaiting_until=until,
                )
                awaiting = {
                    "person_id": event.person_id,
                    "question": event.question,
                    "awaiting_until": until.isoformat(),
                }
                break
            if event.type == "artifact" and event.content:
                artifact = event.content
            elif event.type == "error" and event.message:
                last_error = event.message
    except Exception as exc:
        logger.exception("run_workflow: workflow.run crashed")
        last_error = str(exc)[:_EXC_SNIPPET_MAXLEN]

    if awaiting is not None:
        _audit(
            "run_workflow", "write", True,
            f"run_workflow {name} awaiting_human run_id={run_id}",
            {"workflow": name, "run_id": run_id, "status": "awaiting_human"},
        )
        return json.dumps({
            "status": "awaiting_human",
            "workflow": name,
            "run_id": run_id,
            **awaiting,
            "presentation_hint": (
                "This workflow paused for sign-off from the named person. Tell the "
                "principal it's waiting on them; do not re-run it."
            ),
        })

    # A produced artifact wins — mirrors the HTTP route (api/routes/workflows.py),
    # which completes the run on any artifact regardless of non-fatal `error`
    # progress events. Only when no artifact was produced do we fail the run,
    # surfacing the captured error message if there was one.
    if not artifact:
        msg = last_error or "workflow finished without producing an artifact"
        with contextlib.suppress(Exception):
            fail_run(run_id, msg)
        _audit(
            "run_workflow", "write", False,
            f"run_workflow {name} FAILED — {msg}",
            {"workflow": name, "error": msg, "run_id": run_id},
        )
        return json.dumps({"error": f"workflow error: {msg}", "run_id": run_id})

    with contextlib.suppress(Exception):
        complete_run(run_id, artifact)
    _audit(
        "run_workflow", "write", True,
        f"run_workflow {name} ok run_id={run_id}",
        {"workflow": name, "run_id": run_id},
    )
    return json.dumps({
        "ok": True,
        "workflow": name,
        "run_id": run_id,
        "artifact": artifact,
        "presentation_hint": (
            "The workflow produced the artifact above and queued any reminders it "
            "scheduled. Summarise it for the principal; do not re-run it."
        ),
    })


WORKFLOW_RUN_TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[str]]] = {
    "list_workflows": handle_list_workflows,
    "run_workflow": handle_run_workflow,
}


__all__ = [
    "WORKFLOW_RUN_TOOLS",
    "WORKFLOW_RUN_TOOL_HANDLERS",
]
