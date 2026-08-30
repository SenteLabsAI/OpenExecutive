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


def test_no_magnitude_leaves_the_field_unset() -> None:
    profile = build_profile_from_answers({"business_model": "A boutique consultancy"})

    assert "annual_revenue_arr" not in profile
    assert profile["target_customer"]["pain_points"] == []
