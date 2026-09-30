from __future__ import annotations

import logging
import re
import unicodedata
from typing import TYPE_CHECKING, Any

from openexecutive.agents.base import BaseAgent
from openexecutive.alerts.models import (
    AlertChannel,
    AlertEvent,
    AlertSeverity,
    TriageDecision,
)
from openexecutive.audit.usage import log_model_usage
from openexecutive.providers import get_provider

if TYPE_CHECKING:
    import anthropic

logger = logging.getLogger(__name__)

_TRIAGE_TIMEOUT = 60.0
_MAX_EVENT_CHARS = 8000


TRIAGE_TOOL: dict[str, Any] = {
    "name": "emit_alert_decision",
    "description": (
        "Emit the triage decision for this inbound event. Always call this tool; "
        "never reply in plain text."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "alert": {
                "type": "boolean",
                "description": "True if the user should be alerted; false if the event is not worth surfacing.",
            },
            "severity": {
                "type": "string",
                "enum": ["low", "medium", "high", "urgent"],
            },
            "channels": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": [
                        "web", "slack_dm", "email", "persisted",
                        "department_channel", "company_broadcast",
                    ],
                },
                "description": (
                    "Which channels to deliver on. Always include 'persisted'. "
                    "Use 'department_channel' for dept-scoped alerts (sets "
                    "department_slug + broadcast_integration); 'company_broadcast' "
                    "for company-wide alerts (sets broadcast_integration). "
                    "Never use either for board/comp/legal — those are DM-only."
                ),
            },
            "headline": {
                "type": "string",
                "description": "<= 100 chars. The single most important fact.",
            },
            "body": {
                "type": "string",
                "description": "1-3 sentence summary with implication.",
            },
            "suggested_action": {
                "type": "string",
                "description": (
                    "The single concrete action the Executive will take on approval "
                    "(first-person imperative, <= 1 sentence). Empty string if none."
                ),
            },
            "topic_tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "1-4 lowercase topic tags.",
            },
            "dedup_key": {
                "type": "string",
                "description": "Stable summary key. Same underlying event must produce the same key.",
            },
            "reason_if_suppressed": {
                "type": "string",
                "description": "Reason when alert=false ('duplicate', 'muted: <pattern>', 'low_signal'). Empty otherwise.",
            },
            "department_slug": {
                "type": "string",
                "description": (
                    "Required when channels includes 'department_channel'. "
                    "The slug of the department whose team room to post to "
                    "(e.g. 'marketing', 'finance'). Empty otherwise."
                ),
            },
            "broadcast_integration": {
                "type": "string",
                "enum": ["slack", "discord", "telegram"],
                "description": (
                    "Required when channels includes 'department_channel' or "
                    "'company_broadcast'. The integration to broadcast on. "
                    "Omit the field entirely when no broadcast channel is "
                    "in `channels` — do NOT emit an empty string."
                ),
            },
        },
        "required": [
            "alert",
            "severity",
            "channels",
            "headline",
            "body",
            "dedup_key",
        ],
    },
}


_EVENT_CLOSE_RE = re.compile(r"<\s*/\s*event", re.IGNORECASE)


def _format_event_block(event: AlertEvent) -> str:
    body = (event.body or "")[:_MAX_EVENT_CHARS]
    parts = [f"source: {event.source}", f"external_id: {event.external_id}"]
    if event.subject:
        parts.append(f"subject: {event.subject}")
    if event.from_:
        parts.append(f"from: {event.from_}")
    if event.channel:
        parts.append(f"slack_channel: {event.channel}")
    if event.user:
        parts.append(f"slack_user: {event.user}")
    if event.title:
        parts.append(f"title: {event.title}")
    parts.append("---")
    parts.append(body)
    # Every field is sender-written (a mail body, a chat line, a watched
    # page's text): none may close the <event> block and speak outside it.
    # NFKC first, so a fullwidth "＜/event＞" is caught as the ASCII one.
    text = unicodedata.normalize("NFKC", "\n".join(parts))
    return _EVENT_CLOSE_RE.sub(r"<\\/event", text)


def _format_recent_alerts(recent: list[dict]) -> str:
    if not recent:
        return "(none)"
    lines = []
    for a in recent[:20]:
        lines.append(
            f"- [{a.get('severity', '?')}] {a.get('headline', '')}"
            f" | dedup_key={a.get('dedup_key', '')}"
            f" | tags={','.join(a.get('topic_tags') or [])}"
        )
    return "\n".join(lines)


def _format_mutes(patterns: list[str]) -> str:
    if not patterns:
        return "(none)"
    return "\n".join(f"- {p}" for p in patterns)


def _format_initiatives(initiatives: list[Any]) -> str:
    if not initiatives:
        return "(none)"
    lines = []
    for i in initiatives:
        title = getattr(i, "title", None) or (i.get("title") if isinstance(i, dict) else "")
        status = getattr(i, "status", None) or (i.get("status") if isinstance(i, dict) else "")
        summary = getattr(i, "summary", None) or (i.get("summary") if isinstance(i, dict) else "")
        lines.append(f"- {title} [{status}]: {summary}")
    return "\n".join(lines)


