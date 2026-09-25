"""Anthropic tool definitions + handlers for managing department Goals.

These tools let the Executive update a Goal's `status` and/or `current`
progress text directly from a chat turn — used when the user reports
progress on a tracked Goal ("we shipped the billing migration") or a
setback ("we lost the Acme deal"). They close the loop that Phase A's
`department_check_in` workflow opened — chat-driven activity now feeds
the same `last_reviewed_at` column the cadence-driven review writes to.

`create_goal` starts tracking a NEW goal the user states ("20 paying
clients by the end of Q4"), filed under an area / department named by
slug or title — creating that area when none matches, which only the
principal on a verified surface may do (the roster tools' rule). Its
schema is static (the area is free text, not an enum of the current
departments), so adding an area never changes the cached tool prefix.

Sit alongside `people_tools`, `schedule_tools`, `broadcast_tools`, and
`alert_tools` in the Executive's main tool loop. No authority gate —
the goal-mutation surface is principal-editable at any time through the
UI, so a chat-turn change is no riskier than a manual edit; the audit
row carries `rationale` for accountability. Neither unattended pass
(reflection, research) is offered `create_goal`: a goal is set by what
the user said, and those passes see only inbound text.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from openexecutive.departments.models import GoalStatus, PeriodType

logger = logging.getLogger(__name__)

_VALID_GOAL_STATUSES = ("on_track", "at_risk", "off_track")
_VALID_PERIOD_TYPES = ("week", "month", "quarter", "year", "ongoing")

# Field caps — the same limits the goal and department routes enforce
# (api/routes/departments.py GoalCreate / DepartmentCreate).
_AREA_MAX = 128
_GOAL_TEXT_MAX = 512
_PERIOD_VALUE_MAX = 64

# Goals one turn may create. A user can state a few goals in one message;
# a sweep ("set up goals for every department") is not a stated goal, and
# this is the code-side backstop to the prompt's one-goal-per-stated-target
# rule. Keyed on the audit turn id, so it applies to chat turns only.
_MAX_GOALS_PER_TURN = 5
_TURN_COUNTS_MAX = 256
_goals_created_by_turn: dict[tuple[str | None, str], int] = {}


LIST_DEPARTMENT_GOALS_TOOL: dict[str, Any] = {
    "name": "list_department_goals",
    "description": (
        "List a department's Goals with their ids, key_results, current "
        "progress, statuses, and last-reviewed timestamps. Use this to "
        "resolve a verbal goal reference ('the migration goal', "
        "'our Q2 ARR target') to a goal_id before calling "
        "update_department_goal. Returns a compact JSON list."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "department_slug": {
                "type": "string",
                "description": (
                    "Lowercase slug from the department registry "
                    "(e.g. 'engineering', 'finance', 'marketing')."
                ),
            },
        },
        "required": ["department_slug"],
    },
}


UPDATE_DEPARTMENT_GOAL_TOOL: dict[str, Any] = {
    "name": "update_department_goal",
    "description": (
        "Update a department Goal's status, current-progress text, or "
        "both — based on something the user just told you. Call this "
        "when the user reports concrete progress on (or a setback to) a "
        "tracked Goal: 'we shipped the billing migration' (likely a "
        "current update + bump to on_track), 'lost the Acme deal' "
        "(status to at_risk or off_track), 'closed 40% of the ARR "
        "target' (current update). Use `list_department_goals` first "
        "if you don't already have the goal_id from earlier in the "
        "conversation. Do NOT use this to refuse and redirect to the "
        "UI — the user expects the update to just happen. Do NOT use "
        "for speculation or for goals the user is only asking advice "
        "on. Records ONE evidence-backed change per call: do not action "
        "a blanket 'update all my goals' / 'mark everything off track' "
        "instruction — without per-goal detail you have nothing real to "
        "record, so ask what concretely changed instead of sweeping "
        "every status. Every call is audited with the rationale you "
        "provide, and the principal can re-edit the goal in the UI at "
        "any time."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "department_slug": {
                "type": "string",
                "description": "Lowercase department slug owning the goal.",
            },
            "goal_id": {
                "type": "integer",
                "description": "Integer id of the goal (from list_department_goals).",
            },
            "status": {
                "type": "string",
                "enum": list(_VALID_GOAL_STATUSES),
                "description": (
                    "New status. Omit to leave the existing status unchanged. "
                    "Must supply at least one of `status` or `current`."
                ),
            },
            "current": {
                "type": "string",
                "description": (
                    "New human-readable progress text (e.g. '$750K of $1M ARR', "
                    "'cutover complete'). Overwrites the prior value. Omit to "
                    "leave unchanged. Must supply at least one of `status` or "
                    "`current`."
                ),
            },
            "rationale": {
                "type": "string",
                "description": (
                    "One short sentence: WHY this update — the goal-specific "
                    "thing the user said or that just happened. Required. Must "
                    "cite evidence for THIS goal, not a blanket reason reused "
                    "across many goals. Stored in the audit log (not on the "
                    "goal row) so future readers can see the provenance of "
                    "every change."
                ),
            },
        },
        "required": ["department_slug", "goal_id", "rationale"],
    },
}


CREATE_GOAL_TOOL: dict[str, Any] = {
    "name": "create_goal",
    "description": (
        "Start tracking a NEW goal the user just stated for themselves or "
        "the business — a concrete target in their own words: 'get to 20 "
        "paying clients by the end of Q4', 'ship the mobile app this "
        "quarter'. Files it under an area (a department) named by slug or "
        "title; an area that does not exist yet is created (only when the "
        "principal asks). If the goal may "
        "already be tracked, check with list_department_goals first and "
        "use update_department_goal to change an existing goal's status or "
        "progress instead. Creates ONE goal per call, each backed by a "
        "target the user actually gave: never invent goals, never turn your "
        "own suggestions or advice into goals, and do not action a blanket "
        "'set up goals for everything' / 'give every department some "
        "goals' request — ask which goal and what target instead. Every "
        "call is audited with the rationale you provide, and the user can "
        "edit or delete the goal in the UI at any time."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "area": {
                "type": "string",
                "description": (
                    "The area or department the goal belongs to: its slug "
                    "('finance') or its title ('Marketing', 'Client work'). "
                    "When none matches, a new area with this title is created."
                ),
            },
            "key_result": {
                "type": "string",
                "description": (
                    "What the goal is, as a short phrase "
                    "('Paying clients', 'Launch the mobile app')."
                ),
            },
            "target": {
                "type": "string",
                "description": (
                    "The measurable target the user stated "
                    "('20 by Dec 31', 'live in the App Store by Nov 15')."
                ),
            },
            "period_type": {
                "type": "string",
                "enum": list(_VALID_PERIOD_TYPES),
                "description": "The goal's timeframe. Defaults to quarter.",
            },
            "period_value": {
                "type": "string",
                "description": (
                    "Which period, e.g. 'Q4 2026', 'November 2026', '2026', "
                    "'Ongoing'. Omit for the current one."
                ),
            },
            "current": {
                "type": "string",
                "description": (
                    "Where it stands today, only if the user said "
                    "('12 paying clients'). Omit otherwise."
                ),
            },
            "status": {
                "type": "string",
                "enum": list(_VALID_GOAL_STATUSES),
                "description": "Defaults to on_track.",
            },
            "rationale": {
                "type": "string",
                "description": (
                    "One short sentence: what the user said that sets this "
                    "goal. Required. Stored in the audit log (not on the goal "
                    "row) so future readers can see where the goal came from."
                ),
            },
        },
        "required": ["area", "key_result", "target", "rationale"],
    },
}


DEPARTMENT_TOOLS: list[dict[str, Any]] = [
    LIST_DEPARTMENT_GOALS_TOOL,
    UPDATE_DEPARTMENT_GOAL_TOOL,
    CREATE_GOAL_TOOL,
]


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #


def _audit(
    tool: str,
    kind: str,
    ok: bool,
    summary: str,
    details: dict[str, Any],
    *,
    department: str | None = None,
) -> None:
    """Emit a `tool_invocation` audit row — mirrors `people_tools._audit`.

    Passes `department` as a top-level kwarg (not just inside `details`) so
    per-department audit queries via `audit_logger.query(department=…)`
    pick it up — the same plumbing the `department_check_in` workflow uses.

    Wrapped so a logger backend hiccup never breaks the chat turn — the
    handler returns its JSON response regardless.
    """
    try:
        from openexecutive.audit import log_event as audit_log

        audit_log(
            "tool_invocation",
            summary,
            actor="executive",
            department=department,
            details={"tool": tool, "kind": kind, "ok": ok, **details},
        )
    except Exception:  # noqa: BLE001 - audit must never break the tool path.
        logger.warning("department_tools: audit log failed", exc_info=True)


async def handle_list_department_goals(tool_input: dict[str, Any]) -> str:
    from openexecutive.departments import store as dept_store

    slug = str(tool_input.get("department_slug", "")).strip()
    if not slug:
        _audit(
            "list_department_goals", "read", False,
            "list_department_goals bad input: empty department_slug",
            {"error": "department_slug required"},
        )
        return json.dumps({"error": "department_slug is required"})

    try:
        goals = dept_store.list_goals(slug)
    except Exception as exc:
        logger.exception("list_department_goals: failed slug=%s", slug)
        _audit(
            "list_department_goals", "read", False,
            f"list_department_goals failed: {exc}",
            {"department_slug": slug, "error": str(exc)[:300]},
            department=slug,
        )
        return json.dumps({"error": str(exc)})

    out = [
        {
            "id": g.id,
            "key_result": g.key_result,
            "target": g.target,
            "current": g.current,
            "status": g.status,
            "period": f"{g.period_type} {g.period_value}".strip(),
            "last_reviewed_at": g.last_reviewed_at,
        }
        for g in goals
    ]
    _audit(
        "list_department_goals", "read", True,
        f"list_department_goals {slug}: {len(out)} goal(s)",
        {"department_slug": slug, "count": len(out)},
        department=slug,
    )
    return json.dumps({"department_slug": slug, "goals": out, "count": len(out)})


async def handle_update_department_goal(tool_input: dict[str, Any]) -> str:
    from openexecutive.departments import registry as dept_registry
    from openexecutive.departments import store as dept_store

    def _bad(err: str, **extra: Any) -> str:
        _audit(
            "update_department_goal", "write", False,
            f"update_department_goal bad input: {err}",
            {"error": err[:300], **extra},
            # `extra` may carry `department_slug` — surface it as the audit
            # row's top-level department tag too, when present.
            department=extra.get("department_slug"),
        )
        return json.dumps({"error": err})

    # ---- Input validation ----
    slug = str(tool_input.get("department_slug", "")).strip()
    if not slug:
        return _bad("department_slug is required")

    raw_goal_id = tool_input.get("goal_id")
    try:
        # `bool` is a subclass of `int` in Python — reject explicitly so a
        # `{"goal_id": true}` from a hallucinating model doesn't pass.
        if isinstance(raw_goal_id, bool) or raw_goal_id is None:
            raise TypeError("goal_id must be an integer")
        goal_id = int(raw_goal_id)
    except (TypeError, ValueError):
        return _bad("goal_id must be an integer", department_slug=slug)

    rationale = str(tool_input.get("rationale", "")).strip()
    if not rationale:
        return _bad(
            "rationale is required (one sentence: why this update)",
            department_slug=slug, goal_id=goal_id,
        )

    status_in = tool_input.get("status")
    current_in = tool_input.get("current")
    if status_in is None and current_in is None:
        return _bad(
            "must supply at least one of `status` or `current`",
            department_slug=slug, goal_id=goal_id,
        )

    if status_in is not None and status_in not in _VALID_GOAL_STATUSES:
        return _bad(
            f"status must be one of {list(_VALID_GOAL_STATUSES)}; got {status_in!r}",
            department_slug=slug, goal_id=goal_id,
        )

    current_str: str | None = None
    if current_in is not None:
        try:
            current_str = str(current_in)
        except Exception:  # noqa: BLE001 - some objects raise from str()
            return _bad("`current` must be coercible to a string",
                        department_slug=slug, goal_id=goal_id)

    # ---- Cross-department id guard ----
    existing = dept_store.get_goal(goal_id)
    if existing is None:
        return _bad(f"goal {goal_id} not found",
                    department_slug=slug, goal_id=goal_id)
    if existing.department_slug != slug:
        return _bad(
            f"goal {goal_id} belongs to department "
            f"{existing.department_slug!r}, not {slug!r}",
            department_slug=slug, goal_id=goal_id,
        )

    # ---- Persist ----
    prior_status = existing.status
    prior_current = existing.current
    prior_updated_at = existing.updated_at
    now_iso = datetime.now(UTC).isoformat()

    try:
        # `update_goal` accepts an optional `last_reviewed_at` so we record
        # the chat-driven review in the same SQL transaction — single row
        # mutation, single Honcho mirror, single `updated_at` bump.
        updated = dept_store.update_goal(
            goal_id,
            status=status_in if status_in is not None else None,
            current=current_str,
            last_reviewed_at=now_iso,
        )
    except Exception as exc:
        logger.exception("update_department_goal: store.update_goal failed id=%s", goal_id)
        _audit(
            "update_department_goal", "write", False,
            f"update_department_goal FAILED id={goal_id}: {exc}",
            {"department_slug": slug, "goal_id": goal_id, "error": str(exc)[:300]},
            department=slug,
        )
        return json.dumps({"error": str(exc)})

    if not updated:
        # Shouldn't happen — we just confirmed the row exists above — but
        # surface the corner case rather than claim success.
        return _bad("update_goal returned False (row vanished mid-call?)",
                    department_slug=slug, goal_id=goal_id)

    # Invalidate the in-memory department cache so subsequent reads
    # (the next /departments fetch, the next prompt-block render) see
    # the fresh status without waiting for a TTL.
    try:
        dept_registry.invalidate()
    except Exception:  # noqa: BLE001 - cache-invalidate must not break the tool.
        logger.warning("update_department_goal: registry invalidate failed", exc_info=True)

    new_status = status_in if status_in is not None else prior_status
    summary = (
        f"update_department_goal {slug}/{goal_id}: "
        f"{prior_status}->{new_status}"
        + ("" if current_str is None else " (current updated)")
    )
    _audit(
        "update_department_goal", "write", True, summary,
        {
            "department_slug": slug,
            "goal_id": goal_id,
            "from_status": prior_status,
            "to_status": new_status,
            "prior_current": prior_current,
            "new_current": current_str,
            "prior_updated_at": prior_updated_at,
            "rationale": rationale[:280],
        },
        department=slug,
    )

    return json.dumps({
        "status": "ok",
        "department_slug": slug,
        "goal_id": goal_id,
        "from_status": prior_status,
        "to_status": new_status,
        "current_updated": current_str is not None,
    })


_MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
)


def _today_local() -> date:
    """Today in the user's zone (the workspace setting, else USER_TIMEZONE)."""
    from openexecutive.memory.workspace_settings import get_user_timezone

    return datetime.now(get_user_timezone()).date()


