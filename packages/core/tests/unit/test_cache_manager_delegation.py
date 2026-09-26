"""Act as me in the cached system prompt (prompts/cache_manager.py): off is
byte-identical to before; on appends one constant addendum."""
from __future__ import annotations

from openexecutive.prompts.cache_manager import build_system_blocks
from openexecutive.prompts.executive_persona import DELEGATION_ADDENDUM


def test_off_is_byte_identical_to_the_default() -> None:
    assert build_system_blocks() == build_system_blocks(delegation=False)
    assert "Act as Me" not in build_system_blocks()[0]["text"]


def test_on_appends_the_constant_after_the_untouched_identity_addendum() -> None:
    off = build_system_blocks()[0]["text"]
    on = build_system_blocks(delegation=True)[0]["text"]
    assert DELEGATION_ADDENDUM in on
    # Everything the identity addendum says is still there, word for word.
    for phrase in ("You are Open Executive", "Never impersonate company personnel", "(principal)"):
        assert phrase in on
    identity_end = on.index("You always communicate as")
    assert identity_end < on.index(DELEGATION_ADDENDUM) < on.index("The user's local timezone")
    assert on.replace(DELEGATION_ADDENDUM, "") == off


def test_the_addendum_is_a_constant_for_both_modes() -> None:
    for mode in ("team", "solo"):
        a = build_system_blocks(workspace_mode=mode, delegation=True)[0]
        b = build_system_blocks(workspace_mode=mode, delegation=True)[0]
        assert a == b
        assert a["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    # No placeholder a caller could fill with per-turn text.
    assert "{" not in DELEGATION_ADDENDUM and "}" not in DELEGATION_ADDENDUM
    assert "ghostwrite_email" in DELEGATION_ADDENDUM
