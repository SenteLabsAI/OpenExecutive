"""Reviewer primitives for The Committee.

A Reviewer is a thin coordinator that holds a system prompt, calls the
Anthropic API once, and parses a JSON critique. It deliberately does NOT
extend BaseAgent — its input shape (user message + draft + specialist
outputs) and output shape (structured Critique JSON) differ enough that
reusing BaseAgent.analyze would force awkward overloads and pull in
RAG/episodic plumbing we do not want here.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from openexecutive.audit.usage import log_model_usage
from openexecutive.orchestrator.router import SPECIALIST_DESCRIPTIONS
from openexecutive.prompts.committee_prompts import DOMAIN_REVIEWER_SYSTEM_TEMPLATE
from openexecutive.providers import get_provider

logger = logging.getLogger(__name__)

_VALID_SEVERITIES = {"low", "medium", "high"}

# Cap per-specialist excerpt fed to a reviewer. ~250 tokens — enough for
# the reviewer to spot domain errors, small enough to stay cheap.
_SPECIALIST_EXCERPT_CHARS = 1500


def neutralize_committee_tags(text: str) -> str:
    """Defang the closing-tag forms the committee uses as delimiters.

    The reviewer and revision prompts wrap user-controlled fields (the
    user message, the draft, specialist outputs, critique fields) inside
    XML-style tags like ``<draft_response>...</draft_response>``. A
    malicious user could inject a closing tag inside the draft to escape
    the delimiter and then inject adversarial instructions to the model.

    Mitigation: insert a zero-width space inside any closing tag we use as
    a delimiter. The text reads identically to a human and to the LLM as
    natural language, but a literal ``</draft_response>`` match no longer
    appears, so the delimiter boundary is preserved.
    """
    if not text:
        return text
    for tag in (
        "</user_question>",
        "</draft_response>",
        "</specialist_outputs>",
        "</committee_review>",
    ):
        if tag in text:
            text = text.replace(tag, tag[:1] + "​" + tag[1:])
    return text


@dataclass
class Critique:
    reviewer_name: str
    severity: str
    critique: str
    suggested_edits: str

    def as_dict(self) -> dict[str, str]:
        return {
            "reviewer_name": self.reviewer_name,
            "severity": self.severity,
            "critique": self.critique,
            "suggested_edits": self.suggested_edits,
        }


@dataclass
class Reviewer:
    name: str
    system_prompt: str
    model: str

    async def critique(
        self,
        user_message: str,
        draft: str,
        specialist_outputs: dict[str, str] | None = None,
    ) -> Critique:
        """One reviewer pass. Never raises — on any failure returns a low-severity
        Critique so the committee never blocks the revision."""
        safe_user_message = neutralize_committee_tags(user_message)
        safe_draft = neutralize_committee_tags(draft)

        spec_block = ""
        if specialist_outputs:
            chunks = []
            for spec_name, out in specialist_outputs.items():
                if not out:
                    continue
                excerpt = neutralize_committee_tags(out[:_SPECIALIST_EXCERPT_CHARS])
                chunks.append(f"[{spec_name}]\n{excerpt}")
            if chunks:
                spec_block = (
                    "\n\n<specialist_outputs>\n"
                    + "\n\n".join(chunks)
                    + "\n</specialist_outputs>"
                )

        user_content = (
            f"<user_question>\n{safe_user_message}\n</user_question>\n\n"
            f"<draft_response>\n{safe_draft}\n</draft_response>"
            f"{spec_block}\n\n"
            "Critique the draft per your system instructions. Return JSON only."
        )

        try:
            msg = await get_provider(self.model).messages_create(
                model=self.model,
                max_tokens=1024,
                system=[
                    {
                        "type": "text",
                        "text": self.system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_content}],
            )
            log_model_usage(msg, model=self.model, actor="committee_reviewer")
            text_blocks = [b for b in msg.content if b.type == "text"]
            text = text_blocks[0].text if text_blocks else ""
        except Exception:
            logger.exception("Reviewer %s API call failed", self.name)
            return Critique(
                reviewer_name=self.name,
                severity="low",
                critique="(reviewer call failed)",
                suggested_edits="",
            )

        return self._parse(text)

    def _parse(self, text: str) -> Critique:
        """Parse reviewer JSON. Salvage by finding the outermost {...}.
        On any failure return a no-op low-severity critique."""
        try:
            start = text.find("{")
            end = text.rfind("}") + 1
            if start < 0 or end <= start:
                raise ValueError("no JSON object found")
            data = json.loads(text[start:end])
            severity = str(data.get("severity", "low")).strip().lower()
            if severity not in _VALID_SEVERITIES:
                severity = "low"
            return Critique(
                reviewer_name=self.name,
                severity=severity,
                critique=str(data.get("critique", "")).strip(),
                suggested_edits=str(data.get("suggested_edits", "")).strip(),
            )
        except Exception:
            logger.warning(
                "Reviewer %s produced unparseable output: %s", self.name, text[:200]
            )
            return Critique(
                reviewer_name=self.name,
                severity="low",
                critique="(reviewer output unparseable)",
                suggested_edits="",
            )


def build_quality_reviewer(model: str) -> Reviewer:
    """Build the quality reviewer with Agent Council overrides applied.

    Prompt, model, and deep-reasoning toggle all flow through the
    ``QualityJudgeAgent`` so operators can tune the judge from the
    Council without a code change. The ``model`` argument is retained
    for call-site stability but is effectively unused — the agent's
    ``effective_model()`` is authoritative (falls back to
    ``settings.default_model`` when no override is set).
    """
    from openexecutive.agents.quality_judge import QualityJudgeAgent

    agent = QualityJudgeAgent()
    return Reviewer(
        name="quality_judge",
        system_prompt=agent.effective_system_prompt(),
        model=agent.effective_model(),
    )


def build_domain_reviewer(specialist: str, model: str) -> Reviewer:
    """Build a domain-specific reviewer using the specialist's description as
    the domain blurb. Falls back to a generic blurb if the specialist key
    is unknown — should not happen, but defensive."""
    blurb = SPECIALIST_DESCRIPTIONS.get(
        specialist, "senior business executive with broad cross-functional expertise"
    )
    return Reviewer(
        name=f"{specialist}_domain",
        system_prompt=DOMAIN_REVIEWER_SYSTEM_TEMPLATE.format(domain_blurb=blurb),
        model=model,
    )
