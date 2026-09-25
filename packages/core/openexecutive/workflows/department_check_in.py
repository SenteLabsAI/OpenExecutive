"""Department check-in workflow — proactive daily/weekly cadence artifact.

Runs automatically when a ``dept_cadence`` scheduled_action fires.  Can also
be triggered manually through the workflow API.

Steps
-----
1. ``load_context``      Load DepartmentState, recent audit entries, decisions.
2. ``goal_status``       Specialist grades each Goal (on_track / at_risk / off_track).
3. ``blockers``          Specialist surfaces top-3 blockers + proposed resolutions.
4. ``proposed_actions``  Specialist proposes concrete actions; each is gated via
                         the authority gate before appearing in the artifact.
5. ``assemble``          Combines all sections into a 1-page Markdown artifact.
"""
from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from openexecutive.knowledge.store import ChromaDBStore
from openexecutive.workflows.base import (
    Workflow,
    WorkflowEvent,
    WorkflowSection,
    WorkflowStepDef,
)

if TYPE_CHECKING:
    from openexecutive.departments.models import DepartmentState

logger = logging.getLogger(__name__)

# Actor on the check-in's own `goal_status_review` audit row. `needs_check_in`
# ignores that row: it lands after the Goals are stamped reviewed, so counting
# it as "new" would re-run every check-in.
_CHECK_IN_ACTOR = "department_check_in"


class DepartmentCheckInInput(BaseModel):
    """Inputs for a department check-in run."""

    department_slug: str = Field(
        ...,
        description="Slug of the department to check in on (e.g. 'finance').",
    )
    period_label: str = Field(
        default="",
        description="Human-readable period label, e.g. '2026-05-21'. Auto-filled when blank.",
    )


