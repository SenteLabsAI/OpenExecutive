"""Burn-rate parsing in the onboarding wizard's financials step.

Sibling of the ARR regression in ``test_onboarding_wizard_arr_parse.py``:
the burn-rate heuristic used the same ``[\\d,]+`` character class, so a bare
comma before "monthly", "/month", "per month" or "burn" captured ``","`` and
crashed ``build_profile_from_answers`` on ``float("")``.
"""
from __future__ import annotations

import pytest

from openexecutive.onboarding.wizard import build_profile_from_answers


@pytest.mark.parametrize(
    "text",
    [
        "Runway is fine, monthly costs are low",
        "We are profitable, burn is zero",
    ],
)
def test_comma_before_burn_keyword_does_not_crash(text: str) -> None:
    profile = build_profile_from_answers({"financials": text})

    assert "burn_rate_monthly" not in profile["financials"]


def test_comma_case_still_records_runway() -> None:
    """Used to crash on the bare comma before "monthly".

    The burn figure here follows its keyword, which the magnitude-first
    heuristic does not read; this test only pins the crash and the runway.
    """
    profile = build_profile_from_answers(
        {"financials": "12 months runway, monthly burn 100k"}
    )

    assert profile["financials"]["runway_months"] == 12.0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("burn 50k monthly", 50_000.0),
        ("$120,000 per month, 18 months runway", 120_000.0),
        ("We spend 30K/month", 30_000.0),
        ("about 2,500 monthly burn", 2_500.0),
    ],
)
def test_burn_rate_still_parses(text: str, expected: float) -> None:
    profile = build_profile_from_answers({"financials": text})

    assert profile["financials"]["burn_rate_monthly"] == expected
