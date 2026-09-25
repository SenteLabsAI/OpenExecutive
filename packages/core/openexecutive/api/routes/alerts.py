from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from openexecutive.alerts import lifecycle, store
from openexecutive.alerts.models import Alert, is_private_alert

# The standalone alerts UI (panel, live toast stream, mute/severity settings,
# feedback) was removed — those items now surface only through the briefing
# (`/today`), which reads the live alert view (alerts/lifecycle.py). The
# ingestion → triage → storage pipeline is unchanged; this router exposes the
# queue-grooming endpoints the briefing calls: single ack, bulk ack, reopen
# (the Undo for every autonomous close / expiry) and an on-demand run of the
# Executive's relevance review.
router = APIRouter()


def _visible_alert(alert_id: int, request: Request) -> Alert:
    """The alert, or 404 — also for one private to the principal when the
    caller is someone else, so its existence is not revealed either."""
    existing = store.get_alert(alert_id)
    if existing is None or (is_private_alert(existing) and not _caller_is_principal(request)):
        raise HTTPException(status_code=404, detail="Alert not found")
    return existing


def _caller_is_principal(request: Request) -> bool:
    from openexecutive.api.routes.people import caller_is_principal

    return caller_is_principal(request)


class AckBody(BaseModel):
    status: str = Field(..., pattern="^(read|ack|dismissed)$")
    # Optional on a dismiss: a topic pattern to mute (see alerts/preferences
    # matches_mute), or true to derive one from the alert's own tags
    # (`external:<watch slug>` / `department:<slug>` / first tag).
    mute_topic: str | bool | None = None


@router.post("/alerts/{alert_id}/ack", response_model=Alert)
def ack_alert(alert_id: int, body: AckBody, request: Request) -> Alert:
    existing = _visible_alert(alert_id, request)
    if not store.set_status(alert_id, body.status):
        raise HTTPException(status_code=404, detail="Alert not found")
    # Feedback loop: a dismiss on a watch-sourced alert lowers that watch's
    # trust score; an approval recovers it. Optional mute on dismiss. Only
    # on a real transition — re-posting the same status teaches nothing.
    if existing.status != body.status:
        lifecycle.record_ack_feedback(existing, body.status)
        if body.status == "dismissed":
            lifecycle.apply_mute_request(existing, body.mute_topic)
    updated = store.get_alert(alert_id)
    if updated is None:
        raise HTTPException(status_code=404, detail="Alert not found")
    return updated


class BulkAckBody(BaseModel):
    status: str = Field(..., pattern="^(ack|dismissed)$")
    # Explicit ids (what the UI sends — it knows which cards the caller owns)
    # and/or an age cutoff for ops / API use.
    alert_ids: list[int] | None = Field(default=None, max_length=500)
    # At least one day: a 0-day sweep would close the whole company queue,
    # including items routed to other people.
    older_than_days: int | None = Field(default=None, ge=1)
    category: Literal["action", "monitoring"] | None = None


class BulkAckResponse(BaseModel):
    count: int


@router.post("/alerts/bulk-ack", response_model=BulkAckResponse)
def bulk_ack_alerts(body: BulkAckBody, request: Request) -> BulkAckResponse:
    if body.alert_ids is None and body.older_than_days is None:
        raise HTTPException(
            status_code=400, detail="alert_ids or older_than_days is required"
        )

    before: str | None = None
    if body.older_than_days is not None:
        from datetime import UTC, datetime, timedelta

        before = (datetime.now(UTC) - timedelta(days=body.older_than_days)).isoformat()
    targets = {
        a.id: a
        for a in (store.get_alert(i) for i in (body.alert_ids or [])[:500])
        if a is not None
    }
    updated_ids = store.bulk_set_status(
        body.status,
        alert_ids=body.alert_ids,
        before=before,
        category=body.category,
        exclude_sources=tuple(lifecycle.TTL_EXEMPT_SOURCES),
        # Someone else's sweep never closes a card private to the principal.
        exclude_private=not _caller_is_principal(request),
    )
    count = len(updated_ids)
    # Same feedback loop as a single ack, for the rows we can attribute.
    for alert_id in updated_ids:
        alert = targets.get(alert_id) or store.get_alert(alert_id)
        if alert is not None:
            lifecycle.record_ack_feedback(alert, body.status)
    if count:
        from openexecutive.audit import log_event as audit_log

        audit_log(
            "alerts_bulk_ack",
            f"Bulk {body.status}: {count} alert(s)",
            actor="user",
            details={
                "status": body.status,
                "count": count,
                "alert_ids": (body.alert_ids or [])[:100],
                "older_than_days": body.older_than_days,
                "category": body.category,
            },
        )
    return BulkAckResponse(count=count)


@router.post("/alerts/{alert_id}/reopen", response_model=Alert)
def reopen_alert(alert_id: int, request: Request) -> Alert:
    """Undo for a close / dismiss / expiry: back to ``unread``, verdict cleared."""
    existing = _visible_alert(alert_id, request)
    prior = existing.status
    if not store.reopen_alert(
        alert_id, exclude_sources=tuple(lifecycle.TTL_EXEMPT_SOURCES)
    ):
        raise HTTPException(
            status_code=409,
            detail="Only resolved, expired or dismissed alerts can be reopened",
        )
    from openexecutive.audit import log_event as audit_log

    audit_log(
        "alert_reopened",
        f"Reopened alert {alert_id}: {prior} → unread",
        actor="user",
        details={"alert_id": alert_id, "from_status": prior},
    )
    updated = store.get_alert(alert_id)
    if updated is None:
        raise HTTPException(status_code=404, detail="Alert not found")
    return updated


class ReviewResponse(BaseModel):
    reviewed: int
    closed: int
    changed: int
    routed: int
    nudged: int
    escalated: int
    drafted: int
    merged: int
    suggested: int
    annotated: int


@router.post("/alerts/review", response_model=ReviewResponse)
async def review_alerts() -> ReviewResponse:
    """Run the Executive's relevance review on demand ("Re-check now").

    Ignores the per-alert review interval so every live alert past the
    minimum age is re-examined; the per-pass caps, the per-alert move
    idempotency, the single-flight lock and `ALERT_REVIEW_ENABLED` all
    still apply (a disabled review returns zero counts).
    """
    from openexecutive.alerts.review import run_alert_review

    summary = await run_alert_review(reason="manual", ignore_interval=True)
    return ReviewResponse(**summary.as_dict())
