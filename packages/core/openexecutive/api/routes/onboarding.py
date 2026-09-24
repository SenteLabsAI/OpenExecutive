from __future__ import annotations

import asyncio
import copy
import logging
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from openexecutive.api.intake_uploads import (
    _INTAKE_GEN_CHARS_PER_FILE,
    _gather_intake_attachments,
)
from openexecutive.api.models import (
    ONBOARD_ANSWER_MAX_CHARS,
    ONBOARD_MESSAGE_MAX_CHARS,
    CompanyProfileResponse,
    OnboardAnswerRequest,
    OnboardCommitRequest,
    OnboardDepartmentDraft,
    OnboardMessageRequest,
    OnboardPersonDraft,
    OnboardSessionRequest,
    OnboardSessionResponse,
    OnboardStatusResponse,
    OnboardTranscriptTurn,
    OnboardTurnResponse,
)
from openexecutive.config import get_settings
from openexecutive.memory.company_profile import CompanyProfile
from openexecutive.onboarding.interview import (
    MAX_QUESTIONS,
    MAX_TRANSCRIPT_CHARS,
    OPENING_PROMPT,
    CompanyDraft,
    Turn,
    transcript_chars,
    validate_draft,
)
from openexecutive.onboarding.wizard import (
    TOTAL_STEPS,
    WizardState,
    get_current_question,
    process_answer,
)
from openexecutive.workflows.gate import ensure_workflow_event

logger = logging.getLogger(__name__)

router = APIRouter()

_wizard_sessions: dict[str, WizardState] = {}
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


@router.get("/onboard/start", response_model=OnboardStatusResponse)
async def start_onboarding() -> OnboardStatusResponse:
    session_id = str(uuid.uuid4())
    state = WizardState()
    _wizard_sessions[session_id] = state

    question = get_current_question(state)
    progress = state.get_progress()

    return OnboardStatusResponse(
        session_id=session_id,
        current_step=state.current_step,
        total_steps=TOTAL_STEPS,
        current_question=question,
        progress_percent=progress["percent"],
        completed=state.completed,
    )


