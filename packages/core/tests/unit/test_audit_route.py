"""HTTP-level tests for /audit/logs."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import audit as audit_route
from openexecutive.audit import AuditLogger, set_audit_logger


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    audit = AuditLogger(tmp_path / "audit.db")
    app.state.audit = audit
    set_audit_logger(audit)
    app.include_router(audit_route.router)

    # Seed a few rows so the tests are not purely about empty responses.
    audit.log("chat_turn", "User: cash runway?", session_id="s1", actor="user")
    audit.log("specialist_consult", "Consulted finance: cash runway",
              session_id="s1", actor="finance",
              details={"duration_ms": 1234})
    audit.log("tool_invocation", "mcp:gmail.send to alice@example.com",
              session_id="s1", actor="executive")
    audit.log("integration_inbound", "Inbound email from bob@example.com: hello",
              actor="email")

    return TestClient(app)


def test_list_returns_all_with_metadata(client: TestClient) -> None:
    resp = client.get("/audit/logs")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 4
    assert data["limit"] == 100
    assert data["offset"] == 0
    assert len(data["items"]) == 4
    assert "chat_turn" in data["event_types"]


def test_filter_by_event_type(client: TestClient) -> None:
    resp = client.get("/audit/logs", params={"event_type": "specialist_consult"})
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["actor"] == "finance"
    assert data["items"][0]["details"]["duration_ms"] == 1234


def test_filter_by_session_id(client: TestClient) -> None:
    resp = client.get("/audit/logs", params={"session_id": "s1"})
    assert resp.json()["total"] == 3


def test_q_search_summary(client: TestClient) -> None:
    resp = client.get("/audit/logs", params={"q": "runway"})
    data = resp.json()
    assert data["total"] == 2
    for item in data["items"]:
        assert "runway" in item["summary"]


def test_pagination(client: TestClient) -> None:
    page1 = client.get("/audit/logs", params={"limit": 2, "offset": 0}).json()
    page2 = client.get("/audit/logs", params={"limit": 2, "offset": 2}).json()
    assert page1["total"] == 4
    assert len(page1["items"]) == 2
    assert len(page2["items"]) == 2
    ids = [i["id"] for i in page1["items"]] + [i["id"] for i in page2["items"]]
    assert len(set(ids)) == 4


def test_post_audit_log_creates_entry(client: TestClient) -> None:
    # The fixture pre-seeds 4 rows; auth_login starts at 0.
    assert client.get("/audit/logs", params={"event_type": "auth_login"}).json()["total"] == 0

    r = client.post(
        "/audit/log",
        json={"event_type": "auth_login", "summary": "Login: test@example.com", "actor": "test@example.com"},
    )
    assert r.status_code == 201
    assert isinstance(r.json()["id"], int)

    after = client.get("/audit/logs", params={"event_type": "auth_login"}).json()
    assert after["total"] == 1
    assert after["items"][0]["event_type"] == "auth_login"
    assert after["items"][0]["actor"] == "test@example.com"
    assert after["items"][0]["summary"] == "Login: test@example.com"


def test_post_audit_log_rejects_unknown_event_type(client: TestClient) -> None:
    r = client.post("/audit/log", json={"event_type": "bogus_type", "summary": "x"})
    assert r.status_code == 422


def test_post_audit_log_refuses_usage_rows(client: TestClient) -> None:
    """The monthly AI limit acts on cache_event rows, so a caller could lift
    the owner's budget pause with a negative cost, or hold it forever with a
    huge one."""
    r = client.post(
        "/audit/log",
        json={"event_type": "cache_event", "summary": "x",
              "details": {"model": "claude-sonnet-5", "cost_usd": -1_000_000}},
    )
    assert r.status_code == 403
    assert client.get("/audit/logs", params={"event_type": "cache_event"}).json()["total"] == 0


def _session_client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    audit = AuditLogger(tmp_path / "audit.db")
    app.state.audit = audit
    set_audit_logger(audit)
    app.include_router(audit_route.router)
    return TestClient(app)


def test_session_cost_summary_aggregates_cache_events(tmp_path: Path) -> None:
    client = _session_client(tmp_path)
    audit = client.app.state.audit  # type: ignore[attr-defined]

    # Two turns, each with cache_event rows carrying token details.
    audit.log("cache_event", "t1 call1", session_id="sc", turn_id="t1",
              details={"input_tokens": 100, "output_tokens": 20,
                       "cache_read_input_tokens": 900, "cache_creation_input_tokens": 0})
    audit.log("cache_event", "t1 call2", session_id="sc", turn_id="t1",
              details={"input_tokens": 50, "output_tokens": 10,
                       "cache_read_input_tokens": 0, "cache_creation_input_tokens": 200})
    audit.log("cache_event", "t2 call1", session_id="sc", turn_id="t2",
              details={"input_tokens": 30, "output_tokens": 5,
                       "cache_read_input_tokens": 100, "cache_creation_input_tokens": 0})
    # A non-cache event must be ignored by the cost summary.
    audit.log("specialist_consult", "noise", session_id="sc", turn_id="t1")

    cs = client.get("/audit/sessions/sc").json()["cost_summary"]
    assert cs is not None
    assert cs["calls"] == 3
    assert cs["input_tokens"] == 180             # 100 + 50 + 30
    assert cs["output_tokens"] == 35             # 20 + 10 + 5
    assert cs["cache_read_input_tokens"] == 1000  # 900 + 0 + 100
    assert cs["cache_creation_input_tokens"] == 200
    assert len(cs["per_turn"]) == 2
    t1 = next(t for t in cs["per_turn"] if t["turn_id"] == "t1")
    assert t1["calls"] == 2
    assert t1["input_tokens"] == 150
    assert t1["output_tokens"] == 30
    # per_turn is chronological (t1 logged before t2) despite query() id DESC.
    assert [t["turn_id"] for t in cs["per_turn"]] == ["t1", "t2"]


def test_session_cost_summary_null_without_cache_events(tmp_path: Path) -> None:
    client = _session_client(tmp_path)
    audit = client.app.state.audit  # type: ignore[attr-defined]
    audit.log("chat_turn", "hi", session_id="snc", turn_id="t1")
    assert client.get("/audit/sessions/snc").json()["cost_summary"] is None


def test_session_cost_summary_tolerates_missing_token_fields(tmp_path: Path) -> None:
    # A cache_event with no/garbled token fields counts as 0, never errors.
    client = _session_client(tmp_path)
    audit = client.app.state.audit  # type: ignore[attr-defined]
    audit.log("cache_event", "odd", session_id="sodd", turn_id="t1",
              details={"input_tokens": None, "output_tokens": "x"})
    cs = client.get("/audit/sessions/sodd").json()["cost_summary"]
    assert cs is not None
    assert cs["calls"] == 1
    assert cs["input_tokens"] == 0
    assert cs["output_tokens"] == 0


def test_session_cost_summary_groups_rows_without_turn_id(tmp_path: Path) -> None:
    # Defensive path: a cache_event logged without turn_id groups under None.
    client = _session_client(tmp_path)
    audit = client.app.state.audit  # type: ignore[attr-defined]
    audit.log("cache_event", "no turn", session_id="snt",
              details={"input_tokens": 10, "output_tokens": 2})
    cs = client.get("/audit/sessions/snt").json()["cost_summary"]
    assert cs is not None
    assert len(cs["per_turn"]) == 1
    assert cs["per_turn"][0]["turn_id"] is None
    assert cs["per_turn"][0]["input_tokens"] == 10


def test_session_degradations_surfaces_memory_failures(tmp_path: Path) -> None:
    client = _session_client(tmp_path)
    audit = client.app.state.audit  # type: ignore[attr-defined]
    # Two timeouts (across two turns) + one error; normal outcomes ignored.
    audit.log("peer_memory", "prefetch timeout", session_id="sd", turn_id="t1",
              details={"op": "prefetch", "outcome": "timeout"})
    audit.log("peer_memory", "prefetch timeout", session_id="sd", turn_id="t2",
              details={"op": "prefetch", "outcome": "timeout"})
    audit.log("peer_memory", "prefetch error", session_id="sd", turn_id="t1",
              details={"op": "prefetch", "outcome": "error", "error_type": "ConnectError"})
    audit.log("peer_memory", "normal", session_id="sd", turn_id="t1",
              details={"op": "prefetch", "outcome": "ok"})
    audit.log("peer_memory", "disabled is not degraded", session_id="sd", turn_id="t1",
              details={"op": "sync_turn", "outcome": "disabled"})

    degs = client.get("/audit/sessions/sd").json()["degradations"]
    by_reason = {d["reason"]: d for d in degs}
    assert set(by_reason) == {"timeout", "error"}
    assert all(d["kind"] == "memory" for d in degs)
    assert by_reason["timeout"]["count"] == 2
    assert sorted(by_reason["timeout"]["turn_ids"]) == ["t1", "t2"]
    assert by_reason["error"]["count"] == 1
    assert by_reason["error"]["detail"] == "ConnectError"


def test_session_degradations_empty_when_clean(tmp_path: Path) -> None:
    client = _session_client(tmp_path)
    audit = client.app.state.audit  # type: ignore[attr-defined]
    audit.log("peer_memory", "ok", session_id="sc2", turn_id="t1",
              details={"op": "prefetch", "outcome": "ok"})
    audit.log("chat_turn", "hi", session_id="sc2", turn_id="t1")
    assert client.get("/audit/sessions/sc2").json()["degradations"] == []


# --------------------------------------------------------------------------- #
# GET /audit/usage — cross-session token + cost aggregate
# --------------------------------------------------------------------------- #


def test_usage_endpoint_aggregates_across_sessions(tmp_path: Path) -> None:
    client = _session_client(tmp_path)
    audit = client.app.state.audit  # type: ignore[attr-defined]
    # Two different sessions + two models — the per-session endpoint can't see
    # this span; the usage endpoint must.
    audit.log("cache_event", "s1 call", session_id="sA", turn_id="t1",
              details={"model": "claude-opus-4-8", "input_tokens": 100,
                       "output_tokens": 20, "cache_read_input_tokens": 900,
                       "cache_creation_input_tokens": 0, "cost_usd": 0.05})
    audit.log("cache_event", "s2 call", session_id="sB", turn_id="t2",
              details={"model": "claude-sonnet-4-6", "input_tokens": 50,
                       "output_tokens": 10, "cache_read_input_tokens": 0,
                       "cache_creation_input_tokens": 200, "cost_usd": 0.01})
    # Non-cache events are excluded.
    audit.log("chat_turn", "noise", session_id="sA", turn_id="t1")

    data = client.get("/audit/usage").json()
    assert data["totals"]["calls"] == 2
    assert data["totals"]["input_tokens"] == 150
    assert data["totals"]["output_tokens"] == 30
    assert data["totals"]["cache_read_input_tokens"] == 900
    assert data["totals"]["cost_usd"] == pytest.approx(0.06)

    by_model = {m["model"]: m for m in data["by_model"]}
    assert set(by_model) == {"claude-opus-4-8", "claude-sonnet-4-6"}
    assert by_model["claude-opus-4-8"]["cost_usd"] == pytest.approx(0.05)


def test_usage_endpoint_breaks_down_by_source(tmp_path: Path) -> None:
    client = _session_client(tmp_path)
    audit = client.app.state.audit  # type: ignore[attr-defined]
    audit.log("cache_event", "chat", session_id="sA", turn_id="t1", actor="executive",
              details={"model": "m", "input_tokens": 100, "web_search_requests": 1})
    audit.log("cache_event", "research", actor="specialist_research",
              details={"model": "m", "input_tokens": 40, "web_search_requests": 4})

    data = client.get("/audit/usage").json()
    assert data["totals"]["web_search_requests"] == 5
    by_source = {s["source"]: s for s in data["by_source"]}
    assert by_source["executive"]["calls"] == 1
    assert by_source["specialist_research"]["web_search_requests"] == 4


def test_usage_endpoint_cost_missing_rows_contribute_zero(tmp_path: Path) -> None:
    # A row predating cost capture (no cost_usd) must not break the sum.
    client = _session_client(tmp_path)
    audit = client.app.state.audit  # type: ignore[attr-defined]
    audit.log("cache_event", "with cost", session_id="sA", turn_id="t1",
              details={"model": "m", "input_tokens": 10, "cost_usd": 0.25})
    audit.log("cache_event", "no cost", session_id="sA", turn_id="t2",
              details={"model": "m", "input_tokens": 5})  # no cost_usd

    data = client.get("/audit/usage").json()
    assert data["totals"]["calls"] == 2
    assert data["totals"]["input_tokens"] == 15
    assert data["totals"]["cost_usd"] == pytest.approx(0.25)


def test_usage_endpoint_empty_db_is_all_zero(tmp_path: Path) -> None:
    client = _session_client(tmp_path)
    data = client.get("/audit/usage").json()
    assert data["totals"]["calls"] == 0
    assert data["totals"]["cost_usd"] == 0.0
    assert data["by_day"] == []
    assert data["by_model"] == []
