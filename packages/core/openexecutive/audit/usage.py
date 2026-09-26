"""Per-call model usage recording.

Every model call the system makes (Executive chat turns, specialist consults
and workflow specialist steps, specialist research, the research synthesis
and watchlist passes, triage, the chat memory extractor, the Council test
boxes) records one ``cache_event`` audit row through
:func:`log_model_usage` — tokens, cache hits, provider-reported cost, and the
server-side web searches the call made. ``GET /audit/usage`` and the
per-session cost view sum those rows, so a run's weight can be read from the
audit log instead of guessed.

The ``actor`` column names the source of the call (``executive``,
``specialist`` for chat-turn consults, ``specialist_workflow`` for workflow
steps, ``specialist_mcp`` for the MCP consult tool, ``specialist_research``,
``research_synthesis``, ``research_watchlist``, ``triage``,
``memory_extractor``, ``agent_test`` for the Council test boxes), which is
what the per-source breakdown groups on. A research run binds its
``run_id`` in a ContextVar for its
duration so every row the run produces carries it in ``details``; the
workflow then rolls those rows up into the ``usage`` block on its result.
"""
from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar
from typing import Any

from openexecutive.audit.logger import log_event

EVENT_TYPE = "cache_event"

_research_run_id: ContextVar[str | None] = ContextVar("research_run_id", default=None)
_research_rollup: ContextVar[UsageRollup | None] = ContextVar("research_rollup", default=None)


def get_research_run_id() -> str | None:
    """The research run the current task is part of, or ``None``."""
    return _research_run_id.get()


@contextlib.contextmanager
def bind_research_run(run_id: str | None) -> Iterator[UsageRollup]:
    """Tag every usage row logged inside the block with ``run_id`` and sum
    it into the yielded :class:`UsageRollup`. Tasks spawned inside the
    block (the specialist fan-out) inherit the binding and share the same
    rollup object, so their calls count too.

    Bind in the task that runs the calls, never inside an async generator
    that a consumer may abandon: a ContextVar set inside a generator lands
    in the consumer's context, and an abandoned generator's ``finally``
    runs elsewhere (``executive_research.run`` drives its body in a child
    task for this reason)."""
    rollup = UsageRollup()
    token_id = _research_run_id.set(run_id)
    token_rollup = _research_rollup.set(rollup)
    try:
        yield rollup
    finally:
        _research_rollup.reset(token_rollup)
        _research_run_id.reset(token_id)


def usage_counts(message: Any) -> dict[str, Any] | None:
    """Read the token, cache, cost and web-search counters off a provider
    ``Message`` (or the translator's SimpleNamespace). ``None`` when the
    response carries no ``usage`` at all; every field is guarded so a
    provider that omits one still yields the rest."""
    usage = getattr(message, "usage", None)
    if usage is None:
        return None
    # A real usage block carries integer token counts; anything else (a
    # mock, a provider quirk) is not usage and must not become a row.
    if not any(
        isinstance(getattr(usage, field, None), int)
        for field in ("input_tokens", "output_tokens")
    ):
        return None
    raw_cost = getattr(usage, "cost", None)
    try:
        cost_usd = float(raw_cost) if raw_cost is not None else None
    except (TypeError, ValueError):
        cost_usd = None
    server = getattr(usage, "server_tool_use", None)
    # Anthropic splits cache writes by how long they are kept, and a 1-hour
    # write costs more than a 5-minute one (audit.pricing).
    by_ttl = getattr(usage, "cache_creation", None)
    return {
        "input_tokens": _as_int(getattr(usage, "input_tokens", 0)),
        "output_tokens": _as_int(getattr(usage, "output_tokens", 0)),
        "cache_creation_input_tokens": _as_int(
            getattr(usage, "cache_creation_input_tokens", 0)
        ),
        "cache_creation_5m_input_tokens": _as_int(
            getattr(by_ttl, "ephemeral_5m_input_tokens", 0)
        ),
        "cache_creation_1h_input_tokens": _as_int(
            getattr(by_ttl, "ephemeral_1h_input_tokens", 0)
        ),
        "cache_read_input_tokens": _as_int(getattr(usage, "cache_read_input_tokens", 0)),
        "cost_usd": cost_usd,
        "web_search_requests": _as_int(getattr(server, "web_search_requests", 0)),
    }


def log_model_usage(
    message: Any,
    *,
    model: str,
    actor: str,
    iteration: int = 0,
    session_id: str | None = None,
    turn_id: str | None = None,
) -> dict[str, Any] | None:
    """Record one ``cache_event`` row for a completed model call.

    Never raises and never touches the request: it reads ``message.usage``
    after the call, so prompt caching is unaffected. Returns the counters it
    recorded (``None`` when the response had no usage block — the call still
    counts toward a bound research run's rollup, so the run's call count is
    honest even when a provider omits usage). ``session_id`` / ``turn_id``
    fall back to the audit ContextVars when omitted, as every other audit
    row does.
    """
    try:
        counts = usage_counts(message)
        rollup = _research_rollup.get()
        if rollup is not None:
            rollup.add(actor, counts)
    except Exception:  # noqa: BLE001 — a malformed usage block is not the caller's problem
        return None
    if counts is None:
        return None
    stop_reason = getattr(message, "stop_reason", None)
    details: dict[str, Any] = {
        "model": model,
        "iteration": iteration,
        **counts,
        "stop_reason": stop_reason,
    }
    run_id = get_research_run_id()
    if run_id:
        details["run_id"] = run_id
    try:
        log_event(
            EVENT_TYPE,
            (
                f"{model} iter={iteration} in={counts['input_tokens']} "
                f"out={counts['output_tokens']} "
                f"cache_read={counts['cache_read_input_tokens']} "
                f"cache_create={counts['cache_creation_input_tokens']} "
                f"cost={counts['cost_usd']} "
                f"searches={counts['web_search_requests']} stop={stop_reason}"
            ),
            session_id=session_id,
            turn_id=turn_id,
            actor=actor,
            details=details,
        )
    except Exception:  # noqa: BLE001 — audit is fire-and-forget
        return counts
    return counts


class UsageRollup:
    """Sums the counters of the calls one research run made, so the run's
    result and audit row can say what it did (calls, tokens, searches)."""

    _FIELDS = (
        "input_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "output_tokens",
        "web_search_requests",
    )

    def __init__(self) -> None:
        self.calls = 0
        self.totals: dict[str, int] = dict.fromkeys(self._FIELDS, 0)
        self.by_actor: dict[str, dict[str, int]] = {}

    def add(self, actor: str, counts: dict[str, Any] | None) -> None:
        """Count one call; ``counts`` may be ``None`` for a response that
        carried no usage block (the call happened, its tokens are unknown)."""
        self.calls += 1
        bucket = self.by_actor.setdefault(actor, {"calls": 0, **dict.fromkeys(self._FIELDS, 0)})
        bucket["calls"] += 1
        for field in self._FIELDS:
            value = _as_int((counts or {}).get(field, 0))
            self.totals[field] += value
            bucket[field] += value

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            **self.totals,
            "by_source": {k: dict(v) for k, v in sorted(self.by_actor.items())},
        }


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


__all__ = [
    "EVENT_TYPE",
    "UsageRollup",
    "bind_research_run",
    "get_research_run_id",
    "log_model_usage",
    "usage_counts",
]
