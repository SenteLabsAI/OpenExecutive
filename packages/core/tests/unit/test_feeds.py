"""Tests for the read-only operating-data feeds (no network)."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from openexecutive import feeds
from openexecutive.feeds import billing_feed
from openexecutive.feeds.base import FeedResult
from openexecutive.feeds.billing_feed import BillingFeed
from openexecutive.feeds.linear_feed import LinearFeed
from openexecutive.feeds.stripe_feed import StripeFeed

SECRET = "rk_live_SUPER_SECRET_VALUE"


def _settings(**overrides: Any) -> SimpleNamespace:
    base = {
        "linear_api_key": None,
        "stripe_readonly_key": None,
        "billing_export_table": None,
        "billing_query_project": None,
        "feed_timeout_seconds": 2.0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture()
def settings(monkeypatch: pytest.MonkeyPatch):
    holder = {"value": _settings()}
    monkeypatch.setattr(
        "openexecutive.config.get_settings", lambda: holder["value"]
    )

    def set_(**kw: Any) -> None:
        holder["value"] = _settings(**kw)

    return set_


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Not configured / failure handling (shared behaviour)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("feed", "needle"),
    [
        (StripeFeed(), "STRIPE_READONLY_KEY"),
        (LinearFeed(), "LINEAR_API_KEY"),
        (BillingFeed(), "BILLING_EXPORT_TABLE"),
    ],
)
def test_unconfigured_feed_reports_gap_and_never_calls_out(settings, feed, needle) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("must not make a network call when unconfigured")

    result = run(feed.fetch(_client(handler)))
    assert result.status == "not_configured"
    assert needle in result.text
    assert "Do not estimate" in result.text


def test_http_error_hides_body_and_credentials(settings) -> None:
    settings(stripe_readonly_key=SECRET)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": f"bad key {SECRET}"}}, request=request)

    result = run(StripeFeed().fetch(_client(handler)))
    assert result.status == "error"
    assert "401" in result.text
    assert SECRET not in result.render()


def test_unexpected_exception_is_soft_and_names_only_the_type(settings) -> None:
    settings(linear_api_key=SECRET)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"boom {SECRET}")

    result = run(LinearFeed().fetch(_client(handler)))
    assert result.status == "error"
    assert "ConnectError" in result.text
    assert SECRET not in result.render()


# --------------------------------------------------------------------------- #
# Stripe
# --------------------------------------------------------------------------- #

def _stripe_handler(request: httpx.Request) -> httpx.Response:
    assert request.headers["Authorization"] == f"Bearer {SECRET}"
    assert request.method == "GET"
    path = request.url.path
    if path == "/v1/balance":
        body = {
            "available": [{"currency": "usd", "amount": 1250000}],
            "pending": [{"currency": "usd", "amount": 5000}],
        }
    elif path == "/v1/subscriptions":
        body = {
            "has_more": False,
            "data": [
                {"items": {"data": [{"quantity": 2, "price": {"currency": "usd", "unit_amount": 10000, "recurring": {"interval": "month", "interval_count": 1}}}]}},
                {"items": {"data": [{"quantity": 1, "price": {"currency": "usd", "unit_amount": 120000, "recurring": {"interval": "year", "interval_count": 1}}}]}},
                {"items": {"data": [{"quantity": 1, "price": {"currency": "usd", "unit_amount": None, "recurring": {"interval": "month"}}}]}},
            ],
        }
    elif path == "/v1/invoices":
        body = {"has_more": False, "data": [{"currency": "usd", "amount_remaining": 20000}]}
    else:  # pragma: no cover
        raise AssertionError(path)
    return httpx.Response(200, json=body, request=request)


def test_stripe_snapshot_math(settings) -> None:
    settings(stripe_readonly_key=SECRET)
    result = run(StripeFeed().fetch(_client(_stripe_handler)))
    assert result.status == "ok"
    text = result.text
    assert "available 12,500.00 USD" in text
    assert "pending 50.00 USD" in text
    assert "Active subscriptions: 3" in text
    # 2 x $100/mo + $1,200/yr (=$100/mo); the metered/tiered item is skipped.
    assert "Estimated MRR USD: 300.00 USD" in text
    assert "Open invoice amount USD: 200.00 USD" in text
    assert SECRET not in result.render()


# --------------------------------------------------------------------------- #
# Linear
# --------------------------------------------------------------------------- #

def test_linear_snapshot(settings) -> None:
    settings(linear_api_key=SECRET)
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == SECRET
        payload = json.loads(request.content)
        seen.append(payload)
        if "ActiveCycles" in payload["query"]:
            data = {
                "teams": {
                    "nodes": [
                        {
                            "key": "BUILD",
                            "name": "Build",
                            "activeCycle": {
                                "number": 7,
                                "startsAt": "2026-10-05T00:00:00Z",
                                "endsAt": "2026-10-19T00:00:00Z",
                                "issues": {
                                    "nodes": [
                                        {"state": {"type": "completed"}},
                                        {"state": {"type": "completed"}},
                                        {"state": {"type": "started"}},
                                        {"state": {"type": "unstarted"}},
                                    ]
                                },
                            },
                        },
                        {"key": "OPS", "name": "Ops", "activeCycle": None},
                    ]
                }
            }
        else:
            data = {
                "issues": {
                    "nodes": [
                        {
                            "identifier": "BUILD-63",
                            "title": "Read images over WhatsApp",
                            "updatedAt": "2026-09-30T10:00:00Z",
                            "assignee": {"name": "Dingus"},
                            "team": {"key": "BUILD"},
                        }
                    ]
                }
            }
        return httpx.Response(200, json={"data": data}, request=request)

    result = run(LinearFeed().fetch(_client(handler)))
    assert result.status == "ok"
    assert "Build — cycle 7 (ends 2026-10-19): 2/4 done (50%)" in result.text
    assert "Ops: no active cycle." in result.text
    assert "BUILD-63" in result.text and "Dingus" in result.text
    # The staleness filter is an ISO timestamp roughly a week back.
    stale_vars = next(p["variables"] for p in seen if "StaleInProgress" in p["query"])
    assert stale_vars["before"].startswith("20")


def test_linear_graphql_error_becomes_a_gap(settings) -> None:
    settings(linear_api_key=SECRET)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errors": [{"message": "Field 'x' not found"}]}, request=request)

    result = run(LinearFeed().fetch(_client(handler)))
    assert result.status == "error"
    assert "RuntimeError" in result.text


# --------------------------------------------------------------------------- #
# Billing (BigQuery export)
# --------------------------------------------------------------------------- #

def test_billing_rejects_unsafe_table_reference(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    settings(billing_export_table="proj.ds.tbl`; DROP TABLE x; --")
    monkeypatch.setattr(billing_feed, "_adc_token", lambda: ("tok", "proj"))

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("must not query with an invalid table")

    result = run(BillingFeed().fetch(_client(handler)))
    assert result.status == "error"
    assert "ValueError" in result.text


def test_billing_snapshot(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    settings(billing_export_table="proj.billing.gcp_billing_export_v1_ABC")
    monkeypatch.setattr(billing_feed, "_adc_token", lambda: ("tok", "proj"))
    queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer tok"
        assert "/projects/proj/queries" in request.url.path
        sql = json.loads(request.content)["query"]
        queries.append(sql)
        if "GROUP BY service" in sql:
            body = {
                "jobComplete": True,
                "schema": {"fields": [{"name": "service"}, {"name": "currency"}, {"name": "last_7d"}, {"name": "prior_7d"}]},
                "rows": [
                    {"f": [{"v": "Cloud Run"}, {"v": "USD"}, {"v": "30.0"}, {"v": "20.0"}]},
                    {"f": [{"v": "Vertex AI"}, {"v": "USD"}, {"v": "12.5"}, {"v": "0"}]},
                ],
            }
        else:
            body = {
                "jobComplete": True,
                "schema": {"fields": [{"name": "currency"}, {"name": "mtd"}]},
                "rows": [{"f": [{"v": "USD"}, {"v": "412.34"}]}],
            }
        return httpx.Response(200, json=body, request=request)

    result = run(BillingFeed().fetch(_client(handler)))
    assert result.status == "ok"
    assert "Month-to-date net spend: 412.34 USD" in result.text
    assert "Cloud Run: 30.00 (+50%)" in result.text
    assert "Vertex AI: 12.50 (new)" in result.text
    assert all("proj.billing.gcp_billing_export_v1_ABC" in q for q in queries)


def test_billing_incomplete_job_is_a_gap(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    settings(billing_export_table="proj.billing.tbl")
    monkeypatch.setattr(billing_feed, "_adc_token", lambda: ("tok", "proj"))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jobComplete": False}, request=request)

    result = run(BillingFeed().fetch(_client(handler)))
    assert result.status == "error"


# --------------------------------------------------------------------------- #
# build_operating_context
# --------------------------------------------------------------------------- #

def test_context_for_unmapped_department_is_empty() -> None:
    assert run(feeds.build_operating_context("marketing")) == ""
    assert run(feeds.build_operating_context("nope")) == ""


def test_context_lists_gaps_for_finance_when_nothing_configured(settings) -> None:
    block = run(feeds.build_operating_context("finance"))
    assert block.startswith("<operating_data>") and block.endswith("</operating_data>")
    assert "GCP billing — not_configured" in block
    assert "Stripe — not_configured" in block
    assert "Linear" not in block  # not mapped to finance


def test_context_survives_a_feed_that_hangs(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    settings(feed_timeout_seconds=0.01)

    async def hang(self, client=None):  # noqa: ANN001
        await asyncio.sleep(5)
        return FeedResult(name="x", status="ok", text="never", fetched_at="")

    monkeypatch.setattr(StripeFeed, "fetch", hang)
    monkeypatch.setattr(BillingFeed, "fetch", hang)
    # Shrink the +5s grace used by the builder so the test stays fast.
    monkeypatch.setattr(
        feeds, "_fetch_one", _fast_fetch_one(feeds._fetch_one)
    )

    block = run(feeds.build_operating_context("finance"))
    assert "Timed out" in block


def _fast_fetch_one(orig):
    async def wrapper(feed, timeout):
        return await orig(feed, 0.05)

    return wrapper


def test_strategy_sees_all_three_feeds(settings) -> None:
    block = run(feeds.build_operating_context("strategy"))
    for name in ("GCP billing", "Stripe", "Linear"):
        assert f"## {name}" in block
