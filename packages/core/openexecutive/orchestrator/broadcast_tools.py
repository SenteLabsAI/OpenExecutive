"""Department-scoped and company-wide broadcast tools for the Executive.

Two tools live here:

* ``send_department_message`` — post to a department's configured team
  room on the named integration (slack / discord / telegram). Falls back
  to an error string the model can read and route around when the
  department has no channel configured.
* ``send_company_broadcast`` — post to the integration's
  company-default room. Same error shape on the "not configured" path.

These broadcast tools differ from `send_slack_dm` / `send_discord_dm` /
`send_telegram_message` (in schedule_tools.py) in two ways:

1. They target rooms / channels, not specific people. The People-roster
   gate that protects DMs doesn't apply — channels are team-visible
   surfaces, not private inboxes for individuals who may not have
   consented to being messaged.

2. They are the structural expression of the "Choosing Who to Tell"
   judgment codified in `executive_persona.py` — OE picks among DM /
   dept channel / company broadcast by *which tool it calls*, with
   the persona's section telling it when each is appropriate.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)


# --- Privacy backstop (defence-in-depth for "Choosing Who to Tell") ----------
# The Executive persona is the PRIMARY control over what may be broadcast. This
# is a deterministic backstop on the free-text broadcast tools: it refuses a
# department/company broadcast whose body mentions a board / compensation /
# legal-class matter, which the privacy invariant says must be DM'd to the
# scope-holder, never posted to a channel. Mirrors the alert dispatcher's
# `_violates_privacy_invariant` (which gates on topic_tags); this works on free
# text, so it is curated for PRECISION (few false positives blocking legitimate
# broadcasts) over recall — and word-boundary matched so "board" doesn't trip on
# "onboarding" or "comp" on "company" (the substring overmatch the dispatcher's
# set explicitly warns about). High-frequency-but-legit words (revenue, legal,
# comp) are deliberately excluded. Add new sensitive vocabulary here.
_PRIVACY_SENSITIVE_TERMS: frozenset[str] = frozenset({
    "layoff",
    "rif",
    "down round",
    "severance",
    "valuation",
    "compensation",
    "litigation",
    "lawsuit",
    "termsheet",
    "term sheet",
    "cap table",
    "equity grant",
    "board deck",
    "board meeting",
    "board vote",
})

# Each term is matched with an optional trailing plural "s" (so "lawsuit" also
# catches "lawsuits", "board meeting" -> "board meetings"); the inner
# non-capturing group keeps the `s?` applying to every alternative while the
# outer group still captures the whole hit. Word-boundary anchored so "rif"
# can't trip on "rifle" and "down round" can't trip on "markdown round".
_SENSITIVE_RE = re.compile(
    r"\b((?:" + "|".join(re.escape(t) for t in sorted(_PRIVACY_SENSITIVE_TERMS)) + r")s?)\b",
    re.IGNORECASE,
)


def _privacy_backstop_violation(text: str) -> str | None:
    """Return the matched sensitive term if a broadcast body looks like a
    board / comp / legal matter that must be DM'd to the scope-holder rather
    than broadcast, else None. Defence-in-depth backstop, not the primary gate.
    """
    match = _SENSITIVE_RE.search(text)
    return match.group(1).lower() if match else None


def _privacy_refusal(term: str) -> str:
    """The JSON error a broadcast tool returns when the backstop fires —
    phrased so the model routes to a per-person DM instead."""
    return json.dumps({
        "error": (
            f"Refused for privacy: the message mentions '{term}', a "
            "board / compensation / legal-class matter that must be sent "
            "privately to the scope-holder via a DM, not broadcast to a "
            "channel. Use a per-person DM (send_slack_dm / send_discord_dm / "
            "send_telegram_message) to the right owner instead."
        )
    })


SEND_DEPARTMENT_MESSAGE_TOOL: dict[str, Any] = {
    "name": "send_department_message",
    "description": (
        "Post a message to a department's configured team room (Slack / "
        "Discord / Telegram). Use when the matter is department-scoped "
        "and has no single owner — a Goal flipping at-risk, a cadence "
        "summary, departmental coordination. Falls back with a clear "
        '"no <integration> channel configured" error if the department '
        "has not set up that integration; the model is expected to read "
        "that error and try another route (e.g. DM the department head)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "department_slug": {
                "type": "string",
                "description": "The department's slug (e.g. 'marketing').",
            },
            "integration": {
                "type": "string",
                "enum": ["slack", "discord", "telegram"],
                "description": "Which integration's channel to post to.",
            },
            "text": {
                "type": "string",
                "description": "Message body.",
            },
        },
        "required": ["department_slug", "integration", "text"],
    },
}


SEND_COMPANY_BROADCAST_TOOL: dict[str, Any] = {
    "name": "send_company_broadcast",
    "description": (
        "Post a message to the company-wide default room on Slack / "
        "Discord / Telegram. Use sparingly — only for things that "
        "genuinely belong in front of the whole team and have no clear "
        "single owner (e.g. 'Q3 plan shipped', cross-cutting status). "
        "For decisions involving comp, board, or legal scopes, prefer "
        "DMing the scope-holder instead — broadcasts are visible to "
        "everyone. Errors with 'company default <integration> channel "
        "not configured' when the env-level default channel is unset; "
        "fall back to surfacing the item in /today only."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "integration": {
                "type": "string",
                "enum": ["slack", "discord", "telegram"],
                "description": "Which integration's default room to broadcast on.",
            },
            "text": {
                "type": "string",
                "description": "Message body.",
            },
        },
        "required": ["integration", "text"],
    },
}


BROADCAST_TOOLS: list[dict[str, Any]] = [
    SEND_DEPARTMENT_MESSAGE_TOOL,
    SEND_COMPANY_BROADCAST_TOOL,
]


_MAX_TEXT_CHARS = 4000


def _validate_text(raw: Any) -> tuple[str | None, str | None]:
    """Coerce + validate the text field. Returns (clean_text, err_msg)."""
    if not isinstance(raw, str):
        return None, "text must be a string"
    text = raw.strip()
    if not text:
        return None, "text must not be empty"
    if len(text) > _MAX_TEXT_CHARS:
        return None, f"text must be {_MAX_TEXT_CHARS} characters or fewer"
    return text, None


async def _post_slack_channel(channel_id: str, text: str) -> dict[str, Any]:
    """Post to a Slack channel by ID. Returns a JSON-friendly dict."""
    from openexecutive.config import get_settings

    settings = get_settings()
    if not settings.slack_bot_token:
        return {"error": "slack is not configured"}
    try:
        from slack_sdk.web.async_client import AsyncWebClient
    except ImportError:
        return {"error": "slack_sdk is not installed"}
    client = AsyncWebClient(token=settings.slack_bot_token)
    try:
        result = await client.chat_postMessage(channel=channel_id, text=text)
    except Exception as exc:
        logger.exception("broadcast_tools: slack post failed")
        return {"error": f"slack post failed: {exc}"}
    if not result.get("ok"):
        return {"error": f"slack returned not-ok: {result.get('error', 'unknown')}"}
    return {"status": "sent", "channel": channel_id}


async def _post_discord_channel(channel_id: str, text: str) -> dict[str, Any]:
    """Post to a Discord channel by ID via the existing bot."""
    from openexecutive.config import get_settings
    from openexecutive.integrations.discord_bot import send_channel_message

    settings = get_settings()
    if not settings.discord_bot_token:
        return {"error": "discord is not configured"}
    try:
        await send_channel_message(channel_id, text)
    except Exception as exc:
        logger.exception("broadcast_tools: discord post failed")
        return {"error": f"discord post failed: {exc}"}
    return {"status": "sent", "channel": channel_id}


async def _post_telegram_chat(chat_id: str, text: str) -> dict[str, Any]:
    """Post to a Telegram group / channel by chat_id (numeric as string)."""
    from openexecutive.config import get_settings
    from openexecutive.integrations.telegram_bot import send_message

    settings = get_settings()
    if not settings.telegram_bot_token:
        return {"error": "telegram is not configured"}
    try:
        chat_id_int = int(chat_id)
    except (TypeError, ValueError):
        return {"error": f"telegram chat_id must be a numeric string, got {chat_id!r}"}
    try:
        await send_message(settings.telegram_bot_token, chat_id_int, text)
    except Exception as exc:
        logger.exception("broadcast_tools: telegram post failed")
        return {"error": f"telegram post failed: {exc}"}
    return {"status": "sent", "channel": str(chat_id_int)}


async def _dispatch(
    integration: str, channel_id: str, text: str
) -> dict[str, Any]:
    if integration == "slack":
        return await _post_slack_channel(channel_id, text)
    if integration == "discord":
        return await _post_discord_channel(channel_id, text)
    if integration == "telegram":
        return await _post_telegram_chat(channel_id, text)
    return {"error": f"unknown integration {integration!r}"}


def _audit(
    *,
    tool: str,
    ok: bool,
    integration: str,
    target: str,
    text_len: int,
    reason: str | None = None,
) -> None:
    """Best-effort audit row. Swallows failures so a logging blip never
    breaks the tool's return path."""
    from openexecutive.audit import log_event as audit_log

    summary = f"{tool} {'ok' if ok else 'FAILED'} integration={integration} target={target!r}"
    if reason:
        summary += f" ({reason})"
    try:
        audit_log(
            "tool_invocation",
            summary,
            actor="executive",
            details={
                "tool": tool,
                "kind": "broadcast",
                "ok": ok,
                "integration": integration,
                "target": target,
                "text_len": text_len,
                **({"reason": reason} if reason else {}),
            },
        )
    except Exception:
        logger.exception("broadcast_tools: audit_log failed")


