"""Unit tests for the audit log SQLite writer/reader."""
from __future__ import annotations

from pathlib import Path

import pytest

from openexecutive.audit.logger import EVENT_TYPES, AuditLogger


@pytest.fixture()
def audit(tmp_path: Path) -> AuditLogger:
    return AuditLogger(tmp_path / "audit.db")


def test_log_and_query_round_trip(audit: AuditLogger) -> None:
    rid = audit.log(
        "chat_turn",
        "User: hello world",
        session_id="s1",
        turn_id="t1",
        actor="user",
        details={"len": 11},
    )
    assert rid is not None and rid > 0

    events = audit.query()
    assert len(events) == 1
    e = events[0]
    assert e.event_type == "chat_turn"
    assert e.summary == "User: hello world"
    assert e.session_id == "s1"
    assert e.turn_id == "t1"
    assert e.actor == "user"
    assert e.details == {"len": 11}


def test_filters_apply_independently(audit: AuditLogger) -> None:
    audit.log("chat_turn", "User: hi", session_id="s1", actor="user")
    audit.log("specialist_consult", "Consulted finance: runway", session_id="s1", actor="finance")
    audit.log("tool_invocation", "mcp:gmail.send", session_id="s2", actor="executive")

    assert len(audit.query(event_type="chat_turn")) == 1
    assert len(audit.query(session_id="s1")) == 2
    assert len(audit.query(actor="finance")) == 1


def test_q_does_like_search_on_summary(audit: AuditLogger) -> None:
    audit.log("specialist_consult", "Consulted finance: cash runway")
    audit.log("specialist_consult", "Consulted hr: hiring plan")
    audit.log("chat_turn", "User: finance question")

    finance = audit.query(q="finance")
    assert {e.summary for e in finance} == {
        "Consulted finance: cash runway",
        "User: finance question",
    }


def test_pagination_and_count(audit: AuditLogger) -> None:
    for i in range(7):
        audit.log("chat_turn", f"msg-{i}")

    assert audit.count() == 7
    page1 = audit.query(limit=3, offset=0)
    page2 = audit.query(limit=3, offset=3)
    page3 = audit.query(limit=3, offset=6)

    assert [e.summary for e in page1] == ["msg-6", "msg-5", "msg-4"]
    assert [e.summary for e in page2] == ["msg-3", "msg-2", "msg-1"]
    assert [e.summary for e in page3] == ["msg-0"]


def test_summary_truncation(audit: AuditLogger) -> None:
    long = "x" * 500
    audit.log("chat_turn", long)
    e = audit.query()[0]
    assert len(e.summary) == 300
    assert e.summary.endswith("…")


def test_log_swallows_failures(tmp_path: Path) -> None:
    bad = AuditLogger(tmp_path / "audit.db")

    # Force a serialization failure: an object whose default=str fallback
    # itself raises. We expect the log call to return None, not raise.
    class _Boom:
        def __repr__(self) -> str:
            raise RuntimeError("boom")

    # JSON serialization can still survive via default=str, but the repr
    # fallback path is exercised by passing an un-serializable value.
    result = bad.log("chat_turn", "ok", details={"obj": _Boom()})  # type: ignore[arg-type]
    # Either it succeeded (because default=str caught it) or returned None;
    # the contract is "never raise". Both are acceptable.
    assert result is None or isinstance(result, int)


def test_full_payload_roundtrip_uncapped(audit: AuditLogger) -> None:
    # 50 KB blob — well past the 4000-char details cap.
    big = "x" * 50_000
    rid = audit.log(
        "chat_turn",
        "User: huge",
        full={"message": big},
    )
    assert rid is not None

    # List path must NOT carry the full payload (keeps scan responses small).
    listed = audit.query()[0]
    assert listed.full is None

    fetched = audit.get(rid)
    assert fetched is not None
    assert fetched.full == {"message": big}
    assert len(fetched.full["message"]) == 50_000


def test_get_returns_none_for_missing_id(audit: AuditLogger) -> None:
    audit.log("chat_turn", "hello")
    assert audit.get(9_999_999) is None


def test_get_handles_rows_without_full_payload(audit: AuditLogger) -> None:
    rid = audit.log("chat_turn", "hello", details={"k": "v"})
    assert rid is not None
    fetched = audit.get(rid)
    assert fetched is not None
    assert fetched.full is None
    assert fetched.details == {"k": "v"}


