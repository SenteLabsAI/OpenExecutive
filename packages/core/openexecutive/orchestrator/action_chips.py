"""Classify side-effecting Executive tool calls and produce inline action chips.

Used by the agent loop to emit `action_taken` SSE events whenever the
Executive fires a tool that takes a real-world action — sending a DM,
scheduling a follow-up, opening a workflow, mutating the people roster,
flagging an alert. The UI renders the resulting chips below the assistant's
prose so the user can see *what happened* without it being buried in
narrative.

Read-only tools (`consult_specialist`, `lookup_person`, `list_people`,
`search_skills`, `load_skill`, `search_tools`, `web_search`,
`ask_about_person`, `list_workflows`) deliberately produce no chip — only
actions visible outside the chat get surfaced.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


# Canonical set of tools whose successful invocation produces a user-visible
# real-world action. Anything not in this set is treated as read-only and
# skipped. When adding a new side-effecting tool elsewhere, add it here too —
# otherwise its execution won't surface a chip and the user loses the visible
# trace of what the Executive did.
SIDE_EFFECTING_TOOLS: frozenset[str] = frozenset({
    # Outbound channel sends
    "send_slack_dm",
    "send_discord_dm",
    "send_telegram_message",
    # Person-addressed send: resolves the channel server-side from person_id.
    "message_person",
    # Broadcast (Shift 3) — department-scoped and company-wide channels.
    "send_department_message",
    "send_company_broadcast",
    # Scheduling
    "schedule_followup",
    "suggest_workflow",
    # People roster mutations
    "upsert_person",
    "archive_person",
    "set_department_head",
    # Department goal mutations (Phase B — chat-driven progress updates)
    "update_department_goal",
    # Skills mutations
    "create_skill",
    "update_skill",
    "delete_skill",
    # Triage / alerts
    "create_alert",
    "ack_alert",
    # Universal workflow launcher (any built-in / custom workflow from chat)
    "run_workflow",
    # Research artifacts flagged for review
    "draft_artifact",
    # MCP — generic, classified by underlying tool name at runtime
    "call_tool",
    "load_mcp_server",
})


def _parse_result(tool_result: str) -> dict[str, Any] | None:
    """Best-effort JSON parse of a tool handler's string result.

    Handlers in this codebase return JSON strings — `'{"status": "sent",
    ...}'` on success, `'{"error": "..."}'` on failure. Return the parsed
    dict, or None when the result isn't a JSON object (skill handlers
    occasionally return raw text). Callers treat None as "no structured
    info" rather than "failure" — the action is still surfaced.
    """
    try:
        parsed = json.loads(tool_result)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _humanize_iso(iso_ts: str) -> str:
    """Render an ISO timestamp as a chip-friendly RELATIVE phrase.

    Relative output ("in 2h", "tomorrow", "on Mon") is timezone-free —
    the user already knows their own wall-clock context, and rendering
    "Mon 11pm" in UTC for someone in PT would silently mislead. Falls
    back to the raw ISO string if parsing fails — the chip is best-
    effort, never blocking.
    """
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return iso_ts
    if dt.tzinfo is None:
        # Treat naive timestamps as UTC — matches the convention used by
        # schedule_followup / suggest_workflow callers that normalize to UTC.
        dt = dt.replace(tzinfo=UTC)
    delta_s = (dt - datetime.now(UTC)).total_seconds()
    if delta_s < 0:
        return "just now"
    if delta_s < 60:
        return "in <1m"
    if delta_s < 3600:
        return f"in {int(delta_s // 60)}m"
    if delta_s < 86_400:
        return f"in {int(delta_s // 3600)}h"
    if delta_s < 172_800:
        return "tomorrow"
    if delta_s < 7 * 86_400:
        # Render the weekday — relative to "now" within a week.
        return f"on {dt.strftime('%a')}"
    return f"in {int(delta_s // 86_400)}d"


def summarize_action(
    *,
    tool_name: str,
    tool_input: dict[str, Any],
    tool_result: str,
    iteration: int | None = None,
) -> dict[str, Any] | None:
    """Build an `action_taken` event payload, or None if the call should be skipped.

    Returns None when:
      • `tool_name` is not in SIDE_EFFECTING_TOOLS
      • the tool's parsed result reports an error (so we don't claim
        "DM'd Sara" when Slack returned 4xx)
      • the result is a `not_found` (the target row didn't exist, so nothing
        changed)

    The returned dict is the SSE event body — chat.py wraps it with
    `data: ` framing. Keep summaries terse; the chip is sized for one line.
    """
    if tool_name not in SIDE_EFFECTING_TOOLS:
        return None

    parsed = _parse_result(tool_result)
    if parsed is not None and "error" in parsed:
        # Tool ran but reported a failure — don't claim the action happened.
        # The Executive's prose will explain what went wrong; we just don't
        # paint a green ✓ chip over a red outcome.
        return None
    if parsed is not None and parsed.get("status") == "not_found":
        # The target row didn't exist, so nothing changed (e.g. archive_person
        # for an unknown person). No state change → no ✓ chip.
        return None
    if parsed is not None and parsed.get("noop") is True:
        # Idempotent re-call (e.g. `ack_alert` after the briefing UI already
        # acked the alert via HTTP). No state changed — suppress the chip
        # so we don't paint "Approved proposal #N" over a non-event.
        return None

    payload: dict[str, Any] = {
        "type": "action_taken",
        "tool": tool_name,
        "summary": tool_name,  # fallback, refined per-tool below
        "target": None,
        "link": None,
    }
    if iteration is not None:
        payload["iteration"] = iteration

    if tool_name == "send_slack_dm":
        user_id = tool_input.get("user_id") or "a teammate"
        payload["summary"] = f"DM'd {user_id} on Slack"
        payload["target"] = user_id
    elif tool_name == "send_discord_dm":
        user_id = tool_input.get("discord_user_id") or "a teammate"
        payload["summary"] = f"DM'd {user_id} on Discord"
        payload["target"] = user_id
    elif tool_name == "send_telegram_message":
        chat_id = tool_input.get("chat_id")
        payload["summary"] = (
            f"Sent Telegram message to {chat_id}" if chat_id else "Sent Telegram message"
        )
        payload["target"] = str(chat_id) if chat_id is not None else None
    elif tool_name == "message_person":
        pid = tool_input.get("person_id")
        payload["summary"] = (
            f"Messaged person #{pid}" if pid is not None else "Messaged a person"
        )
        payload["target"] = str(pid) if pid is not None else None
        if isinstance(pid, int):
            payload["link"] = f"/people/{pid}"
    elif tool_name == "schedule_followup":
        when = _humanize_iso(str(tool_input.get("run_at", "")))
        channel = tool_input.get("channel", "")
        suffix = f" via {channel}" if channel else ""
        payload["summary"] = f"Scheduled follow-up for {when}{suffix}".strip()
        payload["target"] = str(tool_input.get("channel_ref", "") or "") or None
    elif tool_name == "suggest_workflow":
        wf = tool_input.get("workflow_name", "")
        when = _humanize_iso(str(tool_input.get("run_at", "")))
        payload["summary"] = f"Queued {wf} workflow suggestion for {when}".strip()
        payload["target"] = wf or None
    elif tool_name == "upsert_person":
        full_name = tool_input.get("full_name", "")
        # parsed result includes the person id when this was an insert/update.
        pid = (parsed or {}).get("id") if parsed else None
        payload["summary"] = f"Updated {full_name}" if full_name else "Updated a person"
        payload["target"] = full_name or None
        if isinstance(pid, int):
            payload["link"] = f"/people/{pid}"
    elif tool_name == "archive_person":
        pid = tool_input.get("person_id")
        payload["summary"] = f"Archived person #{pid}" if pid else "Archived a person"
        payload["target"] = str(pid) if pid else None
    elif tool_name == "set_department_head":
        slug = tool_input.get("department_slug", "")
        payload["summary"] = f"Set {slug} department head" if slug else "Set department head"
        payload["target"] = slug or None
        if slug:
            payload["link"] = f"/departments/{slug}"
    elif tool_name == "update_department_goal":
        slug = tool_input.get("department_slug", "")
        # The handler always returns `from_status`/`to_status` (when status
        # wasn't supplied, `to_status == from_status`). To tell a real
        # status transition from a current-only edit, compare the two
        # rather than just check `to_status` truthiness — otherwise every
        # current-only update would render as "→ on track" even though
        # nothing about the status actually moved.
        from_status = (parsed or {}).get("from_status")
        to_status = (parsed or {}).get("to_status")
        current_updated = bool((parsed or {}).get("current_updated"))
        status_changed = (
            from_status is not None
            and to_status is not None
            and from_status != to_status
        )
        if slug and status_changed:
            payload["summary"] = f"Updated {slug} goal → {str(to_status).replace('_', ' ')}"
        elif slug and current_updated:
            payload["summary"] = f"Updated {slug} goal progress"
        elif slug:
            # Status didn't change AND no current edit — defensive
            # fallback; the handler rejects this combo so this branch
            # should never fire in practice.
            payload["summary"] = f"Touched {slug} goal"
        else:
            payload["summary"] = "Updated a department goal"
        payload["target"] = slug or None
        if slug:
            payload["link"] = f"/departments/{slug}"
    elif tool_name == "create_skill":
        name = tool_input.get("name", "")
        payload["summary"] = f"Saved skill: {name}" if name else "Saved a skill"
        payload["target"] = name or None
    elif tool_name == "update_skill":
        name = tool_input.get("name", "")
        payload["summary"] = f"Updated skill: {name}" if name else "Updated a skill"
        payload["target"] = name or None
    elif tool_name == "delete_skill":
        name = tool_input.get("name", "")
        payload["summary"] = f"Deleted skill: {name}" if name else "Deleted a skill"
        payload["target"] = name or None
    elif tool_name == "create_alert":
        headline = (tool_input.get("headline") or "")[:60]
        payload["summary"] = f"Flagged alert: {headline}" if headline else "Flagged alert"
        payload["target"] = headline or None
    elif tool_name == "draft_artifact":
        title = (tool_input.get("title") or "")[:60]
        payload["summary"] = f"Drafted artifact for review: {title}" if title else "Drafted artifact for review"
        payload["target"] = title or None
    elif tool_name == "ack_alert":
        alert_id = tool_input.get("alert_id")
        status = tool_input.get("status", "ack")
        verb = "Approved" if status == "ack" else "Dismissed"
        payload["summary"] = (
            f"{verb} proposal #{alert_id}" if alert_id is not None else f"{verb} proposal"
        )
        payload["target"] = str(alert_id) if alert_id is not None else None
    elif tool_name == "call_tool":
        # MCP — the underlying tool name lives in tool_input["name"]. We
        # can't tell from here whether the underlying call was a read or a
        # write, so emit a generic chip with the tool name. Users will
        # naturally tolerate "Called google_workspace__send_gmail_message"
        # when that's what just happened.
        mcp_name = tool_input.get("name", "tool")
        payload["tool"] = mcp_name  # surface the real tool for UI mapping
        payload["summary"] = f"Called {mcp_name}"
        payload["target"] = mcp_name
    elif tool_name == "send_department_message":
        slug = tool_input.get("department_slug", "")
        integration = tool_input.get("integration", "")
        if slug and integration:
            payload["summary"] = f"Posted to {slug} on {integration.capitalize()}"
        elif slug:
            payload["summary"] = f"Posted to {slug}"
        else:
            payload["summary"] = "Posted to a department channel"
        payload["target"] = slug or None
        if slug:
            payload["link"] = f"/departments/{slug}"
    elif tool_name == "send_company_broadcast":
        integration = tool_input.get("integration", "")
        payload["summary"] = (
            f"Broadcast to company on {integration.capitalize()}"
            if integration else "Broadcast to company"
        )
        payload["target"] = integration or None
    elif tool_name == "load_mcp_server":
        url = tool_input.get("url", "")
        payload["summary"] = f"Connected MCP server: {url}" if url else "Connected MCP server"
        payload["target"] = url or None
    elif tool_name == "run_workflow":
        wf = str(tool_input.get("workflow", "")).replace("_", " ")
        run_id = (parsed or {}).get("run_id")
        awaiting = (parsed or {}).get("status") == "awaiting_human"
        if wf and awaiting:
            payload["summary"] = f"Started {wf} — awaiting sign-off"
        elif wf:
            payload["summary"] = f"Ran {wf} workflow"
        else:
            payload["summary"] = "Ran a workflow"
        payload["target"] = tool_input.get("workflow") or None
        if isinstance(run_id, str) and run_id:
            payload["link"] = f"/jobs/runs/{run_id}"
    else:
        # Tool is in SIDE_EFFECTING_TOOLS but we have no specific summarizer.
        # Keep the generic fallback so the chip still renders.
        logger.debug("summarize_action: no per-tool summary for %s", tool_name)

    # Trim summaries to chip-friendly length. Most are already ≤60; cap as
    # a safety net in case a tool input field is unexpectedly long.
    if isinstance(payload["summary"], str) and len(payload["summary"]) > 80:
        payload["summary"] = payload["summary"][:77] + "…"

    return payload