async def handle_send_department_message(tool_input: dict[str, Any]) -> str:
    from openexecutive.delegation.lockdown import outside_reach_refusal

    if (refused := outside_reach_refusal('send_department_message')) is not None:
        return refused

    from openexecutive.departments.store import get_department

    try:
        slug = str(tool_input["department_slug"]).strip()
        integration = str(tool_input["integration"]).strip()
        text_raw = tool_input.get("text")
    except (KeyError, TypeError) as exc:
        return json.dumps({"error": f"missing field: {exc}"})

    text, err = _validate_text(text_raw)
    if err is not None or text is None:
        return json.dumps({"error": err})

    if integration not in ("slack", "discord", "telegram"):
        return json.dumps({"error": f"unknown integration {integration!r}"})

    sensitive = _privacy_backstop_violation(text)
    if sensitive is not None:
        _audit(tool="send_department_message", ok=False, integration=integration,
               target=slug, text_len=len(text),
               reason=f"privacy invariant: matched {sensitive!r}")
        return _privacy_refusal(sensitive)

    dept = get_department(slug)
    if dept is None:
        _audit(tool="send_department_message", ok=False, integration=integration,
               target=slug, text_len=len(text), reason="unknown department")
        return json.dumps({"error": f"unknown department {slug!r}"})

    cfg = dept.config
    channel_id: str | None = {
        "slack": cfg.slack_channel_id,
        "discord": cfg.discord_channel_id,
        "telegram": cfg.telegram_chat_id,
    }[integration]
    if not channel_id:
        _audit(tool="send_department_message", ok=False, integration=integration,
               target=slug, text_len=len(text), reason="no channel configured")
        return json.dumps({
            "error": (
                f"department {slug!r} has no {integration} channel configured "
                f"(set one in /departments/{slug} or try another integration)"
            )
        })

    result = await _dispatch(integration, channel_id, text)
    ok = "status" in result and result["status"] == "sent"
    _audit(tool="send_department_message", ok=ok, integration=integration,
           target=slug, text_len=len(text),
           reason=None if ok else str(result.get("error", ""))[:120])
    return json.dumps(result)


