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

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from openexecutive.alerts.models import AlertSeverity
from openexecutive.api import caller as api_caller
from openexecutive.audit import log_event as audit_log
from openexecutive.monitoring import store as ms
from openexecutive.monitoring.describe import (
    DescribeError,
    WatchDraft,
    checks_line,
    describe_watch,
    trigger_line,
    watch_title,
)
from openexecutive.monitoring.models import (
    DECLINE_KIND_EXPLICIT,
    DECLINE_REASON_NOT_RELEVANT,
    DECLINE_REASON_TOO_NOISY,
    EXPLICIT_DECLINE_REASONS,
    MODE_ACTIVE,
    MODE_DRY_RUN,
    ORIGIN_RESEARCH_PROPOSED,
    RESEARCH_ORIGINS,
    WatchlistItem,
    is_valid_cadence,
    is_valid_mode,
)
from openexecutive.monitoring.noise import WatchNoise, quiet_noise_nudge, watch_noise
from openexecutive.monitoring.sources import list_registered_kinds
from openexecutive.monitoring.target_validation import (
    WatchlistTargetError,
    validate_and_normalize_target,
)
from openexecutive.monitoring.validation import (
    VALID_SEVERITY_VALUES,
    display_label_config,
    is_valid_watchlist_slug,
    normalize_target,
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
    # What the watch is for: a goal's id, and the person it tells (their
    # People id). Null clears it; no person means the owner.
    goal_id: int | None = None
    person_id: int | None = None
    # Stop asking whether this watch is too noisy, for the alerts it has
    # raised so far (monitoring.noise).
    quiet_noise_nudge: bool | None = None


class WatchlistDescribe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Bounded in the route, not with max_length, so a 422 never echoes it.
    text: str = ""


class LastTold(BaseModel):
    """The newest thing a watch told the person."""

    at: str
    summary: str
    url: str = ""


class WatchlistView(WatchlistItem):
    """A watch as the screens show it: the row plus its name, what makes it
    speak and how often it checks in words, the last thing it said, and the
    goal and person it is for."""

    title: str = ""
    tells_you_when: str = ""
    checks: str = ""
    last_told: LastTold | None = None
    goal_title: str = ""
    # Who it tells, when that is a teammate ("" = the owner), and their role.
    person_name: str = ""
    person_role: str = ""
    department_title: str = ""


class WatchlistDetail(WatchlistView):
    """One watch plus how noisy its recent alerts were (the detail page's
    "only tell you about bigger changes?" line)."""

    noise: WatchNoise = Field(default_factory=WatchNoise)


class _Names:
    """Goal, person and department names for a batch of watches, each
    looked up once. A missing store or a deleted row reads as no name."""

    def __init__(self) -> None:
        self._goals: dict[int, str] | None = None
        self._departments: dict[str, str] | None = None
        self._people: dict[int, tuple[str, str]] = {}
        self.last_told = ms.last_told_by_watch()

    def goal(self, goal_id: int | None) -> str:
        if goal_id is None:
            return ""
        if self._goals is None:
            try:
                from openexecutive.departments import store as departments

                self._goals = {g.id: g.key_result for g in departments.list_all_goals() if g.id is not None}
            except Exception:
                logger.debug("watchlist: goals unavailable", exc_info=True)
                self._goals = {}
        return self._goals.get(goal_id, "")

    def department(self, slug: str) -> str:
        if not slug:
            return ""
        if self._departments is None:
            try:
                from openexecutive.departments import store as departments

                self._departments = {d.config.slug: d.config.title for d in departments.list_departments()}
            except Exception:
                logger.debug("watchlist: departments unavailable", exc_info=True)
                self._departments = {}
        return self._departments.get(slug, "")

    def person(self, person_id: int | None) -> tuple[str, str]:
        """``(name, role)`` of a current teammate, else two empty strings."""
        if person_id is None:
            return "", ""
        if person_id not in self._people:
            found_name, role = "", ""
            try:
                from openexecutive.people import store as people

                found = people.get_person(person_id)
                if found is not None and not found.archived and found.kind == "team":
                    found_name, role = found.full_name, found.role or ""
            except Exception:
                logger.debug("watchlist: person unavailable", exc_info=True)
            self._people[person_id] = (found_name, role)
        return self._people[person_id]


def _view(item: WatchlistItem, names: _Names) -> dict[str, Any]:
    told = names.last_told.get(item.id or 0)
    person_name, person_role = names.person(item.route_to_person_id)
    return {
        **item.model_dump(),
        "title": watch_title(item),
        "tells_you_when": trigger_line(item.signal_type, item.trigger_json),
        "checks": checks_line(item.signal_type, item.cadence),
        "last_told": LastTold(
            at=str(told.get("processed_at") or told.get("captured_at") or ""),
            summary=_told_summary(str(told.get("normalized_summary") or "")),
            url=_web_link(str(told.get("provenance_url") or "")),
        ) if told else None,
        "goal_title": names.goal(item.goal_id),
        "person_name": person_name,
        "person_role": person_role,
        "department_title": names.department(item.route_to_department),
    }


def _web_link(url: str) -> str:
    """The link when it is a plain web address, else nothing: it comes from
    the feed and the screens open it."""
    return url if url.lower().startswith(("https://", "http://")) else ""


def _told_summary(text: str) -> str:
    """A signal's summary without the ``[label]`` the adapter leads with:
    it is shown under the watch's own name."""
    line = " ".join(text.split())
    if line.startswith("[") and "] " in line[:90]:
        line = line.split("] ", 1)[1]
    return line[:1].upper() + line[1:]


def _one(item: WatchlistItem) -> WatchlistView:
    return WatchlistView(**_view(item, _Names()))


class DeclineBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # not_relevant → the entity is blacklisted; too_noisy → the watch goes
    # live anyway but quieter (big changes only); wrong_source → only this target
    # is blacklisted. See monitoring.research.watch_policy.
    reason: str = DECLINE_REASON_NOT_RELEVANT


class DeclineResponse(BaseModel):
    slug: str
    reason: str
    # "removed" (declined + deleted) or "kept_high_floor" (too_noisy: goes
    # live as a research watch that only tells the person about big changes;
    # the value's name predates that).
    result: str


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


@router.get("/watchlist", response_model=list[WatchlistView])
def list_watchlist(
    enabled_only: bool = False,
    signal_type: str | None = None,
) -> list[WatchlistView]:
    names = _Names()
    return [
        WatchlistView(**_view(item, names))
        for item in ms.list_watchlist(enabled_only=enabled_only, signal_type=signal_type)
    ]


@router.get("/watchlist/{slug}", response_model=WatchlistDetail)
def get_watchlist_entry(slug: str) -> WatchlistDetail:
    item = ms.get_watchlist_item_by_slug(slug)
    if item is None:
        raise HTTPException(status_code=404, detail=f"Watchlist entry {slug!r} not found")
    return WatchlistDetail(**_view(item, _Names()), noise=watch_noise(item))


@router.get("/watchlist/{slug}/signals", response_model=list[SignalRow])
def get_watchlist_signals(
    slug: str, limit: int = _SIGNALS_DEFAULT_LIMIT, told: bool = False,
) -> list[SignalRow]:
    """A watch's recent signals, newest first. ``told=true`` keeps only the
    ones it told the person about (the watch page's "What it told you")."""
    item = ms.get_watchlist_item_by_slug(slug)
    if item is None:
        raise HTTPException(status_code=404, detail=f"Watchlist entry {slug!r} not found")
    assert item.id is not None
    bounded = max(1, min(limit, _SIGNALS_MAX_LIMIT))
    rows = ms.list_signals_for_watchlist(item.id, limit=bounded, told_only=told)
    return [
        SignalRow(**{**row, "provenance_url": _web_link(str(row.get("provenance_url") or ""))})
        for row in rows
    ]


# --------------------------------------------------------------------------- #
# Mutation routes
# --------------------------------------------------------------------------- #


_DESCRIBE_STATUS = {"too_many": 429, "timeout": 504, "unavailable": 502}


@router.post("/watchlist/describe", response_model=WatchDraft)
async def describe_watchlist_entry(body: WatchlistDescribe, request: Request) -> WatchDraft:
    """Draft a watch from a sentence ("tell me if Initech's stock moves more
    than 8%"). Nothing is saved: the UI shows the draft in plain words and
    POSTs it to ``/watchlist`` once the person says Start watching. A
    request that can't become a watch is a 422 with a plain reason."""
    try:
        return await describe_watch(body.text, caller_key=api_caller.caller(request).email)
    except DescribeError as exc:
        raise HTTPException(status_code=_DESCRIBE_STATUS.get(exc.code, 422), detail=exc.message) from exc


@router.post(
    "/watchlist",
    response_model=WatchlistView,
    status_code=status.HTTP_201_CREATED,
)
async def create_watchlist_entry(body: WatchlistCreate) -> WatchlistView:
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
    for key, value in display_label_config(signal_type, body.display_label).items():
        config.setdefault(key, value)

    # Insert-time validation: a non-feed rss target is converted to a
    # page_watch (when the page has readable text) or rejected, so a dead
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
    return _one(created)


def _check_goal(goal_id: int) -> None:
    from openexecutive.departments import store as departments

    if departments.get_goal(goal_id) is None:
        raise HTTPException(status_code=422, detail="That goal no longer exists.")


def _check_person(person_id: int) -> None:
    from openexecutive.people import store as people

    person = people.get_person(person_id)
    if person is None or person.archived or person.kind != "team":
        raise HTTPException(status_code=422, detail="Pick someone on your team.")


@router.patch("/watchlist/{slug}", response_model=WatchlistView)
def patch_watchlist_entry(slug: str, body: WatchlistPatch, request: Request) -> WatchlistView:
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
    if "goal_id" in raw:
        if body.goal_id is not None:
            _check_goal(body.goal_id)
        fields["goal_id"] = body.goal_id
        changes.append("goal set" if body.goal_id is not None else "goal cleared")
    if "person_id" in raw:
        # What the watch finds is sent to that person, so who it tells is
        # the principal's call.
        from openexecutive.api.routes.chat import _caller_is_principal_or_unclaimed

        if not _caller_is_principal_or_unclaimed(request):
            raise HTTPException(status_code=403, detail="Only the owner can choose who a watch tells.")
        if body.person_id is not None:
            _check_person(body.person_id)
        fields["route_to_person_id"] = body.person_id
        changes.append("person set" if body.person_id is not None else "person cleared")
    quiet_nudge = bool(body.quiet_noise_nudge)

    if not fields and not quiet_nudge:
        raise HTTPException(
            status_code=400,
            detail="nothing to change — pass at least one tunable field",
        )

    try:
        if fields:
            ms.update_watchlist_fields(item.id, fields)
        if quiet_nudge:
            quiet_noise_nudge(item)
            changes.append("noise question answered")
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
        {"slug": slug, "watchlist_id": item.id, "changes": changes, "by": api_caller.actor(request)},
    )

    updated = ms.get_watchlist_item(item.id)
    if updated is None:
        raise HTTPException(status_code=500, detail="watchlist entry vanished after update")
    return _one(updated)


