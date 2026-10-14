"""The Reflection screen's look back (chat_review/): what last night's review
found, the lessons the owner approved, and the owner's switches.

GET    /reflection/look-back                 — settings, this week's counts, the runs with their findings, the lessons
PUT    /reflection/look-back/settings        — the owner's switch (on/off)
POST   /reflection/findings/{id}/approve     — a lesson proposal becomes an active lesson (optionally edited)
POST   /reflection/findings/{id}/dismiss     — the owner passed on it
PUT    /reflection/lessons/{id}              — edit or lock a lesson
DELETE /reflection/lessons/{id}              — retire a lesson

Owner only: every route answers 403 to anyone else, since the review reads the
owner's own conversations. Nothing here notifies anyone.

Every lesson is approved by a person here. Nothing on the review path creates
a lesson from model output on its own: ``apply_mode`` is stored for a later
"apply on its own" step and is always ``ask`` today, so the API refuses any
other value.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from openexecutive.api.routes.people import caller_is_principal
from openexecutive.audit import log_event
from openexecutive.chat_review import lessons as lessons_mod
from openexecutive.chat_review import store

router = APIRouter()
logger = logging.getLogger(__name__)


class LookBackSettings(BaseModel):
    enabled: bool
    apply_mode: str
    clean_approvals: int
    last_run_at: str | None


class LookBackSettingsUpdate(BaseModel):
    enabled: bool | None = None
    # Only "ask" exists today; anything else is a 422 from the pattern.
    apply_mode: str | None = Field(None, pattern="^ask$")


class LookBackResponse(BaseModel):
    settings: LookBackSettings
    week: dict[str, int]
    runs: list[dict[str, Any]]
    lessons: list[dict[str, Any]]
    waiting: list[dict[str, Any]]


class FindingApprove(BaseModel):
    text: str | None = None


class FindingDismiss(BaseModel):
    reason: str = ""


class LessonUpdate(BaseModel):
    text: str | None = None
    locked: bool | None = None


class LessonRetire(BaseModel):
    reason: str = ""


def _owner(request: Request) -> int | None:
    """The owner's person id, or 403."""
    if not caller_is_principal(request):
        raise HTTPException(status_code=403, detail="Only the owner can see the look back.")
    from openexecutive.api.routes.chat import _resolve_caller_person_id

    try:
        return _resolve_caller_person_id(request)
    except Exception:  # noqa: BLE001 - the check above already passed; the id is for the record.
        return None


def _settings_payload() -> LookBackSettings:
    s = store.get_settings()
    clean = store.clean_approvals()
    return LookBackSettings(
        enabled=s.enabled,
        apply_mode=s.apply_mode,
        clean_approvals=clean,
        last_run_at=s.last_run_at,
    )


@router.get("/reflection/look-back", response_model=LookBackResponse)
def get_look_back(request: Request, runs: int = 30) -> LookBackResponse:
    _owner(request)
    run_rows = store.list_runs(limit=max(1, min(runs, 120)))
    by_run: dict[int, list[dict[str, Any]]] = {r.id: [] for r in run_rows}
    for f in store.list_findings(limit=1000):
        if f.run_id in by_run:
            by_run[f.run_id].append(f.as_dict())
    out_runs = []
    for r in run_rows:
        row = r.__dict__.copy()
        row["findings"] = sorted(by_run.get(r.id, []), key=lambda x: x["id"])
        out_runs.append(row)
    return LookBackResponse(
        settings=_settings_payload(),
        week=store.week_summary(),
        runs=out_runs,
        lessons=[lsn.as_dict() for lsn in store.list_lessons()],
        waiting=[f.as_dict() for f in store.list_findings(status="proposed")],
    )


@router.put("/reflection/look-back/settings", response_model=LookBackSettings)
def put_look_back_settings(request: Request, body: LookBackSettingsUpdate) -> LookBackSettings:
    person_id = _owner(request)
    store.save_settings(enabled=body.enabled, apply_mode=body.apply_mode, updated_by=f"person:{person_id}")
    log_event(
        "chat_review",
        "Look back settings changed",
        actor="user",
        details={"enabled": body.enabled, "apply_mode": body.apply_mode, "person_id": person_id},
    )
    return _settings_payload()


