"""Standing web-search query adapter.

Lets the watchlist monitor *arbitrary* conditions that no feed publishes —
"Acme Corp layoffs OR restructuring OR funding", "new EU AI-Act enforcement
actions", "competitors announcing SOC 2 Type II". Each poll runs the standing
query through the provider's web_search tool and turns the distinct results
into Signals.

Unlike the feed adapters (rss / stock / vendor_status), this one is **billed** —
it issues an LLM call plus web searches per poll. The cost governors are:

  - a deliberately slow ``default_poll_interval_minutes`` (6h),
  - ``web_search_max_uses`` (caps searches per call, set in config),
  - ``max_results`` per row (caps emitted Signals),
  - and the pipeline's global ``external_monitor_max_signals_per_scan``.

Watchlist row shape:

  - ``signal_type``: ``"query"``
  - ``target``: the natural-language standing query.
  - ``config_json``: optional
    ``{"label": "Acme watch", "max_results": 5, "freshness_days": 7,
       "allowed_domains": [...], "blocked_domains": [...]}``.
    ``allowed_domains`` / ``blocked_domains`` are passed to the model as
    *best-effort* prompt hints — they are NOT the same as the global
    ``WEB_SEARCH_ALLOWED_DOMAINS`` setting, and the OpenRouter web plugin
    does not honour Anthropic's domain-filter fields (see
    ``providers/translator.py``), so do not rely on them as a hard guarantee.
  - ``trigger_json``: optional ``{"keywords": [...]}`` — same semantics as the
    rss adapter (a result surfaces only if a keyword appears in title/summary).

Provider routing: the call goes through ``BaseAgent.analyze_with_tools`` with
``model_override=get_research_model()`` exactly like the research fan-out, so an
OpenRouter Council override is honoured. The provider layer translates the
native web_search tool into OpenRouter's ``plugins:[{id:web}]`` when routed
there; no special-casing lives here.

Severity hint defaults to ``LOW`` — open-web results are noisy. Capture-time
enrichment (``monitoring.enrichment``) scores relevance downstream and the
watchlist's ``severity_floor`` filters aggressively.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openexecutive.agents.base import BaseAgent
from openexecutive.alerts.models import AlertSeverity
from openexecutive.config import get_settings
from openexecutive.monitoring.models import SOURCE_KIND_QUERY, Signal, WatchlistItem
from openexecutive.monitoring.sources._http import strip_url_query, validate_target_url
from openexecutive.orchestrator.web_search_tool import build_web_search_tool

logger = logging.getLogger(__name__)

# Standing queries are not status pages — daily-to-6-hourly is the right band,
# and this is the primary cost governor for a billed source. A per-row
# ``cadence`` of "hourly" can tighten it (the pipeline floor reads this).
_DEFAULT_POLL_MINUTES = 360

# Hard ceiling on results we turn into Signals per poll, regardless of what the
# model returns. Per-row ``config_json.max_results`` may lower it.
_DEFAULT_MAX_RESULTS = 5
_MAX_RESULTS_CEILING = 10

# Web-search-backed calls can run long; mirror the research turn's ceiling.
_QUERY_TIMEOUT_SECONDS = 300.0

_QUERY_SYSTEM_PROMPT = (
    "You are a monitoring research agent. You are given a standing query that "
    "the principal wants watched. Use web_search to find the most recent, "
    "distinct, credible developments matching it, then report them by calling "
    "the emit_query_results tool EXACTLY ONCE.\n\n"
    "Rules:\n"
    "- Only report genuinely relevant, real results you found via web_search. "
    "Never invent results or URLs.\n"
    "- Prefer recent developments over evergreen background.\n"
    "- Each result needs a working source URL the principal can click.\n"
    "- An empty results list is the correct answer when nothing new and "
    "relevant turned up."
)

EMIT_QUERY_RESULTS_TOOL: dict[str, Any] = {
    "name": "emit_query_results",
    "description": (
        "Report the distinct web results you found for the standing query. "
        "Call this EXACTLY ONCE with your full list. Return an empty list when "
        "nothing new and relevant turned up — that is a valid answer."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "Short headline for the development.",
                        },
                        "url": {
                            "type": "string",
                            "description": (
                                "Source URL you verified via web_search. "
                                "Must be a real, clickable http(s) link."
                            ),
                        },
                        "summary": {
                            "type": "string",
                            "description": (
                                "1-2 sentences. Quote key numbers/dates/names "
                                "rather than paraphrasing."
                            ),
                        },
                        "published": {
                            "type": "string",
                            "description": "Publish date if known (else empty).",
                        },
                    },
                    "required": ["title", "url", "summary"],
                },
            },
        },
        "required": ["results"],
    },
}


class _QueryResearchAgent(BaseAgent):
    """Minimal agent shell so the adapter can reuse ``analyze_with_tools``.

    Distinct ``name`` so it never collides with a real specialist's Council
    override; the model is pinned per-call via ``model_override`` regardless.
    """

    name = "query_source"
    domain = "utility"
    model = ""
    use_deep_reasoning = False

    def __init__(self) -> None:
        self.model = get_settings().research_model

    def get_system_prompt(self) -> str:
        return _QUERY_SYSTEM_PROMPT


class QuerySource:
    kind: str = SOURCE_KIND_QUERY
    default_poll_interval_minutes: int = _DEFAULT_POLL_MINUTES
    # A standing query's first results are the research the user asked for.
    seed_on_first_poll: bool = False

    async def poll(
        self, item: WatchlistItem, *, db_path: Path | None = None
    ) -> list[Signal]:
        from openexecutive.agents.research_council import (
            get_research_model,
            get_research_use_deep_reasoning,
        )

        query = (item.target or "").strip()
        if not query:
            logger.warning(
                "query: watchlist %r has empty target — skipping", item.slug
            )
            return []

        web_search = build_web_search_tool()
        if web_search is None:
            # No web search → the model would answer from stale memory with no
            # provenance. Refuse rather than emit unverifiable Signals.
            logger.warning(
                "query: web search disabled (enable_web_search=False) — "
                "skipping watchlist %r", item.slug,
            )
            return []

        tools: list[dict[str, Any]] = [EMIT_QUERY_RESULTS_TOOL, web_search]
        max_results = _coerce_max_results(item.config_json.get("max_results"))
        user_content = _build_user_content(query, item.config_json, max_results)

        agent = _QueryResearchAgent()
        try:
            message = await agent.analyze_with_tools(
                user_content,
                tools=tools,
                timeout_seconds=_QUERY_TIMEOUT_SECONDS,
                model_override=get_research_model(),
                deep_reasoning_override=get_research_use_deep_reasoning(),
            )
        except Exception:
            logger.exception(
                "query: provider call failed for watchlist %r", item.slug
            )
            return []

        results = _extract_results(message, item.slug)
        if not results:
            return []

        label = item.config_json.get("label") or "query"
        signals: list[Signal] = []
        # Cap on VALID signals, not raw results — slicing the raw list first
        # would let a run of dropped results (no URL / SSRF-rejected) at the
        # front silently starve the cap and emit fewer than max_results even
        # when valid results exist further down.
        for raw in results:
            if len(signals) >= max_results:
                break
            signal = _result_to_signal(raw, item, label)
            if signal is not None:
                signals.append(signal)
        return signals

    def matches_trigger(self, signal: Signal, item: WatchlistItem) -> bool:
        """Optional keyword filter — matched against summary + body.

        Same semantics as the rss adapter so operators get one mental model.
        """
        keywords = item.trigger_json.get("keywords") or []
        if not keywords:
            return True
        haystack = (
            signal.normalized_summary
            + " "
            + (signal.raw_payload.get("summary") or "")
        ).lower()
        return any(str(kw).lower() in haystack for kw in keywords)


# --------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------- #


def _coerce_max_results(value: Any) -> int:
    """Clamp a per-row max_results into [1, ceiling]; default on bad input."""
    try:
        n = int(value) if value is not None else _DEFAULT_MAX_RESULTS
    except (TypeError, ValueError):
        return _DEFAULT_MAX_RESULTS
    return max(1, min(n, _MAX_RESULTS_CEILING))


def _build_user_content(
    query: str, config: dict[str, Any], max_results: int
) -> str:
    parts = [
        f"Standing query to monitor:\n{query}",
        f"\nReport at most {max_results} distinct, high-signal results.",
    ]
    freshness = config.get("freshness_days")
    try:
        if freshness is not None and int(freshness) > 0:
            parts.append(
                f"Prioritise developments from the last {int(freshness)} days."
            )
    except (TypeError, ValueError):
        pass
    allowed = config.get("allowed_domains")
    if isinstance(allowed, list) and allowed:
        parts.append(
            "Prefer results from these domains: "
            + ", ".join(str(d) for d in allowed[:20])
        )
    blocked = config.get("blocked_domains")
    if isinstance(blocked, list) and blocked:
        parts.append(
            "Avoid results from these domains: "
            + ", ".join(str(d) for d in blocked[:20])
        )
    return "\n".join(parts)


def _extract_results(message: Any, slug: str) -> list[dict[str, Any]]:
    """Pull the first ``emit_query_results`` tool_use block's results array.

    Mirrors ``specialist_research._extract_findings``: responses interleave
    text / server_tool_use (web_search) / web_search_tool_result / tool_use
    blocks; we honour only the FIRST emit_query_results call.
    """
    content = getattr(message, "content", None) or []
    seen = False
    out: list[dict[str, Any]] = []
    for block in content:
        if getattr(block, "type", "") != "tool_use":
            continue
        if getattr(block, "name", "") != "emit_query_results":
            continue
        if seen:
            logger.warning(
                "query: watchlist %r emitted >1 emit_query_results — "
                "ignoring extras", slug,
            )
            break
        seen = True
        block_input = getattr(block, "input", None) or {}
        items = block_input.get("results")
        if isinstance(items, list):
            out.extend(i for i in items if isinstance(i, dict))
    if not out:
        logger.debug("query: watchlist %r emitted no results", slug)
    return out


def _result_to_signal(
    raw: dict[str, Any], item: WatchlistItem, label: str
) -> Signal | None:
    """Convert one model-reported result into a Signal, or None to drop it."""
    title = str(raw.get("title") or "").strip() or "(untitled)"
    url = str(raw.get("url") or "").strip()
    summary_text = str(raw.get("summary") or "").strip()

    if not url:
        # provenance_url is a mandatory Source invariant — every surfaced
        # Signal must be clickable. A query row's ``target`` is the natural-
        # language query, not a URL, so there is no fallback provenance to
        # synthesise; drop the result rather than emit a dead-end Signal.
        logger.debug(
            "query: dropping result for %r — model returned no url", item.slug
        )
        return None

    # Results come from the open web — the SSRF guard matters more here than
    # for an operator-configured feed URL. Drop a single bad result, keep its
    # siblings.
    ok, reason = validate_target_url(url)
    if not ok:
        logger.warning(
            "query: dropping result for %r — bad url %r (%s)",
            item.slug, url, reason,
        )
        return None
    canonical = strip_url_query(url)

    summary = f"[{label}] {title}"
    return Signal(
        watchlist_id=item.id or 0,
        source_kind=SOURCE_KIND_QUERY,
        source_external_id=canonical[:500],
        captured_at=datetime.now(UTC).isoformat(),
        normalized_summary=summary[:500],
        raw_payload={
            "label": label,
            "query": item.target,
            "title": title,
            "link": canonical,
            "summary": summary_text[:1000],
            "published": str(raw.get("published") or ""),
        },
        provenance_url=canonical,
        severity_hint=AlertSeverity.LOW,
        dedup_key=_make_dedup_key(item.slug, canonical),
    )


def _make_dedup_key(slug: str, natural_id: str) -> str:
    payload = f"{slug}\x00{natural_id}".encode()
    digest = hashlib.sha256(payload).hexdigest()[:32]
    return f"query:{digest}"
