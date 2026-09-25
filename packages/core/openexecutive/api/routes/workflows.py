"""HTTP surface for the workflows package.

Endpoints:
- GET    /workflows                       List available workflows with metadata
- GET    /workflows/{name}                Get one workflow's metadata + input schema
- POST   /workflows/{name}/runs           Start a run; streams progress as SSE
                                          (403 for a principal-only one unless the principal)
- GET    /workflows/runs                  List recent runs across all workflows
- GET    /workflows/runs/{run_id}         Get a specific past run (with artifact)
- DELETE /workflows/runs/{run_id}         Delete a past run
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from openexecutive.workflows import (
    get_workflow,
    list_workflows,
)
from openexecutive.workflows.dynamic_models import (
    TOOL_NAME_RE,
    ActionStepSpec,
    DynamicWorkflowDef,
)
from openexecutive.workflows.dynamic_store import (
    activate_if_unchanged,
    delete_definition,
    get_definition,
    get_owner,
    list_definitions,
    save_if_unchanged,
    set_active,
    upsert_definition,
)
from openexecutive.workflows.gate import checkpoint_gate
from openexecutive.workflows.persistence import (
    complete_run,
    create_run,
    delete_run,
    fail_run,
    get_run,
    initialize_runs_db,
    list_runs,
)
from openexecutive.workflows.tool_catalog import resolve as resolve_tools_catalog
from openexecutive.workflows.tool_catalog import search as search_tools_catalog
from openexecutive.workflows.tool_catalog import validate_definition_and_tools
from openexecutive.workflows.wait_for_human import WaitForHumanEvent

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/workflows")
async def list_workflow_meta() -> dict[str, Any]:
    """Public catalog of available workflows."""
    return {"workflows": [w.meta().model_dump() for w in list_workflows()]}


@router.get("/workflows/runs")
async def list_workflow_runs(workflow: str | None = None, limit: int = 100) -> dict[str, Any]:
    """Recent runs across all workflows (or filtered by workflow name)."""
    initialize_runs_db()
    return {"runs": list_runs(workflow_name=workflow, limit=limit)}


@router.get("/workflows/runs/{run_id}")
async def get_workflow_run(run_id: str) -> dict[str, Any]:
    """Full record of one run, including the artifact if complete.

    `resume_state_json` is replaced by a small `resume_progress` summary. The
    payload holds the full text of every step completed before the gate, and
    the run-detail page POLLS this endpoint while a run is unfinished — so
    returning it would re-send the whole run body every few seconds to render
    a progress list that only needs the step ids.
    """
    run = get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    run["resume_progress"] = _resume_progress(run.pop("resume_state_json", None))
    return run


@router.delete("/workflows/runs/{run_id}")
async def delete_workflow_run(run_id: str) -> dict[str, str]:
    if not delete_run(run_id):
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    return {"status": "deleted", "run_id": run_id}


_WEB_DECISIONS = {"approve", "reject"}


@router.post("/workflows/runs/{run_id}/decision")
async def decide_workflow_run(run_id: str, request: Request) -> dict[str, Any]:
    """Answer a run waiting for a yes/no sign-off from the web app.

    The same resolution a chat reply produces, so the run resumes exactly as
    it would have. Only the person the run is waiting on — or the principal —
    may answer; free-text / numeric / document requests still need a reply in
    chat, since there is nothing here to parse.
    """
    from openexecutive.api.routes.chat import _resolve_caller_person_id
    from openexecutive.people.store import is_principal_or_self
    from openexecutive.workflows.resumer import apply_resolution
    from openexecutive.workflows.wait_for_human import WaitForHumanResolution

    try:
        body = await request.json()
    except json.JSONDecodeError:
        body = {}
    decision = body.get("decision") if isinstance(body, dict) else None
    if decision not in _WEB_DECISIONS:
        raise HTTPException(status_code=422, detail="decision must be 'approve' or 'reject'")
    run = get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    if run.get("status") != "awaiting_human":
        raise HTTPException(status_code=409, detail="This run isn't waiting for an answer.")
    try:
        gate = json.loads(run.get("state_json") or "{}")
    except json.JSONDecodeError:
        gate = {}
    if not isinstance(gate, dict) or gate.get("expected_reply_shape", "approve_reject") != "approve_reject":
        raise HTTPException(
            status_code=409, detail="This request needs a written reply — answer it in chat."
        )
    awaiting = run.get("awaiting_person_id")
    caller = _resolve_caller_person_id(request)
    if not is_principal_or_self(caller, awaiting):
        raise HTTPException(
            status_code=403, detail="Only the person this is waiting on, or the principal, can answer."
        )
    assert caller is not None  # is_principal_or_self refuses an unresolved caller
    resolution = WaitForHumanResolution(
        run_id=run_id,
        reply_text=f"[{decision} from the web app]",
        source_channel="web",
        parsed_decision={"decision": decision, "note": "answered in the web app"},
        person_id=caller,
    )
    if not await apply_resolution(run_id, resolution):
        raise HTTPException(status_code=409, detail="This run was answered already.")
    return {"status": "resolved", "run_id": run_id, "decision": decision}


# -----------------------------------------------------------------------------
# Dynamic (user-created) workflow CRUD. Declared BEFORE "/workflows/{name}" so
# the literal "custom" segment isn't captured by the {name} path parameter.
# -----------------------------------------------------------------------------


_TOOL_SEARCH_MAX_QUERY = 200


@router.get("/workflows/tools/search")
async def search_workflow_tools(q: str = "") -> dict[str, Any]:
    """Tools an action step can use that match ``q`` (built-ins + MCP gateway).

    Backs the advanced editor's tool picker. Literal path, declared before
    ``/workflows/{name}``.
    """
    query = q.strip()[:_TOOL_SEARCH_MAX_QUERY]
    if not query:
        return {"tools": []}
    return {"tools": [t.as_dict() for t in await search_tools_catalog(query)]}


_TOOL_DESCRIBE_MAX_NAMES = 32


@router.get("/workflows/tools/describe")
async def describe_workflow_tools(names: str = "") -> dict[str, Any]:
    """Exact-name lookup for a comma-separated list of tool names.

    Lets the review card and the advanced editor label each approved tool
    (description, reads-only) — the definition itself only stores names.
    Unknown names are simply absent from the result.
    """
    # Only well-formed, distinct names: each non-built-in one costs a gateway
    # search, so junk must not fan out into the shared MCP session.
    wanted = list(
        dict.fromkeys(n.strip() for n in names.split(",") if TOOL_NAME_RE.match(n.strip()))
    )[:_TOOL_DESCRIBE_MAX_NAMES]
    if not wanted:
        return {"tools": []}
    found = await resolve_tools_catalog(wanted)
    return {"tools": [found[n].as_dict() for n in wanted if n in found]}


def _with_owner(defn: DynamicWorkflowDef) -> dict[str, Any]:
    """A definition as the UI sees it: its fields plus who created it.

    ``owner_person_id`` is server-managed (a column, not a definition field),
    so a client echoing it back in a body has no effect.
    """
    return {**defn.model_dump(), "owner_person_id": get_owner(defn.name)}


@router.get("/workflows/custom")
async def list_custom_workflows() -> dict[str, Any]:
    """All dynamic definitions (active and inactive) for the builder UI."""
    return {"definitions": [_with_owner(d) for d in list_definitions(active_only=False)]}


@router.post("/workflows/custom", status_code=201)
async def create_custom_workflow(request: Request) -> dict[str, Any]:
    """Create a new dynamic workflow definition from a builder-UI submission.

    The signed-in caller becomes its owner — the person asked to approve its
    first write to a new target.
    """
    from openexecutive.api.routes.chat import _resolve_caller_person_id

    defn = await _parse_definition(request)
    exists = f"A custom workflow named {defn.name!r} already exists"
    if get_definition(defn.name) is not None:
        raise HTTPException(status_code=409, detail=exists)
    errors = await validate_definition_and_tools(defn)
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    # Insert-only: a create racing another save of the same name is a 409,
    # never a silent overwrite of someone else's workflow (or its owner).
    stored = save_if_unchanged(
        defn, None, owner_person_id=_resolve_caller_person_id(request)
    )
    if stored is None:
        raise HTTPException(status_code=409, detail=exists)
    _sync_cadence(stored)
    return _with_owner(stored)


@router.get("/workflows/custom/{name}")
async def get_custom_workflow(name: str) -> dict[str, Any]:
    defn = get_definition(name)
    if defn is None:
        raise HTTPException(status_code=404, detail=f"Custom workflow {name!r} not found")
    return _with_owner(defn)


@router.put("/workflows/custom/{name}")
async def update_custom_workflow(name: str, request: Request) -> dict[str, Any]:
    if get_definition(name) is None:
        raise HTTPException(status_code=404, detail=f"Custom workflow {name!r} not found")
    defn = await _parse_definition(request)
    if defn.name != name:
        raise HTTPException(
            status_code=422, detail="definition name does not match the path name"
        )
    errors = await validate_definition_and_tools(defn)
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    stored = upsert_definition(defn)
    _sync_cadence(stored)
    return stored.model_dump()


@router.delete("/workflows/custom/{name}")
async def delete_custom_workflow(name: str) -> dict[str, str]:
    from openexecutive.workflows.dynamic_cadence import cancel_cadence_rows

    cancel_cadence_rows(name)
    if not delete_definition(name):
        raise HTTPException(status_code=404, detail=f"Custom workflow {name!r} not found")
    return {"status": "deleted", "name": name}


@router.get("/workflows/custom/{name}/targets")
async def list_approved_targets(name: str) -> dict[str, Any]:
    """Where this workflow's tool steps may write without asking again."""
    from openexecutive.workflows.approved_targets import list_targets

    if get_definition(name) is None:
        raise HTTPException(status_code=404, detail=f"Custom workflow {name!r} not found")
    return {"targets": list_targets(name)}


