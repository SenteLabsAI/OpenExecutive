from __future__ import annotations

import logging
from pathlib import Path

from openexecutive.alerts import sse_bus
from openexecutive.alerts.models import Alert, AlertChannel
from openexecutive.alerts.store import DB_PATH, update_delivery

logger = logging.getLogger(__name__)


def _alert_to_event(alert: Alert) -> dict:
    return {
        "id": alert.id,
        "external_id": alert.external_id,
        "source": alert.source,
        "severity": alert.severity,
        "headline": alert.headline,
        "body": alert.body,
        "suggested_action": alert.suggested_action,
        "topic_tags": alert.topic_tags,
        "status": alert.status,
        "created_at": alert.created_at,
    }


async def dispatch_web(alert: Alert) -> bool:
    try:
        sse_bus.publish({"type": "alert", "alert": _alert_to_event(alert)})
        return True
    except Exception:
        logger.exception("dispatch_web failed")
        return False


async def dispatch_persisted(alert: Alert) -> bool:
    # Already persisted by the pipeline before dispatch; this is a no-op marker.
    return True


async def dispatch_slack_dm(alert: Alert) -> bool:
    # Phase 2 — deferred. Currently a no-op; future implementation will reuse
    # the Bolt client from integrations.slack_bot.
    logger.info("dispatch_slack_dm: phase-2 stub, alert=%s", alert.id)
    return False


async def dispatch_email(alert: Alert) -> bool:
    from openexecutive.config import get_settings
    from openexecutive.orchestrator.mcp_gateway import get_active_gateway

    gateway = get_active_gateway()
    if gateway is None:
        logger.warning("dispatch_email: MCP gateway not available — skipping")
        return False

    settings = get_settings()
    try:
        subject = f"[{alert.severity.upper()}] {alert.headline}"
        body = f"{alert.body}\n\nSuggested action: {alert.suggested_action or 'None'}"
        await gateway.call_tool({
            "name": "google_workspace__send_gmail_message",
            "arguments": {
                "user_google_email": settings.exec_email_address,
                "to": settings.exec_email_address,
                "subject": subject,
                "body": body,
            },
        })
        return True
    except Exception:
        logger.exception("dispatch_email failed for alert=%s", alert.id)
        return False


# Tags that MUST NOT broadcast — even if the triage prompt would have
# routed them to a department channel or company broadcast. Mirrors the
# privacy invariant codified in the Executive persona's "Choosing Who
# to Tell" section. Defence-in-depth: the prompt is the primary
# enforcement, this is the backstop against a single model misroute
# leaking comp / board / legal context to the entire team.
#
# Exact match (not substring) — a substring check overmatched the
# documented tag vocabulary in round-1 review (e.g. "comp" matching
# "competitor", "legal" matching "legalese"). The set lists every
# spelling triage might emit; new sensitive vocabulary must be added
# here explicitly.
_PRIVACY_SENSITIVE_TAGS: frozenset[str] = frozenset({
    # models.PRIVATE_ALERT_TAG: an alert only the principal may see. The
    # pipeline already keeps such an alert off every broadcast channel; this
    # is the backstop.
    "private:principal",
    "board",
    "comp",
    "compensation",
    "compensation-refresh",
    "comp-refresh",
    "legal",
    "litigation",
    "layoffs",
    "severance",
    "valuation",
    "term-sheet",
    "termsheet",
    "term_sheet",
    "revenue",
})


def _violates_privacy_invariant(alert: Alert) -> bool:
    """True when any of the alert's topic_tags is in the sensitive set.

    Uses exact (case-insensitive) match on each tag rather than
    substring — a substring check would block legitimate broadcasts on
    "competitor" / "compliance" / "legalese" / etc.
    """
    if not alert.topic_tags:
        return False
    return any(tag.lower() in _PRIVACY_SENSITIVE_TAGS for tag in alert.topic_tags)


def _format_broadcast_body(alert: Alert) -> str:
    """Render an alert as a short broadcast-friendly plain-text body.

    Plain text — no `*foo*` markdown. Slack treats `*foo*` as bold but
    Discord renders it as italic; Telegram MarkdownV2 needs escaping.
    Cross-platform plain text is the safest cut.
    """
    parts = [alert.headline, "", alert.body]
    if alert.suggested_action:
        parts.append("")
        parts.append(f"Suggested action: {alert.suggested_action}")
    return "\n".join(parts).strip()


