"""FastAPI routes for the external-condition watchlist.

Surfaces the same SQLite-backed watchlist that the Executive manages
via chat tools (``orchestrator/watchlist_tools.py``) to the UI. Same
``store.py`` functions, same validators (``monitoring/validation.py``),
same audit-event shape — chat-originated and UI-originated mutations
are indistinguishable downstream except by the ``actor`` field on the
audit row.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from openexecutive.alerts.models import AlertSeverity
from openexecutive.audit import log_event as audit_log
from openexecutive.monitoring import store as ms
from openexecutive.monitoring.models import (
    MODE_ACTIVE,
    WatchlistItem,
    is_valid_cadence,
    is_valid_mode,
)
from openexecutive.monitoring.sources import list_registered_kinds
from openexecutive.monitoring.target_validation import (
    WatchlistTargetError,
    validate_and_normalize_target,
)
from openexecutive.monitoring.validation import (
    VALID_SEVERITY_VALUES,
    is_valid_watchlist_slug,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_API_ACTOR = "api"
_SIGNALS_DEFAULT_LIMIT = 50
_SIGNALS_MAX_LIMIT = 200


def _audit(event: str, ok: bool, summary: str, details: dict[str, Any]) -> None:
    audit_log(
        "tool_invocation",
        summary,
        actor=_API_ACTOR,
        details={"tool": event, "ok": ok, **details},
    )


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #


class WatchlistCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(min_length=1, max_length=64)
    signal_type: str = Field(min_length=1, max_length=64)
    target: str = Field(min_length=1, max_length=500)
    trigger: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
    cadence: str = "15min"
    severity_floor: str = "low"
    severity_ceiling: str = "urgent"
    mode: str = MODE_ACTIVE
    route_to_specialist: str = ""
    notes: str = Field(default="", max_length=500)
    display_label: str = ""


class WatchlistPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    mode: str | None = None
    cadence: str | None = None
    severity_floor: str | None = None
    severity_ceiling: str | None = None
    trigger: dict[str, Any] | None = None
    notes: str | None = Field(default=None, max_length=500)


class SignalRow(BaseModel):
    """Shape of one row in /watchlist/{slug}/signals.

    Mirrors the dict that ``store.list_signals_for_watchlist`` returns
    (which is the ``external_signals`` row plus the JSON-decoded
    ``raw_payload``). Keep the shape boring so the UI can render rows
    without a transform.
    """

    model_config = ConfigDict(extra="allow")

    id: int
    watchlist_id: int
    source_kind: str
    source_external_id: str
    captured_at: str
    # Upstream publish time (RSS <pubDate>, EDGAR filing date) — None when the
    # source has none or the row predates the column (issue #80).
    published_at: str | None = None
    normalized_summary: str
    provenance_url: str
    severity_hint: str
    dedup_key: str
    processed_at: str | None = None
    processed_outcome: str | None = None
    promoted_alert_id: int | None = None
    raw_payload: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Read routes
# --------------------------------------------------------------------------- #


@router.get("/watchlist", response_model=list[WatchlistItem])
def list_watchlist(
    enabled_only: bool = False,
    signal_type: str | None = None,
) -> list[WatchlistItem]:
    return ms.list_watchlist(enabled_only=enabled_only, signal_type=signal_type)


@router.get("/watchlist/{slug}", response_model=WatchlistItem)
def get_watchlist_entry(slug: str) -> WatchlistItem:
    item = ms.get_watchlist_item_by_slug(slug)
    if item is None:
        raise HTTPException(status_code=404, detail=f"Watchlist entry {slug!r} not found")
    return item


@router.get("/watchlist/{slug}/signals", response_model=list[SignalRow])
def get_watchlist_signals(slug: str, limit: int = _SIGNALS_DEFAULT_LIMIT) -> list[SignalRow]:
    item = ms.get_watchlist_item_by_slug(slug)
    if item is None:
        raise HTTPException(status_code=404, detail=f"Watchlist entry {slug!r} not found")
    assert item.id is not None
    bounded = max(1, min(limit, _SIGNALS_MAX_LIMIT))
    rows = ms.list_signals_for_watchlist(item.id, limit=bounded)
    return [SignalRow(**row) for row in rows]


# --------------------------------------------------------------------------- #
# Mutation routes
# --------------------------------------------------------------------------- #


@router.post(
    "/watchlist",
    response_model=WatchlistItem,
    status_code=status.HTTP_201_CREATED,
)
async def create_watchlist_entry(body: WatchlistCreate) -> WatchlistItem:
    slug = body.slug.strip()
    signal_type = body.signal_type.strip()
    target = body.target.strip()

    if not is_valid_watchlist_slug(slug):
        raise HTTPException(
            status_code=400,
            detail=f"slug {slug!r} must be kebab-case, max 61 chars (a-z, 0-9, '-')",
        )
    if signal_type not in list_registered_kinds():
        raise HTTPException(
            status_code=400,
            detail=f"signal_type {signal_type!r} has no registered adapter",
        )
    if not is_valid_cadence(body.cadence):
        raise HTTPException(status_code=400, detail=f"unknown cadence {body.cadence!r}")
    if not is_valid_mode(body.mode):
        raise HTTPException(status_code=400, detail=f"unknown mode {body.mode!r}")
    if body.severity_floor not in VALID_SEVERITY_VALUES:
        raise HTTPException(
            status_code=400,
            detail=f"unknown severity_floor {body.severity_floor!r}",
        )
    if body.severity_ceiling not in VALID_SEVERITY_VALUES:
        raise HTTPException(
            status_code=400,
            detail=f"unknown severity_ceiling {body.severity_ceiling!r}",
        )

    if ms.get_watchlist_item_by_slug(slug) is not None:
        raise HTTPException(
            status_code=409,
            detail=f"slug {slug!r} already exists; PATCH it instead",
        )

    config: dict[str, Any] = dict(body.config)
    if body.display_label:
        if signal_type == "vendor_status":
            config.setdefault("vendor_label", body.display_label)
        elif signal_type == "rss":
            config.setdefault("feed_label", body.display_label)
        elif signal_type == "stock":
            config.setdefault("display_name", body.display_label)

    # Insert-time validation: a non-feed rss target is converted to a
    # scrape-backed page_watch (when scrapeable) or rejected, so a dead
    # row never reaches the scan loop. Non-rss types pass through untouched.
    try:
        signal_type, target, config = await validate_and_normalize_target(
            signal_type, target, config,
        )
    except WatchlistTargetError as exc:
        _audit(
            "watchlist.create", False,
            f"watchlist.create: target rejected for {slug!r}",
            {"slug": slug, "target": target, "reason": exc.reason},
        )
        raise HTTPException(status_code=422, detail=exc.reason) from exc

    try:
        new_id = ms.insert_watchlist_item(
            slug=slug,
            signal_type=signal_type,
            target=target,
            config=config,
            trigger=body.trigger,
            cadence=body.cadence,
            severity_floor=AlertSeverity(body.severity_floor),
            severity_ceiling=AlertSeverity(body.severity_ceiling),
            route_to_specialist=body.route_to_specialist,
            mode=body.mode,
            notes=body.notes[:500],
        )
    except sqlite3.IntegrityError as exc:
        # The pre-check above narrows the duplicate-slug race window
        # but doesn't close it. The UNIQUE constraint is the real guard;
        # if it fires, surface as 409 to match the pre-check branch.
        _audit(
            "watchlist.create", False,
            f"watchlist.create: integrity error on {slug!r}",
            {"slug": slug, "error": "integrity"},
        )
        logger.exception("watchlist.create: integrity error slug=%s", slug)
        raise HTTPException(
            status_code=409,
            detail=f"slug {slug!r} already exists; PATCH it instead",
        ) from exc
    except Exception as exc:
        _audit(
            "watchlist.create", False,
            f"watchlist.create: insert failed for {slug!r}",
            {"slug": slug, "error": "insert"},
        )
        logger.exception("watchlist.create: insert failed slug=%s", slug)
        raise HTTPException(status_code=500, detail="insert failed") from exc

    _audit(
        "watchlist.create", True,
        f"watchlist.create: created {slug!r} ({signal_type})",
        {
            "watchlist_id": new_id,
            "slug": slug,
            "signal_type": signal_type,
            "target": target,
            "mode": body.mode,
        },
    )

    created = ms.get_watchlist_item(new_id)
    if created is None:
        raise HTTPException(status_code=500, detail="watchlist entry vanished after insert")
    return created


@router.patch("/watchlist/{slug}", response_model=WatchlistItem)
def patch_watchlist_entry(slug: str, body: WatchlistPatch) -> WatchlistItem:
    item = ms.get_watchlist_item_by_slug(slug)
    if item is None:
        raise HTTPException(status_code=404, detail=f"Watchlist entry {slug!r} not found")
    assert item.id is not None

    # Same per-field validation as the chat tune tool — build the update
    # dict only after each input is validated so a bad value short-circuits
    # the whole PATCH rather than partially applying.
    raw = body.model_dump(exclude_unset=True)
    fields: dict[str, Any] = {}
    changes: list[str] = []

    if "enabled" in raw:
        fields["enabled"] = 1 if body.enabled else 0
        changes.append(f"enabled={body.enabled}")
    if "mode" in raw and body.mode is not None:
        if not is_valid_mode(body.mode):
            raise HTTPException(status_code=400, detail=f"unknown mode {body.mode!r}")
        fields["mode"] = body.mode
        changes.append(f"mode={body.mode}")
    if "cadence" in raw and body.cadence is not None:
        if not is_valid_cadence(body.cadence):
            raise HTTPException(status_code=400, detail=f"unknown cadence {body.cadence!r}")
        fields["cadence"] = body.cadence
        changes.append(f"cadence={body.cadence}")
    if "severity_floor" in raw and body.severity_floor is not None:
        if body.severity_floor not in VALID_SEVERITY_VALUES:
            raise HTTPException(
                status_code=400,
                detail=f"unknown severity_floor {body.severity_floor!r}",
            )
        fields["severity_floor"] = body.severity_floor
        changes.append(f"severity_floor={body.severity_floor}")
    if "severity_ceiling" in raw and body.severity_ceiling is not None:
        if body.severity_ceiling not in VALID_SEVERITY_VALUES:
            raise HTTPException(
                status_code=400,
                detail=f"unknown severity_ceiling {body.severity_ceiling!r}",
            )
        fields["severity_ceiling"] = body.severity_ceiling
        changes.append(f"severity_ceiling={body.severity_ceiling}")
    if "trigger" in raw and body.trigger is not None:
        fields["trigger_json"] = json.dumps(body.trigger)
        changes.append("trigger replaced")
    if "notes" in raw and body.notes is not None:
        fields["notes"] = body.notes[:500]
        changes.append("notes updated")

    if not fields:
        raise HTTPException(
            status_code=400,
            detail="nothing to change — pass at least one tunable field",
        )

    try:
        ms.update_watchlist_fields(item.id, fields)
    except Exception as exc:
        _audit(
            "watchlist.patch", False,
            f"watchlist.patch: UPDATE failed for {slug!r}",
            {"slug": slug, "watchlist_id": item.id, "error": "update"},
        )
        logger.exception("watchlist.patch: UPDATE failed slug=%s", slug)
        raise HTTPException(status_code=500, detail="UPDATE failed") from exc

    _audit(
        "watchlist.patch", True,
        f"watchlist.patch: {slug} -> {', '.join(changes)}",
        {"slug": slug, "watchlist_id": item.id, "changes": changes},
    )

    updated = ms.get_watchlist_item(item.id)
    if updated is None:
        raise HTTPException(status_code=500, detail="watchlist entry vanished after update")
    return updated


@router.delete("/watchlist/{slug}", status_code=status.HTTP_204_NO_CONTENT)
def delete_watchlist_entry(slug: str) -> Response:
    item = ms.get_watchlist_item_by_slug(slug)
    if item is None:
        raise HTTPException(status_code=404, detail=f"Watchlist entry {slug!r} not found")
    assert item.id is not None
    try:
        removed = ms.delete_watchlist_item(item.id)
    except Exception as exc:
        _audit(
            "watchlist.delete", False,
            f"watchlist.delete: failed for {slug!r}",
            {"slug": slug, "watchlist_id": item.id, "error": "delete"},
        )
        logger.exception("watchlist.delete: failed slug=%s", slug)
        raise HTTPException(status_code=500, detail="delete failed") from exc

    _audit(
        "watchlist.delete", removed,
        f"watchlist.delete: {slug} ({'deleted' if removed else 'no-op'})",
        {"slug": slug, "watchlist_id": item.id, "removed": removed},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