class DepartmentCheckInWorkflow(Workflow):
    name = "department_check_in"
    title = "Department Check-In"
    description = (
        "Proactive daily or weekly check-in for a single department. "
        "Grades Goals, surfaces blockers, and proposes gated actions. "
        "Typically triggered by the cadence scheduler, not manually."
    )
    section = WorkflowSection.OPERATING
    estimated_minutes = 2

    def input_model(self) -> type[BaseModel]:
        return DepartmentCheckInInput

    def steps(self) -> list[WorkflowStepDef]:
        return [
            WorkflowStepDef(
                id="load_context",
                title="Load department context",
                description="Pull current Goals, recent audit entries, and decisions.",
            ),
            WorkflowStepDef(
                id="goal_status",
                title="Goal status review",
                description="Specialist grades each Goal against current progress.",
            ),
            WorkflowStepDef(
                id="blockers",
                title="Blockers & resolutions",
                description="Identify top-3 blockers and proposed resolutions.",
            ),
            WorkflowStepDef(
                id="proposed_actions",
                title="Proposed actions (gated)",
                description="Surface concrete actions; each is checked through the authority gate.",
            ),
            WorkflowStepDef(
                id="assemble",
                title="Assemble check-in report",
                description="Combine all sections into a 1-page Markdown artifact.",
            ),
        ]

    async def run(
        self,
        inputs: BaseModel,
        store: ChromaDBStore,
    ) -> AsyncIterator[WorkflowEvent]:
        assert isinstance(inputs, DepartmentCheckInInput)
        slug = inputs.department_slug
        period = inputs.period_label or datetime.now(UTC).strftime("%Y-%m-%d")
        now = datetime.now(UTC)

        # ------------------------------------------------------------------ #
        # Step 1: Load context
        # ------------------------------------------------------------------ #
        yield WorkflowEvent(
            type="step_start",
            step_id="load_context",
            step_title="Load department context",
        )

        from openexecutive.audit.logger import get_audit_logger
        from openexecutive.departments import registry as dept_registry
        from openexecutive.memory import episodic
        from openexecutive.memory.episodic import get_recent_decisions

        state = dept_registry.get_state(slug)
        if state is None:
            yield WorkflowEvent(
                type="error",
                message=f"Department {slug!r} not found in registry.",
            )
            return

        # Gather recent decisions tagged to this department. Pass
        # ``db_path`` from the live module attribute so a test fixture
        # that monkeypatches ``episodic.DB_PATH`` to a tmp file is
        # respected — the function's default argument otherwise captures
        # the original DB_PATH at definition time and ignores the patch.
        all_decisions = get_recent_decisions(limit=20, db_path=episodic.DB_PATH)
        dept_decisions = [d for d in all_decisions if d.department == slug]

        # Recent audit entries — filter by department in Python since the
        # query() method doesn't accept a department filter yet. Never the
        # rows private to the principal: the check-in reports to the
        # department.
        try:
            all_audit = get_audit_logger().query(limit=30, include_private=False)
            audit_entries = [e for e in all_audit if e.department == slug][:20]
        except Exception:
            audit_entries = []

        yield WorkflowEvent(
            type="step_done",
            step_id="load_context",
            summary=(
                f"Loaded {len(state.goals)} Goals, "
                f"{len(dept_decisions)} decisions, "
                f"{len(audit_entries)} audit events."
            ),
        )

        # ------------------------------------------------------------------ #
        # Step 2: Goal status
        # ------------------------------------------------------------------ #
        yield WorkflowEvent(
            type="step_start",
            step_id="goal_status",
            step_title="Goal status review",
        )

        from openexecutive.knowledge.retriever import retrieve
        from openexecutive.orchestrator.router import route_to_specialist

        specialist_key = state.config.specialist_key
        if not specialist_key:
            yield WorkflowEvent(
                type="error",
                message=(
                    f"Department {slug!r} is informational only "
                    "(no specialist agent). Check-in workflow requires a "
                    "specialist_key — skipping."
                ),
            )
            return
        goal_block = _render_goals(state)
        decision_block = _render_decisions(dept_decisions)

        goal_context = (
            f"Department: {state.config.title}\n"
            f"Mission: {state.config.charter.mission}\n\n"
            f"{goal_block}"
        )
        if decision_block:
            goal_context += f"\n\nRecent decisions:\n{decision_block}"

        goal_status_text = await route_to_specialist(
            specialist_name=specialist_key,
            query=(
                f"Review the current Goal progress for the {state.config.title} department "
                f"for the period {period}. For each Goal, assess whether it is "
                "on_track, at_risk, or off_track based on the current value vs. target.\n\n"
                "Return ONLY a JSON object on its own — no prose before or after, "
                "no markdown fences — with this exact shape:\n"
                '{"verdicts": [{"goal_id": <int from the [id=N] prefix>, '
                '"status": "on_track" | "at_risk" | "off_track", '
                '"rationale": "<one short sentence>"}, ...], '
                '"narrative": "<one paragraph rendered into the check-in report; '
                'mention any goal that moved status since the last period>"}\n\n'
                "Use the integer id from each Goal's `[id=N]` prefix below. "
                "Skip Goals you cannot grade. Include every Goal you do grade — "
                "the verdict is persisted back to the database.\n\n"
                f"{goal_context}"
            ),
            context=goal_context,
            retrieved_knowledge=retrieve(
                query=f"{state.config.title} Goal performance review {period}",
                specialist_name=specialist_key,
                n_builtin=3,
                n_company=2,
                store=store,
            ),
        )

        verdicts, narrative_text = _parse_verdicts(goal_status_text)

        # Persist verdicts back to the goal rows. Always overwrites — the
        # audit row below preserves prior status for the diff trail, and
        # the principal retains the manual UI edit path as the final
        # override. Hallucinated goal_ids (specialist referenced an id not
        # in `state.goals`) are silently dropped.
        from openexecutive.departments import registry as _dept_registry
        from openexecutive.departments import store as _dept_store

        goals_by_id = {g.id: g for g in state.goals if g.id is not None}
        transitions: list[dict[str, Any]] = []
        for v in verdicts:
            goal = goals_by_id.get(v["goal_id"])
            if goal is None:
                continue
            prior_status = goal.status
            try:
                _dept_store.record_goal_review(
                    v["goal_id"],
                    status=v["status"],
                    last_reviewed_at=now.isoformat(),
                )
            except Exception:  # noqa: BLE001 - persistence is best-effort; artifact must still ship.
                logger.exception(
                    "check_in: record_goal_review failed slug=%s goal_id=%s",
                    slug, v["goal_id"],
                )
                continue
            transitions.append({
                "goal_id": v["goal_id"],
                "from": prior_status,
                "to": v["status"],
                "rationale": v["rationale"],
            })

        if transitions:
            # Single audit row per workflow run keeps audit volume sane
            # (~8 rows/day default across the seeded departments).
            try:
                get_audit_logger().log(
                    "goal_status_review",
                    f"{state.config.title}: reviewed {len(transitions)} goal(s)",
                    actor=_CHECK_IN_ACTOR,
                    department=slug,
                    details={"period": period, "transitions": transitions},
                )
            except Exception:  # noqa: BLE001 - audit must not break the workflow.
                logger.warning("check_in: audit log failed", exc_info=True)
            # Invalidate the registry cache so the UI's next read sees fresh status.
            _dept_registry.invalidate()

        yield WorkflowEvent(
            type="step_done",
            step_id="goal_status",
            summary=(
                f"Persisted {len(transitions)} verdict(s); "
                f"{_first_line(narrative_text)}"
            ),
        )

        # ------------------------------------------------------------------ #
        # Step 3: Blockers
        # ------------------------------------------------------------------ #
        yield WorkflowEvent(
            type="step_start",
            step_id="blockers",
            step_title="Blockers & resolutions",
        )

        audit_block = _render_audit(audit_entries)
        blockers_text = await route_to_specialist(
            specialist_name=specialist_key,
            query=(
                f"Identify the top 3 blockers currently facing the {state.config.title} "
                f"department as of {period}. For each blocker, propose a concrete resolution "
                "the Executive or a team member can take. "
                "Base your analysis on the Goal status, recent audit activity, and decisions below.\n\n"
                f"{goal_context}\n\n"
                f"{audit_block}"
            ),
            context=goal_context,
            retrieved_knowledge="",
        )

        yield WorkflowEvent(
            type="step_done",
            step_id="blockers",
            summary=_first_line(blockers_text),
        )

        # ------------------------------------------------------------------ #
        # Step 4: Proposed actions (gated)
        # ------------------------------------------------------------------ #
        yield WorkflowEvent(
            type="step_start",
            step_id="proposed_actions",
            step_title="Proposed actions (gated)",
        )

        raw_actions_text = await route_to_specialist(
            specialist_name=specialist_key,
            query=(
                f"Based on the {state.config.title} department's current Goal status and blockers, "
                f"propose up to 3 concrete actions for the period starting {period}. "
                "For each action output a JSON object on its own line with keys: "
                '"kind" (string, e.g. "vendor_negotiation"), '
                '"payload" (1-sentence description), '
                '"required_scope" (one of: spend_lt_2k, spend_lt_10k, spend_gt_10k, '
                "hiring_signoff, vendor_onboarding, customer_credit, legal_sign, "
                'board_comms, wildcard — or omit if none needed). '
                "Output ONLY the JSON lines, no markdown fences.\n\n"
                f"{goal_context}\n\nBlockers summary:\n{blockers_text[:800]}"
            ),
            context=goal_context,
            retrieved_knowledge="",
        )

        gated_actions = _gate_proposed_actions(raw_actions_text, slug, now)

        yield WorkflowEvent(
            type="step_done",
            step_id="proposed_actions",
            summary=f"{len(gated_actions)} action(s) proposed and gated.",
        )

        # ------------------------------------------------------------------ #
        # Step 5: Assemble
        # ------------------------------------------------------------------ #
        yield WorkflowEvent(
            type="step_start",
            step_id="assemble",
            step_title="Assemble check-in report",
        )

        artifact = _assemble_artifact(
            state=state,
            period=period,
            # Render the parsed narrative (or, on parse failure, the raw
            # response — `_parse_verdicts` falls back to `text`). Either
            # way the report shows prose, never raw JSON.
            goal_status_text=narrative_text,
            blockers_text=blockers_text,
            gated_actions=gated_actions,
        )

        yield WorkflowEvent(
            type="step_done",
            step_id="assemble",
            summary=f"Assembled {len(artifact)} character report.",
        )
        # Mirror the check-in summary into the dept peer's notes
        # timeline. Best-effort — a Honcho failure must not break the
        # workflow's artifact emission. The dept peer accumulates a
        # chronological narrative of check-ins, so "how has Engineering
        # evolved this quarter?" returns a story rather than disjoint
        # rows. Body is the assembled artifact, kind-tagged so the dept
        # peer's representation extraction can distinguish cadence notes
        # from other note kinds.
        try:
            from openexecutive.memory.honcho_client import append_department_note

            append_department_note(
                department_slug=slug,
                kind="cadence_checkin",
                body=f"Check-in for {period}:\n{artifact[:4000]}",
            )
        except Exception:  # noqa: BLE001 - mirror must not break the workflow.
            pass
        yield WorkflowEvent(type="artifact", content=artifact)


