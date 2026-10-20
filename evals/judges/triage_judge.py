"""LLM-as-judge for Triage agent decisions.

Scores three dimensions on a 1-5 scale: severity_accuracy,
channel_appropriateness, dedup_correctness. Returns the same JSON shape as
the executive_quality_judge so run_evals can accumulate results uniformly.
"""
from __future__ import annotations

import json
from typing import Any


def _format_decision(decision: dict[str, Any]) -> str:
    return (
        f"alert: {decision.get('alert')}\n"
        f"severity: {decision.get('severity')}\n"
        f"channels: {decision.get('channels')}\n"
        f"headline: {decision.get('headline')}\n"
        f"body: {decision.get('body')}\n"
        f"suggested_action: {decision.get('suggested_action')}\n"
        f"topic_tags: {decision.get('topic_tags')}\n"
        f"dedup_key: {decision.get('dedup_key')}\n"
        f"reason_if_suppressed: {decision.get('reason_if_suppressed')}\n"
    )


def _format_event(event: dict[str, Any]) -> str:
    parts = [f"source: {event.get('source')}"]
    if event.get("subject"):
        parts.append(f"subject: {event['subject']}")
    if event.get("from"):
        parts.append(f"from: {event['from']}")
    parts.append("body:")
    parts.append((event.get("body") or "")[:4000])
    return "\n".join(parts)


def _format_expected(expected: dict[str, Any]) -> str:
    lines = []
    if "alert" in expected:
        lines.append(f"alert: {expected['alert']}")
    if "severity" in expected:
        lines.append(f"severity: {expected['severity']}")
    if expected.get("channels_must_include"):
        lines.append(f"channels_must_include: {expected['channels_must_include']}")
    if expected.get("channels_must_exclude"):
        lines.append(f"channels_must_exclude: {expected['channels_must_exclude']}")
    if expected.get("topic_tags_should_include"):
        lines.append(f"topic_tags_should_include: {expected['topic_tags_should_include']}")
    if expected.get("reason_if_suppressed_must_contain"):
        lines.append(
            f"reason_if_suppressed_must_contain: {expected['reason_if_suppressed_must_contain']}"
        )
    return "\n".join(lines) or "(none specified)"


async def judge_triage(
    scenario: dict[str, Any],
    decision: dict[str, Any],
) -> dict[str, Any]:
    from openexecutive.evals.judges import judge_message

    judge_prompt = f"""You are an evaluator for a Triage agent that decides whether incoming
events (emails, Slack messages, documents) should fire proactive alerts to a
busy CEO.

Score the agent's decision on three dimensions on a 1-5 scale (1=poor,
3=acceptable, 5=excellent):

1. severity_accuracy: Is the chosen severity right for the event? Is "urgent"
   reserved for genuine <24h actionable risk, "low" for noise?
2. channel_appropriateness: Do the chosen delivery channels match the
   severity? Always include "persisted". Loud channels (slack_dm, email) only
   for high/urgent or things truly worth interrupting for.
3. dedup_correctness: If the event duplicates something in recent_alerts, was
   it correctly suppressed? If it is genuinely new, was it correctly NOT
   suppressed? Stable dedup_key?

EVENT:
{_format_event(scenario.get('event', {}))}

CONTEXT (recent_alerts, muted_topics, active_initiatives):
{json.dumps(scenario.get('context', {}), indent=2)}

EXPECTED:
{_format_expected(scenario.get('expected_decision', {}))}

ACTUAL DECISION:
{_format_decision(decision)}

Respond in JSON:
{{"severity_accuracy": N, "channel_appropriateness": N, "dedup_correctness": N, "overall": N, "notes": "brief explanation"}}
"""

    text = await judge_message(judge_prompt)
    try:
        start = text.find("{")
        end = text.rfind("}") + 1
        return json.loads(text[start:end])
    except Exception:
        return {"overall": 0, "notes": "Failed to parse triage judge response"}