class TriageAgent(BaseAgent):
    name = "triage"
    domain = "triage"

    @property
    def model(self) -> str:  # type: ignore[override]
        # Cheap and fast. Triage runs on every inbound event. Uses the
        # configured routing model (like utility_fast) so an Anthropic-free
        # deployment routes triage to its local / OpenRouter model too,
        # rather than forcing a hardcoded Claude model.
        from openexecutive.config import get_settings

        return get_settings().routing_model

    def get_system_prompt(self) -> str:
        from openexecutive.prompts.triage_prompt import TRIAGE_PROMPT

        return TRIAGE_PROMPT

    def _build_user_content(
        self,
        event: AlertEvent,
        recent: list[dict],
        mute_patterns: list[str],
        initiatives: list[Any],
    ) -> str:
        return (
            f"<event>\n{_format_event_block(event)}\n</event>\n\n"
            f"<recent_alerts>\n{_format_recent_alerts(recent)}\n</recent_alerts>\n\n"
            f"<muted_topics>\n{_format_mutes(mute_patterns)}\n</muted_topics>\n\n"
            f"<active_initiatives>\n{_format_initiatives(initiatives)}\n</active_initiatives>"
        )

    async def triage(
        self,
        event: AlertEvent,
        *,
        recent_alerts: list[dict] | None = None,
        mute_patterns: list[str] | None = None,
        active_initiatives: list[Any] | None = None,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> TriageDecision:
        """Run triage and return a structured decision. Never raises — on
        failure, returns a low-severity persisted-only decision so the event
        still gets recorded."""
        # The optional ``client`` parameter is a legacy hook for callers that
        # need to inject a specific Anthropic SDK instance (evals/runner.py
        # and a few unit tests). When unset, we route through the provider
        # registry so a Council override on this agent's model takes effect.
        caller = client if client is not None else get_provider(self.effective_model())

        user_content = self._build_user_content(
            event,
            recent_alerts or [],
            mute_patterns or [],
            active_initiatives or [],
        )

        # Both code paths use the same Anthropic-shape kwargs; ``client``
        # exposes ``messages.create`` while ``provider`` exposes
        # ``messages_create`` — branch only on the call shape.
        create_kwargs: dict[str, Any] = {
            "model": self.effective_model(),
            "max_tokens": 512,
            "timeout": _TRIAGE_TIMEOUT,
            "system": self.effective_system_prompt(),
            "tools": [TRIAGE_TOOL],
            "tool_choice": {"type": "tool", "name": "emit_alert_decision"},
            "messages": [{"role": "user", "content": user_content}],
        }

        try:
            if client is not None:
                message = await caller.messages.create(**create_kwargs)  # type: ignore[union-attr]
            else:
                message = await caller.messages_create(**create_kwargs)  # type: ignore[union-attr]
        except Exception:
            logger.exception("Triage LLM call failed for event %s", event.external_id)
            return TriageDecision(
                alert=False,
                severity=AlertSeverity.LOW,
                channels=[AlertChannel.PERSISTED],
                headline=event.subject or event.title or event.source,
                body=(event.body or "")[:280],
                dedup_key=f"fallback-{event.source}-{event.external_id}",
                reason_if_suppressed="triage_error",
            )
        log_model_usage(message, model=self.effective_model(), actor="triage")

        for block in message.content:
            if block.type == "tool_use" and block.name == "emit_alert_decision":
                return _parse_decision(block.input)

        logger.warning(
            "Triage returned no tool_use for event %s — treating as low/persisted",
            event.external_id,
        )
        return TriageDecision(
            alert=False,
            severity=AlertSeverity.LOW,
            channels=[AlertChannel.PERSISTED],
            headline=event.subject or event.title or event.source,
            body=(event.body or "")[:280],
            dedup_key=f"no-tool-{event.source}-{event.external_id}",
            reason_if_suppressed="no_decision",
        )

def _parse_decision(raw: dict) -> TriageDecision:
    """Tolerantly coerce the tool input into a TriageDecision."""
    try:
        severity_str = (raw.get("severity") or "low").lower()
        severity = AlertSeverity(severity_str) if severity_str in AlertSeverity._value2member_map_ else AlertSeverity.LOW

        channels: list[AlertChannel] = []
        for c in raw.get("channels") or []:
            c_str = str(c).lower()
            if c_str in AlertChannel._value2member_map_:
                channels.append(AlertChannel(c_str))
        if AlertChannel.PERSISTED not in channels:
            channels.append(AlertChannel.PERSISTED)

        topic_tags = [str(t).lower() for t in (raw.get("topic_tags") or [])]

        # Shift 3: broadcast routing context. Coerce defensively — the
        # model may omit them entirely (default "") or emit a value
        # outside the integration enum (drop it silently in that case,
        # the dispatcher returns False on missing context and the
        # broadcast becomes a no-op rather than a crash).
        dept_slug = str(raw.get("department_slug") or "")[:64]
        bi_raw = str(raw.get("broadcast_integration") or "")
        broadcast_integration = (
            bi_raw if bi_raw in ("slack", "discord", "telegram") else ""
        )

        return TriageDecision(
            alert=bool(raw.get("alert", False)),
            severity=severity,
            channels=channels,
            headline=(raw.get("headline") or "")[:200],
            body=(raw.get("body") or "")[:2000],
            suggested_action=(raw.get("suggested_action") or "")[:500],
            topic_tags=topic_tags[:8],
            dedup_key=(raw.get("dedup_key") or "")[:120],
            reason_if_suppressed=(raw.get("reason_if_suppressed") or "")[:200],
            department_slug=dept_slug,
            broadcast_integration=broadcast_integration,
        )
    except Exception:
        logger.exception("Failed to parse triage decision: %r", raw)
        return TriageDecision(
            alert=False,
            severity=AlertSeverity.LOW,
            channels=[AlertChannel.PERSISTED],
            headline="(parse error)",
            body="",
            dedup_key="parse-error",
            reason_if_suppressed="parse_error",
        )
