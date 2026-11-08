"""Cache-manager tests for the org-context block.

Key invariants guarded here:
1. Block layout: [1h persona+knowledge, (opt 5m company+org)]
   — max 2 cache_control blocks here so tools + message cache fit in the
   API limit of 4 total.
2. Mutating a department OKR changes the 5m context block but NOT the
   1h persona+knowledge block.
3. No context block is emitted when both company profile and departments
   are absent / empty.
4. Context block content includes OKR key_result text and status.
5. Existing committee-cache safety tests still pass (non-regression).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

# Ensure settings resolves; avoids ImportError for ANTHROPIC_API_KEY.
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-used")

from openexecutive.departments import registry as dept_registry  # noqa: E402
from openexecutive.departments import store as dept_store  # noqa: E402
from openexecutive.people import registry as people_registry  # noqa: E402
from openexecutive.people import store as people_store  # noqa: E402
from openexecutive.prompts.cache_manager import build_system_blocks  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_depts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each test gets a fresh, isolated DB for departments + people, plus
    a cleared registry cache. Isolating people is required because
    render_org_block() renders a people section from the live people DB —
    without isolation, a real episodic_memory.db that has people seeded
    (e.g. from a `make dev` run) would leak into these tests.
    """
    path = tmp_path / "deps.db"
    monkeypatch.setattr(dept_store, "DB_PATH", path)
    monkeypatch.setattr(people_store, "DB_PATH", path)
    dept_registry.invalidate()
    people_registry.invalidate()
    # Don't call initialize_db here — tests that need it do so explicitly.
    yield
    dept_registry.invalidate()
    people_registry.invalidate()


# --------------------------------------------------------------------------- #
# Block structure
# --------------------------------------------------------------------------- #

