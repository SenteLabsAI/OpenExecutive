"""Render current department state into a compact Markdown block for the
5m-TTL system prompt cache slot.

Called by `prompts.cache_manager.build_system_blocks` on every turn. The
result is appended after the company-profile block; the 1h persona and
knowledge blocks are never touched.

Key invariants:
- Output must be deterministic for the same DB state (no timestamps, UUIDs,
  or non-deterministic ordering). Determinism keeps the 5m cache warm.
- Output is capped at _ORG_BLOCK_CHAR_CAP characters so a department with
  many Goals never inflates the system prompt.
- An empty departments table (fresh install, test env) returns "" so no block
  is appended — the function must never raise.
- Phase 3 extended this to include a compact People roster below the
  departments section. The People section is omitted when no People are seeded.
- Solo mode (one person — the principal — uses Open Executive;
  ``render_org_block(mode="solo")``) renders a different, smaller block: the
  principal's own line and their goals grouped by *area* (a department row is
  an area there). No authority levels, heads, cadences, channels or roster —
  the principal decides, and the people in their world are contacts, not a
  team the Executive routes to. Same determinism, sanitizing and cap.

Security note: Goal text fields (key_result, current, target, mission) are
user-controlled strings that land inside the system prompt. All values are
sanitized via _safe() before rendering to prevent prompt-injection via
embedded newlines, Markdown headings, or instruction-like text.
"""
from __future__ import annotations

import logging
import re

from openexecutive.departments.models import DepartmentState, Goal

logger = logging.getLogger(__name__)

# Hard cap on total block size. A 4000-char block at ~4 bytes/char averages
# ~1000 tokens — well within the 5m-TTL cache slot budget.
_ORG_BLOCK_CHAR_CAP = 4000

# Per-field length caps to prevent any single Goal from dominating the block.
_MISSION_CHAR_CAP = 120
_KEY_RESULT_CHAR_CAP = 300
_CURRENT_CHAR_CAP = 100
_PERIOD_VALUE_CHAR_CAP = 64  # matches the API GoalCreate.period_value max_length

# Header string for the org block — constant so Phase 3 can reliably locate it.
_ORG_BLOCK_HEADER = "## Departments You Manage"

# Solo-mode headers (see `_render_solo_block`). "You" in this prompt is the
# Executive, so both name the principal from its side — never "## You", which
# the Executive could read as its own identity.
_SOLO_PRINCIPAL_HEADER = "## Your Principal"
_SOLO_GOALS_HEADER = "## Your Principal's Goals"

# Goal status → words for the solo block, which has room to be plain.
_SOLO_STATUS_LABEL: dict[str, str] = {
    "on_track": "on track",
    "at_risk": "at risk",
    "off_track": "off track",
}
_TARGET_CHAR_CAP = 100

# Goal status → short display label so the block stays scannable.
_STATUS_LABEL: dict[str, str] = {
    "on_track": "✓",
    "at_risk": "⚠",
    "off_track": "✗",
}

# Characters that could start a Markdown heading or break the prompt structure.
_UNSAFE_PATTERN = re.compile(r"[\n\r\x0b\x0c]|^#+\s*", re.MULTILINE)


def _safe(value: str, max_len: int) -> str:
    """Sanitize a user-supplied string for safe embedding in a system prompt.

    Strips embedded newlines (which could introduce new Markdown sections or
    instruction-like text) and caps length. Any text that looks like a
    Markdown heading at the start of the value is also stripped.
    """
    cleaned = _UNSAFE_PATTERN.sub(" ", value).strip()
    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 1] + "…"
    return cleaned


def _status(s: str) -> str:
    return _STATUS_LABEL.get(s, s)


def _render_department_section(state: DepartmentState, head_name: str | None = None) -> str:
    """Render one department section: heading + mission + Goals."""
    config = state.config
    goals = state.goals

    authority_label = config.authority_level.value.replace("_", " ")
    specialist_label = (
        config.specialist_key.upper() if config.specialist_key else "INFORMATIONAL"
    )
    heading = (
        f"### {config.title} "
        f"({specialist_label}) — {authority_label}"
    )
    if head_name:
        heading += f" — head: {_safe(head_name, 60)}"

    if not goals and not config.charter.mission:
        return heading

    lines: list[str] = [heading]

    if config.charter.mission:
        lines.append(f"Mission: {_safe(config.charter.mission, _MISSION_CHAR_CAP)}")

    if goals:
        # Group by (period_type, period_value); sort the bucket keys lexically
        # so output is deterministic across runs against the same DB.
        by_period: dict[tuple[str, str], list[Goal]] = {}
        for goal in goals:
            by_period.setdefault((goal.period_type, goal.period_value), []).append(goal)

        for (period_type, period_value) in sorted(by_period):
            # period_type is constrained to the enum so it's safe to render raw;
            # period_value is free-text from the user so it must go through _safe()
            # before landing in the cached system prompt (otherwise a malicious
            # value like "Q2\n\n## OVERRIDE" could inject a new section).
            safe_value = _safe(period_value, _PERIOD_VALUE_CHAR_CAP)
            header_label = (
                safe_value if period_type == "ongoing"
                else f"{period_type.capitalize()} {safe_value}"
            )
            lines.append(f"{header_label} Goals:")
            # Secondary sort by id for determinism within a period when two
            # Goals were inserted in the same transaction.
            for goal in sorted(by_period[(period_type, period_value)], key=lambda g: (g.id or 0)):
                current_note = (
                    f"  (now: {_safe(goal.current, _CURRENT_CHAR_CAP)})"
                    if goal.current
                    else ""
                )
                lines.append(
                    f"  [{_status(goal.status)}]"
                    f" {_safe(goal.key_result, _KEY_RESULT_CHAR_CAP)}{current_note}"
                )

    return "\n".join(lines)