# --------------------------------------------------------------------------- #
# Skip rule — asked by the scheduler before it creates a run
# --------------------------------------------------------------------------- #

def needs_check_in(state: DepartmentState, now: datetime) -> str | None:
    """Why a scheduled check-in for ``state`` can be skipped, or None to run it.

    The scheduler asks before it creates the run, so a department with nothing
    to review costs no specialist calls and leaves no empty run in the activity
    rail. Skips when:

    - the department has no ``specialist_key`` (informational only; the
      workflow would stop at the goal step anyway);
    - it has no Goals (nothing to grade);
    - nothing is new since the oldest Goal's ``last_reviewed_at``: no
      decision or audit row tagged to the department (the check-in's own
      review row excluded), and no Goal whose content changed after it was
      last graded.

    A Goal that was never reviewed always runs the check-in. Reads the same
    sources as the ``load_context`` step and fails open: a source that cannot
    be read counts as "something may be new".
    """
    from openexecutive.alerts.lifecycle import parse_aware

    slug = state.config.slug
    if not state.config.specialist_key:
        return "informational department (no specialist agent)"
    if not state.goals:
        return "no Goals to review"

    reviewed = [parse_aware(g.last_reviewed_at) for g in state.goals]
    if any(r is None for r in reviewed):
        return None  # a Goal was never graded
    baseline = min(r for r in reviewed if r is not None)

    for goal, goal_reviewed in zip(state.goals, reviewed, strict=True):
        edited = parse_aware(goal.updated_at)
        if edited is not None and goal_reviewed is not None and edited > goal_reviewed:
            return None

    try:
        from openexecutive.memory import episodic
        from openexecutive.memory.episodic import has_department_decision_since

        if has_department_decision_since(slug, baseline, db_path=episodic.DB_PATH):
            return None
    except Exception:
        logger.warning("check_in: decisions unreadable for %s — running", slug, exc_info=True)
        return None

    try:
        from openexecutive.audit.logger import get_audit_logger

        # Not the rows private to the principal: whether the check-in runs
        # would otherwise tell the department one was written.
        rows = get_audit_logger().query(
            department=slug,
            since=baseline.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),  # audit ts format
            limit=200,
            include_private=False,
        )
    except Exception:
        logger.warning("check_in: audit unreadable for %s — running", slug, exc_info=True)
        return None
    if any(row.actor != _CHECK_IN_ACTOR for row in rows):
        return None

    days = max(0, (now - baseline).days)
    return f"nothing new since the Goals were last reviewed ({days}d ago)"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _render_goals(state: object) -> str:  # type: ignore[type-arg]
    from openexecutive.departments.models import DepartmentState
    assert isinstance(state, DepartmentState)
    if not state.goals:
        return "No Goals configured for this department."
    lines = ["**Goals:**"]
    for goal in state.goals:
        current = f" (current: {goal.current})" if goal.current else ""
        period_label = (
            goal.period_value
            if goal.period_type == "ongoing"
            else f"{goal.period_type} {goal.period_value}"
        )
        # `[id=N]` is required so the structured-JSON `goal_status` step
        # can reference Goals by stable id in its `verdicts` array — the
        # specialist's text otherwise has nothing to anchor a per-goal
        # status assessment to. Goals without an id (theoretically
        # impossible post-insert, but ``Goal.id`` is Optional) render
        # without the prefix and are simply ungradable this cycle.
        id_prefix = f"[id={goal.id}] " if goal.id is not None else ""
        lines.append(
            f"- {id_prefix}[{goal.status}] {goal.key_result} — target: {goal.target}{current}"
            f" ({period_label})"
        )
    return "\n".join(lines)