async def handle_send_company_broadcast(tool_input: dict[str, Any]) -> str:
    from openexecutive.delegation.lockdown import outside_reach_refusal

    if (refused := outside_reach_refusal('send_company_broadcast')) is not None:
        return refused

    from openexecutive.config import get_settings

    try:
        integration = str(tool_input["integration"]).strip()
        text_raw = tool_input.get("text")
    except (KeyError, TypeError) as exc:
        return json.dumps({"error": f"missing field: {exc}"})

    text, err = _validate_text(text_raw)
    if err is not None or text is None:
        return json.dumps({"error": err})

    if integration not in ("slack", "discord", "telegram"):
        return json.dumps({"error": f"unknown integration {integration!r}"})

    sensitive = _privacy_backstop_violation(text)
    if sensitive is not None:
        _audit(tool="send_company_broadcast", ok=False, integration=integration,
               target="<unset>", text_len=len(text),
               reason=f"privacy invariant: matched {sensitive!r}")
        return _privacy_refusal(sensitive)

    settings = get_settings()
    channel_id: str | None = {
        "slack": settings.slack_default_channel_id,
        "discord": settings.discord_default_channel_id,
        "telegram": settings.telegram_default_chat_id,
    }[integration]
    if not channel_id:
        _audit(tool="send_company_broadcast", ok=False, integration=integration,
               target="<unset>", text_len=len(text), reason="no default channel")
        return json.dumps({
            "error": (
                f"company default {integration} channel not configured "
                f"(set {integration.upper()}_DEFAULT_"
                f"{'CHANNEL_ID' if integration != 'telegram' else 'CHAT_ID'} "
                f"or use a department channel)"
            )
        })

    result = await _dispatch(integration, channel_id, text)
    ok = "status" in result and result["status"] == "sent"
    _audit(tool="send_company_broadcast", ok=ok, integration=integration,
           target=channel_id, text_len=len(text),
           reason=None if ok else str(result.get("error", ""))[:120])
    return json.dumps(result)