@router.post("/onboard/answer", response_model=OnboardStatusResponse)
async def submit_answer(body: OnboardAnswerRequest) -> OnboardStatusResponse:
    state = _wizard_sessions.get(body.session_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Onboarding session not found")
    if state.completed:
        raise HTTPException(status_code=400, detail="Onboarding already completed")
    if len(body.answer) > ONBOARD_ANSWER_MAX_CHARS:
        raise HTTPException(
            status_code=422,
            detail=f"Answer is too long (limit {ONBOARD_ANSWER_MAX_CHARS:,} characters).",
        )

    # process_answer mutates the stored state in place. Keep a snapshot so
    # a failed profile build on the final answer can be rolled back —
    # otherwise the session is stuck at completed=True and every retry
    # hits the 400 above, forcing the user to restart onboarding.
    snapshot = copy.deepcopy(state)
    state = process_answer(state, body.answer)

    if state.completed:
        from openexecutive.onboarding.profile_builder import build_and_save_profile

        try:
            build_and_save_profile(state)
        except Exception as exc:
            # Type name only: a pydantic ValidationError's str() embeds the
            # offending input, and the wizard answers include financials
            # the UI promises are "stored locally only".
            logger.error(
                "onboarding: profile build failed on the final answer (%s)",
                type(exc).__name__,
            )
            _wizard_sessions[body.session_id] = snapshot
            raise HTTPException(
                status_code=422,
                detail=(
                    "Could not build the company profile from your answers. "
                    "Rephrase your last answer and try again, or restart "
                    "onboarding if an earlier answer is the problem."
                ),
            ) from exc

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

    _wizard_sessions[body.session_id] = state

    question = get_current_question(state) if not state.completed else None
    progress = state.get_progress()

    return OnboardStatusResponse(
        session_id=body.session_id,
        current_step=state.current_step,
        total_steps=TOTAL_STEPS,
        current_question=question,
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
                event = ensure_workflow_event(event, site='onboarding.post_onboarding_research')
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
async def get_onboard_status(session_id: str) -> OnboardStatusResponse:
    state = _wizard_sessions.get(session_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Onboarding session not found")

    question = get_current_question(state) if not state.completed else None
    progress = state.get_progress()

    return OnboardStatusResponse(
        session_id=session_id,
        current_step=state.current_step,
        total_steps=TOTAL_STEPS,
        current_question=question,
        progress_percent=progress["percent"],
        completed=state.completed,
    )


# ── conversational onboarding (/onboard/interview/*) ─────────────────────────
#
# The user describes their business, the interviewer asks clarifying questions,
# and the result is a DRAFT the user reviews and edits before anything is
# written. The step-by-step wizard above stays reachable as a fallback.
#
# Two properties this code exists to preserve, both tested:
#
# 1. **Nothing echoes the user's input.** Every 4xx/5xx detail below is a fixed
#    string, and every log line carries an exception TYPE, not its message.
#    These transcripts contain ARR, burn, and runway.
#
# 2. **The commit is write-once and ordered.** Validation happens before the
#    first write, so a failure leaves the session exactly as it was and the
#    user can fix the draft and retry. That replaces the deepcopy-snapshot
#    rollback the wizard needs (process_answer mutates before it can fail);
#    here the interview never writes at all, so ordering alone is enough.


@dataclass
class InterviewSession:
    transcript: list[Turn] = field(default_factory=list)
    questions_asked: int = 0
    draft: CompanyDraft | None = None
    # Hint for the LATEST question only. Lives on the session (not a side
    # dict) so it is swept with everything else and cannot leak.
    last_hint: str = ""
    saved: bool = False
    last_touched: float = field(default_factory=time.monotonic)


# In-memory, single-process — same assumption as _wizard_sessions above, but
# with the TTL and cap that one is missing. Deliberately NOT persisted: the
# transcript is unedited free text containing financials, and the artifact
# worth keeping (the draft) is already in the client's hands. A page refresh
# is covered by GET /onboard/interview/{id}.
_interview_sessions: OrderedDict[str, InterviewSession] = OrderedDict()
_INTERVIEW_TTL_SECONDS = 2 * 3600
_MAX_INTERVIEW_SESSIONS = 50


def _sweep_interview_sessions() -> None:
    now = time.monotonic()
    for sid in [
        s
        for s, sess in _interview_sessions.items()
        if now - sess.last_touched > _INTERVIEW_TTL_SECONDS
    ]:
        _interview_sessions.pop(sid, None)
    while len(_interview_sessions) > _MAX_INTERVIEW_SESSIONS:
        _interview_sessions.popitem(last=False)


def _get_interview(session_id: str) -> InterviewSession:
    _sweep_interview_sessions()
    session = _interview_sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Setup session not found or expired.")
    session.last_touched = time.monotonic()
    _interview_sessions.move_to_end(session_id)
    return session


def _turn_response(session_id: str, session: InterviewSession) -> OnboardTurnResponse:
    """Render a session's current state. Phase is driven by draft presence."""
    if session.draft is not None:
        d = session.draft
        return OnboardTurnResponse(
            session_id=session_id,
            phase="draft",
            questions_asked=session.questions_asked,
            max_questions=MAX_QUESTIONS,
            draft=CompanyProfileResponse(**d.profile.model_dump()),
            draft_people=[
                OnboardPersonDraft(**p.model_dump()) for p in d.people
            ],
            draft_departments=[
                OnboardDepartmentDraft(
                    title=dep.title,
                    mission=dep.mission,
                    head_person_name=dep.head_person_name,
                    authority_level=dep.authority_level.value,
                )
                for dep in d.departments
            ],
            confidence_notes=list(d.confidence_notes),
            summary=d.summary,
        )
    question = ""
    for turn in reversed(session.transcript):
        if turn.role == "assistant":
            question = turn.text
            break
    return OnboardTurnResponse(
        session_id=session_id,
        phase="question",
        questions_asked=session.questions_asked,
        max_questions=MAX_QUESTIONS,
        question=question or OPENING_PROMPT,
        question_hint=session.last_hint or None,
    )



async def _advance_interview(
    session_id: str, session: InterviewSession, *, force_draft: bool = False
) -> OnboardTurnResponse:
    """Run one interviewer turn and fold the result into the session."""
    from openexecutive.onboarding.interview import (
        InterviewError,
        InterviewTimeout,
        advance,
    )
    from openexecutive.onboarding.profile_builder import load_or_create_profile

    existing = load_or_create_profile()
    try:
        result = await advance(
            session.transcript,
            existing_profile=None if existing.is_empty() else existing,
            force_draft=force_draft,
            questions_asked=session.questions_asked,
        )
    except InterviewTimeout as exc:
        logger.warning("onboarding interview: timed out (%s)", type(exc).__name__)
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except InterviewError as exc:
        # InterviewError messages are fixed, input-free strings by contract.
        logger.error("onboarding interview: failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if isinstance(result, CompanyDraft):
        session.draft = result
        session.last_hint = ""
        # Record the draft as an assistant turn. Without it the transcript ends
        # on a user turn, and the next message would make two user turns in a
        # row — which the Messages API rejects, bricking the session.
        session.transcript.append(
            Turn(
                role="assistant",
                text=result.summary.strip()
                or f"(drafted a profile for {result.profile.name})",
            )
        )
    else:
        # A new question supersedes any previous draft — but only now that the
        # call has succeeded.
        session.draft = None
        session.transcript.append(Turn(role="assistant", text=result.question))
        session.questions_asked += 1
        session.last_hint = result.hint
    return _turn_response(session_id, session)


@router.post("/onboard/interview/start", response_model=OnboardTurnResponse)
async def start_interview(
    description: str = Form(""),
    files: list[UploadFile] = File(  # noqa: B008 — FastAPI multipart marker, mirrors chat.py
        default_factory=list
    ),
) -> OnboardTurnResponse:
    """Open a setup session from a free-text description plus optional files.

    Returns the fixed opening prompt when no description is given — that path
    costs no model call and cannot fail.
    """
    if len(description) > ONBOARD_MESSAGE_MAX_CHARS:
        raise HTTPException(
            status_code=422,
            detail=f"Description is too long (limit {ONBOARD_MESSAGE_MAX_CHARS:,} characters).",
        )

    # Extract BEFORE registering a session. 400/413 from here propagate
    # unchanged (they name a filename and a limit, never file contents), and
    # doing it first means a rejected upload cannot leave an orphan session
    # behind — 50 of those would evict every live session via the cap sweep.
    extracted = await _gather_intake_attachments(files or [])

    session_id = str(uuid.uuid4())
    session = InterviewSession()
    _interview_sessions[session_id] = session
    # Sweep AFTER inserting: the cap is an invariant on what is stored, and
    # sweeping first would let the dict sit one over the cap until the next
    # request touched it.
    _sweep_interview_sessions()

    opening = description.strip()
    for name, text in extracted:
        opening += f"\n\n=== Attached: {name} ===\n{text[:_INTAKE_GEN_CHARS_PER_FILE]}"

    if not opening.strip():
        # Nothing to work with yet — ask the opening question without a model
        # call so an empty start is instant and free.
        return _turn_response(session_id, session)

    session.transcript.append(Turn(role="user", text=opening.strip()))
    try:
        return await _advance_interview(session_id, session)
    except HTTPException:
        # The error body carries no session_id, so the client can never resume
        # this one — leaving it in the dict would orphan it until the TTL, and
        # a burst of failures during a provider blip would evict live sessions.
        _interview_sessions.pop(session_id, None)
        raise


@router.post("/onboard/interview/message", response_model=OnboardTurnResponse)
async def interview_message(body: OnboardMessageRequest) -> OnboardTurnResponse:
    # Bounded here, not with Field(max_length=...) — FastAPI's 422 body echoes
    # the rejected input, and this message may carry financials.
    if len(body.message) > ONBOARD_MESSAGE_MAX_CHARS:
        raise HTTPException(
            status_code=422,
            detail=f"Message is too long (limit {ONBOARD_MESSAGE_MAX_CHARS:,} characters).",
        )
    session = _get_interview(body.session_id)
    if session.saved:
        raise HTTPException(status_code=409, detail="This setup session was already saved.")
    if not body.message.strip():
        raise HTTPException(status_code=422, detail="Message is empty.")

    # Bound the conversation in memory as well as in questions. The budget in
    # interview.py only forces a draft; without this a client could grow the
    # transcript (and the payload re-sent every turn) without limit.
    if transcript_chars(session.transcript) + len(body.message) > MAX_TRANSCRIPT_CHARS:
        raise HTTPException(
            status_code=422,
            detail=(
                "This setup conversation has gotten long. Draft the profile "
                "now and edit it directly."
            ),
        )

    session.transcript.append(Turn(role="user", text=body.message.strip()))
    # NOTE: the stale draft is cleared by _advance_interview only once the new
    # turn succeeds. Clearing it up front would mean a 502 destroyed the draft
    # the user was in the middle of reviewing.
    return await _advance_interview(body.session_id, session)


@router.post("/onboard/interview/draft", response_model=OnboardTurnResponse)
async def interview_draft(body: OnboardSessionRequest) -> OnboardTurnResponse:
    """Force a draft now, however much is still unknown."""
    session = _get_interview(body.session_id)
    if session.saved:
        raise HTTPException(status_code=409, detail="This setup session was already saved.")
    if not session.transcript:
        raise HTTPException(
            status_code=422,
            detail="Tell me a little about your company first.",
        )
    return await _advance_interview(body.session_id, session, force_draft=True)


@router.get("/onboard/interview/{session_id}", response_model=OnboardSessionResponse)
async def get_interview(session_id: str) -> OnboardSessionResponse:
    """Resume after a page refresh: the transcript plus any existing draft."""
    session = _get_interview(session_id)
    base = _turn_response(session_id, session)
    return OnboardSessionResponse(
        **base.model_dump(),
        turns=[
            OnboardTranscriptTurn(role=t.role, text=t.text) for t in session.transcript
        ],
        saved=session.saved,
    )


@router.post("/onboard/interview/commit", response_model=CompanyProfileResponse)
async def commit_interview(body: OnboardCommitRequest) -> CompanyProfileResponse:
    """Save the reviewed draft. The single write in this whole flow.

    Ordering matters and is load-bearing — see the section header above.
    Validation and the empty-name check run before ``save_to_yaml``, so any
    rejection leaves the session untouched and the user can edit and retry.
    """
    from openexecutive.onboarding.commit import (
        derive_org_structure,
        reconcile_onboarding_departments,
        save_onboarding_people,
    )
    from openexecutive.onboarding.interview import DepartmentDraft, PersonDraft

    session = _get_interview(body.session_id)
    if session.saved:
        raise HTTPException(
            status_code=409,
            detail=(
                "This setup session was already saved. Edit your company "
                "profile on the Company Profile page."
            ),
        )

    settings = get_settings()

    # 1. Validate — nothing is written yet.
    # model_dump already converted every nested model to a plain dict, so this
    # is the merge input as-is.
    update_data = body.profile.model_dump(exclude_unset=True)
    try:
        people = [PersonDraft(**p.model_dump()) for p in body.people]
        departments = [DepartmentDraft(**d.model_dump()) for d in body.departments]
        merged = CompanyProfile().model_copy(update=update_data)
        profile = CompanyProfile.model_validate(merged.model_dump())
    except Exception as exc:
        # Type name only: a ValidationError's str() embeds the offending input.
        logger.error(
            "onboarding commit: profile did not validate (%s)", type(exc).__name__
        )
        raise HTTPException(
            status_code=422,
            detail=(
                "That profile could not be saved. Check that numbers are "
                "numbers and no field was left in a bad state, then try again."
            ),
        ) from exc

    # The body is client-supplied and need not match the drafted session, so the
    # referential invariants have to be re-checked HERE, not only inside
    # advance(). Without this a caller can save a company with no principal at
    # all — find_principal_person() then returns None and caller resolution,
    # alert routing and the scheduler's brief have nobody to target.
    errors = validate_draft(
        CompanyDraft(profile=profile, people=people, departments=departments)
    )
    if errors:
        # .safe, never .detail — the detailed rendering quotes the rejected
        # value (a name, a head reference), which is client text sitting next
        # to the company's financials. Only the repair turn sees .detail.
        logger.info("onboarding commit: rejected draft (%d error(s))", len(errors))
        raise HTTPException(status_code=422, detail=errors[0].safe)

    profile = derive_org_structure(profile, people, departments)

    # 2. is_empty() keys off the name — a nameless profile is invisible to
    #    every consumer, including the health check that gates onboarding.
    if not profile.name.strip():
        raise HTTPException(status_code=422, detail="Your company needs a name.")

    # 3. The write. Atomic (write-then-rename, O_EXCL, fsync) inside.
    try:
        profile.save_to_yaml(settings.company_profile_path)
    except Exception as exc:
        logger.error("onboarding commit: save failed (%s)", type(exc).__name__)
        raise HTTPException(
            status_code=422,
            detail="Could not save the company profile. Try again.",
        ) from exc

    # 4. Past this point the profile exists; nothing below may fail the request.
    session.saved = True

    # 5-6. Best-effort seeding. Departments are reconciled additively — see
    #      onboarding/commit.py for why this must not mirror the fixture loader.
    person_ids = save_onboarding_people(people)
    reconcile_onboarding_departments(departments, person_ids)

    # 7. Same post-onboarding research fire the wizard does, same dedup set.
    if body.session_id not in _onboarding_research_fired:
        _onboarding_research_fired.add(body.session_id)
        task = asyncio.create_task(_fire_post_onboarding_research(body.session_id))
        _background_research_tasks.add(task)
        task.add_done_callback(_background_research_tasks.discard)

    return CompanyProfileResponse(**profile.model_dump())
