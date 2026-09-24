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
  (``gate.checkpoint_gate``) and reported as ``awaiting_human`` (exactly as the route
  does, ``api/routes/workflows.py``), rather than hanging or erroring.

JSON-in / JSON-out, audited, matching the other orchestrator tools.
"""
from __future__ import annotations

import contextlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from openexecutive.audit import log_event as audit_log
from openexecutive.audit.redaction import ERROR_DETAIL_LEN

logger = logging.getLogger(__name__)

# Workflows that must NOT be launched through the generic ``run_workflow`` path.
# ``executive_research`` already has the dedicated ``run_executive_research``
# tool with its own routing/synthesis affordances; exposing a second entry point
# would let the model run it two different ways. ``list_workflows`` hides these
# and ``run_workflow`` refuses them, pointing the model at the dedicated tool.
_CHAT_LAUNCH_BLOCKLIST = frozenset({"executive_research"})

# What to tell the principal for each gate-delivery outcome. "Paused for
# sign-off" is only true when someone was actually asked; the other branches
# exist so the Executive never reports a question that was never delivered as
# though it were waiting on a reply.
_AWAITING_HINTS: dict[str, str] = {
    "self": (
        "This workflow paused for the principal's own sign-off. Put the "
        "question to them in your reply — their answer in this conversation "
        "will be recorded against the run. Do not re-run it."
    ),
    "sent": (
        "This workflow paused for sign-off from the named person, and the "
        "question has been sent to them on their preferred channel. Tell the "
        "principal it's waiting on them; do not re-run it."
    ),
    "alerted": (
        "This workflow paused for sign-off, but the named person could not be "
        "reached on any messaging channel — the request is on their briefing "
        "board instead. Say so plainly; do not claim they were messaged, and "
        "do not re-run it."
    ),
    "suppressed": (
        "This workflow paused for sign-off, but the outbound guard suppressed "
        "the message (duplicate, rate cap, or quiet hours), so the person has "
        "NOT been asked yet. Say that, and offer to reach them another way. "
        "Do not re-run the workflow."
    ),
    "failed": (
        "This workflow paused for sign-off, but the question could not be "
        "delivered, so nobody has been asked yet. Say that plainly rather "
        "than implying it is waiting on them. Do not re-run it."
    ),
}


def _assert_hints_cover_every_delivery_status() -> None:
    """Raise if a DeliveryStatus has no hint. Called by the unit suite.

    Deliberately NOT called at import: `api/main.py` builds the app at module
    level, so an import-time raise here takes the whole process down rather
    than degrading one tool — the same trap CLAUDE.md records for
    OE_PUBLIC_DEPLOYMENT. A test gives identical coverage with no production
    blast radius.

    The hints and the `run_workflow` tool description are two hand-written
    paraphrases of the same delivery semantics, so a new status could
    otherwise be added with nothing forcing either to be updated — and the
    fallback would quietly describe it as "could not be delivered".
    """
    from typing import get_args

    from openexecutive.workflows.gate_delivery import DeliveryStatus

    missing = set(get_args(DeliveryStatus)) - set(_AWAITING_HINTS)
    if missing:
        raise RuntimeError(
            "_AWAITING_HINTS is missing a presentation hint for "
            f"{sorted(missing)} — add one, and check whether "
            "RUN_WORKFLOW_TOOL's description still describes the delivery "
            "outcomes correctly."
        )


# Whether the run carries on by itself once answered. Kept separate from
# _AWAITING_HINTS because delivery and resumability are independent: a
# question can be delivered to a pause-only gate, and a resumable gate's
# question can fail to send.
_RESUMABLE_CLAUSE = (
    " Once they answer, the workflow picks up where it left off and finishes "
    "on its own — you do not need to do anything further."
)
_PAUSE_ONLY_CLAUSE = (
    " Their answer will be recorded, but this workflow does not continue past "
    "the gate on its own, so say so rather than implying more will happen."
)


def _awaiting_hint(delivery: str, *, resumable: bool) -> str:
    """How the Executive should present a paused run to the principal."""
    base = _AWAITING_HINTS.get(delivery, _AWAITING_HINTS["failed"])
    return base + (_RESUMABLE_CLAUSE if resumable else _PAUSE_ONLY_CLAUSE)


# The exception snippet surfaced back to the model when a workflow crashes
# mid-run. Shorter than ERROR_DETAIL_LEN because it is quoted inside a longer
# sentence; audit-detail truncation uses the shared cap.
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
        "- Running a workflow HERE returns its artifact into this "
        "conversation. It does NOT deliver DMs, post broadcasts, or send the "
        "artifact anywhere. The recurring principal briefs (morning_brief, "
        "end_of_day_digest) are delivered only by the scheduler on their own "
        "cadence — running one here renders it for you to relay, nothing "
        "more. To actually send the result to someone, call message_person "
        "afterwards.\n"
        "- The one exception is executive_reflection, which executes tool "
        "calls of its own (it can DM department heads, post company "
        "broadcasts, and create alerts). Confirm the principal wants an "
        "ad-hoc run of THAT one before firing it.\n"
        "- If a workflow pauses for a human sign-off, this returns "
        "status='awaiting_human' with who it's waiting on, whether the "
        "question was actually delivered to them, and whether the run "
        "continues by itself once answered (`resumable`); relay that and do "
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
    _audit(tool, kind, False, f"{tool}: {msg}", {"error": msg[:ERROR_DETAIL_LEN]})
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
    from openexecutive.workflows.gate import checkpoint_gate
    from openexecutive.workflows.persistence import (
        complete_run,
        create_run,
        fail_run,
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

    # Constructing the store can fail (bad persist path, a Chroma client that
    # won't initialise). It used to sit outside the try below, so the exception
    # escaped this handler entirely, took down the whole chat turn via the
    # orchestrator's tool gather, and left the run row stuck at 'running'.
    try:
        store = ChromaDBStore(persist_directory=get_settings().vector_store_path)
    except Exception as exc:
        logger.exception("run_workflow: knowledge store init failed")
        with contextlib.suppress(Exception):
            fail_run(run_id, f"knowledge store unavailable: {exc}")
        return _err(
            "run_workflow",
            f"knowledge store unavailable: {exc}",
            kind="write",
        )

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
                # Delivery + checkpoint live in `workflows.gate`, shared with
                # the HTTP route and the resumer. Deliberately NOT suppressed:
                # if the checkpoint can't be written the run must NOT be
                # reported as awaiting_human — a silently-dropped checkpoint
                # would orphan the run in 'running' where the resumer and the
                # inbound resolver can never find it. Let the failure fall
                # through to the outer handler, which fails the run.
                pause = await checkpoint_gate(
                    run_id=run_id,
                    event=event,
                    workflow_title=workflow.title,
                )
                awaiting = {
                    "person_id": pause.person_id,
                    "question": pause.question,
                    "awaiting_until": pause.awaiting_until.isoformat(),
                    "delivery": pause.delivery,
                    "resumable": pause.resumable,
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
            f"run_workflow {name} awaiting_human run_id={run_id} "
            f"delivery={awaiting.get('delivery', '')}",
            {
                "workflow": name,
                "run_id": run_id,
                "status": "awaiting_human",
                "delivery": awaiting.get("delivery", ""),
            },
        )
        return json.dumps({
            "status": "awaiting_human",
            "workflow": name,
            "run_id": run_id,
            **awaiting,
            "presentation_hint": _awaiting_hint(
                str(awaiting.get("delivery", "")),
                resumable=bool(awaiting.get("resumable")),
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
