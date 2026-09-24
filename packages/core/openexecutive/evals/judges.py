"""LLM-as-judge functions for the eval runner."""
from __future__ import annotations

import json
from typing import Any

from openexecutive.providers import get_provider

# The judge routes through the provider abstraction (like every other LLM call),
# so when OPENROUTER_ENABLED is on, judging bills the OpenRouter account too.
_JUDGE_MODEL = "claude-opus-4-7"


def _peer_memory_section(scenario: dict[str, Any]) -> str:
    """Judge context for scenarios that inject a ``<peer_memory>`` block.

    Only rendered when the scenario carries ``peer_memory_context`` — every
    other chat judge prompt stays byte-identical. The block is what the
    assistant was shown about the asker, so the judge needs it to score
    "did not re-ask" and "let the current message override an older note";
    the scenario's true ``quality_criteria`` are listed in the same case.
    """
    memory = scenario.get("peer_memory_context")
    if not memory:
        return ""
    # Scenario keys are snake_case identifiers; the judge reads prose.
    criteria = [k.replace("_", " ") for k, v in (scenario.get("quality_criteria") or {}).items() if v]
    section = (
        "\nBACKGROUND THE ASSISTANT WAS GIVEN ABOUT THE ASKER "
        "(older notes; the question itself is more recent):\n"
        f"{str(memory).strip()}\n"
    )
    if criteria:
        section += (
            f"\nAdditional criteria this response must satisfy: {', '.join(criteria)}. "
            "Check each one. If any is not satisfied, overall must be 2 or lower and "
            "notes must name the criterion that failed.\n"
        )
    return section


async def judge_chat(
    scenario: dict[str, Any],
    response: str,
) -> dict[str, Any]:
    judge_prompt = f"""You are an evaluator for an AI executive advisory system.

Evaluate this response to an executive question on a 1-5 scale for each dimension.

QUESTION: {scenario['query']}

RESPONSE: {response}

Expected topics to cover: {', '.join(scenario.get('expected_topics', []))}
{_peer_memory_section(scenario)}
Rate each dimension (1=poor, 3=acceptable, 5=excellent):
1. persona_coherence: Does it sound like a senior executive, not a generic AI?
2. domain_accuracy: Is the advice factually correct and professionally sound?
3. actionability: Does it give concrete next steps with clear recommendations?
4. topic_coverage: Does it address the expected topics?
5. specificity: Is it specific to the situation, not generic advice?

Respond in JSON format:
{{"persona_coherence": N, "domain_accuracy": N, "actionability": N, "topic_coverage": N, "specificity": N, "overall": N, "notes": "brief explanation"}}"""

    message = await get_provider(_JUDGE_MODEL).messages_create(
        model=_JUDGE_MODEL,
        max_tokens=500,
        messages=[{"role": "user", "content": judge_prompt}],
    )
    text = message.content[0].text  # type: ignore[union-attr]
    try:
        start = text.find("{")
        end = text.rfind("}") + 1
        return json.loads(text[start:end])
    except Exception:
        return {"overall": 0, "notes": "Failed to parse judge response"}


async def judge_workflow(
    scenario: dict[str, Any],
    artifact: str,
) -> dict[str, Any]:
    expected_sections = scenario.get("expected_artifact_sections", []) or []
    quality_criteria = scenario.get("quality_criteria", {}) or {}

    judge_prompt = f"""You are evaluating an artifact produced by an AI executive workflow.

WORKFLOW: {scenario.get('workflow')}
DESCRIPTION: {scenario['description']}

ARTIFACT (Markdown):
{artifact}

Expected sections (should appear as headings or clear sections):
{', '.join(expected_sections) or 'n/a'}

Quality criteria the artifact should satisfy:
{json.dumps(quality_criteria, indent=2)}

Rate each dimension 1-5 (1=poor, 3=acceptable, 5=excellent):
1. structure: All expected sections present and well-organized.
2. specificity: Uses concrete facts from the inputs; no fabricated numbers.
3. actionability: Recommendations and decisions are clear and concrete.
4. coherence: Reads as a unified document, not stitched fragments.
5. completeness: Each section is substantive, not a stub.

Respond in JSON:
{{"structure": N, "specificity": N, "actionability": N, "coherence": N, "completeness": N, "overall": N, "notes": "brief"}}"""

    message = await get_provider(_JUDGE_MODEL).messages_create(
        model=_JUDGE_MODEL,
        max_tokens=500,
        messages=[{"role": "user", "content": judge_prompt}],
    )
    text = message.content[0].text  # type: ignore[union-attr]
    try:
        start = text.find("{")
        end = text.rfind("}") + 1
        return json.loads(text[start:end])
    except Exception:
        return {"overall": 0, "notes": "Failed to parse judge response"}


async def judge_triage(
    scenario: dict[str, Any],
    decision: dict[str, Any],
) -> dict[str, Any]:
    def _fmt_decision(d: dict[str, Any]) -> str:
        return (
            f"alert: {d.get('alert')}\n"
            f"severity: {d.get('severity')}\n"
            f"channels: {d.get('channels')}\n"
            f"headline: {d.get('headline')}\n"
            f"body: {d.get('body')}\n"
            f"suggested_action: {d.get('suggested_action')}\n"
            f"topic_tags: {d.get('topic_tags')}\n"
            f"dedup_key: {d.get('dedup_key')}\n"
            f"reason_if_suppressed: {d.get('reason_if_suppressed')}\n"
        )

    def _fmt_event(ev: dict[str, Any]) -> str:
        parts = [f"source: {ev.get('source')}"]
        if ev.get("subject"):
            parts.append(f"subject: {ev['subject']}")
        if ev.get("from"):
            parts.append(f"from: {ev['from']}")
        parts.append("body:")
        parts.append((ev.get("body") or "")[:4000])
        return "\n".join(parts)

    def _fmt_expected(ex: dict[str, Any]) -> str:
        lines = []
        for key in ("alert", "severity"):
            if key in ex:
                lines.append(f"{key}: {ex[key]}")
        for key in (
            "channels_must_include",
            "channels_must_exclude",
            "topic_tags_should_include",
            "reason_if_suppressed_must_contain",
        ):
            if ex.get(key):
                lines.append(f"{key}: {ex[key]}")
        return "\n".join(lines) or "(none specified)"

    judge_prompt = f"""You are an evaluator for a Triage agent that decides whether incoming
events should fire proactive alerts to a busy CEO.

Score the agent's decision on three dimensions (1-5):
1. severity_accuracy: Is the chosen severity right? "urgent" only for genuine <24h risk.
2. channel_appropriateness: Do channels match severity? Always include "persisted".
3. dedup_correctness: Was a duplicate correctly suppressed / a new event correctly NOT suppressed?

EVENT:
{_fmt_event(scenario.get('event', {}))}

CONTEXT:
{json.dumps(scenario.get('context', {}), indent=2)}

EXPECTED:
{_fmt_expected(scenario.get('expected_decision', {}))}

ACTUAL DECISION:
{_fmt_decision(decision)}

Respond in JSON:
{{"severity_accuracy": N, "channel_appropriateness": N, "dedup_correctness": N, "overall": N, "notes": "brief"}}"""

    message = await get_provider(_JUDGE_MODEL).messages_create(
        model=_JUDGE_MODEL,
        max_tokens=500,
        messages=[{"role": "user", "content": judge_prompt}],
    )
    text = message.content[0].text  # type: ignore[union-attr]
    try:
        start = text.find("{")
        end = text.rfind("}") + 1
        return json.loads(text[start:end])
    except Exception:
        return {"overall": 0, "notes": "Failed to parse triage judge response"}
