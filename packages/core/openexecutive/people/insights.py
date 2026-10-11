"""Per-person brief insight notes, generated via the utility-fast model.

A note is one terse sentence an executive reads in the daily brief next to a
team member — "who owes a reply, an SLA risk, on leave, or available". It is
synthesised from the structured signals the /today builder already computes,
optionally enriched with a low-reasoning Honcho relationship snippet.

Generation is the slow part, so it never runs on the hot /today request: the
route serves notes from `insights_cache` and regenerates stale ones in the
background. `build_insight_input_hash` keys that cache off the structured
signals (timestamps bucketed to the hour to avoid per-request churn) plus a
daily bucket, so the Honcho-derived nuance refreshes at least once a day.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from openexecutive.people.models import Person

logger = logging.getLogger(__name__)

# Static system prompt — MUST stay free of per-person data so the utility-fast
# prompt cache keeps hitting (see CLAUDE.md "Prompt Caching"). All dynamic
# content goes in the user turn.
_INSIGHT_SYSTEM = (
    "You write one terse note (8-15 words) for an executive's daily brief "
    "about a single team member, from their status signals and any "
    "relationship context. State the single most actionable or relevant fact "
    "— who owes a reply, an SLA risk, that they're on leave, their "
    "availability, or a notable concern or preference. No greeting, no name, "
    "no quotes, no trailing punctuation, no leading label."
)

_HONCHO_QUESTION = (
    "In one short sentence, what is the single most relevant thing to know "
    "about working with this person right now — a current concern, preference, "
    "or commitment?"
)
_HONCHO_TIMEOUT_S = 4.0
_MAX_NOTE_CHARS = 140


def _bucket_hour(iso: str | None) -> str | None:
    """Truncate an ISO timestamp to hour precision ("YYYY-MM-DDTHH").

    Keeps the cache key stable across sub-hour request jitter so a person's
    note is not regenerated every few seconds as clocks tick.
    """
    if not iso:
        return None
    return iso[:13]


def build_insight_input_hash(signals: dict[str, Any]) -> str:
    """Deterministic SHA-256 over the structured signals that should, when
    changed, invalidate a cached note. Timestamps are hour-bucketed and a
    daily bucket forces at-least-daily refresh of Honcho-derived nuance.
    """
    material = {
        "role": signals.get("role") or "",
        "is_principal": bool(signals.get("is_principal")),
        "status": signals.get("status"),
        "awaiting_count": signals.get("awaiting_count", 0),
        "awaiting_reply_count": signals.get("awaiting_reply_count", 0),
        "overdue": bool(signals.get("overdue")),
        "on_leave_until": signals.get("on_leave_until"),
        "reachable_now": bool(signals.get("reachable_now")),
        "authority_scope": sorted(signals.get("authority_scope") or []),
        "department_slugs": sorted(signals.get("department_slugs") or []),
        "soonest_sla_at": _bucket_hour(signals.get("soonest_sla_at")),
        "oldest_awaiting_reply_at": _bucket_hour(signals.get("oldest_awaiting_reply_at")),
        "next_window_at": _bucket_hour(signals.get("next_window_at")),
        "last_contact_at": _bucket_hour(signals.get("last_contact_at")),
        "day": datetime.now(UTC).date().isoformat(),
    }
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _render_signals(signals: dict[str, Any]) -> str:
    lines: list[str] = [f"Role: {signals.get('role') or 'unknown'}"]
    if signals.get("is_principal"):
        lines.append("This is the principal (founder).")
    lines.append(f"Status: {signals.get('status')}")

    awaiting = signals.get("awaiting_count", 0)
    if awaiting:
        line = f"Workflow checkpoints awaiting their action: {awaiting}"
        if signals.get("soonest_sla_at"):
            line += f" (soonest SLA {signals['soonest_sla_at']})"
        lines.append(line)

    replies = signals.get("awaiting_reply_count", 0)
    if replies:
        line = f"Open commitments awaiting their reply: {replies}"
        if signals.get("oldest_awaiting_reply_at"):
            line += f" (oldest since {signals['oldest_awaiting_reply_at']})"
        lines.append(line)

    if signals.get("overdue"):
        lines.append("At least one item is overdue.")

    if signals.get("on_leave_until"):
        lines.append(f"On leave until {signals['on_leave_until']}.")
    elif signals.get("reachable_now"):
        lines.append("Reachable right now.")
    elif signals.get("next_window_at"):
        lines.append(f"Not reachable now; next available {signals['next_window_at']}.")
    else:
        lines.append("Not reachable right now.")

    scope = signals.get("authority_scope") or []
    if scope:
        lines.append("Can approve: " + ", ".join(scope))

    if signals.get("last_contact_at"):
        lines.append(f"Last outbound contact: {signals['last_contact_at']}.")

    return "\n".join(lines)


async def _honcho_snippet(person: Person) -> str:
    """Low-reasoning Honcho relationship snippet, or "" when unavailable.

    Never raises — Honcho being off, slow, or erroring degrades to a
    structured-only note rather than dropping the insight entirely.
    """
    try:
        from openexecutive.config import get_settings

        if person.id is None or not get_settings().honcho_enabled:
            return ""
        from openexecutive.memory.honcho_client import directional_chat

        return await asyncio.wait_for(
            directional_chat(person.id, _HONCHO_QUESTION, reasoning_level="minimal"),
            timeout=_HONCHO_TIMEOUT_S,
        )
    except Exception:
        logger.info("person insight: honcho snippet failed for person_id=%s", person.id)
        return ""


async def generate_person_insight(
    person: Person,
    signals: dict[str, Any],
) -> str | None:
    """Generate a one-line insight note for `person`, or None on failure.

    Caller is responsible for caching the result. Returns None (rather than a
    placeholder) on any failure so the caller keeps whatever it already had.
    """
    try:
        from openexecutive.agents.utility_fast import get_fast_model
        from openexecutive.audit.usage import log_model_usage
        from openexecutive.providers import get_provider

        honcho = (await _honcho_snippet(person)).strip()
        content = "TEAM MEMBER SIGNALS:\n" + _render_signals(signals)
        if honcho:
            content += f"\n\nRELATIONSHIP CONTEXT (from memory):\n{honcho[:600]}"
        content += "\n\nNote:"

        model = get_fast_model()
        response = await get_provider(model).messages_create(
            model=model,
            max_tokens=48,
            system=_INSIGHT_SYSTEM,
            messages=[{"role": "user", "content": content}],
        )
        log_model_usage(response, model=model, actor="person_insight")
        raw = "".join(
            getattr(b, "text", "") for b in response.content
            if getattr(b, "type", "") == "text"
        ).strip()
        if not raw:
            return None
        cleaned = raw.strip().strip('"').strip("'").rstrip(".!?,:;").strip()
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        cleaned = "".join(c for c in cleaned if c.isprintable())
        if not cleaned:
            return None
        return cleaned[:_MAX_NOTE_CHARS]
    except Exception:
        logger.exception("generate_person_insight: failed for person_id=%s", person.id)
        return None
