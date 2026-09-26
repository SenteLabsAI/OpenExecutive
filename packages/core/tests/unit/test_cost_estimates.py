"""What the model calls cost: list prices (audit.pricing) and the estimates
the usage summary adds to every total and breakdown."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from openexecutive.audit import pricing
from openexecutive.audit.logger import AuditLogger, set_audit_logger
from openexecutive.audit.usage import log_model_usage


@pytest.mark.parametrize(
    ("recorded", "key"),
    [
        ("claude-sonnet-5", "claude-sonnet-5"),
        ("Claude-Haiku-4-5-20251001", "claude-haiku-4-5"),
        ("anthropic/claude-sonnet-4.5", "claude-sonnet-4-5"),  # OpenRouter
        ("us.anthropic.claude-sonnet-4-5-20250929-v1:0", "claude-sonnet-4-5"),  # Bedrock
        ("claude-opus-4-5@20251101", "claude-opus-4-5"),  # Vertex
        ("claude-opus-5[1m]", "claude-opus-5"),
    ],
)
def test_recorded_model_names_map_to_price_keys(recorded: str, key: str) -> None:
    assert pricing.normalize_model(recorded) == key
    assert pricing.price_for(recorded) is pricing.PRICES[key]


def test_a_newer_model_is_not_priced_as_its_older_neighbour() -> None:
    assert pricing.price_for("claude-opus-5-5") == pricing.ModelPrice(4.0, 20.0, 0.20)
    assert pricing.price_for("claude-opus-5") == pricing.ModelPrice(5.0, 25.0, 0.50)
    # A model the table does not know is unpriced, not priced like a relative.
    assert pricing.price_for("claude-opus-5-9") is None
    assert pricing.price_for("llama3.3") is None
    assert pricing.estimate_cost("llama3.3", input_tokens=1_000_000) is None


def test_list_prices_per_million_tokens() -> None:
    million = 1_000_000
    sonnet = "claude-sonnet-5"
    assert pricing.estimate_cost(sonnet, input_tokens=million) == pytest.approx(2.0)
    assert pricing.estimate_cost(sonnet, output_tokens=million) == pytest.approx(10.0)
    assert pricing.estimate_cost(sonnet, cache_read_tokens=million) == pytest.approx(0.20)
    assert pricing.estimate_cost(sonnet, cache_write_5m_tokens=million) == pytest.approx(2.50)
    assert pricing.estimate_cost(sonnet, cache_write_1h_tokens=million) == pytest.approx(4.0)
    assert pricing.estimate_cost(sonnet, web_searches=1000) == pytest.approx(10.0)
    # Cache reads are not 0.1x input everywhere.
    assert pricing.estimate_cost("claude-fable-5-1", cache_read_tokens=million) == pytest.approx(0.25)
    assert pricing.estimate_cost("claude-opus-5-5", cache_read_tokens=million) == pytest.approx(0.20)


# ---------------------------------------------------------------------------
# The usage summary
# ---------------------------------------------------------------------------


@pytest.fixture
def audit(tmp_path: Path) -> Iterator[AuditLogger]:
    logger = AuditLogger(tmp_path / "audit.db")
    set_audit_logger(logger)
    yield logger
    set_audit_logger(None)


def _call(model: str, actor: str, **usage: object) -> None:
    fields = {"input_tokens": 0, "output_tokens": 0, **usage}
    log_model_usage(
        SimpleNamespace(usage=SimpleNamespace(**fields), stop_reason="end_turn"),
        model=model,
        actor=actor,
    )


def test_every_total_and_breakdown_carries_an_estimate(audit: AuditLogger) -> None:
    _call("claude-sonnet-5", "executive", input_tokens=1_000_000)  # $2.00
    _call("claude-opus-5", "specialist", output_tokens=100_000)  # $2.50
    # An older row: a cache write with no lifetime recorded counts as 1 hour.
    _call("claude-sonnet-5", "triage", cache_creation_input_tokens=1_000_000)  # $4.00
    # OpenRouter reported what it charged: that wins over the tokens.
    _call("anthropic/claude-sonnet-5", "executive", input_tokens=1_000_000, cost="0.30")
    # A local model has no list price: counted, never treated as free.
    _call("llama3.3", "executive", input_tokens=5_000)

    usage = audit.usage_summary()
    totals = usage["totals"]
    assert totals["estimated_cost_usd"] == pytest.approx(8.80)
    assert totals["cost_usd"] == pytest.approx(0.30)  # only what was charged
    assert totals["unpriced_calls"] == 1

    by_model = {r["model"]: r for r in usage["by_model"]}
    assert by_model["claude-sonnet-5"]["estimated_cost_usd"] == pytest.approx(6.0)
    assert by_model["anthropic/claude-sonnet-5"]["estimated_cost_usd"] == pytest.approx(0.30)
    assert (by_model["llama3.3"]["estimated_cost_usd"], by_model["llama3.3"]["unpriced_calls"]) == (0.0, 1)

    by_source = {r["source"]: r for r in usage["by_source"]}
    assert by_source["executive"]["estimated_cost_usd"] == pytest.approx(2.30)
    assert by_source["executive"]["unpriced_calls"] == 1
    assert by_source["triage"]["estimated_cost_usd"] == pytest.approx(4.0)
    assert [d["estimated_cost_usd"] for d in usage["by_day"]] == [pytest.approx(8.80)]


def test_a_recorded_cache_write_split_is_priced_by_lifetime(audit: AuditLogger) -> None:
    _call(
        "claude-sonnet-5", "executive",
        cache_creation_input_tokens=2_000_000,
        cache_creation=SimpleNamespace(
            ephemeral_5m_input_tokens=1_000_000, ephemeral_1h_input_tokens=1_000_000
        ),
    )
    assert audit.usage_summary()["totals"]["estimated_cost_usd"] == pytest.approx(2.50 + 4.0)


def test_an_empty_window_estimates_nothing(audit: AuditLogger) -> None:
    totals = audit.usage_summary(since="2999-01-01")["totals"]
    assert (totals["estimated_cost_usd"], totals["unpriced_calls"]) == (0.0, 0)


def test_the_spend_estimate_matches_the_summary_totals(audit: AuditLogger) -> None:
    _call("claude-sonnet-5", "executive", input_tokens=1_000_000)
    _call("claude-sonnet-5", "triage", output_tokens=100_000)
    _call("anthropic/claude-sonnet-5", "executive", input_tokens=1_000_000, cost="0.30")
    _call("llama3.3", "executive", input_tokens=5_000)
    totals = audit.usage_summary(since="2000-01-01")["totals"]
    assert audit.estimated_spend(since="2000-01-01") == (
        pytest.approx(totals["estimated_cost_usd"]), totals["unpriced_calls"],
    )
    assert audit.estimated_spend(since="2999-01-01") == (0.0, 0)


def test_negative_counts_and_charges_add_nothing(audit: AuditLogger) -> None:
    """The monthly limit acts on the estimate: a row with a negative charge
    or token count must not lower it."""
    _call("claude-sonnet-5", "executive", input_tokens=1_000_000)  # $2.00
    _call("anthropic/claude-sonnet-5", "executive", cost="-1000000")
    _call("claude-sonnet-5", "executive", input_tokens=-100_000_000)
    assert audit.usage_summary()["totals"]["estimated_cost_usd"] == pytest.approx(2.0)
    assert audit.estimated_spend(since="2000-01-01")[0] == pytest.approx(2.0)
