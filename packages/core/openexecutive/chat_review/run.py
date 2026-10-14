"""The look back, as the reflection runs it.

``run_look_back`` is the second step of the ``executive_reflection`` scheduled
run (scheduler/runner.py ``_run_executive_reflection``), after the standup
step has acted. It never raises and never blocks the chain: a failure is
recorded on the run row and in the audit log, and the next morning tries
again from the same watermark.

Gates, in order: the operator switch ``CHAT_REVIEW_ENABLED``; the owner's
switch on the Reflection screen; one run per local day; one run at a time (a
run still marked running from the last two hours means another process has
it; an older one is a crash, marked failed). Then it picks the conversations
(pick.py), reviews each (review.py) a few at a time, advances the watermark to
the newest message it read, files proposals nobody acted on in 30 days, and
writes one ``chat_review`` audit row.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from openexecutive.chat_review import pick, review, store

logger = logging.getLogger(__name__)

_CONCURRENCY = 3
_STALE_RUN = timedelta(hours=2)
AUDIT_EVENT = "chat_review"


def _principal_id() -> int | None:
    try:
        from openexecutive.people.store import find_principal_person

        principal = find_principal_person()
        return principal.id if principal is not None else None
    except Exception:  # noqa: BLE001 - no roster means the owner's chats are unattributed.
        return None


def _local_day(now: datetime) -> str:
    try:
        from openexecutive.memory.workspace_settings import get_user_timezone

        return now.astimezone(get_user_timezone()).date().isoformat()
    except Exception:  # noqa: BLE001
        return now.date().isoformat()


def _audit(summary: str, details: dict[str, Any]) -> None:
    try:
        from openexecutive.audit import log_event

        log_event(AUDIT_EVENT, summary, actor="reflection", details=details)
    except Exception:  # noqa: BLE001 - the audit row is a courtesy.
        logger.exception("chat_review: audit row failed")


async def run_look_back(now: datetime | None = None, *, db_path: Path | None = None) -> dict[str, Any]:
    """One look back over the conversations since the last one. Returns a
    summary dict (``skipped`` names the gate that stopped it). Never raises."""
    now = now or datetime.now(UTC)
    try:
        from openexecutive.config import get_settings as app_settings

        settings = app_settings()
        if not settings.chat_review_enabled:
            return {"skipped": "operator switch off"}
        model = settings.chat_review_model
        limit = max(0, settings.chat_review_max_sessions)
    except Exception:  # noqa: BLE001
        logger.exception("chat_review: settings unavailable")
        return {"skipped": "settings unavailable"}
    try:
        owner = store.get_settings(db_path)
        if not owner.enabled:
            return {"skipped": "owner switch off"}
        # Claim the day first, in one transaction, so two schedulers waking
        # together (or a restart mid-run) never review the same night twice.
        claimed, why = store.claim_run(day=_local_day(now), now=now, stale_after=_STALE_RUN, db_path=db_path)
        if claimed is None:
            return {"skipped": why}
        run_id = claimed
        chosen, considered = pick.pick_sessions(
            since_message_id=owner.last_message_id,
            limit=limit,
            principal_id=_principal_id(),
            db_path=db_path,
        )
        store.set_sessions_considered(run_id, considered, db_path=db_path)
    except Exception:  # noqa: BLE001
        logger.exception("chat_review: could not start")
        return {"skipped": "could not start"}

    results: list[dict[str, Any]] = []
    try:
        sem = asyncio.Semaphore(_CONCURRENCY)

        async def one(c: pick.Candidate) -> dict[str, Any]:
            async with sem:
                out = await review.review_session(c.session_id, run_id=run_id, model=model, db_path=db_path)
                out["signals"] = c.signals
                return out

        results = list(await asyncio.gather(*(one(c) for c in chosen)))
        reviewed = sum(1 for r in results if "error" not in r)
        newest = max([c.last_message_id for c in chosen], default=owner.last_message_id)
        store.advance_watermark(newest, db_path=db_path)
        filed = store.file_stale_proposals(db_path=db_path)
        store.finish_run(run_id, sessions_reviewed=reviewed, db_path=db_path)
        found = sum(int(r.get("findings", 0)) for r in results)
        summary = {
            "run_id": run_id,
            "considered": considered,
            "reviewed": reviewed,
            "findings": found,
            "filed": filed,
            "errors": sum(1 for r in results if "error" in r),
            "dropped": [d for r in results for d in r.get("dropped", [])][:20],
        }
        _audit(
            f"Looked back at {reviewed} conversation(s): {found} finding(s)",
            summary,
        )
        return summary
    except Exception as exc:  # noqa: BLE001 - never break the reflection chain.
        logger.exception("chat_review: run %s failed", run_id)
        try:
            store.finish_run(
                run_id,
                sessions_reviewed=sum(1 for r in results if "error" not in r),
                status="failed",
                error=str(exc),
                db_path=db_path,
            )
        except Exception:  # noqa: BLE001
            logger.exception("chat_review: could not record the failure")
        _audit(f"Look back FAILED: {exc}", {"run_id": run_id, "error": str(exc)[:300]})
        return {"run_id": run_id, "error": str(exc)[:300]}