def test_full_payload_survives_pre_existing_schema(tmp_path: Path) -> None:
    """Boot against a DB created with the original schema (no full_json column),
    confirm the additive ALTER runs and new writes persist `full`."""
    import sqlite3 as _sqlite3
    db = tmp_path / "old.db"
    conn = _sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            event_type TEXT NOT NULL,
            session_id TEXT,
            turn_id TEXT,
            actor TEXT,
            summary TEXT NOT NULL,
            details_json TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO audit_log (ts, event_type, summary) VALUES (?, ?, ?)",
        ("2026-01-01T00:00:00Z", "chat_turn", "legacy row"),
    )
    conn.commit()
    conn.close()

    audit = AuditLogger(db)
    rid = audit.log("chat_turn", "new row", full={"message": "hello"})
    assert rid is not None

    new = audit.get(rid)
    assert new is not None and new.full == {"message": "hello"}

    # Legacy row should still be readable; its `full` is None.
    legacy = audit.query(q="legacy")[0]
    legacy_fetched = audit.get(legacy.id)
    assert legacy_fetched is not None and legacy_fetched.full is None


def test_event_types_constant_includes_known_types() -> None:
    assert "chat_turn" in EVENT_TYPES
    assert "specialist_consult" in EVENT_TYPES
    assert "tool_invocation" in EVENT_TYPES
    assert "scheduled_action" in EVENT_TYPES
    assert "alert" in EVENT_TYPES
    assert "integration_inbound" in EVENT_TYPES
    assert "memory_extraction" in EVENT_TYPES, (
        "every extraction pass writes this row; it must be selectable in the audit UI filter"
    )


def test_q_escapes_like_wildcards(audit: AuditLogger) -> None:
    audit.log("chat_turn", "literal percent: 100%")
    audit.log("chat_turn", "underscore_value")
    audit.log("chat_turn", "no special chars")

    # A bare % should NOT act as a wildcard — it should only match the literal '%'.
    only_percent = audit.query(q="%")
    assert {e.summary for e in only_percent} == {"literal percent: 100%"}

    only_underscore = audit.query(q="_")
    assert {e.summary for e in only_underscore} == {"underscore_value"}


def test_since_until_filters(audit: AuditLogger) -> None:
    audit.log("chat_turn", "old", session_id="s1")
    # ts comes from datetime.now(UTC).isoformat(), so we filter using the
    # row's own ts to make this deterministic.
    rows = audit.query()
    ts = rows[0].ts
    assert len(audit.query(since=ts)) >= 1
    assert audit.query(until="0000-01-01T00:00:00+00:00") == []


# --------------------------------------------------------------------------- #
# usage_summary — cross-session token + cost aggregation
# --------------------------------------------------------------------------- #

import json  # noqa: E402 — grouped with the usage_summary tests it supports
import sqlite3  # noqa: E402


def _insert_cache_event(
    db_path: Path, ts: str, *, session_id: str, model: str, details: dict
) -> None:
    """Insert a cache_event row with an explicit ts (the public log() stamps
    ts itself, so direct insertion is the only way to exercise by-day grouping)."""
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO audit_log (ts, event_type, session_id, summary, details_json) "
            "VALUES (?, 'cache_event', ?, ?, ?)",
            (ts, session_id, f"{model} call", json.dumps({"model": model, **details})),
        )
        conn.commit()


def test_usage_summary_empty_db_returns_zeros(tmp_path: Path) -> None:
    audit = AuditLogger(tmp_path / "audit.db")
    s = audit.usage_summary()
    assert s["totals"] == {
        "calls": 0,
        "input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "output_tokens": 0,
        "web_search_requests": 0,
        "cost_usd": 0.0,
    }
    assert s["by_day"] == []
    assert s["by_model"] == []
    assert s["by_source"] == []


def test_usage_summary_groups_by_source_and_sums_searches(audit: AuditLogger) -> None:
    audit.log("cache_event", "chat", actor="executive",
              details={"model": "m", "input_tokens": 100, "web_search_requests": 1})
    audit.log("cache_event", "research 1", actor="specialist_research",
              details={"model": "m", "input_tokens": 40, "web_search_requests": 3})
    audit.log("cache_event", "research 2", actor="specialist_research",
              details={"model": "m", "input_tokens": 60, "web_search_requests": 2})
    audit.log("cache_event", "legacy row, no actor",
              details={"model": "m", "input_tokens": 5})

    s = audit.usage_summary()
    assert s["totals"]["web_search_requests"] == 6
    by_source = {r["source"]: r for r in s["by_source"]}
    assert set(by_source) == {"executive", "specialist_research", "unknown"}
    assert by_source["specialist_research"]["calls"] == 2
    assert by_source["specialist_research"]["input_tokens"] == 100
    assert by_source["specialist_research"]["web_search_requests"] == 5
    assert by_source["unknown"]["input_tokens"] == 5


