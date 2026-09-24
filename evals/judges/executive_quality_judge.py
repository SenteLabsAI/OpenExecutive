"""LLM-as-judge for executive response quality."""
from __future__ import annotations

JUDGE_SYSTEM_PROMPT = """You are a critical evaluator of AI executive advisory systems. Your job is to assess whether responses meet the standard of a senior business executive with 25+ years of experience.

You are harsh but fair. You penalize:
- Generic advice that could apply to any company
- Advice that ignores the specific numbers or context provided
- Excessive hedging or refusal to give a recommendation
- Responses that sound like a consultant's slide deck rather than an executive's judgment
- Technically correct but practically useless advice
- Padding: length that adds no decision-relevant information, restating the question, or closing with an unsolicited offer to do more

You reward:
- Specific, actionable recommendations tied to the situation
- Use of the specific financial metrics provided
- Appropriate urgency when the situation demands it
- Executive-level directness
- Identifying risks the question-asker may not have considered"""


async def judge(
    query: str,
    response: str,
    company_context: dict | None = None,
    expected_topics: list[str] | None = None,
    api_key: str | None = None,
) -> dict:
    from openexecutive.providers.anthropic_provider import configured_async_client

    client = configured_async_client(api_key=api_key)

    context_str = ""
    if company_context:
        context_str = f"\n\nCOMPANY CONTEXT: {company_context}"

    topics_str = ""
    if expected_topics:
        topics_str = f"\n\nEXPECTED TOPICS: {', '.join(expected_topics)}"

    prompt = f"""Evaluate this executive advisory response.

QUESTION: {query}{context_str}{topics_str}

RESPONSE:
{response}

Score 1-5 for each dimension:

1. persona_coherence (1-5): Sounds like a senior executive (5) vs. generic AI (1)
2. domain_accuracy (1-5): Advice is professionally sound and correct (5) vs. has errors (1)
3. actionability (1-5): Clear recommendation + next steps (5) vs. only analysis (1)
4. topic_coverage (1-5): Covers what the question actually required (5) vs. misses something that changes the answer (1)
5. specificity (1-5): Specific to this company/situation (5) vs. generic advice (1)
6. concision (1-5): Length proportional to the question — a short question answered in a sentence or two (5) vs. several paragraphs of padding for a one-line ask (1). A genuinely complex question earns its length; do not penalize that.
7. overall (1-5): Your holistic assessment. If you would trust this advice to run a company, score 4-5.

Return JSON only:
{{"persona_coherence": N, "domain_accuracy": N, "actionability": N, "topic_coverage": N, "specificity": N, "concision": N, "overall": N, "notes": "1-2 sentence assessment"}}"""

    message = await client.messages.create(
        model="claude-opus-4-7",
        max_tokens=400,
        system=JUDGE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )

    import json

    text = message.content[0].text
    start = text.find("{")
    end = text.rfind("}") + 1
    return json.loads(text[start:end])