async def dispatch_department_channel(
    alert: Alert, *, department_slug: str, integration: str
) -> bool:
    """Post the alert to a department's configured team room.

    Uses the existing `send_department_message` handler so the routing
    + channel-lookup + error-shape logic lives in one place. Returns
    False when the department has no channel configured for the
    requested integration; the triage decision should have fallen
    back to another channel type at that point.
    """
    if not department_slug or not integration:
        logger.warning(
            "dispatch_department_channel: missing routing context "
            "(alert=%s slug=%r integration=%r)",
            alert.id, department_slug, integration,
        )
        return False
    if _violates_privacy_invariant(alert):
        # Triage prompt is supposed to prevent this; defence-in-depth
        # backstop. Log loudly so a misroute is visible in the audit.
        logger.error(
            "privacy invariant breach: refusing department broadcast for "
            "alert=%s tags=%s",
            alert.id, alert.topic_tags,
        )
        return False
    import json as _json

    # Lazy import so test code can `monkeypatch.setattr(bt, "handle_send_…", …)`
    # against the broadcast_tools module attribute and have the dispatcher
    # pick it up. A module-level import would freeze the binding at import
    # time and silently bypass the monkeypatch.
    from openexecutive.orchestrator.broadcast_tools import (
        handle_send_department_message,
    )
    try:
        result = await handle_send_department_message({
            "department_slug": department_slug,
            "integration": integration,
            "text": _format_broadcast_body(alert),
        })
        parsed = _json.loads(result)
    except Exception:
        logger.exception(
            "dispatch_department_channel failed for alert=%s slug=%s",
            alert.id, department_slug,
        )
        return False
    return isinstance(parsed, dict) and "error" not in parsed


async def dispatch_company_broadcast(
    alert: Alert, *, integration: str
) -> bool:
    """Post the alert to the configured company-wide channel.

    Returns False when the env-level default channel for the
    integration is unset — the triage decision should have fallen
    back to /today-only in that case.
    """
    if not integration:
        logger.warning(
            "dispatch_company_broadcast: missing integration (alert=%s)", alert.id,
        )
        return False
    if _violates_privacy_invariant(alert):
        logger.error(
            "privacy invariant breach: refusing company broadcast for "
            "alert=%s tags=%s",
            alert.id, alert.topic_tags,
        )
        return False
    import json as _json

    # Lazy import so test code can `monkeypatch.setattr(bt, "handle_send_…", …)`
    # against the broadcast_tools module attribute and have the dispatcher
    # pick it up. A module-level import would freeze the binding at import
    # time and silently bypass the monkeypatch.
    from openexecutive.orchestrator.broadcast_tools import (
        handle_send_company_broadcast,
    )
    try:
        result = await handle_send_company_broadcast({
            "integration": integration,
            "text": _format_broadcast_body(alert),
        })
        parsed = _json.loads(result)
    except Exception:
        logger.exception("dispatch_company_broadcast failed for alert=%s", alert.id)
        return False
    return isinstance(parsed, dict) and "error" not in parsed


# Per-channel single-arg dispatchers — used for channels that need no
# extra routing context (web, persisted, email, the slack_dm stub).
# The two broadcast channels are dispatched separately in dispatch_all
# because they need the department_slug / broadcast_integration kwargs.
_SIMPLE_DISPATCHERS = {
    AlertChannel.WEB.value: dispatch_web,
    AlertChannel.PERSISTED.value: dispatch_persisted,
    AlertChannel.SLACK_DM.value: dispatch_slack_dm,
    AlertChannel.EMAIL.value: dispatch_email,
}


async def dispatch_all(
    alert: Alert,
    channels: list[AlertChannel],
    db_path: Path = DB_PATH,
    *,
    department_slug: str = "",
    broadcast_integration: str = "",
) -> tuple[list[str], list[str]]:
    """Fire all per-channel dispatchers. Returns (attempted, delivered).

    The two Shift 3 broadcast channels (DEPARTMENT_CHANNEL,
    COMPANY_BROADCAST) need extra routing context — `department_slug`
    names which dept room to post to, `broadcast_integration` names
    the integration ("slack" / "discord" / "telegram"). The triage
    agent emits these on the TriageDecision; the alerts pipeline
    threads them through.
    """
    attempted: list[str] = []
    delivered: list[str] = []
    for ch in channels:
        attempted.append(ch.value)
        ok = False
        try:
            if ch == AlertChannel.DEPARTMENT_CHANNEL:
                ok = await dispatch_department_channel(
                    alert,
                    department_slug=department_slug,
                    integration=broadcast_integration,
                )
            elif ch == AlertChannel.COMPANY_BROADCAST:
                ok = await dispatch_company_broadcast(
                    alert, integration=broadcast_integration,
                )
            else:
                fn = _SIMPLE_DISPATCHERS.get(ch.value)
                if fn is None:
                    logger.warning("dispatch_all: unknown channel %s", ch)
                    continue
                ok = await fn(alert)
        except Exception:
            logger.exception("dispatcher %s raised", ch.value)
            ok = False
        if ok:
            delivered.append(ch.value)
    if alert.id is not None:
        update_delivery(alert.id, attempted, delivered, db_path=db_path)
    return attempted, delivered
