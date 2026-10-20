"""Tests for the privacy backstop on the free-text broadcast tools.

Defence-in-depth for the persona's "Choosing Who to Tell" rule: board / comp /
legal-class matters must be DM'd to the scope-holder, never broadcast to a
channel. Curated for precision — legitimate broadcasts (revenue, onboarding,
competitor, compliance) must NOT be blocked.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from openexecutive.audit import logger as audit_logger
from openexecutive.audit.logger import AuditLogger
from openexecutive.orchestrator.broadcast_tools import (
    _privacy_backstop_violation,
    handle_send_company_broadcast,
    handle_send_department_message,
)


@pytest.fixture(autouse=True)
def _isolated_audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Audit rows go to a per-test DB, never the default ./episodic_memory.db:
    a file left there without a people table makes later modules' principal
    checks fail closed (CLAUDE.md, "Audit-log test pollution")."""
    monkeypatch.setattr(audit_logger, "_default_logger", AuditLogger(db_path=tmp_path / "audit.db"))


def test_guard_flags_sensitive_terms() -> None:
    assert _privacy_backstop_violation("we should discuss the layoffs") == "layoffs"
    assert _privacy_backstop_violation("severance details attached") == "severance"
    assert _privacy_backstop_violation("the board deck is ready") == "board deck"
    assert _privacy_backstop_violation("new valuation from the round") == "valuation"
    assert _privacy_backstop_violation("updated compensation bands") == "compensation"
    assert _privacy_backstop_violation("the term sheet arrived") == "term sheet"


def test_guard_has_no_false_positives_on_legit_broadcasts() -> None:
    # Word-boundary + curated set: none of these legit broadcasts may trip.
    for ok_text in [
        "onboarding update for new hires",   # 'board' substring inside 'onboarding'
        "Q3 revenue is up 20%, great work",  # 'revenue' is too common to gate
        "competitor analysis shipped",       # 'comp' substring inside 'competitor'
        "compliance training reminder",      # 'comp' substring inside 'compliance'
        "the company all-hands is Friday",   # 'comp' substring inside 'company'
        "Q3 plan shipped — nice work team",
    ]:
        assert _privacy_backstop_violation(ok_text) is None, ok_text


def test_company_broadcast_refuses_sensitive_text() -> None:
    out = json.loads(
        asyncio.run(
            handle_send_company_broadcast(
                {"integration": "slack", "text": "heads up: layoffs next week"}
            )
        )
    )
    assert "error" in out
    assert "privacy" in out["error"].lower()
    assert "layoff" in out["error"].lower()


def test_department_message_refuses_sensitive_text() -> None:
    out = json.loads(
        asyncio.run(
            handle_send_department_message(
                {
                    "department_slug": "marketing",
                    "integration": "slack",
                    "text": "the compensation bands changed",
                }
            )
        )
    )
    assert "error" in out
    assert "compensation" in out["error"].lower()


def test_non_sensitive_text_passes_guard_then_hits_channel_error() -> None:
    # 'revenue' is intentionally not gated. The guard lets it through and the
    # handler fails LATER on missing channel config — NOT a privacy refusal.
    out = json.loads(
        asyncio.run(
            handle_send_company_broadcast(
                {"integration": "slack", "text": "Q3 revenue up 20 percent"}
            )
        )
    )
    assert "error" in out
    assert "privacy" not in out["error"].lower()
    assert "channel" in out["error"].lower()


def test_guard_matches_plural_forms() -> None:
    # Trailing-s tolerance: plurals must NOT bypass the backstop (the
    # false-negative direction caught in review).
    assert _privacy_backstop_violation("two lawsuits were filed") == "lawsuits"
    assert _privacy_backstop_violation("the board meetings this quarter") == "board meetings"
    assert _privacy_backstop_violation("valuations came in low") == "valuations"
    assert _privacy_backstop_violation("equity grants for the team") == "equity grants"


def test_guard_matches_rif_and_down_round() -> None:
    assert _privacy_backstop_violation("we're planning a RIF") == "rif"
    assert _privacy_backstop_violation("it ended up a down round") == "down round"


def test_guard_word_boundary_holds_after_plural_and_acronym() -> None:
    # The s?/boundary additions must not create new false positives.
    for ok_text in [
        "rifle range is booked",        # 'rif' inside 'rifle'
        "markdown round-trip is done",  # 'down round' inside 'markdown round'
        "rounds of feedback incoming",  # 'round' is not a term
    ]:
        assert _privacy_backstop_violation(ok_text) is None, ok_text
