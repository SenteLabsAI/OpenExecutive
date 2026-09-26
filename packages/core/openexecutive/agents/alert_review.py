"""Alert review agent: verdict + recommended move per open alert.

Structured-output utility agent (same shape as :mod:`agents.triage`): one
forced tool call, never raises. It never mutates state — the policy code in
:mod:`alerts.review` reads the verdicts and executes moves within authority.
"""
from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from openexecutive.agents.base import BaseAgent
from openexecutive.audit.usage import log_model_usage
from openexecutive.providers import get_provider

logger = logging.getLogger(__name__)

_REVIEW_TIMEOUT = 90.0
_MAX_TOKENS = 4000

VERDICTS: frozenset[str] = frozenset({"relevant", "changed", "resolved", "stale"})
CONFIDENCES: frozenset[str] = frozenset({"high", "medium", "low"})
MOVES: frozenset[str] = frozenset({
    "none", "route", "nudge", "escalate", "draft", "suggest_workflow", "merge", "close",
})
SEVERITIES: frozenset[str] = frozenset({"low", "medium", "high", "urgent"})


class AlertVerdict(BaseModel):
    """One reviewed alert, as the model returned it (coerced, bounded)."""

    model_config = ConfigDict(extra="ignore")

    alert_id: int
    verdict: str = "relevant"
    confidence: str = "low"
    evidence: str = ""
    # The server-supplied evidence id (S1 / R2 / A3) a closing verdict cites.
    evidence_ref: str = ""
    note: str = ""
    why_now: str = ""
    due_at: str | None = None
    recommended_move: str = "none"
    target_person_id: int | None = None
    superseded_by_alert_id: int | None = None
    headline: str | None = None
    body: str | None = None
    severity: str | None = None
    draft_title: str = ""
    draft_document: str = ""
    workflow_name: str = ""
    message: str = ""
    raw: dict[str, Any] = Field(default_factory=dict, exclude=True)


REVIEW_TOOL: dict[str, Any] = {
    "name": "emit_alert_reviews",
    "description": (
        "Emit the review for every alert in the batch. Always call this tool; "
        "never reply in plain text."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "reviews": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "alert_id": {"type": "integer"},
                        "verdict": {
                            "type": "string",
                            "enum": sorted(VERDICTS),
                        },
                        "confidence": {"type": "string", "enum": sorted(CONFIDENCES)},
                        "evidence": {
                            "type": "string",
                            "description": (
                                "The specific evidence item you rely on (quote it). "
                                "Required for resolved / stale."
                            ),
                        },
                        "evidence_ref": {
                            "type": "string",
                            "description": (
                                "The bracketed id of the evidence item you rely on, exactly as "
                                "shown in the alert block (e.g. 'S1', 'R2', 'A3'). REQUIRED for "
                                "resolved / stale — a close without a valid ref is not applied."
                            ),
                        },
                        "note": {
                            "type": "string",
                            "description": "<=160 chars: what changed since the principal last looked.",
                        },
                        "why_now": {"type": "string", "description": "<=80 chars, only under time pressure."},
                        "due_at": {"type": "string", "description": "ISO 8601 UTC deadline, if one exists."},
                        "recommended_move": {"type": "string", "enum": sorted(MOVES)},
                        "target_person_id": {"type": "integer"},
                        "superseded_by_alert_id": {"type": "integer"},
                        "headline": {"type": "string"},
                        "body": {"type": "string"},
                        "severity": {"type": "string", "enum": sorted(SEVERITIES)},
                        "draft_title": {"type": "string"},
                        "draft_document": {"type": "string"},
                        "workflow_name": {"type": "string"},
                        "message": {"type": "string", "description": "The DM text for route / nudge."},
                    },
                    "required": ["alert_id", "verdict", "confidence", "note", "recommended_move"],
                },
            },
        },
        "required": ["reviews"],
    },
}


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def parse_verdicts(raw: Any) -> list[AlertVerdict]:
    """Tolerantly coerce the tool input into verdicts; drops malformed entries."""
    out: list[AlertVerdict] = []
    entries = raw.get("reviews") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        return out
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        alert_id = _coerce_int(entry.get("alert_id"))
        if alert_id is None:
            continue
        verdict = str(entry.get("verdict") or "relevant").lower()
        confidence = str(entry.get("confidence") or "low").lower()
        move = str(entry.get("recommended_move") or "none").lower()
        severity_raw = entry.get("severity")
        severity = str(severity_raw).lower() if severity_raw else None
        out.append(AlertVerdict(
            alert_id=alert_id,
            verdict=verdict if verdict in VERDICTS else "relevant",
            confidence=confidence if confidence in CONFIDENCES else "low",
            evidence=str(entry.get("evidence") or "")[:600],
            evidence_ref=str(entry.get("evidence_ref") or "")[:8],
            note=str(entry.get("note") or "")[:160],
            why_now=str(entry.get("why_now") or "")[:80],
            due_at=(str(entry.get("due_at")) if entry.get("due_at") else None),
            recommended_move=move if move in MOVES else "none",
            target_person_id=_coerce_int(entry.get("target_person_id")),
            superseded_by_alert_id=_coerce_int(entry.get("superseded_by_alert_id")),
            headline=(str(entry["headline"])[:200] if entry.get("headline") else None),
            body=(str(entry["body"])[:4000] if entry.get("body") else None),
            severity=severity if severity in SEVERITIES else None,
            draft_title=str(entry.get("draft_title") or "")[:160],
            draft_document=str(entry.get("draft_document") or "")[:8000],
            workflow_name=str(entry.get("workflow_name") or "")[:64],
            message=str(entry.get("message") or "")[:1500],
            raw=entry,
        ))
    return out


class AlertReviewAgent(BaseAgent):
    name = "alert_review"
    domain = "alert_review"

    @property
    def model(self) -> str:  # type: ignore[override]
        # Cheap and fast, like triage — this runs on every open alert a few
        # times a day. Uses the configured routing model so an Anthropic-free
        # deployment routes it to its local / OpenRouter model too.
        from openexecutive.config import get_settings

        return get_settings().routing_model

    def get_system_prompt(self) -> str:
        from openexecutive.prompts.alert_review_prompt import ALERT_REVIEW_PROMPT

        return ALERT_REVIEW_PROMPT

    async def review(self, batch_context: str) -> list[AlertVerdict]:
        """Return verdicts for one batch; empty on any failure (never raises)."""
        provider = get_provider(self.effective_model())
        try:
            message = await provider.messages_create(
                model=self.effective_model(),
                max_tokens=_MAX_TOKENS,
                timeout=_REVIEW_TIMEOUT,
                system=self.effective_system_prompt(),
                tools=[REVIEW_TOOL],
                tool_choice={"type": "tool", "name": "emit_alert_reviews"},
                messages=[{"role": "user", "content": batch_context}],
            )
        except Exception:
            logger.exception("alert_review: model call failed — no verdicts this batch")
            return []
        log_model_usage(message, model=self.effective_model(), actor="alert_review")
        for block in getattr(message, "content", []) or []:
            if getattr(block, "type", "") == "tool_use" and getattr(block, "name", "") == "emit_alert_reviews":
                return parse_verdicts(getattr(block, "input", {}))
        logger.warning("alert_review: model returned no tool_use — no verdicts this batch")
        return []


__all__ = [
    "MOVES",
    "REVIEW_TOOL",
    "VERDICTS",
    "AlertReviewAgent",
    "AlertVerdict",
    "parse_verdicts",
]
