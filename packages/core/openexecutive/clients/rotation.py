"""Overnight client rotation — keeps parked clients fresh (cockpit v2).

During a quiet window the scheduler activates each parked client slot in
turn, generates that client's morning brief (artifact into the client's own
record), runs its external monitor scan, saves it back, and finally restores
the original active client — then composes a cross-client digest from the
now-fresh cockpit.

Safety model (this feature automates the destructive switch path, so the
rails are explicit):

- **Opt-in**: ``CLIENT_ROTATION_ENABLED`` defaults to false. Each rotation
  costs one brief generation per parked client per night.
- **Claim pause**: the scheduler loop checks :func:`rotation_in_progress`
  and skips claiming due actions while a rotation runs. Without this, the
  loop's next tick would claim the just-activated client's overdue OUTBOUND
  follow-ups and fire them at 3am from a context nobody is watching. The
  rotation does only the work listed here; everything else resumes on the
  first tick after the original client is restored.
- **No outbound**: per-client work is brief generation + monitor scan only.
  The brief is persisted as a workflow artifact, never DM'd from the parked
  client's context (delivery happens once, after restore, via the original
  client's principal path — see the runner handler).
- **Always restore**: the original active client is re-activated in a
  ``finally`` and the ``.rotation_in_progress`` marker is removed even when
  individual clients fail. Per-client failures are recorded and skipped.
- **Marker**: ``company/_client_slots/.rotation_in_progress`` is the single
  source of truth — the scheduler's claim pause and the /clients UI badge
  both read it. Stale markers (crash mid-rotation) are reconciled at
  scheduler startup via :func:`clear_stale_rotation_marker`.

Known, documented residual race: actions claimed in the same tick that
claims the rotation row run concurrently with the rotation's first switch.
They belong to the original client and were due anyway; at a 03:30 window
this is rare, and it is the same exposure as a human switching during an
in-flight action.
"""
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openexecutive.clients.slots import (
    activate_client_slot,
    get_active_client,
    list_client_slots,
)

logger = logging.getLogger(__name__)

ROTATION_KIND = "client_rotation"
ROTATION_TIME_ENV = "CLIENT_ROTATION_TIME"
ROTATION_DEFAULT_TIME = "03:30"

# Engagements in these states sleep through rotation.
_SKIP_STATUSES = frozenset({"completed"})


def _rotation_marker(settings: Any) -> Path:
    from openexecutive.clients.slots import _clients_root

    return _clients_root(settings) / ".rotation_in_progress"


def rotation_in_progress(settings: Any) -> bool:
    return _rotation_marker(settings).exists()


def clear_stale_rotation_marker(settings: Any) -> bool:
    """Remove a marker left by a crash mid-rotation. Called at scheduler boot.

    The marker only ever exists while ``run_client_rotation`` holds it; at
    process start nothing can legitimately be rotating, so any marker found
    is stale and would otherwise pause claiming forever.
    """
    marker = _rotation_marker(settings)
    if marker.exists():
        logger.warning(
            "rotation: clearing stale .rotation_in_progress marker from a "
            "previous run (crash mid-rotation?)"
        )
        marker.unlink(missing_ok=True)
        return True
    return False


