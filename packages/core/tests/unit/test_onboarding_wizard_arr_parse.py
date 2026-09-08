"""ARR parsing in the onboarding wizard's business-model step.

Regression test for a crash that aborted onboarding entirely. The ARR
heuristic searched ``\\$?([\\d,]+)\\s*[Mm]``, whose character class matches a
bare comma with no digits. Any business-model answer containing a comma
followed by a word starting with M captured ``","``, which
``.replace(",", "")`` reduced to the empty string, and ``float("")`` raised
``ValueError`` out of ``build_profile_from_answers``. The wizard returned 500,
the profile was never written, and every subsequent step failed.

The trigger is an ordinary sentence, not a malformed one — see the parametrised
cases below.
"""
from __future__ import annotations

import pytest

from openexecutive.onboarding.wizard import build_profile_from_answers


@pytest.mark.parametrize(
    "text",
    [
        "Consultoría estratégica, marketing y operaciones",
        "Servicios profesionales, mantenimiento de sistemas",
        "Vendemos software B2B, modelo de suscripción",
        "We sell software, mostly to mid-market teams",
        "Design and build, maintenance included",
        # The exact answer from issue #84.
        "IT, marketing and video agency",
    ],
)
def test_comma_before_m_word_does_not_crash(text: str) -> None:
    """A comma followed by an m-word must not be read as a magnitude."""
    profile = build_profile_from_answers({"business_model": text})

    assert profile["target_customer"]["profile"] == text
    assert "annual_revenue_arr" not in profile


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("We do $2M in ARR", 2_000_000.0),
        ("Roughly 12M annually", 12_000_000.0),
        ("ARR is $1,500M across all lines", 1_500_000_000.0),
        # Phrasings the bare `[Mm]\b` narrowing silently dropped.
        ("roughly 50 million in revenue", 50_000_000.0),
        ("About 3 Million ARR", 3_000_000.0),
        ("$50MM ARR last year", 50_000_000.0),
        ("we closed the year at 7mm", 7_000_000.0),
    ],
)
def test_magnitude_still_parses(text: str, expected: float) -> None:
    """A real magnitude is still picked up after the narrowing."""
    profile = build_profile_from_answers({"business_model": text})

    assert profile["annual_revenue_arr"] == expected


def test_digits_before_an_m_word_are_not_a_magnitude() -> None:
    """The word boundary keeps "300 clients, marketing" from meaning $300M.

    Without it the capture starts at a real digit, so the crash guard alone
    would not help — it would silently record a fabricated ARR instead.
    """
    profile = build_profile_from_answers(
        {"business_model": "We have 300 clients, marketing is word of mouth"}
    )

    assert "annual_revenue_arr" not in profile


@pytest.mark.parametrize(
    "text",
    [
        "$5 minimum order, mostly SMBs",
        "We have 300 clients, marketing is word of mouth",
        "12 major accounts and growing",
    ],
)
def test_digits_before_a_non_magnitude_m_word_are_ignored(text: str) -> None:
    """The word boundary must hold across the whole alternation."""
    profile = build_profile_from_answers({"business_model": text})

    assert "annual_revenue_arr" not in profile


def test_no_magnitude_leaves_the_field_unset() -> None:
    profile = build_profile_from_answers({"business_model": "A boutique consultancy"})

    assert "annual_revenue_arr" not in profile
    assert profile["target_customer"]["pain_points"] == []


def test_long_digit_run_is_linear_time() -> None:
    """A "1,1,1,…" run with no magnitude after it must not go quadratic.

    Every digit in the run used to be a candidate match start, so a 32k-char
    answer took ~10s and a 320k one ~20 minutes on the event loop. With the
    lookbehind only the run's first digit is a candidate. If this regresses
    the test does not fail, it hangs — which is the point.
    """
    text = "1," * 100_000
    profile = build_profile_from_answers({"business_model": text, "financials": text})

    assert "annual_revenue_arr" not in profile
    assert "burn_rate_monthly" not in profile["financials"]


def test_long_whitespace_run_is_linear_time() -> None:
    """A digit followed by a long space run must not go quadratic either.

    The burn-rate pattern once had `\\s*[Kk]?\\s*`; with the K absent the two
    `\\s*` could split the run every possible way (400ms at 10k chars, 6s at
    40k). Sized past the API's answer bound so it does not depend on it.
    """
    text = "1" + " " * 200_000
    profile = build_profile_from_answers({"business_model": text, "financials": text})

    assert "annual_revenue_arr" not in profile
    assert "burn_rate_monthly" not in profile["financials"]


def test_long_digit_run_without_commas_is_linear_time() -> None:
    """Pure digits hit the runway and headcount parsers, not only ARR/burn.

    `(\\d+)\\s*month` without a lookbehind was quadratic (750ms at 10k),
    and int() on a 5000-digit headcount raises at Python's digit limit.
    """
    text = "9" * 200_000
    profile = build_profile_from_answers(
        {"business_model": text, "financials": text, "headcount_and_founding": text}
    )

    assert "annual_revenue_arr" not in profile
    assert profile["financials"] == {}
    assert profile["headcount"] == 999_999_999