async def post_department_notice(
    department_slug: str, text: str, *, tool: str = "department_notice"
) -> dict[str, Any]:
    """Post a notice to a department's FIRST configured channel.

    A public, internal-caller entry point (e.g. onboarding kickoff) that applies
    the SAME controls as ``send_department_message`` — the length cap, the
    privacy backstop, and the broadcast audit trail — so internal broadcasts
    can't bypass the guardrails that protect the team-visible channels. Unlike
    the chat tool it doesn't take an integration: it tries slack → discord →
    telegram and uses the first one configured.

    Returns a status dict: ``sent`` (with integration/channel), ``refused``
    (privacy backstop), ``skipped`` (no department / no channel), or ``error``
    (bad text). Never raises.
    """
    from openexecutive.departments.store import get_department

    clean, err = _validate_text(text)
    if err is not None or clean is None:
        return {"status": "error", "error": err}

    sensitive = _privacy_backstop_violation(clean)
    if sensitive is not None:
        _audit(tool=tool, ok=False, integration="auto", target=department_slug,
               text_len=len(clean), reason=f"privacy invariant: matched {sensitive!r}")
        return {"status": "refused", "term": sensitive}

    dept = get_department(department_slug)
    if dept is None:
        return {"status": "skipped", "reason": "unknown department"}

    cfg = dept.config
    for integration, channel_id in (
        ("slack", cfg.slack_channel_id),
        ("discord", cfg.discord_channel_id),
        ("telegram", cfg.telegram_chat_id),
    ):
        if not channel_id:
            continue
        result = await _dispatch(integration, channel_id, clean)
        ok = result.get("status") == "sent"
        _audit(tool=tool, ok=ok, integration=integration, target=department_slug,
               text_len=len(clean), reason=None if ok else str(result.get("error", ""))[:120])
        if ok:
            return {"status": "sent", "integration": integration, "channel": channel_id}
    return {"status": "skipped", "reason": "no channel configured"}


BROADCAST_TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[str]]] = {
    "send_department_message": handle_send_department_message,
    "send_company_broadcast": handle_send_company_broadcast,
}
