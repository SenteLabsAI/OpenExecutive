"""Unit tests for the briefing narrative prompts.

Two distinct surfaces:
  - the /today header (a SYNTHESIS — actionable items render as cards below,
    so it must NOT re-list them or render a 'Needs you' section);
  - the standalone morning-brief / EoD DM (no cards beside it, so it MUST
    enumerate what needs the principal's attention).
"""
from __future__ import annotations

from openexecutive.briefing.narrative import (
    BRIEFING_NARRATIVE_SYSTEM,
    STANDALONE_BRIEF_SYSTEM,
    _viewer_system_prompt,
)


def test_today_header_prompt_is_synthesis_not_a_needs_you_list() -> None:
    assert "Needs you" not in BRIEFING_NARRATIVE_SYSTEM
    lowered = BRIEFING_NARRATIVE_SYSTEM.lower()
    assert "synthesis" in lowered
    assert "re-list" in lowered  # explicit instruction not to duplicate the cards
    # Scannable shape: bottom-line headline → read bullets → a "Move today" line.
    assert "bottom-line" in lowered
    assert "bullets" in lowered
    assert "Move today:" in BRIEFING_NARRATIVE_SYSTEM


def test_today_header_prompt_enforces_plain_language() -> None:
    """The header was rendering as dense, hard-to-parse prose (run-on
    bottom-lines, bullets stacking several clauses, '→' shorthand). The prompt
    must steer toward plain, one-idea-per-bullet, first-read-clear writing."""
    lowered = BRIEFING_NARRATIVE_SYSTEM.lower()
    assert "one idea per bullet" in lowered
    assert "first read" in lowered
    assert "plain" in lowered
    # The cryptic '→' shorthand must be explicitly disallowed (it was the
    # example before, and the model copied it).
    assert "'→' shorthand" in BRIEFING_NARRATIVE_SYSTEM


def test_viewer_header_prompt_enforces_plain_language() -> None:
    prompt = _viewer_system_prompt("Sam Rivera", "CFO").lower()
    assert "one idea each" in prompt
    assert "first read" in prompt
    assert "plain" in prompt


def test_today_header_prompt_steers_a_lively_non_templated_voice() -> None:
    """It should read with energy and vary day to day — not the same
    boilerplate template every morning — while keeping clarity first."""
    lowered = BRIEFING_NARRATIVE_SYSTEM.lower()
    assert "voice" in lowered
    assert "vary" in lowered  # explicit instruction not to read like a fixed template
    assert "point of view" in lowered


def test_viewer_header_prompt_steers_a_lively_voice() -> None:
    lowered = _viewer_system_prompt("Sam Rivera", "CFO").lower()
    assert "voice" in lowered
    assert "vary" in lowered


def test_viewer_header_prompt_is_synthesis_and_addresses_the_person() -> None:
    prompt = _viewer_system_prompt("Sam Rivera", "CFO")
    assert "Sam Rivera" in prompt
    assert "CFO" in prompt
    assert "Needs you" not in prompt
    assert "synthesis" in prompt.lower()
    # Same scannable shape, scoped to them.
    assert "bottom-line" in prompt.lower()
    assert "Your move:" in prompt


def test_standalone_dm_prompt_enumerates_actionables() -> None:
    # The DM has no cards beside it, so it MUST list what needs attention.
    assert "Needs you" in STANDALONE_BRIEF_SYSTEM
    assert "What changed" in STANDALONE_BRIEF_SYSTEM
    # ...and must NOT assume a card list renders below it.
    assert "cards BELOW" not in STANDALONE_BRIEF_SYSTEM


def test_standalone_dm_prompt_carries_older_items_as_one_line() -> None:
    lowered = STANDALONE_BRIEF_SYSTEM.lower()
    assert "never re-list carried items" in lowered
    assert "Handled overnight" in STANDALONE_BRIEF_SYSTEM
    assert "Top call" in STANDALONE_BRIEF_SYSTEM
    assert "delta" in lowered


def test_quiet_lines_are_single_sourced_into_the_prompts() -> None:
    """The quiet-day text exists once and the prompts carry it verbatim.

    `today._regen_briefing_narrative` writes these strings directly when it
    short-circuits an empty board, and `morning_brief` uses one as its empty
    fallback — both skip the model, so their text must be exactly what the
    model is told to emit. They were five hand-copied literals across three
    files; this is the guard that keeps a prompt reword from silently
    desyncing them.
    """
    import inspect

    from openexecutive.api.routes import today as today_route
    from openexecutive.briefing.narrative import (
        BRIEFING_NARRATIVE_SYSTEM,
        QUIET_PRINCIPAL,
        QUIET_VIEWER,
        STANDALONE_BRIEF_SYSTEM,
        _viewer_system_prompt,
    )
    from openexecutive.workflows import morning_brief

    assert QUIET_PRINCIPAL in BRIEFING_NARRATIVE_SYSTEM
    assert QUIET_PRINCIPAL in STANDALONE_BRIEF_SYSTEM
    assert QUIET_VIEWER in _viewer_system_prompt("Dan", "CFO")

    # The consumers must import the constants, never re-declare the phrase.
    # Checked against source because both import them function-locally (this
    # module's convention), so there is no attribute to compare identities on.
    phrase = "Quiet right now"
    for module in (today_route, morning_brief):
        assert phrase not in inspect.getsource(module), (
            f"{module.__name__} hardcodes a quiet-day line; import "
            "QUIET_PRINCIPAL / QUIET_VIEWER from briefing.narrative instead"
        )