async def run_client_rotation(
    settings: Any, *, app_state: Any | None = None
) -> dict[str, Any]:
    """Rotate through parked clients: activate → brief + monitors → save back.

    Returns a summary dict (clients rotated, briefs generated, failures,
    skipped) plus the cross-client digest markdown for the caller to
    deliver. No-ops with a reason when preconditions aren't met.
    """
    from openexecutive.cli.fixture_loader import get_fixture_status

    if not getattr(settings, "client_rotation_enabled", False):
        return {"ran": False, "reason": "disabled"}
    if get_fixture_status(settings).get("active_fixture"):
        return {"ran": False, "reason": "fixture_active"}

    original = get_active_client(settings)
    if original is None:
        # No active client means live state isn't a slot — rotation could
        # not restore it afterwards. Multi-client users always have one.
        return {"ran": False, "reason": "no_active_client"}

    slots_list = list_client_slots(settings)
    if len(slots_list) < 2:
        return {"ran": False, "reason": "single_client"}

    targets = [
        s
        for s in slots_list
        if s["slug"] != original
        and s.get("has_state")  # never-activated seed/blank slots sleep
        and (s.get("status") or "active") not in _SKIP_STATUSES
    ]
    if not targets:
        return {"ran": False, "reason": "no_rotatable_clients"}

    marker = _rotation_marker(settings)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(datetime.now(UTC).isoformat(), encoding="utf-8")

    rotated: list[str] = []
    failed: dict[str, str] = {}
    # A failed ACTIVATION can leave the live state half-restored while the
    # sentinel still names the previous client — saving that back would
    # overwrite the previous client's good slot copy with corrupted state.
    # So: activation failures abort the loop and force a save-back-FREE
    # restore of the original. Quiet-work failures leave live state
    # consistent and are safe to continue past.
    live_is_consistent = True
    try:
        for slot in targets:
            slug = slot["slug"]
            try:
                live_is_consistent = False
                await activate_client_slot(settings, slug, app_state=app_state)
                live_is_consistent = True
            except Exception as exc:
                logger.exception(
                    "rotation: activating %r failed — aborting rotation to "
                    "protect slot state",
                    slug,
                )
                failed[slug] = f"activation failed: {str(exc)[:160]}"
                break
            try:
                await _run_quiet_work_for_live_client(settings, slug)
                rotated.append(slug)
            except Exception as exc:
                logger.exception("rotation: client %r quiet work failed", slug)
                failed[slug] = str(exc)[:200]
    finally:
        try:
            if get_active_client(settings) != original or not live_is_consistent:
                if live_is_consistent:
                    # Normal path: save the last rotated client, restore.
                    await activate_client_slot(
                        settings, original, app_state=app_state
                    )
                else:
                    # Half-restored live state must be DISCARDED, never saved
                    # back over anyone's good copy.
                    await _force_restore(settings, original, app_state=app_state)
        except Exception:
            # The one failure mode we cannot paper over: the operator must
            # know their active client wasn't restored.
            logger.exception(
                "rotation: FAILED to restore original client %r — manual "
                "activation required",
                original,
            )
            failed["__restore__"] = f"could not restore {original}"
        marker.unlink(missing_ok=True)

    digest = _compose_digest(settings, rotated, failed)
    return {
        "ran": True,
        "original": original,
        "rotated": rotated,
        "failed": failed,
        "digest": digest,
    }


async def _force_restore(
    settings: Any, slug: str, *, app_state: Any | None = None
) -> None:
    """Restore ``slug`` WITHOUT saving the current live state back first.

    Only for recovery from a failed activation, where the live state is
    half-restored garbage: a normal ``activate_client_slot`` would write
    that garbage over the sentinel-named client's good slot copy. The
    abandoned target's slot still holds its own last good save, so nothing
    is lost by discarding the live state.
    """
    from openexecutive.clients.slots import (
        _FIXTURE_OP_LOCK,
        _active_client_sentinel,
        _require_slot,
        _restore_slot_state,
        _set_honcho_client_workspace,
    )

    async with _FIXTURE_OP_LOCK:
        slot = _require_slot(settings, slug)
        await _restore_slot_state(settings, slot, app_state=app_state)
        _active_client_sentinel(settings).parent.mkdir(parents=True, exist_ok=True)
        _active_client_sentinel(settings).write_text(slug, encoding="utf-8")
        _set_honcho_client_workspace(slug)


