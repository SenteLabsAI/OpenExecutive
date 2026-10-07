"""Anthropic tool definitions + handlers for managing the external-monitor watchlist.

These tools let the Executive add, list, remove, and tune watchlist
entries directly from a chat turn — so the principal can say "watch
Apple's stock and ping me if it drops more than 5% today" or "stop
watching that competitor's blog, too noisy" and have the right row
land in / leave the SQLite table.

Sit alongside `create_alert`, `upsert_person`, and the schedule/send
tools in the Executive's main tool loop. Pattern matches
``orchestrator/people_tools.py`` — JSON in / JSON out, audit on every
call, no broken-tool fallbacks.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from openexecutive.alerts.models import AlertSeverity
from openexecutive.audit import log_event as audit_log
from openexecutive.monitoring import store as ms
from openexecutive.monitoring.models import (
    DECLINE_KIND_EXPLICIT,
    DECLINE_REASON_NOT_RELEVANT,
    EXPLICIT_DECLINE_REASONS,
    MODE_ACTIVE,
    ORIGIN_EXECUTIVE,
    ORIGIN_RESEARCH_PROPOSED,
    RESEARCH_ORIGINS,
    is_valid_cadence,
    is_valid_mode,
)
from openexecutive.monitoring.sources import list_registered_kinds
from openexecutive.monitoring.sources._http import validate_target_url
from openexecutive.monitoring.target_validation import (
    WatchlistTargetError,
    validate_and_normalize_target,
)
from openexecutive.monitoring.validation import (
    VALID_SEVERITY_VALUES as _VALID_SEVERITY_VALUES,
)
from openexecutive.monitoring.validation import (
    WATCHLIST_SLUG_RE as _SLUG_RE,
)
from openexecutive.monitoring.validation import normalize_target

logger = logging.getLogger(__name__)


# Cap returned list sizes so the Executive doesn't drown in a long
# watchlist when it only wants a quick check.
_LIST_DEFAULT_LIMIT = 50

# Signal types whose target is a URL (validated at insert time).
_URL_SIGNAL_TYPES: frozenset[str] = frozenset({"rss", "vendor_status", "page_watch"})


# --------------------------------------------------------------------- #
# Tool specs
# --------------------------------------------------------------------- #


ADD_WATCHLIST_ENTRY_TOOL: dict[str, Any] = {
    "name": "add_watchlist_entry",
    "description": (
        "Add an external-condition monitor entry. The Executive watches the "
        "configured source on a periodic tick and surfaces qualifying changes "
        "as briefing proposals (via the existing alert pipeline). Use when "
        "the principal asks the Executive to watch a stock ticker, a vendor "
        "status page, an RSS / Atom feed, a competitor's changelog, or a "
        "standing web-search query (e.g. 'Acme Corp layoffs OR funding') for "
        "developments that no single feed publishes."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "slug": {
                "type": "string",
                "description": (
                    "Stable user-visible identifier, kebab-case "
                    "(e.g. 'stock-aapl', 'vendor-aws', 'rss-acme-changelog'). "
                    "Used in nudges + audit. Must be unique."
                ),
            },
            "signal_type": {
                "type": "string",
                "enum": [
                    "vendor_status", "rss", "stock", "query", "edgar",
                    "page_watch",
                ],
                "description": (
                    "Which adapter handles this entry. vendor_status polls "
                    "Statuspage atom/RSS feeds; rss polls a generic Atom/RSS "
                    "URL; stock polls Yahoo's chart endpoint for a ticker; "
                    "query runs a standing natural-language web search on a "
                    "cadence (billed — uses web search) and surfaces new "
                    "matching results; edgar polls SEC EDGAR for a public "
                    "company's new filings (8-K / 10-K / 10-Q / Form 4) by "
                    "ticker or CIK; page_watch detects content changes on an "
                    "arbitrary web page that has no feed (e.g. a pricing or "
                    "careers page)."
                ),
            },
            "target": {
                "type": "string",
                "description": (
                    "For vendor_status / rss / page_watch: the full page/feed "
                    "URL. For stock: the ticker symbol (e.g. 'AAPL'). "
                    "For query: the natural-language search to watch "
                    "(e.g. 'Acme Corp layoffs OR restructuring OR funding'). "
                    "For edgar: a ticker (e.g. 'AAPL') or CIK (e.g. '320193'). "
                    "URLs must be public http/https — the SSRF guard "
                    "rejects file://, private IPs, link-local, etc."
                ),
            },
            "display_label": {
                "type": "string",
                "description": (
                    "Optional human label (e.g. 'Apple', 'AWS', 'Acme "
                    "Changelog'). Used as a prefix in the normalized "
                    "summary; defaults to a host-derived label when missing."
                ),
            },
            "trigger": {
                "type": "object",
                "description": (
                    "Optional adapter-specific filter. For stock: "
                    "{'abs_change_pct_gte': 5} fires when |today vs prev "
                    "close| ≥ 5%. For rss / vendor_status / query: "
                    "{'keywords': ['pricing', 'launch']} fires only on "
                    "matching entries. For edgar: {'forms': ['8-K', '10-K']} "
                    "limits to those filing types (default 8-K/10-K/10-Q). "
                    "Pass {} for the per-adapter default."
                ),
            },
            "cadence": {
                "type": "string",
                "enum": ["real_time", "15min", "hourly", "daily", "weekly"],
                "description": (
                    "Desired surfacing cadence label. Stored as metadata; "
                    "PR-C uses this to bucket signals into morning brief vs "
                    "real-time DM. Defaults to '15min'."
                ),
            },
            "severity_floor": {
                "type": "string",
                "enum": ["low", "medium", "high", "urgent"],
                "description": (
                    "Suppress signals BELOW this severity. The adapter "
                    "computes a per-signal severity_hint; signals below the "
                    "floor are recorded in audit but never promoted to an "
                    "alert. Defaults to 'low' (everything surfaces)."
                ),
            },
            "severity_ceiling": {
                "type": "string",
                "enum": ["low", "medium", "high", "urgent"],
                "description": (
                    "Cap signal severity at this level. Useful for noisy "
                    "sources where you want acknowledgements but never a "
                    "real-time DM. Defaults to 'urgent' (no cap)."
                ),
            },
            "route_to_specialist": {
                "type": "string",
                "description": (
                    "Optional hint — which specialist should weigh in on "
                    "this signal's interpretation (e.g. 'cfo', 'cso', "
                    "'coo'). PR-C uses this when the reflection LLM decides "
                    "whether to consult_specialist on a surfaced alert."
                ),
            },
            "mode": {
                "type": "string",
                "enum": ["active", "dry_run"],
                "description": (
                    "'active' (default) surfaces signals normally. "
                    "'dry_run' captures + scores but never promotes — use "
                    "this when adding a new source you want to calibrate "
                    "before the principal sees the noise."
                ),
            },
            "notes": {
                "type": "string",
                "description": "Free-text note (max ~500 chars).",
            },
        },
        "required": ["slug", "signal_type", "target"],
    },
}


LIST_WATCHLIST_TOOL: dict[str, Any] = {
    "name": "list_watchlist",
    "description": (
        "List external-monitor watchlist entries. Use to resolve a slug or "
        "answer 'what are we currently watching?'. Returns a compact JSON "
        "list including slug, signal_type, target, mode, severity_floor, "
        "fired_count, and last_polled_at."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "signal_type": {
                "type": "string",
                "enum": [
                    "vendor_status", "rss", "stock", "query", "edgar",
                    "page_watch",
                ],
                "description": "Filter by adapter kind. Omit for all kinds.",
            },
            "enabled_only": {
                "type": "boolean",
                "description": (
                    "Default true — only return enabled rows. Pass false to "
                    "see disabled / muted entries too."
                ),
            },
        },
    },
}


REMOVE_WATCHLIST_ENTRY_TOOL: dict[str, Any] = {
    "name": "remove_watchlist_entry",
    "description": (
        "Hard-delete a watchlist entry by slug (its recorded signal history "
        "goes with it; alerts it already raised and the audit log stay). Use "
        "when the principal says 'stop watching X' AND wants the config gone. "
        "For temporary suppression prefer ``tune_watchlist_entry`` with "
        "enabled=false. For a research-added watch, an optional ``reason`` "
        "(not_relevant | too_noisy | wrong_source) is remembered so the "
        "research pass never re-proposes it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "slug": {"type": "string", "description": "Slug of the entry to remove."},
            "reason": {
                "type": "string",
                "enum": ["not_relevant", "too_noisy", "wrong_source"],
                "description": "Why (research-added watches only); defaults to not_relevant.",
            },
        },
        "required": ["slug"],
    },
}


TUNE_WATCHLIST_ENTRY_TOOL: dict[str, Any] = {
    "name": "tune_watchlist_entry",
    "description": (
        "Modify an existing watchlist entry. Use to disable a noisy source, "
        "raise the severity floor, flip into dry_run mode, or change the "
        "trigger threshold. Pass only the fields you want to change."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "slug": {"type": "string", "description": "Slug of the entry to modify."},
            "enabled": {
                "type": "boolean",
                "description": "Toggle on/off. False disables future polling.",
            },
            "mode": {
                "type": "string",
                "enum": ["active", "dry_run"],
                "description": "Switch between active surfacing and dry-run capture.",
            },
            "severity_floor": {
                "type": "string",
                "enum": ["low", "medium", "high", "urgent"],
            },
            "severity_ceiling": {
                "type": "string",
                "enum": ["low", "medium", "high", "urgent"],
            },
            "cadence": {
                "type": "string",
                "enum": ["real_time", "15min", "hourly", "daily", "weekly"],
            },
            "trigger": {
                "type": "object",
                "description": (
                    "Replace the trigger filter. Pass {} to clear all "
                    "filters; pass a new dict to overwrite (no partial "
                    "merge — keep all fields you want preserved)."
                ),
            },
            "notes": {"type": "string"},
        },
        "required": ["slug"],
    },
}


# Research-only tool: the executive_research watchlist pass may only PROPOSE
# a watch. Deterministic policy (monitoring.research.watch_policy) decides
# whether it is added on its own or shown to the principal as a suggestion.
# Not part of WATCHLIST_TOOLS — the chat Executive keeps add_watchlist_entry.
PROPOSE_WATCH_TOOL: dict[str, Any] = {
    "name": "propose_watch",
    "description": (
        "Propose an ongoing external source for the company to monitor, tied "
        "to a named company entity (a competitor, vendor, ticker or initiative "
        "from the company context). Policy code decides: a proposal grounded in "
        "company data and backed by a verified finding is added on its own; "
        "anything you are not sure about becomes a suggestion the principal "
        "approves. Never re-propose a target listed as declined."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "slug": {"type": "string", "description": "Unique kebab-case id, e.g. 'stock-acme', 'rss-acme-blog'."},
            "signal_type": {
                "type": "string",
                "enum": ["stock", "rss", "vendor_status", "edgar", "page_watch", "query"],
            },
            "target": {
                "type": "string",
                "description": (
                    "Concrete, real target: a ticker (stock/edgar), a feed or page URL "
                    "taken from a finding's urls (rss/vendor_status/page_watch), or a "
                    "search query (query). Never invent a URL."
                ),
            },
            "grounding_entity": {
                "type": "string",
                "description": (
                    "The company-context entity this watch is about, by name — a "
                    "competitor, vendor, ticker, initiative or the company itself."
                ),
            },
            "rationale": {
                "type": "string",
                "description": "<=240 chars, shown to the principal: why this source is worth watching.",
            },
            "finding_index": {
                "type": "integer",
                "description": (
                    "REQUIRED. The 1-based '#N' of the finding this proposal comes "
                    "from — the one whose urls or text name this source. A proposal "
                    "with no finding behind it is rejected as having no evidence."
                ),
            },
            "certainty": {
                "type": "string",
                "enum": ["confident", "unsure"],
                "description": (
                    "'confident' only when the entity is in the company data and the "
                    "source is the entity's own (its ticker, its site, its status page). "
                    "'unsure' can only turn an add into a suggestion — never the reverse."
                ),
            },
            "display_label": {"type": "string"},
            "trigger": {"type": "object", "description": "Optional adapter trigger; policy adds quiet defaults."},
            "route_to_specialist": {"type": "string"},
        },
        "required": [
            "slug", "signal_type", "target", "grounding_entity", "rationale", "finding_index", "certainty",
        ],
    },
}


WATCHLIST_TOOLS: list[dict[str, Any]] = [
    ADD_WATCHLIST_ENTRY_TOOL,
    LIST_WATCHLIST_TOOL,
    REMOVE_WATCHLIST_ENTRY_TOOL,
    TUNE_WATCHLIST_ENTRY_TOOL,
]


# --------------------------------------------------------------------- #
# Audit helper
# --------------------------------------------------------------------- #


def _audit(tool: str, ok: bool, summary: str, details: dict[str, Any]) -> None:
    audit_log(
        "tool_invocation",
        summary,
        actor="executive",
        details={"tool": tool, "ok": ok, **details},
    )


def _err(tool: str, msg: str) -> str:
    _audit(tool, False, f"{tool} failed: {msg}", {"error": msg[:300]})
    return json.dumps({"error": msg})


# --------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------- #


def _display_label_config(signal_type: str, display_label: Any) -> dict[str, Any]:
    """Map the user-facing label onto the adapter-specific config key."""
    config: dict[str, Any] = {}
    if display_label:
        if signal_type == "vendor_status":
            config["vendor_label"] = display_label
        elif signal_type == "rss":
            config["feed_label"] = display_label
        elif signal_type == "stock":
            config["display_name"] = display_label
        elif signal_type in ("query", "edgar", "page_watch"):
            config["label"] = display_label
    return config


async def _validated_target(
    tool: str, tool_input: dict[str, Any],
) -> tuple[str, str, str, dict[str, Any], dict[str, Any]] | str:
    """Shared insert-time validation for add_watchlist_entry / propose_watch.

    Returns ``(slug, signal_type, target, config, trigger)`` with the target
    validated + normalized (a non-feed rss URL may convert to page_watch), or
    the JSON error string to return to the model."""
    try:
        slug = str(tool_input["slug"]).strip()
        signal_type = str(tool_input["signal_type"]).strip()
        target = str(tool_input["target"]).strip()
    except (KeyError, TypeError) as exc:
        return _err(tool, f"missing required field: {exc}")

    if not slug or not signal_type or not target:
        return _err(tool, "slug, signal_type, target are all required and non-empty")
    if not _SLUG_RE.match(slug):
        return _err(tool, f"slug {slug!r} must match {_SLUG_RE.pattern}")
    if signal_type not in list_registered_kinds():
        return _err(tool, f"signal_type {signal_type!r} has no registered adapter")

    trigger = tool_input.get("trigger") or {}
    if not isinstance(trigger, dict):
        return _err(tool, "trigger must be a JSON object")

    if ms.get_watchlist_item_by_slug(slug) is not None:
        return _err(tool, f"slug {slug!r} already exists; use tune_watchlist_entry to modify")

    if signal_type in _URL_SIGNAL_TYPES:
        # Reject non-public / unparseable URLs at insert time for every
        # URL-typed kind (the adapters guard again at poll time, but a junk
        # row must not land — a dry-run suggestion still polls and is shown
        # to the principal with an Approve button).
        ok, reason = validate_target_url(target)
        if not ok:
            return _err(tool, f"target {target!r} is not a fetchable public URL: {reason}")

    config = _display_label_config(signal_type, tool_input.get("display_label"))
    # Validate the target before it lands: a non-feed rss URL is converted
    # to a page_watch (when the page has readable text) or rejected, so the
    # research workflow can't seed the watchlist with dead feed rows.
    try:
        signal_type, target, config = await validate_and_normalize_target(
            signal_type, target, config,
        )
    except WatchlistTargetError as exc:
        return _err(tool, exc.reason)
    return slug, signal_type, target, config, trigger


async def handle_add_watchlist_entry(tool_input: dict[str, Any]) -> str:
    from openexecutive.delegation.lockdown import outside_reach_refusal

    tool = "add_watchlist_entry"
    if (refused := outside_reach_refusal(tool)) is not None:
        return refused
    validated = await _validated_target(tool, tool_input)
    if isinstance(validated, str):
        return validated
    slug, signal_type, target, config, trigger = validated

    cadence = str(tool_input.get("cadence", "15min"))
    if not is_valid_cadence(cadence):
        return _err(tool, f"unknown cadence {cadence!r}")

    mode = str(tool_input.get("mode", MODE_ACTIVE))
    if not is_valid_mode(mode):
        return _err(tool, f"unknown mode {mode!r}")

    floor_str = str(tool_input.get("severity_floor", "low"))
    ceiling_str = str(tool_input.get("severity_ceiling", "urgent"))
    if floor_str not in _VALID_SEVERITY_VALUES:
        return _err(tool, f"unknown severity_floor {floor_str!r}")
    if ceiling_str not in _VALID_SEVERITY_VALUES:
        return _err(tool, f"unknown severity_ceiling {ceiling_str!r}")
    floor = AlertSeverity(floor_str)
    ceiling = AlertSeverity(ceiling_str)

    try:
        new_id = ms.insert_watchlist_item(
            slug=slug,
            signal_type=signal_type,
            target=target,
            config=config,
            trigger=trigger,
            cadence=cadence,
            severity_floor=floor,
            severity_ceiling=ceiling,
            # route_to_department and route_to_person_id are not yet
            # exposed via the chat tool schema — the routing UX in PR-C
            # will introduce them with proper validation. For now the
            # specialist hint is the only routing field the Executive
            # can set from a chat turn.
            route_to_specialist=str(tool_input.get("route_to_specialist", "")),
            mode=mode,
            notes=str(tool_input.get("notes", ""))[:500],
            origin=ORIGIN_EXECUTIVE,
        )
    except Exception as exc:
        logger.exception("add_watchlist_entry: insert failed")
        return _err(tool, f"insert failed: {exc}")

    _audit(
        tool, True,
        f"add_watchlist_entry: created {slug!r} ({signal_type})",
        {
            "watchlist_id": new_id,
            "slug": slug,
            "signal_type": signal_type,
            "target": target,
            "mode": mode,
        },
    )
    return json.dumps({
        "ok": True,
        "id": new_id,
        "slug": slug,
        "signal_type": signal_type,
        "mode": mode,
    })


async def handle_propose_watch(tool_input: dict[str, Any], collector: list[Any]) -> str:
    """Validate a research proposal and queue it for the watch policy.

    Nothing is written here — ``watch_policy.apply_proposals`` runs after the
    model's turn and decides add / suggest / reject. A target the principal
    declined is refused HERE so a new slug cannot route around the decline.
    """
    from openexecutive.monitoring.research.watch_policy import (
        WatchProposal,
        entity_declined,
    )

    tool = "propose_watch"
    # Decline checks come first — before the target is fetched for
    # validation, so a declined source is never even requested again.
    raw_type = str(tool_input.get("signal_type", "")).strip()
    raw_target = str(tool_input.get("target", "")).strip()
    if raw_target and ms.is_declined(normalize_target(raw_type, raw_target)):
        return _err(tool, f"target {raw_target!r} was declined by the principal — do not re-propose it")
    entity = str(tool_input.get("grounding_entity", ""))[:120]
    if entity_declined(entity, ms.list_declines()):
        return _err(tool, f"the principal declined watching {entity!r} — do not re-propose it")

    validated = await _validated_target(tool, tool_input)
    if isinstance(validated, str):
        return validated
    slug, signal_type, target, config, trigger = validated

    normalized = normalize_target(signal_type, target)
    if ms.is_declined(normalized):
        # The rss → page_watch conversion can change the target; re-check.
        return _err(tool, f"target {target!r} was declined by the principal — do not re-propose it")

    certainty = str(tool_input.get("certainty", "unsure")).strip().lower()
    if certainty not in ("confident", "unsure"):
        certainty = "unsure"
    raw_index = tool_input.get("finding_index")
    finding_index: int | None
    try:
        # 1-based "#N" from the findings turn; 0 or negative means "none".
        # Tolerated here even though the schema requires it: the policy
        # links the finding that cites the source when it is missing.
        finding_index = int(raw_index) - 1 if raw_index is not None and int(raw_index) >= 1 else None
    except (TypeError, ValueError):
        finding_index = None
    if any(getattr(p, "slug", "") == slug for p in collector):
        return _err(tool, f"slug {slug!r} was already proposed this run")

    collector.append(WatchProposal(
        slug=slug,
        signal_type=signal_type,
        target=target,
        normalized_target=normalized,
        rationale=str(tool_input.get("rationale", ""))[:240],
        grounding_entity=entity,
        finding_index=finding_index,
        certainty=certainty,
        config=config,
        trigger=trigger,
        route_to_specialist=str(tool_input.get("route_to_specialist", ""))[:32],
        display_label=str(tool_input.get("display_label", ""))[:120],
    ))
    return json.dumps({"ok": True, "queued": slug, "signal_type": signal_type})


async def handle_list_watchlist(tool_input: dict[str, Any]) -> str:
    tool = "list_watchlist"
    signal_type = tool_input.get("signal_type")
    enabled_only = bool(tool_input.get("enabled_only", True))
    try:
        items = ms.list_watchlist(
            enabled_only=enabled_only,
            signal_type=signal_type,
        )
    except Exception as exc:
        logger.exception("list_watchlist: failed")
        return _err(tool, f"list failed: {exc}")

    out = [
        {
            "id": i.id,
            "slug": i.slug,
            "signal_type": i.signal_type,
            "target": i.target,
            "cadence": i.cadence,
            "mode": i.mode,
            "enabled": i.enabled,
            "severity_floor": i.severity_floor.value,
            "severity_ceiling": i.severity_ceiling.value,
            "route_to_specialist": i.route_to_specialist,
            "fired_count": i.fired_count,
            "last_polled_at": i.last_polled_at,
            "last_fired_at": i.last_fired_at,
        }
        for i in items[:_LIST_DEFAULT_LIMIT]
    ]
    _audit(
        tool, True,
        f"list_watchlist returned {len(out)}",
        {"count": len(out), "signal_type": signal_type, "enabled_only": enabled_only},
    )
    return json.dumps({"watchlist": out, "count": len(out)})


async def handle_remove_watchlist_entry(tool_input: dict[str, Any]) -> str:
    tool = "remove_watchlist_entry"
    try:
        slug = str(tool_input["slug"]).strip()
    except (KeyError, TypeError) as exc:
        return _err(tool, f"missing slug: {exc}")
    if not slug:
        return _err(tool, "slug is required and cannot be empty")

    item = ms.get_watchlist_item_by_slug(slug)
    if item is None:
        return _err(tool, f"slug {slug!r} not found")

    try:
        removed = ms.delete_watchlist_item(item.id or 0)
    except Exception as exc:
        logger.exception("remove_watchlist_entry: delete failed")
        return _err(tool, f"delete failed: {exc}")

    # "Stop watching" a research-added source is feedback: remember the
    # decline so the research pass never re-proposes it under a new slug.
    if removed and item.origin in RESEARCH_ORIGINS:
        reason = str(tool_input.get("reason") or DECLINE_REASON_NOT_RELEVANT)
        if reason not in EXPLICIT_DECLINE_REASONS:
            reason = DECLINE_REASON_NOT_RELEVANT
        try:
            ms.insert_decline(
                normalized_target=normalize_target(item.signal_type, item.target),
                kind=DECLINE_KIND_EXPLICIT,
                reason=reason,
                signal_type=item.signal_type,
                slug=item.slug,
            )
        except Exception:
            logger.exception("remove_watchlist_entry: decline record failed")

    _audit(
        tool, removed,
        f"remove_watchlist_entry: {slug} ({'deleted' if removed else 'no-op'})",
        {"slug": slug, "watchlist_id": item.id, "removed": removed},
    )
    return json.dumps({"ok": removed, "slug": slug})


async def handle_tune_watchlist_entry(tool_input: dict[str, Any]) -> str:
    from openexecutive.delegation.lockdown import outside_reach_refusal

    tool = "tune_watchlist_entry"
    if (refused := outside_reach_refusal(tool)) is not None:
        return refused
    try:
        slug = str(tool_input["slug"]).strip()
    except (KeyError, TypeError) as exc:
        return _err(tool, f"missing slug: {exc}")
    if not slug:
        return _err(tool, "slug is required and cannot be empty")

    item = ms.get_watchlist_item_by_slug(slug)
    if item is None:
        return _err(tool, f"slug {slug!r} not found")
    assert item.id is not None

    # Validate every field BEFORE building the UPDATE so a bad value
    # short-circuits without leaving a partial change on disk. All
    # validated fields are applied in a single transaction via
    # `update_watchlist_fields` — that keeps tune_* atomic (the
    # previous split between set_enabled and a bulk UPDATE could
    # half-apply a tune on a transient SQLite error).
    fields: dict[str, Any] = {}
    changes: list[str] = []

    if item.origin == ORIGIN_RESEARCH_PROPOSED and any(
        k in tool_input for k in ("mode", "enabled", "cadence", "severity_floor", "severity_ceiling", "trigger")
    ):
        # A pending research suggestion leaves the pending state only through
        # the principal's approve / decline — never by a tune from a model —
        # and it goes live with the quiet defaults policy gave it, not with
        # a cadence / floor / trigger loosened while it waited.
        return _err(
            tool,
            f"{slug!r} is a pending research suggestion; the principal approves or "
            "declines it on the watch list (only notes can be edited meanwhile)",
        )
    if "enabled" in tool_input:
        enabled = bool(tool_input["enabled"])
        fields["enabled"] = 1 if enabled else 0
        changes.append(f"enabled={enabled}")
    if "mode" in tool_input:
        mode = str(tool_input["mode"])
        if not is_valid_mode(mode):
            return _err(tool, f"unknown mode {mode!r}")
        fields["mode"] = mode
        changes.append(f"mode={mode}")
    if "severity_floor" in tool_input:
        v = str(tool_input["severity_floor"])
        if v not in _VALID_SEVERITY_VALUES:
            return _err(tool, f"unknown severity_floor {v!r}")
        fields["severity_floor"] = v
        changes.append(f"severity_floor={v}")
    if "severity_ceiling" in tool_input:
        v = str(tool_input["severity_ceiling"])
        if v not in _VALID_SEVERITY_VALUES:
            return _err(tool, f"unknown severity_ceiling {v!r}")
        fields["severity_ceiling"] = v
        changes.append(f"severity_ceiling={v}")
    if "cadence" in tool_input:
        v = str(tool_input["cadence"])
        if not is_valid_cadence(v):
            return _err(tool, f"unknown cadence {v!r}")
        fields["cadence"] = v
        changes.append(f"cadence={v}")
    if "trigger" in tool_input:
        trig = tool_input["trigger"] or {}
        if not isinstance(trig, dict):
            return _err(tool, "trigger must be a JSON object")
        fields["trigger_json"] = json.dumps(trig)
        changes.append("trigger replaced")
    if "notes" in tool_input:
        fields["notes"] = str(tool_input["notes"])[:500]
        changes.append("notes updated")

    if not fields:
        return _err(tool, "nothing to change — pass at least one tunable field")

    try:
        ms.update_watchlist_fields(item.id, fields)
    except Exception as exc:
        logger.exception("tune_watchlist_entry: UPDATE failed")
        return _err(tool, f"UPDATE failed: {exc}")

    # Audit MUST include the actual changes so a later 'why is this
    # muted?' question can be answered from the audit log alone.
    _audit(
        tool, True,
        f"tune_watchlist_entry: {slug} -> {', '.join(changes)}",
        {"slug": slug, "watchlist_id": item.id, "changes": changes},
    )
    return json.dumps({"ok": True, "slug": slug, "changes": changes})


WATCHLIST_TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[str]]] = {
    "add_watchlist_entry": handle_add_watchlist_entry,
    "list_watchlist": handle_list_watchlist,
    "remove_watchlist_entry": handle_remove_watchlist_entry,
    "tune_watchlist_entry": handle_tune_watchlist_entry,
}


__all__ = [
    "ADD_WATCHLIST_ENTRY_TOOL",
    "LIST_WATCHLIST_TOOL",
    "REMOVE_WATCHLIST_ENTRY_TOOL",
    "TUNE_WATCHLIST_ENTRY_TOOL",
    "WATCHLIST_TOOLS",
    "WATCHLIST_TOOL_HANDLERS",
    "handle_add_watchlist_entry",
    "handle_list_watchlist",
    "handle_remove_watchlist_entry",
    "handle_tune_watchlist_entry",
]