def _record_decline(item: WatchlistItem, reason: str) -> None:
    """Remember that the principal said no to this research source, so the
    research pass never re-proposes it under a new slug."""
    from openexecutive.monitoring.research import watch_policy

    ms.insert_decline(
        normalized_target=normalize_target(item.signal_type, item.target),
        kind=DECLINE_KIND_EXPLICIT,
        reason=reason,
        entity=str(watch_policy.policy_stamp_of(item).get("entity", "")),
        signal_type=item.signal_type,
        slug=item.slug,
    )
    watch_policy.record_outcome_for(item, watch_policy.OUTCOME_DECLINED)


@router.delete("/watchlist/{slug}", status_code=status.HTTP_204_NO_CONTENT)
def delete_watchlist_entry(slug: str, reason: str | None = None) -> Response:
    item = ms.get_watchlist_item_by_slug(slug)
    if item is None:
        raise HTTPException(status_code=404, detail=f"Watchlist entry {slug!r} not found")
    assert item.id is not None
    if reason is not None and reason not in EXPLICIT_DECLINE_REASONS:
        raise HTTPException(status_code=400, detail=f"unknown reason {reason!r}")
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

    if removed and item.origin in RESEARCH_ORIGINS:
        # "Stop watching" a research-added source is feedback.
        try:
            _record_decline(item, reason or DECLINE_REASON_NOT_RELEVANT)
        except Exception:
            logger.exception("watchlist.delete: decline record failed slug=%s", slug)

    _audit(
        "watchlist.delete", removed,
        f"watchlist.delete: {slug} ({'deleted' if removed else 'no-op'})",
        {"slug": slug, "watchlist_id": item.id, "removed": removed, "reason": reason},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Research suggestions: approve / decline
# --------------------------------------------------------------------------- #


def _pending_suggestion_or_409(slug: str) -> WatchlistItem:
    item = ms.get_watchlist_item_by_slug(slug)
    if item is None:
        raise HTTPException(status_code=404, detail=f"Watchlist entry {slug!r} not found")
    if item.origin != ORIGIN_RESEARCH_PROPOSED or item.mode != MODE_DRY_RUN:
        raise HTTPException(
            status_code=409,
            detail=f"{slug!r} is not a pending research suggestion",
        )
    return item


@router.post("/watchlist/{slug}/approve", response_model=WatchlistView)
def approve_watchlist_suggestion(slug: str) -> WatchlistView:
    """Turn a research suggestion into a live watch (origin=research)."""
    from openexecutive.monitoring.research import watch_policy

    item = _pending_suggestion_or_409(slug)
    assert item.id is not None
    if not ms.approve_suggestion(item.id):
        raise HTTPException(status_code=409, detail=f"{slug!r} is not a pending research suggestion")
    watch_policy.record_outcome_for(item, watch_policy.OUTCOME_APPROVED)
    audit_log(
        watch_policy.EVENT_SUGGESTION_APPROVED,
        f"Approved watching {slug} — {item.notes[:120] or item.target}",
        actor=_API_ACTOR,
        details={"slug": slug, "watchlist_id": item.id, "target": item.target,
                 "signal_type": item.signal_type},
    )
    updated = ms.get_watchlist_item(item.id)
    if updated is None:
        raise HTTPException(status_code=500, detail="watchlist entry vanished after approve")
    return _one(updated)


@router.post("/watchlist/{slug}/decline", response_model=DeclineResponse)
def decline_watchlist_suggestion(slug: str, body: DeclineBody | None = None) -> DeclineResponse:
    """Decline a research suggestion. The reason picks the remedy:
    not_relevant / wrong_source remove it and blacklist (entity / target);
    too_noisy keeps the source, live, telling the person about big changes
    only — the one way a suggestion goes live without an approve."""
    from openexecutive.monitoring.research import watch_policy

    reason = (body.reason if body else DECLINE_REASON_NOT_RELEVANT)
    if reason not in EXPLICIT_DECLINE_REASONS:
        raise HTTPException(status_code=400, detail=f"unknown reason {reason!r}")
    item = _pending_suggestion_or_409(slug)
    assert item.id is not None

    # Both remedies are compare-and-act on "still pending", so a concurrent
    # approve (or the sweep) cannot be undone by this decline.
    if reason == DECLINE_REASON_TOO_NOISY:
        if not ms.quiet_pending_suggestion(item.id):
            raise HTTPException(status_code=409, detail=f"{slug!r} is not a pending research suggestion")
        # The principal kept the source but objected to the shape of the
        # guess — that is a decline for calibration, not an approval.
        watch_policy.record_outcome_for(item, watch_policy.OUTCOME_DECLINED)
        audit_log(
            watch_policy.EVENT_SUGGESTION_DECLINED,
            f"Declined watching {slug} ({reason}) — kept_high_floor",
            actor=_API_ACTOR,
            details={"slug": slug, "watchlist_id": item.id, "target": item.target,
                     "signal_type": item.signal_type, "reason": reason, "result": "kept_high_floor"},
        )
        return DeclineResponse(slug=slug, reason=reason, result="kept_high_floor")
    if not watch_policy.decline_suggestion(item, reason, actor=_API_ACTOR):
        raise HTTPException(status_code=409, detail=f"{slug!r} is not a pending research suggestion")
    return DeclineResponse(slug=slug, reason=reason, result="removed")