def _render_decisions(decisions: Sequence[object]) -> str:
    if not decisions:
        return ""
    lines = []
    for d in decisions[:5]:
        lines.append(f"- {getattr(d, 'timestamp', '')[:10]}: {getattr(d, 'summary', '')}")
    return "\n".join(lines)


def _render_audit(entries: Sequence[object]) -> str:
    if not entries:
        return "No recent audit activity."
    lines = ["**Recent activity:**"]
    for e in entries[:10]:
        raw_ts = getattr(e, "ts", None) or getattr(e, "created_at", "") or ""
        ts = raw_ts[:16] if raw_ts else ""
        raw_summary = getattr(e, "summary", None) or ""
        summary = raw_summary[:120]
        lines.append(f"- {ts}: {summary}")
    return "\n".join(lines)


_VALID_GOAL_STATUSES = frozenset({"on_track", "at_risk", "off_track"})


def _parse_verdicts(text: str) -> tuple[list[dict[str, Any]], str]:
    """Extract structured per-goal verdicts + a narrative from the
    specialist's response.

    Expected shape::

        {"verdicts": [{"goal_id": int, "status": <one of three>, "rationale": str}, ...],
         "narrative": "<one paragraph rendered into the artifact>"}

    Lenient on real-world LLM output: strips ``​\\`\\`\\`json`` /
    ``\\`\\`\\``` fences, skips malformed verdicts, and bails to
    ``([], text)`` on any JSON error. Bailing keeps the original prose
    available so ``_assemble_artifact`` can still render *something* —
    the workflow's contract is "the artifact always ships," even when
    persistence has to no-op for the cycle.
    """
    import json as _json
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        # Drop the opening fence line (``` or ```json) and a trailing fence.
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        obj = _json.loads(stripped)
    except _json.JSONDecodeError:
        return [], text
    if not isinstance(obj, dict):
        return [], text
    # An empty `narrative` must NOT fall back to `text` — `text` is the
    # raw JSON-encoded specialist response, and `_assemble_artifact`
    # renders this value directly under "## Goal Status" in the
    # Markdown report. Falling back to `text` here leaked
    # `{"verdicts": [...], "narrative": ""}` literally into the
    # check-in artifact whenever a model returned a valid JSON object
    # with an empty narrative — a real ~5% failure mode on quiet
    # departments. The `text` fallback is reserved for the JSON-parse
    # failure branch above; an empty narrative is "the model had
    # nothing extra to say," and `_assemble_artifact`'s `_ensure_section`
    # renders "(No content generated.)" for that case, which is the
    # intended graceful-empty UX.
    narrative_raw = obj.get("narrative", "")
    narrative = str(narrative_raw) if narrative_raw else ""
    raw_verdicts = obj.get("verdicts", [])
    if not isinstance(raw_verdicts, list):
        return [], narrative
    valid: list[dict[str, Any]] = []
    for v in raw_verdicts:
        if not isinstance(v, dict):
            continue
        goal_id = v.get("goal_id")
        status = v.get("status")
        if not isinstance(goal_id, int):
            continue
        if status not in _VALID_GOAL_STATUSES:
            continue
        # Cap rationale length defensively — the audit row preserves the
        # transition trail, and an over-long string just bloats it.
        # Coerce a JSON `null` rationale to '' rather than the string
        # "None" that `str(None)` would produce.
        rationale_raw = v.get("rationale", "") or ""
        valid.append({
            "goal_id": goal_id,
            "status": status,
            "rationale": str(rationale_raw)[:280],
        })
    return valid, narrative