@router.post("/reflection/findings/{finding_id}/approve")
def approve_finding(request: Request, finding_id: int, body: FindingApprove) -> dict[str, Any]:
    person_id = _owner(request)
    finding = store.get_finding(finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="No such finding.")
    if finding.status != "proposed":
        raise HTTPException(status_code=409, detail="This one was already decided.")
    if finding.kind != "lesson":
        # Gaps and product issues have nothing to apply; approving means "noted".
        resolved = store.resolve_finding(finding_id, status="approved", person_id=person_id)
        if resolved is None:
            raise HTTPException(status_code=409, detail="This one was already decided.")
        return {"finding": resolved.as_dict(), "lesson": None}
    text = lessons_mod.normalize(body.text if body.text is not None else finding.proposal)
    why = lessons_mod.lesson_rejection(text)
    if why:
        raise HTTPException(status_code=400, detail=f"That can't be a lesson: {why}.")
    edited = text != lessons_mod.normalize(finding.proposal)
    # Decide the finding first: the conditional update wins for exactly one
    # caller, so two approvals of one proposal make one lesson, not two.
    resolved = store.resolve_finding(
        finding_id,
        status="approved",
        person_id=person_id,
        note="edited" if edited else "",
    )
    if resolved is None:
        raise HTTPException(status_code=409, detail="This one was already decided.")
    lesson = store.create_lesson(
        text,
        source_finding_id=finding.id,
        approved_by_person_id=person_id,
        updated_by=f"person:{person_id}",
    )
    resolved = store.attach_lesson(finding_id, lesson.id)
    log_event(
        "chat_review",
        f"Lesson approved{' with edits' if edited else ''}: {text[:120]}",
        actor="user",
        details={"finding_id": finding.id, "lesson_id": lesson.id, "edited": edited, "person_id": person_id},
    )
    return {"finding": resolved.as_dict() if resolved else None, "lesson": lesson.as_dict()}


@router.post("/reflection/findings/{finding_id}/dismiss")
def dismiss_finding(request: Request, finding_id: int, body: FindingDismiss) -> dict[str, Any]:
    person_id = _owner(request)
    finding = store.get_finding(finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="No such finding.")
    if finding.status != "proposed":
        raise HTTPException(status_code=409, detail="This one was already decided.")
    resolved = store.resolve_finding(finding_id, status="dismissed", person_id=person_id, note=body.reason)
    if resolved is None:
        raise HTTPException(status_code=409, detail="This one was already decided.")
    log_event(
        "chat_review",
        f"Finding dismissed: {finding.title[:120]}",
        actor="user",
        details={"finding_id": finding.id, "kind": finding.kind, "person_id": person_id},
    )
    return {"finding": resolved.as_dict()}


@router.put("/reflection/lessons/{lesson_id}")
def update_lesson(request: Request, lesson_id: int, body: LessonUpdate) -> dict[str, Any]:
    person_id = _owner(request)
    if store.get_lesson(lesson_id) is None:
        raise HTTPException(status_code=404, detail="No such lesson.")
    text = None
    if body.text is not None:
        text = lessons_mod.normalize(body.text)
        why = lessons_mod.lesson_rejection(text)
        if why:
            raise HTTPException(status_code=400, detail=f"That can't be a lesson: {why}.")
    lesson = store.update_lesson(lesson_id, text=text, locked=body.locked, updated_by=f"person:{person_id}")
    log_event(
        "chat_review",
        f"Lesson edited: {(text or '')[:120]}" if text else "Lesson lock changed",
        actor="user",
        details={"lesson_id": lesson_id, "locked": body.locked, "person_id": person_id},
    )
    return {"lesson": lesson.as_dict() if lesson else None}


@router.delete("/reflection/lessons/{lesson_id}")
def retire_lesson(request: Request, lesson_id: int, body: LessonRetire | None = None) -> dict[str, Any]:
    person_id = _owner(request)
    if store.get_lesson(lesson_id) is None:
        raise HTTPException(status_code=404, detail="No such lesson.")
    reason = body.reason if body else ""
    lesson = store.retire_lesson(lesson_id, reason=reason, updated_by=f"person:{person_id}")
    log_event(
        "chat_review",
        "Lesson retired",
        actor="user",
        details={"lesson_id": lesson_id, "reason": reason[:200], "person_id": person_id},
    )
    return {"lesson": lesson.as_dict() if lesson else None}