def _render_people_section(people: list | None = None) -> str:
    """Render a compact ## People You Coordinate With section.

    Returns "" when the people table is empty or unavailable. Each person
    is one line; full_name and role are sanitized via _safe().

    Accepts an optional pre-fetched people list to avoid a second DB call
    when render_org_block() has already loaded people for head-name lookup.
    """
    try:
        if people is None:
            from openexecutive.people.registry import list_people as _list_people
            people = _list_people()

        if not people:
            return ""

        # id → name map for reports-to lookup; sanitized once so we don't
        # re-_safe() per row.
        name_by_id: dict[int, str] = {
            p.id: _safe(p.full_name, 80) for p in people if p.id is not None
        }

        lines = ["## People You Coordinate With\n"]
        for person in people:
            scope_tokens = ", ".join(
                s.value for s in person.authority_scope
            ) if person.authority_scope else "none"
            name = _safe(person.full_name, 80)
            role = _safe(person.role, 80) if person.role else "—"
            channel = person.preferred_channel
            sla = person.response_sla_hours
            principal_tag = " (principal)" if person.is_principal else ""

            # Contact channels and org placement: only render fields that are
            # set, so the Executive can see them when answering "what do you
            # know about me". All values are sanitized — they originate from
            # user input via the API and land in the cached system prompt.
            extras: list[str] = []
            if person.email:
                extras.append(f"email: {_safe(person.email, 120)}")
            if person.slack_user_id:
                extras.append(f"slack: {_safe(person.slack_user_id, 64)}")
            if person.telegram_chat_id:
                extras.append(f"telegram: {_safe(person.telegram_chat_id, 64)}")
            if person.discord_user_id:
                extras.append(f"discord: {_safe(person.discord_user_id, 64)}")
            if person.department_slugs:
                slugs = ", ".join(_safe(s, 40) for s in person.department_slugs)
                extras.append(f"depts: {slugs}")
            if person.reports_to_person_id is not None:
                manager = name_by_id.get(person.reports_to_person_id)
                if manager:
                    extras.append(f"reports to: {manager}")
            extras_tail = (" — " + " — ".join(extras)) if extras else ""

            lines.append(
                f"- {name} — {role}{principal_tag}"
                f" — {channel} — approves: {scope_tokens} — SLA {sla}h"
                f"{extras_tail}"
            )
        return "\n".join(lines)
    except Exception:
        # Same broad catch as the departments section — must never crash a turn.
        logger.warning("render_people_section failed — omitting People context", exc_info=True)
        return ""


def _period_label(period_type: str, period_value: str) -> str:
    """A goal's period, e.g. ``Quarter Q3 2026`` — the same rule as the team
    block's period headers. period_value is user text, so it is sanitized."""
    safe_value = _safe(period_value, _PERIOD_VALUE_CHAR_CAP)
    if period_type == "ongoing":
        return safe_value or "Ongoing"
    return f"{period_type.capitalize()} {safe_value}".strip()


def _render_solo_goal(goal: Goal) -> str:
    status = _SOLO_STATUS_LABEL.get(goal.status, _safe(goal.status, 20))
    line = (
        f"- [{status}] {_period_label(goal.period_type, goal.period_value)}: "
        f"{_safe(goal.key_result, _KEY_RESULT_CHAR_CAP)}"
    )
    if goal.target:
        line += f" — target: {_safe(goal.target, _TARGET_CHAR_CAP)}"
    if goal.current:
        line += f" — now: {_safe(goal.current, _CURRENT_CHAR_CAP)}"
    if goal.id is not None:
        line += f" (goal_id {goal.id})"
    return line