def _gate_proposed_actions(
    raw_text: str,
    department_slug: str,
    now: datetime,
) -> list[dict[str, Any]]:
    """Parse JSON action lines from ``raw_text``, run each through gate_action."""
    import json as _json

    from openexecutive.departments.authority import gate_action
    from openexecutive.people.models import AuthorityScope

    results: list[dict[str, Any]] = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            action = _json.loads(line)
        except _json.JSONDecodeError:
            continue

        kind = str(action.get("kind", "ad_hoc"))
        payload = str(action.get("payload", ""))
        scope_str = action.get("required_scope")
        required_scope: AuthorityScope | None = None
        if scope_str:
            with contextlib.suppress(ValueError):
                required_scope = AuthorityScope(scope_str)

        try:
            decision = gate_action(
                department_slug,
                kind,
                required_scope=required_scope,
                now=now,
            )
        except Exception:
            logger.exception("check_in: gate_action failed for kind=%r", kind)
            decision = None

        results.append({
            "kind": kind,
            "payload": payload,
            "required_scope": scope_str,
            "gate": decision.model_dump() if decision else None,
        })

    return results


def _assemble_artifact(
    *,
    state: object,
    period: str,
    goal_status_text: str,
    blockers_text: str,
    gated_actions: list[dict[str, Any]],
) -> str:
    from openexecutive.departments.models import DepartmentState
    assert isinstance(state, DepartmentState)

    title = state.config.title
    authority = state.config.authority_level.value.replace("_", " ")

    action_lines: list[str] = []
    for a in gated_actions:
        gate = a.get("gate") or {}
        gate_action_str = gate.get("action", "unknown")
        assignee = gate.get("assignee_person_id")
        deliver_raw = gate.get("deliver_at", "")
        if hasattr(deliver_raw, "isoformat"):
            deliver = deliver_raw.isoformat()
        elif deliver_raw:
            # Normalise Z-suffix so slice [:16] always gives "YYYY-MM-DDTHH:MM"
            deliver = str(deliver_raw).replace("Z", "+00:00")
        else:
            deliver = ""
        _ISO_MINUTE_LEN = 16  # "YYYY-MM-DDTHH:MM"
        deliver_str = f" (deliver at {deliver[:_ISO_MINUTE_LEN]})" if deliver else ""
        action_lines.append(
            f"- **{a['kind']}**: {a['payload']}"
            f" → gate: *{gate_action_str}*"
            + (f" (assignee: person {assignee})" if assignee else "")
            + deliver_str
        )
    actions_block = "\n".join(action_lines) if action_lines else "_No actions proposed._"

    parts = [
        f"# {title} — Check-In Report",
        f"**Period:** {period}  |  **Authority level:** {authority}",
        "",
        "## Goal Status",
        "",
        _ensure_section(goal_status_text),
        "",
        "## Blockers & Resolutions",
        "",
        _ensure_section(blockers_text),
        "",
        "## Proposed Actions",
        "",
        actions_block,
        "",
        "---",
        "_Generated by Open Executive department check-in. "
        "Review before acting on any proposals._",
    ]
    return "\n".join(parts).strip() + "\n"


def _ensure_section(text: str) -> str:
    t = text.strip()
    return t if t else "_(No content generated.)_"


def _first_line(text: str, max_len: int = 140) -> str:
    if not text:
        return "(empty)"
    line = text.strip().split("\n", 1)[0].lstrip("# ").strip()
    return line[: max_len - 1] + "…" if len(line) > max_len else line