def test_no_org_block_when_departments_missing() -> None:
    """Before initialize_db runs (e.g. test env without the DB), no context
    block is appended — build_system_blocks must never raise."""
    blocks = build_system_blocks()
    # Baseline shape: single persona+knowledge block (1h).
    assert len(blocks) == 1
    assert blocks[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


def test_no_org_block_when_departments_table_empty() -> None:
    """After initialize_db but before seeding, list_departments returns [] and
    render_org_block returns "", so no context block is appended."""
    dept_store.initialize_db()
    blocks = build_system_blocks()
    assert len(blocks) == 1


def test_org_block_appended_after_seeding(tmp_path: Path) -> None:
    """After seeding (no OKRs), a context block is appended because
    departments have charter missions — not completely empty."""
    dept_store.initialize_db()
    dept_store.seed_default_departments()
    blocks = build_system_blocks()
    # Should be exactly 2: persona+knowledge (1h), context (5m).
    assert len(blocks) == 2
    assert "Departments You Manage" in blocks[1]["text"]


def test_block_order_is_1h_then_5m(tmp_path: Path) -> None:
    """API constraint: 1h block must precede 5m block."""
    dept_store.initialize_db()
    dept_store.seed_default_departments()
    dept_store.insert_goal(
        "finance",
        period_value="Q2 2026",
        key_result="Close Series A",
        target="funds in account",
        current="term sheet signed",
        status="on_track",
    )
    dept_registry.invalidate()
    blocks = build_system_blocks()

    assert len(blocks) == 2
    assert blocks[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    # Second block is 5m (no explicit ttl key = ephemeral default = 5m).
    assert blocks[1]["cache_control"] == {"type": "ephemeral"}


def test_org_block_with_company_profile_still_ordered_correctly() -> None:
    """When a company profile is also supplied the ordering must be:
    persona+knowledge(1h), combined company+org(5m)."""
    from unittest.mock import MagicMock

    dept_store.initialize_db()
    dept_store.seed_default_departments()

    mock_profile = MagicMock()
    mock_profile.to_prompt_block.return_value = "## Company\nAcme Corp"

    blocks = build_system_blocks(company_profile=mock_profile)
    assert len(blocks) == 2
    ttls = [b["cache_control"].get("ttl") for b in blocks]
    assert ttls == ["1h", None]  # None = 5m default
    # Both company profile and org content must appear in the single 5m block.
    assert "## Company" in blocks[1]["text"]
    assert "Departments You Manage" in blocks[1]["text"]


# --------------------------------------------------------------------------- #
# Content correctness
# --------------------------------------------------------------------------- #

def test_org_block_contains_okr_key_result() -> None:
    dept_store.initialize_db()
    dept_store.seed_default_departments()
    dept_store.insert_goal(
        "finance",
        period_value="Q2 2026",
        key_result="Reduce monthly burn to below $550K",
        target="$550K",
        current="$612K",
        status="at_risk",
    )
    dept_registry.invalidate()
    blocks = build_system_blocks()
    org_text = blocks[1]["text"]

    assert "Reduce monthly burn to below $550K" in org_text
    assert "$612K" in org_text


def test_org_block_shows_authority_level() -> None:
    dept_store.initialize_db()
    dept_store.seed_default_departments()
    dept_registry.invalidate()
    blocks = build_system_blocks()
    org_text = blocks[1]["text"]
    # The Approvals level each department runs at (the suite runs at Careful).
    assert "escalate" in org_text


def test_org_block_respects_4000_char_cap() -> None:
    dept_store.initialize_db()
    dept_store.seed_default_departments()
    # Insert enough OKRs to push past 4000 chars.
    for i in range(40):
        dept_store.insert_goal(
            "finance",
            period_value=f"Q{(i % 4) + 1} 2026",
            key_result=f"{'x' * 120} OKR number {i}",
            target="t",
            current="c" * 50,
            status="on_track",
        )
    dept_registry.invalidate()
    blocks = build_system_blocks()
    # context block is always the last block (no company_profile passed here).
    assert len(blocks[1]["text"]) <= 4000


# --------------------------------------------------------------------------- #
# Cache-safety: persona bytes must not change when OKRs change
# --------------------------------------------------------------------------- #

def test_persona_block_bytes_unchanged_when_okr_mutated() -> None:
    """The 1h persona block must be bit-identical before and after an OKR edit.
    Any change here = every Executive turn misses the 1h cache = ~10x cost."""
    # Capture persona before adding any dept data.
    blocks_before = build_system_blocks()
    persona_before = blocks_before[0]["text"]

    # Add OKRs.
    dept_store.initialize_db()
    dept_store.seed_default_departments()
    dept_store.insert_goal(
        "finance",
        period_value="Q2 2026",
        key_result="New OKR that must NOT affect persona",
        target="x",
    )
    dept_registry.invalidate()

    blocks_after = build_system_blocks()
    persona_after = blocks_after[0]["text"]

    assert persona_before == persona_after, (
        "Persona block changed — the 1h cache key has shifted. "
        "Dynamic department data must go in the 5m org block, not the persona."
    )


def test_knowledge_block_bytes_unchanged_when_okr_mutated() -> None:
    """The 1h persona+knowledge block must be byte-identical before and after
    an OKR edit — knowledge index is now inlined in block[0]."""
    blocks_before = build_system_blocks()
    block0_before = blocks_before[0]["text"]

    dept_store.initialize_db()
    dept_store.seed_default_departments()
    dept_store.insert_goal(
        "strategy",
        period_value="Q3 2026",
        key_result="Enter APAC market",
        target="3 enterprise logos",
    )
    dept_registry.invalidate()

    blocks_after = build_system_blocks()
    block0_after = blocks_after[0]["text"]

    assert block0_before == block0_after


def test_build_system_blocks_is_deterministic_with_depts() -> None:
    """Two back-to-back calls with the same DB state must produce byte-equal
    blocks (mirrors test_committee_prompt_cache_safety requirement)."""
    dept_store.initialize_db()
    dept_store.seed_default_departments()
    dept_store.insert_goal(
        "operations",
        period_value="Q2 2026",
        key_result="Cut AWS spend by 20%",
        target="$40K/month",
        current="$50K/month",
        status="at_risk",
    )
    dept_registry.invalidate()

    a = build_system_blocks()
    # No invalidate between calls — second call uses cached registry state.
    b = build_system_blocks()

    assert a == b, "build_system_blocks is not deterministic — cache will thrash"


# --------------------------------------------------------------------------- #
# Persona addendum smoke check
# --------------------------------------------------------------------------- #

def test_okr_newlines_stripped_before_system_prompt() -> None:
    """Newlines in user-controlled OKR text must be stripped before the value
    lands in the system prompt — otherwise a malicious key_result could open a
    new Markdown section (`\\n## NEW HEADING`) at system-prompt trust level.

    The invariant: the org block must not contain any raw newline characters
    WITHIN an OKR field value. Newlines between OKR rows (structural) are fine;
    user-supplied text must be collapsed to spaces so `## Injected` can never
    start a new section within the rendered OKR text.
    """
    dept_store.initialize_db()
    dept_store.seed_default_departments()
    dept_store.insert_goal(
        "finance",
        period_value="Q2 2026",
        key_result="Normal start\n\n## INJECTED HEADING\nfollow-on text",
        target="t",
        current="c",
        status="on_track",
    )
    dept_registry.invalidate()
    blocks = build_system_blocks()
    org_text = blocks[1]["text"]

    # The injected `## HEADING` must not appear at the start of a rendered
    # line — that's what makes it a Markdown heading with injection power.
    lines = org_text.splitlines()
    for line in lines:
        assert not line.strip().startswith("## INJECTED"), (
            f"Injected heading appeared as a line start: {line!r}"
        )
    # The useful part of the OKR (before the injection) is still present.
    assert "Normal start" in org_text


def test_period_value_newlines_stripped_before_system_prompt() -> None:
    """Same invariant as test_okr_newlines_stripped_before_system_prompt, but
    for the period_value field — a malicious value like "Q2\n\n## INJECTED"
    must not produce a Markdown heading inside the cached system prompt.
    """
    dept_store.initialize_db()
    dept_store.seed_default_departments()
    dept_store.insert_goal(
        "finance",
        period_value="Q2 2026\n\n## INJECTED HEADING\nfollow-on",
        key_result="A normal key result",
        target="t",
        current="c",
        status="on_track",
    )
    dept_registry.invalidate()
    blocks = build_system_blocks()
    org_text = blocks[1]["text"]

    for line in org_text.splitlines():
        assert not line.strip().startswith("## INJECTED"), (
            f"Injected heading appeared as a line start: {line!r}"
        )


def test_persona_contains_departments_addendum() -> None:
    """The static addendum added to executive_persona.py must appear in the
    assembled persona block."""
    blocks = build_system_blocks()
    persona = blocks[0]["text"]
    assert "Departments & Org Coordination" in persona
    assert "authority_level" in persona


def test_org_block_includes_person_contact_channels() -> None:
    """The People section must surface email/Slack/Telegram/Discord IDs,
    department assignments, and reports-to relationships when set. Without
    them in the system prompt, the Executive can't answer "what do you know
    about me" even though the data is in the DB.
    """
    dept_store.initialize_db()
    people_store.initialize_db()

    manager_id = people_store.upsert_person(
        full_name="Sarah Chen",
        role="CFO",
    )
    people_store.upsert_person(
        full_name="Jordan Avery",
        role="Resident Bad Ass",
        is_principal=True,
        department_slugs=["finance", "operations"],
        email="rufus@example.com",
        slack_user_id="U12345",
        telegram_chat_id="987654",
        discord_user_id="rufus#0001",
        reports_to_person_id=manager_id,
    )
    people_registry.invalidate()

    blocks = build_system_blocks()
    org_text = blocks[-1]["text"]

    assert "rufus@example.com" in org_text
    assert "U12345" in org_text
    assert "987654" in org_text
    assert "rufus#0001" in org_text
    assert "finance" in org_text and "operations" in org_text
    assert "reports to: Sarah Chen" in org_text


def test_org_block_omits_contact_fields_when_unset() -> None:
    """A Person with no contact channels must render without empty `email:`
    or `slack:` markers — only set fields appear."""
    dept_store.initialize_db()
    people_store.initialize_db()
    people_store.upsert_person(full_name="Plain Person", role="Advisor")
    people_registry.invalidate()

    blocks = build_system_blocks()
    org_text = blocks[-1]["text"]

    assert "Plain Person" in org_text
    assert "email:" not in org_text
    assert "slack:" not in org_text
    assert "telegram:" not in org_text
    assert "discord:" not in org_text
    assert "depts:" not in org_text
    assert "reports to:" not in org_text


def test_org_block_sanitizes_contact_fields() -> None:
    """Contact fields are user-controlled and must be sanitized — a malicious
    email containing a newline + heading must not break out into a new section.
    """
    dept_store.initialize_db()
    people_store.initialize_db()
    people_store.upsert_person(
        full_name="Eve",
        email="eve@example.com\n\n## INJECTED HEADING\n",
    )
    people_registry.invalidate()

    blocks = build_system_blocks()
    org_text = blocks[-1]["text"]

    for line in org_text.splitlines():
        assert not line.strip().startswith("## INJECTED"), (
            f"Injected heading appeared as a line start: {line!r}"
        )


def test_org_block_shows_head_person_name() -> None:
    """When a department has a head_person_id, the org block must include
    that person's name in the department heading line."""
    dept_store.initialize_db()
    people_store.initialize_db()
    dept_store.seed_default_departments()
    person_id = people_store.upsert_person(full_name="Sarah Chen", role="CFO")
    dept_store.update_department("finance", head_person_id=person_id)
    dept_registry.invalidate()
    people_registry.invalidate()

    blocks = build_system_blocks()
    org_text = blocks[1]["text"]

    assert "Sarah Chen" in org_text, (
        f"Head person name missing from org block. Block content:\n{org_text}"
    )