async def _run_quiet_work_for_live_client(settings: Any, slug: str) -> None:
    """Brief + monitors for the CURRENTLY LIVE client. Never sends outbound.

    Isolated for testability (tests stub this to observe rotation order)
    and so the no-outbound rule has exactly one place to hold.
    """
    # Morning brief — persisted as a workflow run artifact in this client's
    # own record (it will surface in their /artifacts and next /today),
    # deliberately NOT delivered to anyone from here. Brief failures
    # PROPAGATE: the caller records the client under ``failed`` so the
    # digest never claims a brief that wasn't generated.
    from openexecutive.knowledge.store import ChromaDBStore
    from openexecutive.workflows import WORKFLOW_REGISTRY
    from openexecutive.workflows.persistence import complete_run, create_run, fail_run

    workflow = WORKFLOW_REGISTRY["morning_brief"]
    run_id = str(uuid.uuid4())
    inputs = workflow.input_model()()
    create_run(
        run_id,
        workflow.name,
        f"Overnight brief — {slug}",
        inputs.model_dump(),
    )
    store = ChromaDBStore(persist_directory=settings.vector_store_path)
    artifact = ""
    try:
        async for event in workflow.run(inputs, store):
            if event.type == "artifact" and event.content:
                artifact = event.content
            elif event.type == "error":
                raise RuntimeError(event.message or "morning brief failed")
        complete_run(run_id, artifact)
    except Exception as exc:
        fail_run(run_id, str(exc)[:500])
        raise

    # External monitor scan — refreshes this client's watchlist signals.
    if getattr(settings, "external_monitor_enabled", False):
        try:
            from openexecutive.monitoring.pipeline import run_external_monitor_scan

            written = await run_external_monitor_scan(now=datetime.now(UTC))
            logger.info(
                "rotation: monitor scan for %r wrote %d signal(s)", slug, written
            )
        except Exception:
            logger.exception("rotation: monitor scan for %r failed", slug)


def _compose_digest(
    settings: Any, rotated: list[str], failed: dict[str, str]
) -> str:
    """Cross-client morning digest from the now-fresh cockpit."""
    from openexecutive.clients.cockpit import practice_overview

    lines = ["# Across your clients — overnight rotation", ""]
    try:
        cards = practice_overview(settings)
    except Exception:
        logger.exception("rotation: digest cockpit read failed")
        cards = []

    for c in cards:
        bits = []
        if c.overdue_actions:
            bits.append(f"{c.overdue_actions} overdue")
        if c.awaiting_replies:
            bits.append(f"{c.awaiting_replies} awaiting reply")
        if c.unread_alerts:
            bits.append(f"{c.unread_alerts} alerts")
        if c.onboarding_due_soon:
            bits.append(f"{c.onboarding_due_soon} onboarding due")
        if isinstance(c.days_to_renewal, int) and c.days_to_renewal <= 30:
            bits.append(
                "renewal due"
                if c.days_to_renewal <= 0
                else f"renewal in {c.days_to_renewal}d"
            )
        status = ", ".join(bits) if bits else "all quiet"
        marker = " (active)" if c.is_active else ""
        role = f" — {c.role}" if c.role else ""
        lines.append(f"- **{c.display_name}**{role}{marker}: {status}")

    if rotated:
        lines.append("")
        lines.append(
            f"Overnight briefs generated for: {', '.join(rotated)} "
            "(see each client's Artifacts)."
        )
    if failed:
        lines.append("")
        lines.append(
            "Rotation issues: "
            + "; ".join(f"{slug}: {err}" for slug, err in failed.items())
        )
    return "\n".join(lines)


def seed_client_rotation() -> int | None:
    """Idempotently enqueue the next rotation occurrence in the LIVE DB.

    Called at scheduler startup AND after every slot activation: scheduled
    rows swap with the client, so without the re-seed the rotation row would
    vanish the first time the operator switched clients. Idempotent — a
    pending/running rotation row suppresses a new one. Returns the new
    action id, or None when disabled / already pending / insert failed.
    """
    import os

    from openexecutive.config import get_settings
    from openexecutive.memory.episodic import insert_scheduled_action
    from openexecutive.scheduler.runner import (
        _has_pending_brief,
        _next_occurrence,
        _parse_hhmm,
    )

    if not get_settings().client_rotation_enabled:
        return None
    try:
        if _has_pending_brief(ROTATION_KIND):
            return None
        hh, mm = _parse_hhmm(
            os.environ.get(ROTATION_TIME_ENV, ""), ROTATION_DEFAULT_TIME
        )
        run_at = _next_occurrence(datetime.now(UTC), hh, mm)
        action_id = insert_scheduled_action(
            run_at=run_at.isoformat(),
            channel="__internal__",
            channel_ref="practice",
            intent_text=(
                "Overnight client rotation: refresh each parked client's "
                "brief and monitors, then deliver the cross-client digest."
            ),
            kind=ROTATION_KIND,
        )
        logger.info(
            "rotation: seeded %s at %s (id=%s)",
            ROTATION_KIND,
            run_at.isoformat(),
            action_id,
        )
        return int(action_id)
    except Exception:
        logger.exception("rotation: seeding failed")
        return None