def _render_solo_principal() -> str:
    """The principal's own line: name and the channels they can be reached on.

    The principal is ``people.store.find_principal_person`` — the oldest
    non-archived principal, the same rule every solo check uses (the
    principal-only messaging guard, follow-ups, the meeting gate) — so the
    person this block names is the person those checks let through. The
    identifiers are there so a follow-up to the principal can name its
    channel_ref without a lookup. Contacts are not listed — they are reached
    only when the principal asks, via list_people.
    """
    from openexecutive.people.store import find_principal_person

    try:
        principal = find_principal_person()
    except Exception:
        logger.warning("render_org_block: principal lookup failed — omitting the principal", exc_info=True)
        return ""
    if principal is None:
        return ""
    reach: list[str] = []
    if principal.email:
        reach.append(f"email {_safe(principal.email, 120)}")
    if principal.slack_user_id:
        reach.append(f"slack {_safe(principal.slack_user_id, 64)}")
    if principal.telegram_chat_id:
        reach.append(f"telegram {_safe(principal.telegram_chat_id, 64)}")
    if principal.discord_user_id:
        reach.append(f"discord {_safe(principal.discord_user_id, 64)}")
    line = f"- {_safe(principal.full_name, 80)} (principal)"
    if principal.id is not None:
        line += f" — person_id {principal.id}"
    if reach:
        line += " — reachable on: " + ", ".join(reach)
    if principal.preferred_channel and principal.preferred_channel != "any":
        line += f" — prefers {principal.preferred_channel}"
    return f"{_SOLO_PRINCIPAL_HEADER}\n\n{line}"


def _render_solo_goals(states: list[DepartmentState]) -> str:
    """Goals grouped by area (a department row), areas with no goals skipped.

    Areas keep the registry's order; goals sort by (period, id) like the team
    block, so the output is deterministic for the same DB state.
    """
    sections: list[str] = []
    for state in states:
        if not state.goals:
            continue
        lines = [f"### {_safe(state.config.title, 80)} (area slug: {_safe(state.config.slug, 40)})"]
        for goal in sorted(
            state.goals, key=lambda g: (g.period_type, g.period_value, g.id or 0)
        ):
            lines.append(_render_solo_goal(goal))
        sections.append("\n".join(lines))
    if not sections:
        return ""
    return "\n\n".join([
        f"{_SOLO_GOALS_HEADER}\n\n"
        "Grouped by area. Tools take the area slug as `department_slug`.",
        *sections,
    ])


def _render_solo_block(states: list[DepartmentState]) -> str:
    return "\n\n".join(
        part for part in (_render_solo_principal(), _render_solo_goals(states)) if part
    )


def _cap(body: str) -> str:
    """Hold the block to _ORG_BLOCK_CHAR_CAP, cutting at a section boundary."""
    if len(body) > _ORG_BLOCK_CHAR_CAP:
        # Truncate at the nearest preceding section boundary ("\n\n") to
        # avoid cutting mid-Goal and producing malformed Markdown.
        truncated = body[: _ORG_BLOCK_CHAR_CAP - 3]
        last_boundary = truncated.rfind("\n\n")
        body = (
            truncated[:last_boundary] + "\n\n…"
            if last_boundary > 0
            else truncated + "…"
        )
    return body


def render_org_block(mode: str = "team") -> str:
    """Return a Markdown block of all department states + People roster.

    Capped at 4000 chars total. Returns "" only when both departments and
    people are absent (fresh install / test env).

    ``mode="solo"`` renders the solo block instead (see the module docstring);
    any other value renders the team block, unchanged.

    Any unexpected error is logged and "" is returned — this function must
    never crash a chat turn.
    """
    try:
        from openexecutive.departments.registry import list_states
        from openexecutive.people.registry import list_people

        states = list_states()

        # Fetch people once — used both for the head-name lookup per
        # department and for the People section below.
        try:
            people = list_people()
        except Exception:
            people = []

        if mode == "solo":
            return _cap(_render_solo_block(states).strip())

        # Build a quick id→name map for head-person lookups.
        head_name_by_id: dict[int, str] = {
            p.id: p.full_name for p in people if p.id is not None
        }

        # Build departments section.
        dept_sections: list[str] = []
        if states:
            dept_sections = [_ORG_BLOCK_HEADER + "\n"]
            for state in states:
                head_name = (
                    head_name_by_id.get(state.config.head_person_id)
                    if state.config.head_person_id is not None
                    else None
                )
                dept_sections.append(_render_department_section(state, head_name=head_name))

        # Build people section — pass the already-fetched list to avoid a
        # second DB call.
        people_section = _render_people_section(people)

        if not dept_sections and not people_section:
            return ""

        parts: list[str] = []
        if dept_sections:
            parts.append("\n\n".join(dept_sections))
        if people_section:
            parts.append(people_section)

        return _cap("\n\n".join(parts).strip())

    except Exception:
        # Broad catch: this is a prompt-building path that must never crash a
        # chat turn. Real errors are logged with exc_info for diagnostics.
        logger.warning("render_org_block failed — org context omitted", exc_info=True)
        return ""
