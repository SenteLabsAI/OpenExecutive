from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException

from openexecutive.api.authorization import require_principal
from openexecutive.api.models import OnboardAnswerRequest, OnboardStatusResponse
from openexecutive.onboarding.wizard import (
    TOTAL_STEPS,
    WizardState,
    get_current_question,
    get_step,
    process_answer,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_wizard_sessions: dict[str, WizardState] = {}
_wizard_session_owners: dict[str, str] = {}
_onboarding_completion_lock = asyncio.Lock()
# Hold strong refs to background research runs so GC can't cancel
# them mid-flight (mirrors alerts/pipeline.py). Auto-cleared via
# add_done_callback after the task finishes.
_background_research_tasks: set[asyncio.Task] = set()
# Per-session dedup — a client that retries the final answer between
# our process_answer commit and the 400 response would otherwise spawn
# a second research run. Persistence is OK at module scope: a process
# restart loses the set but onboarding is rare, the cap on the auto-
# fire below means duplicates are still bounded by elapsed time.
_onboarding_research_fired: set[str] = set()
# Hard ceiling on a single auto-fire — 7 specialists × web_search +
# tool-use should finish well under this, but we don't want a hung
# provider to leak a long-lived background task.
_RESEARCH_WALLCLOCK_TIMEOUT_SECONDS = 600


def _verified_caller_email(x_caller_email: str | None) -> str:
    if not x_caller_email or not x_caller_email.strip():
        raise HTTPException(status_code=403, detail="Verified caller identity required.")
    return x_caller_email.strip().lower()


def _require_onboarding_access(caller_email: str) -> None:
    """Allow bootstrap once; afterward only the canonical principal may onboard."""
    from openexecutive.api.authorization import configured_principal_email
    from openexecutive.people.store import find_principal_person

    principal = find_principal_person()
    configured_principal = configured_principal_email()
    if principal is not None:
        require_principal(caller_email)
    elif configured_principal and caller_email != configured_principal:
        raise HTTPException(status_code=403, detail="Principal access required.")


def _step_required(state: WizardState) -> bool:
    step = get_step(state.current_step)
    return bool(step and step["required"])


def _invalidate_other_owner_sessions(_owner_email: str, completed_session_id: str) -> None:
    """A completed wizard makes every pre-completion snapshot stale and unsafe."""
    for session_id in list(_wizard_session_owners):
        if session_id != completed_session_id:
            _wizard_session_owners.pop(session_id, None)
            _wizard_sessions.pop(session_id, None)


@router.get("/onboard/start", response_model=OnboardStatusResponse)
async def start_onboarding(
    x_caller_email: Annotated[str | None, Header(alias="X-Caller-Email")] = None,
) -> OnboardStatusResponse:
    caller_email = _verified_caller_email(x_caller_email)
    _require_onboarding_access(caller_email)
    session_id = str(uuid.uuid4())
    state = WizardState()
    _wizard_sessions[session_id] = state
    _wizard_session_owners[session_id] = caller_email

    question = get_current_question(state)
    progress = state.get_progress()

    return OnboardStatusResponse(
        session_id=session_id,
        current_step=state.current_step,
        total_steps=TOTAL_STEPS,
        current_question=question,
        current_step_required=_step_required(state),
        progress_percent=progress["percent"],
        completed=state.completed,
    )


@router.post("/onboard/answer", response_model=OnboardStatusResponse)
async def submit_answer(
    body: OnboardAnswerRequest,
    x_caller_email: Annotated[str | None, Header(alias="X-Caller-Email")] = None,
) -> OnboardStatusResponse:
    caller_email = _verified_caller_email(x_caller_email)
    state = _wizard_sessions.get(body.session_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Onboarding session not found")
    if _wizard_session_owners.get(body.session_id) != caller_email:
        raise HTTPException(status_code=403, detail="Onboarding session belongs to another user.")
    _require_onboarding_access(caller_email)
    if state.completed:
        raise HTTPException(status_code=400, detail="Onboarding already completed")

    state = process_answer(state, body.answer)
    _wizard_sessions[body.session_id] = state

    if state.completed:
        async with _onboarding_completion_lock:
            # A sibling completion can invalidate this session while it waits
            # for the lock; never save an obsolete wizard snapshot.
            if _wizard_sessions.get(body.session_id) is not state:
                raise HTTPException(status_code=409, detail="Onboarding session is stale.")
            _require_onboarding_access(caller_email)
            from openexecutive.onboarding.profile_builder import build_and_save_profile

            build_and_save_profile(state, principal_email=caller_email)
            _invalidate_other_owner_sessions(caller_email, body.session_id)

            # Fire the watchlist-research workflow once at onboarding
            # completion so the principal's first /today after install
            # carries a "here's what I researched we should be watching"
            # alert. Best-effort — a research failure must NOT block
            # finishing onboarding, so the helper swallows exceptions.
            # Dedup on session_id so a duplicate completion (client retry
            # before we 400) can't double-fire.
            if body.session_id not in _onboarding_research_fired:
                _onboarding_research_fired.add(body.session_id)
                task = asyncio.create_task(
                    _fire_post_onboarding_research(body.session_id)
                )
                _background_research_tasks.add(task)
                # Auto-cleanup so the set doesn't grow unboundedly across
                # the process lifetime.
                task.add_done_callback(_background_research_tasks.discard)

    question = get_current_question(state) if not state.completed else None
    progress = state.get_progress()

    return OnboardStatusResponse(
        session_id=body.session_id,
        current_step=state.current_step,
        total_steps=TOTAL_STEPS,
        current_question=question,
        current_step_required=_step_required(state),
        progress_percent=progress["percent"],
        completed=state.completed,
    )


async def _fire_post_onboarding_research(session_id: str) -> None:
    """Run the executive_research workflow once at end of onboarding.

    The workflow itself routes findings via the Executive's outbound
    toolkit — DMs to heads of departments, briefing alerts via
    create_alert, watchlist additions via add_watchlist_entry, etc.
    This wrapper just runs the workflow with a wall-clock ceiling and
    creates / completes the workflow_run row for audit.

    Best-effort: any exception is logged and swallowed. Onboarding
    has already returned 200 to the client by the time this fires.
    """
    from openexecutive.workflows.persistence import (
        complete_run,
        create_run,
        fail_run,
    )

    run_id = str(uuid.uuid4())
    last_error = ""
    try:
        from openexecutive.config import get_settings
        from openexecutive.knowledge.store import ChromaDBStore
        from openexecutive.workflows import WORKFLOW_REGISTRY

        workflow = WORKFLOW_REGISTRY["executive_research"]
        input_cls = workflow.input_model()
        wf_inputs = input_cls(note="initial post-onboarding research run")

        try:
            create_run(
                run_id,
                "executive_research",
                f"{workflow.title} (post-onboarding auto-fire)",
                wf_inputs.model_dump(),
            )
        except Exception:
            logger.exception("post-onboarding research: create_run failed")

        store = ChromaDBStore(persist_directory=get_settings().vector_store_path)
        artifact = ""

        async def _run() -> str:
            captured = ""
            async for event in workflow.run(inputs=wf_inputs, store=store):
                if event.type == "artifact" and event.content:
                    captured = event.content
                elif event.type == "error" and event.message:
                    raise RuntimeError(event.message)
            return captured

        artifact = await asyncio.wait_for(
            _run(), timeout=_RESEARCH_WALLCLOCK_TIMEOUT_SECONDS,
        )

        try:
            complete_run(run_id, artifact or "(no artifact)")
        except Exception:
            logger.exception("post-onboarding research: complete_run failed")
    except TimeoutError:
        last_error = "wall-clock timeout"
        logger.warning(
            "post-onboarding research timed out after %ds (session=%s)",
            _RESEARCH_WALLCLOCK_TIMEOUT_SECONDS, session_id,
        )
    except Exception as exc:
        last_error = str(exc)[:200]
        logger.exception(
            "post-onboarding research failed (session=%s)", session_id,
        )

    if last_error:
        try:
            fail_run(run_id, last_error)
        except Exception:
            logger.exception("post-onboarding research: fail_run failed")


@router.get("/onboard/status/{session_id}", response_model=OnboardStatusResponse)
async def get_onboard_status(
    session_id: str,
    x_caller_email: Annotated[str | None, Header(alias="X-Caller-Email")] = None,
) -> OnboardStatusResponse:
    caller_email = _verified_caller_email(x_caller_email)
    state = _wizard_sessions.get(session_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Onboarding session not found")
    if _wizard_session_owners.get(session_id) != caller_email:
        raise HTTPException(status_code=403, detail="Onboarding session belongs to another user.")
    _require_onboarding_access(caller_email)

    question = get_current_question(state) if not state.completed else None
    progress = state.get_progress()

    return OnboardStatusResponse(
        session_id=session_id,
        current_step=state.current_step,
        total_steps=TOTAL_STEPS,
        current_question=question,
        current_step_required=_step_required(state),
        progress_percent=progress["percent"],
        completed=state.completed,
    )