@router.delete("/workflows/custom/{name}/targets")
async def forget_approved_target(name: str, value: str) -> dict[str, str]:
    """Forget one approved target; the next write there asks again."""
    from openexecutive.workflows.approved_targets import forget

    if not forget(name, value):
        raise HTTPException(status_code=404, detail="That target isn't approved for this workflow")
    return {"status": "forgotten", "name": name}


# Fields a reviewer can't see or that the server manages; the rest is what the
# review card shows, and what an activation must match.
_REVIEW_EXCLUDE = {"is_active", "created_at", "updated_at"}


def _reviewed_matches(stored: DynamicWorkflowDef, reviewed: Any) -> bool:
    """True when ``reviewed`` (the definition the user looked at) is ``stored``."""
    try:
        seen = DynamicWorkflowDef.model_validate(reviewed)
    except ValidationError:
        return False
    return seen.model_dump(exclude=_REVIEW_EXCLUDE) == stored.model_dump(exclude=_REVIEW_EXCLUDE)


@router.post("/workflows/custom/{name}/activate")
async def activate_custom_workflow(name: str, request: Request) -> dict[str, Any]:
    """Turn a custom workflow on or off.

    Turning one on is the approval for a tool workflow chat saved switched off,
    so the body must carry the ``definition`` the user reviewed when the stored
    one has action steps: a mismatch (e.g. chat overwrote it while the card was
    open) is a 409, so the click can only switch on what was shown.
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    is_active = bool(body.get("is_active", True))
    current = get_definition(name)
    if current is None:
        raise HTTPException(status_code=404, detail=f"Custom workflow {name!r} not found")
    if is_active:
        reviewed = body.get("definition")
        needs_review = any(isinstance(s, ActionStepSpec) for s in current.steps)
        changed = "This workflow changed since you opened it — reload to review the current version."
        if (needs_review or reviewed is not None) and not _reviewed_matches(current, reviewed):
            raise HTTPException(status_code=409, detail=changed)
        # Its tools must still resolve — same check as create/update.
        errors = await validate_definition_and_tools(current)
        if errors:
            raise HTTPException(status_code=422, detail=errors)
        # The check above may await the gateway, and chat may run in another
        # process: switch on only if the row is still the revision validated.
        switched = activate_if_unchanged(current)
        if switched is None:
            raise HTTPException(status_code=404, detail=f"Custom workflow {name!r} not found")
        if not switched:
            raise HTTPException(status_code=409, detail=changed)
    elif not set_active(name, is_active):
        raise HTTPException(status_code=404, detail=f"Custom workflow {name!r} not found")
    defn = get_definition(name)
    assert defn is not None
    _sync_cadence(defn)
    return defn.model_dump()


@router.get("/workflows/{name}")
async def get_workflow_meta(name: str) -> dict[str, Any]:
    try:
        workflow = get_workflow(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return workflow.meta().model_dump()


@router.get("/workflows/{name}/sample")
async def get_workflow_sample(name: str) -> dict[str, Any]:
    """Realistic sample inputs for the 'Load sample run' button.

    Returns 404 if the workflow does not exist or has no sample defined.
    """
    try:
        workflow = get_workflow(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    sample = workflow.sample_inputs()
    if sample is None:
        raise HTTPException(
            status_code=404, detail=f"Workflow {name!r} does not expose a sample"
        )
    return {"workflow": name, "inputs": sample}


@router.post("/workflows/{name}/runs")
async def start_workflow_run(name: str, request: Request) -> StreamingResponse:
    """Start a workflow run. Streams progress events as Server-Sent Events.

    Each SSE event is a JSON-encoded `WorkflowEvent`. The final event in
    a successful run is `{"type": "done", "run_id": "..."}`. A workflow that
    is the principal's alone in the workspace's mode is a 403 for anyone
    else (`refuse_principal_only_run`), before any run row is created.
    """
    try:
        workflow = get_workflow(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    from openexecutive.memory.workspace_settings import read_stored_mode

    refuse_principal_only_run(request, workflow, read_stored_mode(), surface="jobs")

    try:
        payload = await request.json()
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}") from e

    input_model_cls = workflow.input_model()
    try:
        inputs = input_model_cls.model_validate(payload)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors()) from e

    run_id = uuid.uuid4().hex
    title = _derive_title(workflow.name, payload)
    create_run(
        run_id=run_id,
        workflow_name=workflow.name,
        title=title,
        inputs=payload,
    )

    store = getattr(request.app.state, "store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Knowledge store not initialized")

    async def event_stream():
        t_start = time.monotonic()
        artifact: str = ""
        paused = False
        try:
            # Tell the client which run_id to track and what the planned steps are.
            yield _sse({
                "type": "run_created",
                "run_id": run_id,
                "title": title,
                "workflow": workflow.name,
                "steps": [s.model_dump() for s in workflow.steps()],
            })

            async for event in workflow.run(inputs=inputs, store=store):
                # An approval-gate step yields a WaitForHumanEvent (not a
                # WorkflowEvent): checkpoint the run, tell the client it is
                # paused, and stop. The resumer applies the timeout policy;
                # the inbound resolver records the human's reply.
                if isinstance(event, WaitForHumanEvent):
                    # Delivery + checkpoint live in `workflows.gate` so this
                    # route, the chat tool and the resumer cannot drift apart.
                    pause = await checkpoint_gate(
                        run_id=run_id,
                        event=event,
                        workflow_title=workflow.title,
                    )
                    paused = True
                    yield _sse({
                        "type": "awaiting_human",
                        "run_id": run_id,
                        "person_id": pause.person_id,
                        "question": pause.question,
                        "awaiting_until": pause.awaiting_until.isoformat(),
                        # "sent" / "self" / "suppressed" / "alerted" / "failed" —
                        # the client must not say "waiting on them" when the
                        # question never reached them.
                        "delivery": pause.delivery,
                        # True when the run will continue by itself once the
                        # answer lands; False for a pause-only gate.
                        "resumable": pause.resumable,
                        # No `done` or `error` follows a pause — this IS the
                        # last frame. Without it a client waiting for a
                        # terminal event just sees the connection close and
                        # spins on the gate step forever.
                        "terminal": True,
                    })
                    break

                event_dict = event.model_dump()
                if event.type == "artifact" and event.content is not None:
                    artifact = event.content
                yield _sse(event_dict)

            if paused:
                # Run left in 'awaiting_human'; the awaiting_human frame above
                # was terminal, so nothing more is emitted.
                pass
            elif artifact:
                complete_run(run_id=run_id, artifact=artifact)
                yield _sse({"type": "done", "run_id": run_id})
            else:
                fail_run(run_id=run_id, error="Workflow finished without producing an artifact")
                yield _sse({
                    "type": "error",
                    "run_id": run_id,
                    "message": "Workflow finished without producing an artifact",
                })
        except Exception as exc:  # noqa: BLE001 — must report any failure to the client
            logger.exception("workflow.run_failed run_id=%s workflow=%s", run_id, workflow.name)
            # NOT after a successful checkpoint. `fail_run` has no status
            # guard, so failing here would overwrite 'awaiting_human' with
            # 'error' and destroy the resume payload — for something as
            # ordinary as the client disconnecting while we yield the frame.
            # The run is safely parked; only the stream broke.
            if not paused:
                fail_run(run_id=run_id, error=str(exc))
            yield _sse({"type": "error", "run_id": run_id, "message": str(exc)})
        finally:
            logger.info(
                "workflow.run_complete run_id=%s workflow=%s duration_s=%.2f",
                run_id,
                workflow.name,
                time.monotonic() - t_start,
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering for SSE
        },
    )


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


PRINCIPAL_ONLY_DETAIL = "Only the principal can run this workflow."


def refuse_principal_only_run(
    request: Request,
    workflow: Any,
    mode: str | None,
    *,
    surface: str,
    detail: str = PRINCIPAL_ONLY_DETAIL,
    extra: dict[str, Any] | None = None,
) -> None:
    """Raise 403 unless this web caller may start ``workflow`` in ``mode``.

    This decides who may start (or, from an eval, trigger) a run, not who
    may read one: a stored run and its artifact are read under the
    ``/workflows/runs`` rules like any other run. A workflow whose
    ``principal_only_modes`` holds the mode it would run in (the weekly
    review in both modes, the morning brief in solo) may be started over
    HTTP only by the principal (``_caller_is_the_principal``), as
    ``run_workflow`` allows in chat. ``mode`` None (unreadable) counts as
    principal-only. The refusal is audited like the chat tool's, and
    ``detail`` does not spell out the rule.
    """
    from openexecutive.workflows.base import principal_only_in

    if not principal_only_in(workflow, mode):
        return
    if _caller_is_the_principal(request):
        return
    from openexecutive.api.routes.chat import _resolve_caller_person_id
    from openexecutive.audit import log_event as audit_log

    name = str(getattr(workflow, "name", "") or "")
    try:
        # For the audit row only; the refusal stands whatever this reads.
        caller_person_id = _resolve_caller_person_id(request)
    except Exception:
        caller_person_id = None
    audit_log(
        "tool_invocation",
        f"run_workflow {name} refused over HTTP ({surface}): not the principal",
        actor=(request.headers.get("x-caller-email") or "").strip()[:200] or "api",
        details={
            "tool": "run_workflow",
            "kind": "write",
            "ok": False,
            "workflow": name,
            "refused": True,
            "workspace_mode": mode or "unknown",
            "caller_person_id": caller_person_id,
            "surface": surface,
            **(extra or {}),
        },
    )
    raise HTTPException(status_code=403, detail=detail)


def _caller_is_the_principal(request: Request) -> bool:
    """Whether this web caller is the principal, for starting a principal-only
    run.

    Stricter than ``chat._caller_is_principal_or_unclaimed`` (the
    ``PUT /workspace`` rule, which is unchanged): nobody gets in because no
    principal is on the roster. That happens after the old principal is
    archived to re-run onboarding, or when the roster was filled in before
    anyone was flagged, and teammates can be signed in then. A request with
    no ``x-caller-email`` is the principal (the CLI, local login, a direct
    call behind the shared secret; the UI proxy stamps every other session's
    email), so it passes without a roster read. Otherwise the email must be
    the principal's own, on an entry that is not archived. Fails closed: a
    roster that cannot be read answers no.
    """
    if not (request.headers.get("x-caller-email") or "").strip():
        return True
    from openexecutive.api.routes.chat import _resolve_caller_person_id
    from openexecutive.people.store import is_principal_or_self

    try:
        return is_principal_or_self(_resolve_caller_person_id(request), None)
    except Exception:
        logger.exception("principal check failed — refusing the principal-only run")
        return False


def _resume_progress(resume_state_json: str | None) -> dict[str, Any] | None:
    """The client-safe digest of a paused run's resume payload.

    Enough for the UI to mark which steps are already done and which one is
    waiting; none of the step text itself.
    """
    if not resume_state_json:
        return None
    try:
        state = json.loads(resume_state_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(state, dict):
        return None
    outputs = state.get("outputs")
    return {
        "gate_step_id": str(state.get("gate_step_id") or ""),
        "completed_step_ids": list(outputs) if isinstance(outputs, dict) else [],
    }


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload)}\n\n"


async def _parse_definition(request: Request) -> DynamicWorkflowDef:
    """Parse + shape-validate a DynamicWorkflowDef from a request body."""
    try:
        payload = await request.json()
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}") from e
    try:
        return DynamicWorkflowDef.model_validate(payload)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors()) from e


def _sync_cadence(defn: DynamicWorkflowDef) -> None:
    """Reconcile the scheduler rows for a definition's cadence.

    Cancels any existing pending dynamic_workflow rows for this workflow, then
    enqueues the next occurrence if the (active) definition has a cadence. Used
    on create/update/activate so the schedule always matches the stored def.
    """
    from openexecutive.workflows.dynamic_cadence import (
        cancel_cadence_rows,
        schedule_dynamic_workflow_cadence,
    )

    cancel_cadence_rows(defn.name)
    if defn.is_active and defn.cadence:
        schedule_dynamic_workflow_cadence(defn)


_TITLE_MAX = 80


def _derive_title(workflow_name: str, payload: dict[str, Any]) -> str:
    """Best-effort short title for a run, surfaced in lists."""
    # Resolve via get_workflow (not WORKFLOW_REGISTRY) so dynamic, user-created
    # workflows get a real title instead of a KeyError.
    try:
        workflow = get_workflow(workflow_name)
    except KeyError:
        return workflow_name
    # Prefer a payload field that looks like a period/quarter label.
    for field in ("quarter_label", "period", "title", "topic"):
        if field in payload and payload[field]:
            base = f"{workflow.title} — {payload[field]}"
            return base[:_TITLE_MAX]
    return workflow.title