def test_usage_summary_aggregates_across_sessions_and_models(audit: AuditLogger) -> None:
    audit.log("cache_event", "c1", session_id="s1", turn_id="t1",
              details={"model": "claude-opus-4-8", "input_tokens": 100,
                       "output_tokens": 20, "cache_read_input_tokens": 900,
                       "cache_creation_input_tokens": 0, "cost_usd": 0.05})
    audit.log("cache_event", "c2", session_id="s2", turn_id="t9",
              details={"model": "claude-sonnet-4-6", "input_tokens": 50,
                       "output_tokens": 10, "cache_read_input_tokens": 0,
                       "cache_creation_input_tokens": 200, "cost_usd": 0.01})
    # Non-cache events are excluded from totals.
    audit.log("chat_turn", "noise", session_id="s1", turn_id="t1")
    audit.log("specialist_consult", "noise2", session_id="s2")

    s = audit.usage_summary()
    t = s["totals"]
    assert t["calls"] == 2  # only the two cache_events, spanning two sessions
    assert t["input_tokens"] == 150
    assert t["output_tokens"] == 30
    assert t["cache_read_input_tokens"] == 900
    assert t["cache_creation_input_tokens"] == 200
    assert t["cost_usd"] == pytest.approx(0.06)

    by_model = {m["model"]: m for m in s["by_model"]}
    assert set(by_model) == {"claude-opus-4-8", "claude-sonnet-4-6"}
    assert by_model["claude-opus-4-8"]["input_tokens"] == 100
    assert by_model["claude-opus-4-8"]["cost_usd"] == pytest.approx(0.05)
    assert by_model["claude-sonnet-4-6"]["cache_creation_input_tokens"] == 200


def test_usage_summary_tolerates_missing_token_and_cost_fields(audit: AuditLogger) -> None:
    audit.log("cache_event", "odd", session_id="s1", turn_id="t1",
              details={"input_tokens": None, "output_tokens": "x"})  # no cost_usd, no model
    s = audit.usage_summary()
    assert s["totals"]["calls"] == 1
    assert s["totals"]["input_tokens"] == 0
    assert s["totals"]["output_tokens"] == 0
    assert s["totals"]["cost_usd"] == 0.0
    assert s["by_model"][0]["model"] == "unknown"


def test_usage_summary_groups_by_utc_day(tmp_path: Path) -> None:
    db = tmp_path / "audit.db"
    audit = AuditLogger(db)
    _insert_cache_event(db, "2026-06-01T10:00:00.000000Z", session_id="s1",
                        model="m", details={"input_tokens": 10, "cost_usd": 0.1})
    _insert_cache_event(db, "2026-06-01T23:00:00.000000Z", session_id="s2",
                        model="m", details={"input_tokens": 5, "cost_usd": 0.2})
    _insert_cache_event(db, "2026-06-02T08:00:00.000000Z", session_id="s3",
                        model="m", details={"input_tokens": 7, "cost_usd": 0.3})

    by_day = {d["day"]: d for d in audit.usage_summary()["by_day"]}
    assert list(by_day) == ["2026-06-01", "2026-06-02"]  # ascending
    assert by_day["2026-06-01"]["input_tokens"] == 15
    assert by_day["2026-06-01"]["calls"] == 2
    assert by_day["2026-06-01"]["cost_usd"] == pytest.approx(0.3)
    assert by_day["2026-06-02"]["input_tokens"] == 7


def test_usage_summary_respects_time_window(tmp_path: Path) -> None:
    db = tmp_path / "audit.db"
    audit = AuditLogger(db)
    _insert_cache_event(db, "2026-06-01T10:00:00.000000Z", session_id="s1",
                        model="m", details={"input_tokens": 10})
    _insert_cache_event(db, "2026-06-05T10:00:00.000000Z", session_id="s2",
                        model="m", details={"input_tokens": 99})
    _insert_cache_event(db, "2026-06-09T10:00:00.000000Z", session_id="s3",
                        model="m", details={"input_tokens": 7})

    # since alone (lower bound) keeps the two later rows.
    after = audit.usage_summary(since="2026-06-04T00:00:00.000000Z")
    assert after["totals"]["calls"] == 2
    assert after["totals"]["input_tokens"] == 106

    # until alone (upper bound) keeps the two earlier rows.
    before = audit.usage_summary(until="2026-06-06T00:00:00.000000Z")
    assert before["totals"]["calls"] == 2
    assert before["totals"]["input_tokens"] == 109

    # both bounds isolate the single middle row.
    windowed = audit.usage_summary(
        since="2026-06-04T00:00:00.000000Z", until="2026-06-06T00:00:00.000000Z"
    )
    assert windowed["totals"]["calls"] == 1
    assert windowed["totals"]["input_tokens"] == 99
