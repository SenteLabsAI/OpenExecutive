"""Unit tests for per-call model usage recording (audit.usage)."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from openexecutive.audit import usage as au
from openexecutive.audit.logger import AuditLogger, set_audit_logger


@pytest.fixture
def audit(tmp_path: Path) -> AuditLogger:
    logger = AuditLogger(tmp_path / "audit.db")
    set_audit_logger(logger)
    yield logger  # type: ignore[misc]
    set_audit_logger(None)


def _message(**usage: object) -> SimpleNamespace:
    return SimpleNamespace(usage=SimpleNamespace(**usage), stop_reason="end_turn")


def test_usage_counts_reads_tokens_cost_and_server_searches() -> None:
    msg = _message(
        input_tokens=120, output_tokens=30, cache_creation_input_tokens=10,
        cache_read_input_tokens=900, cost="0.04",
        server_tool_use=SimpleNamespace(web_search_requests=3),
    )
    counts = au.usage_counts(msg)
    assert counts == {
        "input_tokens": 120, "output_tokens": 30,
        "cache_creation_input_tokens": 10,
        # No per-lifetime breakdown on this response (OpenRouter sends none).
        "cache_creation_5m_input_tokens": 0, "cache_creation_1h_input_tokens": 0,
        "cache_read_input_tokens": 900,
        "cost_usd": 0.04, "web_search_requests": 3,
    }


def test_usage_counts_reads_the_cache_write_split_by_lifetime() -> None:
    # A 1-hour write costs more than a 5-minute one, so the split is kept.
    msg = _message(
        input_tokens=1, output_tokens=1, cache_creation_input_tokens=700,
        cache_creation=SimpleNamespace(ephemeral_5m_input_tokens=200, ephemeral_1h_input_tokens=500),
    )
    counts = au.usage_counts(msg)
    assert counts is not None
    assert (counts["cache_creation_5m_input_tokens"], counts["cache_creation_1h_input_tokens"]) == (200, 500)


def test_usage_counts_tolerates_missing_fields_and_no_usage() -> None:
    assert au.usage_counts(SimpleNamespace(content=[])) is None
    # A mock (or a usage object without integer counts) is not usage.
    from unittest.mock import MagicMock
    assert au.usage_counts(MagicMock()) is None
    assert au.usage_counts(SimpleNamespace(usage=SimpleNamespace(cost="0.1"))) is None
    counts = au.usage_counts(_message(input_tokens=5, cost="not-a-number"))
    assert counts is not None
    assert counts["output_tokens"] == 0 and counts["web_search_requests"] == 0
    assert counts["cost_usd"] is None


def test_log_model_usage_writes_one_cache_event_row(audit: AuditLogger) -> None:
    msg = _message(input_tokens=50, output_tokens=7,
                   server_tool_use=SimpleNamespace(web_search_requests=2))
    counts = au.log_model_usage(msg, model="claude-sonnet-5", actor="specialist_research",
                                iteration=1, session_id="s1", turn_id="t1")
    assert counts is not None and counts["web_search_requests"] == 2
    rows = audit.query(event_type="cache_event")
    assert len(rows) == 1
    row = rows[0]
    assert row.actor == "specialist_research" and row.session_id == "s1"
    assert row.details["model"] == "claude-sonnet-5"
    assert row.details["input_tokens"] == 50 and row.details["web_search_requests"] == 2
    assert "run_id" not in row.details


def test_log_model_usage_tags_rows_with_the_bound_research_run(audit: AuditLogger) -> None:
    with au.bind_research_run("run-abc") as rollup:
        assert au.get_research_run_id() == "run-abc"
        au.log_model_usage(_message(input_tokens=1), model="m", actor="research_synthesis")
        au.log_model_usage(
            _message(input_tokens=4, server_tool_use=SimpleNamespace(web_search_requests=2)),
            model="m", actor="specialist_research",
        )
    assert au.get_research_run_id() is None
    # The bound rollup summed both calls; a call outside the block does not count.
    au.log_model_usage(_message(input_tokens=100), model="m", actor="executive")
    out = rollup.as_dict()
    assert out["calls"] == 2 and out["input_tokens"] == 5 and out["web_search_requests"] == 2
    assert set(out["by_source"]) == {"research_synthesis", "specialist_research"}
    au.log_model_usage(_message(input_tokens=1), model="m", actor="executive")
    rows = {r.actor: r for r in audit.query(event_type="cache_event")}
    assert rows["research_synthesis"].details["run_id"] == "run-abc"
    assert rows["specialist_research"].details["run_id"] == "run-abc"
    assert "run_id" not in rows["executive"].details


@pytest.mark.asyncio
async def test_rollup_collects_calls_made_in_gathered_tasks(audit: AuditLogger) -> None:
    """The specialist fan-out runs in asyncio.gather sub-tasks; they inherit
    the binding and share the rollup object."""
    import asyncio

    async def one_call(i: int) -> None:
        au.log_model_usage(_message(input_tokens=i), model="m", actor="specialist_research")

    with au.bind_research_run("run-gather") as rollup:
        await asyncio.gather(one_call(1), one_call(2), one_call(3))
    assert rollup.as_dict()["calls"] == 3 and rollup.as_dict()["input_tokens"] == 6


def test_log_model_usage_never_raises_on_a_malformed_usage_block(audit: AuditLogger) -> None:
    msg = _message(input_tokens=float("inf"), output_tokens=3)
    assert au.log_model_usage(msg, model="m", actor="triage") is not None
    row = audit.query(event_type="cache_event")[0]
    assert row.details["input_tokens"] == 0 and row.details["output_tokens"] == 3


def test_log_model_usage_returns_none_without_usage(audit: AuditLogger) -> None:
    assert au.log_model_usage(SimpleNamespace(content=[]), model="m", actor="triage") is None
    assert audit.query(event_type="cache_event") == []


def test_usage_rollup_sums_per_source() -> None:
    roll = au.UsageRollup()
    roll.add("specialist_research", {"input_tokens": 10, "output_tokens": 2, "web_search_requests": 3})
    roll.add("specialist_research", {"input_tokens": 5, "output_tokens": 1, "web_search_requests": 1})
    roll.add("research_synthesis", {"input_tokens": 7, "cache_read_input_tokens": 100})
    roll.add("research_watchlist", None)  # a call without usage still counts as a call
    out = roll.as_dict()
    assert out["calls"] == 4
    assert out["by_source"]["research_watchlist"]["calls"] == 1
    assert out["input_tokens"] == 22 and out["web_search_requests"] == 4
    assert out["cache_read_input_tokens"] == 100
    assert out["by_source"]["specialist_research"] == {
        "calls": 2, "input_tokens": 15, "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0, "output_tokens": 3, "web_search_requests": 4,
    }
    assert list(out["by_source"]) == ["research_synthesis", "research_watchlist", "specialist_research"]


def test_log_model_usage_counts_a_call_without_usage_in_the_rollup(audit: AuditLogger) -> None:
    with au.bind_research_run("run-x") as rollup:
        assert au.log_model_usage(SimpleNamespace(content=[]), model="m", actor="triage") is None
    assert rollup.as_dict()["calls"] == 1
    assert audit.query(event_type="cache_event") == []  # no row without counts