def default_period_value(period_type: str, today: date) -> str:
    """The current period's label, in the shape the goal editor suggests
    (``TimeframePicker.suggestPeriodValue``): "Week of Sep 21",
    "September 2026", "Q3 2026", "2026", "Ongoing"."""
    if period_type == "week":
        monday = today - timedelta(days=today.weekday())
        return f"Week of {_MONTH_NAMES[monday.month - 1][:3]} {monday.day}"
    if period_type == "month":
        return f"{_MONTH_NAMES[today.month - 1]} {today.year}"
    if period_type == "year":
        return str(today.year)
    if period_type == "ongoing":
        return "Ongoing"
    return f"Q{(today.month - 1) // 3 + 1} {today.year}"


def _norm_goal_text(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _area_title(area: str) -> str:
    """A title for a new area: as given, except a bare slug the model passed
    ("customer_success") reads as words ("Customer Success")."""
    if " " not in area and area == area.lower() and re.search(r"[-_]", area):
        return " ".join(w.capitalize() for w in re.split(r"[-_]+", area) if w)
    return area


def _turn_key() -> tuple[str | None, str] | None:
    from openexecutive.audit.context import get_active_ids

    session_id, turn_id = get_active_ids()
    return (session_id, turn_id) if turn_id else None


def _turn_cap_reached(key: tuple[str | None, str] | None) -> bool:
    return key is not None and _goals_created_by_turn.get(key, 0) >= _MAX_GOALS_PER_TURN


def _count_turn_goal(key: tuple[str | None, str] | None) -> None:
    if key is None:
        return
    _goals_created_by_turn[key] = _goals_created_by_turn.get(key, 0) + 1
    while len(_goals_created_by_turn) > _TURN_COUNTS_MAX:
        # Oldest turn first (dicts keep insertion order).
        _goals_created_by_turn.pop(next(iter(_goals_created_by_turn)))


def _may_add_area() -> bool:
    """Whether this turn may create an area (a department): only the principal
    on a surface that verified it is them — the roster tools' rule. A goal in
    an EXISTING area stays as open as update_department_goal, but a new
    department is org structure, and create_goal is offered on turns an
    inbound email or a teammate started. Fails closed."""
    from openexecutive.orchestrator.people_tools import is_principal_on_verified_surface
    from openexecutive.orchestrator.schedule_tools import current_session

    return is_principal_on_verified_surface(current_session.get())


def _optional_text(tool_input: dict[str, Any], name: str) -> str:
    raw = tool_input.get(name)
    return "" if raw is None else " ".join(str(raw).split())


async def handle_create_goal(tool_input: dict[str, Any]) -> str:
    from openexecutive.departments import registry as dept_registry
    from openexecutive.departments import store as dept_store

    def _bad(err: str, **extra: Any) -> str:
        _audit(
            "create_goal", "write", False,
            f"create_goal bad input: {err}",
            {"error": err[:300], **extra},
            department=extra.get("department_slug"),
        )
        return json.dumps({"error": err})

    # ---- Input validation ----
    area = _optional_text(tool_input, "area")
    if not area:
        return _bad("area is required (an area / department slug or title)")
    if len(area) > _AREA_MAX:
        return _bad(f"area must be at most {_AREA_MAX} characters")

    key_result = _optional_text(tool_input, "key_result")
    target = _optional_text(tool_input, "target")
    if not key_result or not target:
        return _bad("key_result and target are both required", area=area)
    current = _optional_text(tool_input, "current")
    for name, value in (("key_result", key_result), ("target", target), ("current", current)):
        if len(value) > _GOAL_TEXT_MAX:
            return _bad(f"{name} must be at most {_GOAL_TEXT_MAX} characters", area=area)

    rationale = _optional_text(tool_input, "rationale")
    if not rationale:
        return _bad(
            "rationale is required (one sentence: what the user said that sets this goal)",
            area=area,
        )

    raw_period_type = tool_input.get("period_type") or "quarter"
    if raw_period_type not in _VALID_PERIOD_TYPES:
        return _bad(
            f"period_type must be one of {list(_VALID_PERIOD_TYPES)}; got {raw_period_type!r}",
            area=area,
        )
    period_type = cast("PeriodType", raw_period_type)
    raw_status = tool_input.get("status") or "on_track"
    if raw_status not in _VALID_GOAL_STATUSES:
        return _bad(
            f"status must be one of {list(_VALID_GOAL_STATUSES)}; got {raw_status!r}",
            area=area,
        )
    status = cast("GoalStatus", raw_status)
    period_value = _optional_text(tool_input, "period_value")
    if not period_value:
        try:
            period_value = default_period_value(period_type, _today_local())
        except Exception:  # noqa: BLE001 - a zone read must not fail the tool.
            period_value = default_period_value(period_type, datetime.now(UTC).date())
    if len(period_value) > _PERIOD_VALUE_MAX:
        return _bad(f"period_value must be at most {_PERIOD_VALUE_MAX} characters", area=area)

    turn = _turn_key()
    if _turn_cap_reached(turn):
        return _bad(
            f"refused: {_MAX_GOALS_PER_TURN} goals were already created this turn. "
            "Goals are created one per target the user actually stated — ask "
            "which other goals they want tracked and what each target is.",
            area=area, refused=True,
        )

    # ---- Resolve (or create) the area ----
    try:
        dept_store.initialize_db()
        existing = dept_store.list_departments()
        match = dept_store.match_department(area, [d.config for d in existing])
        created = False
        specialist_key: str | None = None
        if match is not None:
            slug, title = match.slug, match.title
            state = next(d for d in existing if d.config.slug == slug)
            wanted = _norm_goal_text(key_result)
            dupe = next(
                (g for g in state.goals if _norm_goal_text(g.key_result) == wanted), None
            )
            if dupe is not None:
                return _bad(
                    f"goal {dupe.id} in {slug!r} already tracks {dupe.key_result!r} — "
                    "call update_department_goal with that goal_id to change it",
                    department_slug=slug, goal_id=dupe.id,
                )
        else:
            if not _may_add_area():
                return _bad(
                    f"refused: no area matches {area!r}, and only the principal, "
                    "asking from a surface that confirms it is them, can add an "
                    "area. File the goal under one of the existing areas in your "
                    "context, or tell whoever asked that the principal needs to "
                    "add this area.",
                    area=area, refused=True,
                )
            title = _area_title(area)
            candidate = dept_store.specialist_key_for_area(title)
            # One department per specialist: a second would make the
            # specialist -> department mapping ambiguous.
            if candidate and not any(d.config.specialist_key == candidate for d in existing):
                specialist_key = candidate
            new_state = dept_store.create_department(title, specialist_key=specialist_key)
            slug, title = new_state.config.slug, new_state.config.title
            created = True

        goal_id = dept_store.insert_goal(
            slug,
            period_type=period_type,
            period_value=period_value,
            key_result=key_result,
            target=target,
            current=current,
            status=status,
        )
    except Exception as exc:
        logger.exception("create_goal: store write failed area=%s", area)
        _audit(
            "create_goal", "write", False,
            f"create_goal FAILED area={area}: {exc}",
            {"area": area, "error": str(exc)[:300]},
        )
        return json.dumps({"error": str(exc)})

    _count_turn_goal(turn)
    try:
        dept_registry.invalidate()
    except Exception:  # noqa: BLE001 - cache-invalidate must not break the tool.
        logger.warning("create_goal: registry invalidate failed", exc_info=True)

    _audit(
        "create_goal", "write", True,
        f"create_goal {slug}/{goal_id}"
        + (" (new area)" if created else "")
        + f": {key_result[:80]}",
        {
            "department_slug": slug,
            "goal_id": goal_id,
            "area_created": created,
            "specialist_key": specialist_key,
            "key_result": key_result[:280],
            "target": target[:280],
            "period": f"{period_type} {period_value}",
            "status": status,
            "rationale": rationale[:280],
        },
        department=slug,
    )
    return json.dumps({
        "status": "ok",
        "goal_id": goal_id,
        "area_slug": slug,
        "area_title": title,
        "area_created": created,
        "period": f"{period_type} {period_value}",
    })


DEPARTMENT_TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[str]]] = {
    "list_department_goals": handle_list_department_goals,
    "update_department_goal": handle_update_department_goal,
    "create_goal": handle_create_goal,
}
