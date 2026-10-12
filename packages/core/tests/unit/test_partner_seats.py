"""Tests for the owner/partner seats and their prompts."""
from __future__ import annotations

import pytest

from openexecutive.agents.base import BaseAgent
from openexecutive.knowledge.retriever import DOMAIN_ALIASES
from openexecutive.mcp_server.server import SpecialistKey
from openexecutive.orchestrator.router import (
    SPECIALIST_DESCRIPTIONS,
    SPECIALIST_REGISTRY,
    SPECIALIST_TOOLS,
)
from openexecutive.prompts import partner_prompts as pp

SEATS = ("cfo", "coo", "cmo", "cpo", "ceo_advisor")


def test_roster_has_owner_and_partner_seats() -> None:
    assert {"cfo", "coo", "gc", "cmo", "cpo", "ceo_advisor", "talent", "triage"} == set(
        SPECIALIST_REGISTRY
    )
    # Every registered seat is described, and the tool enum is generated sorted
    # (prompt-cache requirement: tool definitions must be byte-stable).
    assert set(SPECIALIST_DESCRIPTIONS) == set(SPECIALIST_REGISTRY)
    enum = SPECIALIST_TOOLS[0]["input_schema"]["properties"]["specialist"]["enum"]
    assert enum == sorted(enum)


def test_every_seat_is_wired_into_knowledge_and_mcp() -> None:
    for seat in SEATS:
        assert seat in DOMAIN_ALIASES
    assert set(SPECIALIST_REGISTRY) == set(SpecialistKey.__args__)  # type: ignore[attr-defined]


@pytest.mark.parametrize("seat", SEATS)
def test_seat_prompt_is_byte_stable_and_nonempty(seat: str) -> None:
    agent: BaseAgent = SPECIALIST_REGISTRY[seat]
    first, second = agent.get_system_prompt(), agent.get_system_prompt()
    assert first == second and len(first) > 1000
    assert "{" not in first.replace("{{", "").replace("}}", "") or "{VOICE" not in first
    assert agent.name == seat


def test_owner_seats_extend_not_replace_the_upstream_prompts() -> None:
    from openexecutive.prompts.domain_prompts import CFO_PROMPT, COO_PROMPT

    cfo = SPECIALIST_REGISTRY["cfo"].get_system_prompt()
    coo = SPECIALIST_REGISTRY["coo"].get_system_prompt()
    assert cfo.startswith(CFO_PROMPT) and cfo.endswith(pp.CFO_OPERATING_ADDENDUM)
    assert coo.startswith(COO_PROMPT) and coo.endswith(pp.COO_OPERATING_ADDENDUM)
    assert "There is no human CFO" in cfo and "There is no human COO" in coo
    assert SPECIALIST_REGISTRY["cfo"].use_deep_reasoning is True


@pytest.mark.parametrize("seat", SEATS)
def test_every_seat_forbids_inventing_numbers_and_demands_owned_actions(seat: str) -> None:
    prompt = SPECIALIST_REGISTRY[seat].get_system_prompt()
    assert "<operating_data>" in prompt
    assert "Never estimate" in prompt
    assert "owner" in prompt and "date" in prompt


@pytest.mark.parametrize("seat", ("cmo", "cpo", "ceo_advisor"))
def test_partner_seats_defer_to_the_human_and_carry_the_priority_stack(seat: str) -> None:
    prompt = SPECIALIST_REGISTRY[seat].get_system_prompt()
    assert "Safety and legal exposure" in prompt
    assert "Privacy and trust" in prompt
    # Partners advise; they do not act on the human's behalf.
    assert any(w in prompt for w in ("decides", "the CEO decides", "CMO decides", "CPO decides"))
    assert "do not" in prompt.lower()


def test_ceo_advisor_is_challenger_first_with_a_scorecard() -> None:
    prompt = SPECIALIST_REGISTRY["ceo_advisor"].get_system_prompt()
    assert "Challenge before you support" in prompt
    assert "pre-mortem" in prompt.lower()
    for line in ("Runway", "Customer evidence", "Operational capacity", "Legal, privacy and safety"):
        assert line in prompt
    assert SPECIALIST_REGISTRY["ceo_advisor"].use_deep_reasoning is True
