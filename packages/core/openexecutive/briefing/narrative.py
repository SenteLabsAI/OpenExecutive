"""Shared Executive-voice briefing narrative.

One synthesizer (`synthesize_briefing_narrative`), two consumers with two
prompts:
  - the **on-page /today header** (`api/routes/today.py`) — a SYNTHESIS that
    does not re-list the cards rendered below it (per-viewer; served from
    `narrative_cache`, regenerated off the request hot path), and
  - the **standalone morning-brief DM** (`workflows/morning_brief.py`,
    `standalone=True`) — an enumerated brief, since the DM has no cards beside
    it.

(The EoD digest has its own separate prompt and does not route through here.)
Sharing the provider call + context rendering keeps both surfaces in the
Executive's voice while letting each use the right structure.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from openexecutive.alerts.lifecycle import parse_aware

logger = logging.getLogger(__name__)

# The quiet-day lines. Single-sourced here, in the module that owns the
# prompts, because three different code paths must emit text identical to what
# the model is told to emit on a quiet day: this module's prompts, `today`'s
# empty-board short-circuit (which skips the model entirely), and
# `morning_brief`'s empty fallback. They were five separate literals across
# three files, matching only by convention — a prompt reword would have
# silently desynced the short-circuit from the model's own wording.
# `test_briefing_narrative.py` asserts each prompt still carries its line.
QUIET_PRINCIPAL = "Quiet right now — nothing pressing."
QUIET_VIEWER = "Quiet right now — nothing needs you."

# Standalone morning-brief DM prompt. Unlike the /today header, this is
# delivered as a DM with NO cards beside it — so it MUST enumerate what needs
# the principal's attention (it's the only thing they see). Whole-company,
# principal audience. Used when `standalone=True`. (The EoD digest has its own
# separate prompt and does not route through here.)
STANDALONE_BRIEF_SYSTEM = (
    "You are the user's Executive. You are writing the daily brief — a short "
    "message the principal reads on its own (delivered as a DM; there is no "
    "other list beside it, so this message must stand alone). Write "
    "peer-to-peer, not as a corporate broadcast. The context is a DELTA since "
    "the last brief you sent, so never re-tell yesterday's news.\n\n"
    "Output ≤200 words of Markdown with these sections, in this order, each "
    "only included when there is real content for it:\n"
    "  1. **Top call** — the single decision you'd recommend the principal "
    "focus on today, with your suggested move. One or two sentences.\n"
    "  2. **What changed** — anything NEW since the last brief: a goal that "
    "flipped, a reply that landed, an external signal that moved. One bullet "
    "per item, terse.\n"
    "  3. **Handled overnight** — what you already completed on your own from "
    "the HANDLED block (routed, nudged, escalated, drafted, merged, closed "
    "with evidence). One bullet each, past tense, naming the person or item. "
    "Items under REWRITTEN are still open — they belong in 'What changed', "
    "never here.\n"
    "  4. **Needs you** — ONLY the items under NEW SINCE LAST BRIEF, most "
    "time-sensitive first, each with its why-now when given. If the context "
    "has a CARRIED OVER line, add exactly one sentence after the list "
    "('N older items still open — see /today'); never re-list carried items.\n"
    "  5. **Waiting on** — people whose reply you're still waiting for, with "
    "how long. One line each.\n"
    "  6. **At risk** — departments / goals trending off-track the principal "
    "hasn't already been briefed on.\n\n"
    "Skip headers entirely for sections with no content. If everything is "
    "genuinely quiet, output one line: '" + QUIET_PRINCIPAL + "'"
)


# Synthesis system prompt for the /today header. The actionable items render as
# cards BELOW this header, so the narrative must NOT re-list them — it adds the
# connective tissue a list can't: how items relate, what's most urgent and why,
# and the single recommended focus. It's the first thing the principal sees, so
# it's structured for scanning: bottom-line → read bullets → move.
BRIEFING_NARRATIVE_SYSTEM = (
    "You are the user's Executive. You are writing the 'What's going on' "
    "header the principal reads first — a brief, scannable SYNTHESIS of the "
    "company right now. The actionable items (proposals, in-flight work, "
    "at-risk departments) render as cards BELOW this header, so do NOT re-list "
    "them — synthesize.\n\n"
    "Output ≤120 words of Markdown in this shape:\n"
    "1. A bold one-line bottom-line opener — the single most important read, as "
    "ONE plain sentence anyone can grasp at a glance. Vary the actual wording "
    "day to day; you do NOT have to literally start with the words 'Bottom "
    "line' (e.g. '**Supply shock's live — your hedges are holding, but six "
    "teams are stuck on execution.**').\n"
    "2. 2–4 short bullets — the situational read, NOT a to-do list. ONE idea "
    "per bullet, written as a plain, complete sentence: name the thing, then "
    "say in plain words why it matters or what it's blocking. Bold the subject "
    "(e.g. '- **Hormuz closure risk** — about 38% of our crude still ships "
    "through the strait, so a closure hits our pricing before the "
    "diversification plan catches up.'). Do NOT stack multiple clauses into one "
    "bullet and do NOT use '→' shorthand — if a bullet carries two ideas, make "
    "it two bullets.\n"
    "3. A final line starting '**Move today:**' — the single action you'd "
    "recommend, in one plain sentence, and what stays secondary until it's "
    "cleared.\n\n"
    "VOICE: write peer-to-peer with energy and a clear point of view — like a "
    "sharp chief of staff talking to you, not a status report. Vary your "
    "phrasing and your opening from day to day so it never reads like a fixed "
    "daily template; a little personality is good. CLARITY comes first, "
    "though: a smart reader should get every line on the FIRST read — short "
    "sentences, plain words over jargon, and when a domain term is unavoidable "
    "state its consequence plainly. Reference specifics by name. If it's "
    "genuinely quiet, output one line: '" + QUIET_PRINCIPAL + "'"
)


def _viewer_system_prompt(name: str, role: str) -> str:
    """System prompt for a NON-principal teammate's personalized synthesis.

    The provided context is already scoped to this person; their actionable
    items render as cards below, so this is a short, scannable read of what
    matters for them — not a re-list.
    """
    return (
        f"You are {name}'s Executive. You are writing the 'What's going on' "
        f"header {name} ({role}) reads first — a brief, scannable SYNTHESIS of "
        "what's on their plate right now. Their actionable items render as "
        "cards BELOW this header, so do NOT re-list them — synthesize.\n\n"
        "Output ≤80 words of Markdown in this shape: (1) a bold one-line "
        "bottom-line for them, as one plain sentence (vary the wording day to "
        "day); (2) 1–3 short bullets, ONE idea each, written as a plain "
        "complete sentence — name it, then say in plain words why it matters "
        "(the read, not a to-do list; bold each bullet's subject; no '→' "
        "shorthand and no stacked clauses); (3) a final line starting "
        "'**Your move:**' with the single next step. VOICE: peer-to-peer with "
        "energy and a clear point of view, varied day to day — not a status "
        "report. But clarity first: they should get every line on the first "
        "read — short sentences, plain words over jargon. Address them directly "
        "('you'); the context is already scoped to them. If nothing is on their "
        "plate, output one line: '" + QUIET_VIEWER + "'"
    )


def _age_days(iso: str | None, now: datetime) -> int:
    dt = parse_aware(iso)
    return max(0, (now - dt).days) if dt is not None else 0


def render_briefing_context(
    *,
    period_label: str,
    today_data: dict[str, Any],
    activity: list[dict[str, Any]],
    since: datetime | None = None,
    handled: list[dict[str, Any]] | None = None,
    pending_watch_suggestions: int = 0,
) -> str:
    """Pack the structured /today + activity inputs into a single user-turn block.

    With ``since`` (the standalone briefs) proposals are split into NEW SINCE
    LAST BRIEF vs a one-line CARRIED OVER count, and ``handled`` (autonomous
    alert-review moves in the window) renders as its own block. With both
    unset the output is byte-identical to the legacy /today header context.
    """
    parts: list[str] = [f"PERIOD: {period_label}\n"]
    now = datetime.now(UTC)

    depts = today_data.get("departments", [])
    at_risk = [d for d in depts if d.get("at_risk_count", 0) or d.get("off_track_count", 0)]
    if at_risk:
        parts.append("DEPARTMENTS WITH RISK:")
        for d in at_risk:
            parts.append(
                f"- {d['title']}: at_risk={d.get('at_risk_count', 0)} "
                f"off_track={d.get('off_track_count', 0)} "
                f"awaiting={d.get('awaiting_count', 0)}"
            )
        parts.append("")

    proposals = today_data.get("proposals", [])
    if since is None:
        if proposals:
            parts.append("PROPOSALS AWAITING DECISION:")
            for p in proposals[:10]:
                parts.append(f"- {p.get('headline', '')[:160]}")
            parts.append("")
    else:
        from openexecutive.briefing.brief_state import rewritten_lines, split_proposals

        new_items, carried = split_proposals(proposals, since)
        if new_items:
            parts.append("NEEDS YOU — NEW SINCE LAST BRIEF:")
            for p in new_items[:10]:
                line = f"- {p.get('headline', '')[:160]}"
                if p.get("why_now"):
                    line += f" (why now: {str(p['why_now'])[:80]})"
                move = p.get("recommended_move")
                if move and move != "none":
                    line += f" [next move: {move}]"
                parts.append(line)
            parts.append("")
        if carried:
            oldest = max(_age_days(p.get("created_at"), now) for p in carried)
            stale = sum(1 for p in carried if p.get("review_verdict") == "likely_stale")
            line = f"CARRIED OVER: {len(carried)} older item(s) still open (oldest {oldest}d"
            if stale:
                line += f", {stale} flagged likely stale"
            parts.append(line + ") — see /today")
            parts.append("")
        rewritten = rewritten_lines(carried, since)
        if rewritten:
            parts.append(
                "REWRITTEN BY THE EXECUTIVE SINCE LAST BRIEF (still open — mention under "
                "\"What changed\", never as done):"
            )
            parts.extend(rewritten)
            parts.append("")
        if handled:
            parts.append("HANDLED OVERNIGHT BY THE EXECUTIVE (already done — report, don't ask):")
            for h in handled[:15]:
                parts.append(f"- [{str(h.get('at', ''))[:10]}] {h.get('kind', '')}: {str(h.get('summary', ''))[:140]}")
            parts.append("")
        if pending_watch_suggestions > 0:
            n = pending_watch_suggestions
            parts.append(
                f"WATCH SUGGESTIONS WAITING: {n} source{'s' if n != 1 else ''} the Executive "
                "would like to monitor but is not sure about — approve or decline on /watchlist "
                "(mention in one line, never list them)"
            )
            parts.append("")

    people = today_data.get("people", [])
    awaiting = [p for p in people if p.get("awaiting_count", 0)]
    if awaiting:
        parts.append("PEOPLE WAITING ON YOU:")
        for p in awaiting:
            parts.append(
                f"- {p.get('full_name', '')} ({p.get('role', '')}): "
                f"{p.get('awaiting_count', 0)} awaiting, "
                f"SLA {p.get('soonest_sla_at', 'unset')}"
            )
        parts.append("")

    if activity:
        # Only the standalone briefs pass `since`, and only they bound the
        # activity list to it — so only they may call it a delta. The /today
        # header gets whatever the rail holds, which can predate the last
        # brief entirely; labelling that "since last brief" made the header
        # report weeks-old rows as overnight news.
        if since is not None:
            parts.append("OE ACTIVITY SINCE LAST BRIEF (most recent first):")
        else:
            parts.append(
                "RECENT OE ACTIVITY (most recent first) — this is a history "
                "rail, NOT a delta: the principal may have seen these already, "
                "so never describe them as new or as having just happened:"
            )
        for item in activity[:15]:
            parts.append(
                f"- [{item.get('at', '')[:10]}] {item.get('kind', 'action')}: "
                f"{item.get('summary', '')[:140]}"
            )

    if len(parts) == 1:
        # Only the PERIOD line — genuinely quiet day.
        parts.append("(No org activity, proposals, or at-risk goals this period.)")

    return "\n".join(parts)


async def synthesize_briefing_narrative(
    *,
    today_data: dict[str, Any],
    activity: list[dict[str, Any]],
    period_label: str,
    viewer: dict[str, str] | None = None,
    standalone: bool = False,
    since: datetime | None = None,
    handled: list[dict[str, Any]] | None = None,
    pending_watch_suggestions: int = 0,
    rendered_context: str | None = None,
) -> str:
    """Synthesize the briefing narrative. Returns Markdown, or "" when empty.

    Prompt selection:
    ``rendered_context`` overrides the context render (see below).

      - ``standalone=True`` → the enumerated whole-company DM brief
        (morning_brief): a self-contained message with no cards beside it, so
        it lists what needs attention. (`viewer` is ignored.)
      - else ``viewer`` set → a non-principal teammate's scoped /today header
        synthesis (caller passes a `today_data` already scoped to them);
      - else → the whole-company /today header synthesis.

    The /today header variants are a SYNTHESIS (the actionable items render as
    cards below the header), so they do not re-list the queue. Raises on
    provider failure so the caller can decide how to surface it (the morning-
    brief workflow yields an error event; the page regen logs and moves on).
    """
    from openexecutive.agents.utility_fast import get_fast_model
    from openexecutive.providers import get_provider

    if standalone:
        system = STANDALONE_BRIEF_SYSTEM
    elif viewer:
        system = _viewer_system_prompt(viewer["name"], viewer["role"])
    else:
        system = BRIEFING_NARRATIVE_SYSTEM
    # `rendered_context` lets a caller hand in the exact block it already
    # rendered. The /today header path does, because it hashes that string as
    # its cache key — re-rendering here could quietly drift from what was
    # hashed and leave the cache keyed on something the model never saw.
    user_content = rendered_context if rendered_context is not None else render_briefing_context(
        period_label=period_label, today_data=today_data, activity=activity,
        since=since, handled=handled,
        pending_watch_suggestions=pending_watch_suggestions,
    )
    model = get_fast_model()
    response = await get_provider(model).messages_create(
        model=model,
        max_tokens=600,
        system=system,
        messages=[{"role": "user", "content": user_content}],
    )
    text_blocks = [b for b in response.content if getattr(b, "type", "") == "text"]
    return text_blocks[0].text.strip() if text_blocks else ""


__all__ = [
    "BRIEFING_NARRATIVE_SYSTEM",
    "QUIET_PRINCIPAL",
    "QUIET_VIEWER",
    "STANDALONE_BRIEF_SYSTEM",
    "render_briefing_context",
    "synthesize_briefing_narrative",
]
