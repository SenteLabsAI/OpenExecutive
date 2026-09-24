"""Tests for cache_manager persona assembly — email identity and exec override."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-used")

from openexecutive.agents import overrides as ov_mod  # noqa: E402
from openexecutive.prompts.cache_manager import build_system_blocks  # noqa: E402
from openexecutive.prompts.executive_persona import EXECUTIVE_PERSONA_PROMPT  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ov_mod, "DB_PATH", tmp_path / "ov.db")
    ov_mod.invalidate_cache()
    yield
    ov_mod.invalidate_cache()


def test_persona_includes_exec_email_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXEC_EMAIL_ADDRESS", "ceo.test@example.com")
    blocks = build_system_blocks()
    persona_text = blocks[0]["text"]
    assert "ceo.test@example.com" in persona_text
    assert "## Your Identity" in persona_text
    assert "Never ask the user which address to send from" in persona_text
    assert "This mailbox belongs to you" in persona_text


def test_persona_uses_default_when_no_override() -> None:
    blocks = build_system_blocks()
    persona_text = blocks[0]["text"]
    # The voice persona placeholder must be substituted out.
    assert "{VOICE_PERSONA}" not in persona_text
    # The structural opening of the default persona must still be present.
    assert "You are the Executive" in persona_text
    assert "You are not a consultant who generates frameworks." in persona_text


def test_persona_applies_persona_override_parameter() -> None:
    blocks = build_system_blocks(persona_override="CUSTOM EXEC PERSONA")
    persona_text = blocks[0]["text"]
    assert "CUSTOM EXEC PERSONA" in persona_text
    # Default persona must NOT be present when overridden.
    assert EXECUTIVE_PERSONA_PROMPT not in persona_text
    # Addenda still applied on top of the override.
    assert "## Your Identity" in persona_text


def test_persona_block_has_1h_ttl_first() -> None:
    blocks = build_system_blocks()
    assert blocks[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


def test_identity_addendum_uses_default_display_name() -> None:
    blocks = build_system_blocks()
    persona_text = blocks[0]["text"]
    assert "You are Open Executive" in persona_text


def test_identity_addendum_honors_exec_display_name_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXEC_DISPLAY_NAME", "Acme Bot")
    blocks = build_system_blocks()
    persona_text = blocks[0]["text"]
    assert "You are Acme Bot" in persona_text
    # The default name must not leak when an override is set.
    assert "You are Open Executive" not in persona_text


def test_identity_addendum_forbids_impersonation() -> None:
    blocks = build_system_blocks()
    persona_text = blocks[0]["text"]
    assert "Never impersonate company personnel" in persona_text
    # Must reference both rosters by their actual section headings so the
    # model can tie the rule to the context blocks it sees.
    assert "People You Coordinate With" in persona_text
    assert "Leadership" in persona_text
    # The `(principal)` tag on People-roster entries is the most common
    # confusion vector — the rule must explicitly cover it.
    assert "(principal)" in persona_text


def test_persona_holds_the_line_on_followups() -> None:
    blocks = build_system_blocks()
    persona_text = blocks[0]["text"]
    # The persona must instruct the Executive not to be talked out of a
    # legitimate follow-up by casual deflection — the "fair enough" failure.
    assert "## Holding the Line and Staying on the Business" in persona_text
    assert "fair enough" in persona_text.lower()
    # It must distinguish casual deflection from genuine reprioritization
    # and tell the model to steer back on topic rather than drift.
    assert "reprioritization" in persona_text
    assert "steer back" in persona_text


def test_identity_addendum_reinforced_in_inbound_email_section() -> None:
    blocks = build_system_blocks()
    persona_text = blocks[0]["text"]
    # The "Handling Inbound Emails" guidance must remind the model to sign
    # as itself, not as the original sender or any roster person.
    assert "Be signed as yourself" in persona_text


def test_persona_has_single_authoritative_length_rule() -> None:
    """Exactly one length rule may exist, and it must be the ladder.

    The persona previously carried "Keep responses under 500 words" mid-prompt
    and "Short questions get short answers - one or two sentences" as its final
    line. Two ceilings ~250x apart let the model pick, and it picked the looser
    one. Both competing rules must stay gone: asserting only that the ladder is
    present would not catch either being reinstated alongside it.
    """
    assert "## Length" in EXECUTIVE_PERSONA_PROMPT
    for superseded in ("Keep responses under 500 words", "Short questions get short answers"):
        assert superseded not in EXECUTIVE_PERSONA_PROMPT, (
            f"superseded length rule reinstated alongside the ladder: {superseded!r}"
        )
    # The ladder's rungs, lowest first.
    for rung in ("one sentence", "one or two sentences", "under 80 words", "under 200 words"):
        assert rung in EXECUTIVE_PERSONA_PROMPT, f"missing length rung: {rung}"


def test_persona_forbids_closing_offers() -> None:
    """Guards the no-closing-offer instruction against silent deletion.

    This asserts the instruction is present, not that the model obeys it --
    a unit test cannot verify the latter without a model call. Behavioral
    coverage lives in the eval suite's `concision` dimension.
    """
    assert "Do not close with an offer." in EXECUTIVE_PERSONA_PROMPT


def test_persona_brevity_never_suppresses_a_hedge() -> None:
    """Guards the hedge carve-out against silent deletion.

    Presence check only, for the same reason as above -- the ladder must never
    be trimmed without this clause surviving alongside it.
    """
    assert "Brevity never costs a hedge." in EXECUTIVE_PERSONA_PROMPT


def test_persona_follow_up_loop_is_bounded() -> None:
    """Guards both follow-up stopping rules against silent deletion.

    Presence check only; see the note on the closing-offer test above.
    """
    assert "A correction is not a deflection." in EXECUTIVE_PERSONA_PROMPT
    assert 'An explicit "drop it" ends it.' in EXECUTIVE_PERSONA_PROMPT
