"""Unit tests for the voice persona loader and override round-trip."""
from __future__ import annotations

from pathlib import Path

import pytest

from openexecutive.personas.loader import (
    create_persona,
    delete_persona,
    get_active_body,
    get_persona,
    invalidate_builtin_cache,
    list_personas,
    reset_persona,
    upsert_persona,
)
from openexecutive.prompts.cache_manager import build_system_blocks

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clear_builtin_cache() -> None:
    """Each test gets a fresh builtin cache so YAML fixes take effect."""
    invalidate_builtin_cache()


@pytest.fixture()
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


# ---------------------------------------------------------------------------
# Built-in loading
# ---------------------------------------------------------------------------

def test_list_personas_includes_default(tmp_db: Path) -> None:
    metas = list_personas(db_path=tmp_db)
    slugs = {m.slug for m in metas}
    assert "default" in slugs
    assert "supportive" in slugs


def test_default_persona_is_builtin(tmp_db: Path) -> None:
    metas = list_personas(db_path=tmp_db)
    default = next(m for m in metas if m.slug == "default")
    assert default.is_builtin is True
    assert default.is_customized is False


def test_get_persona_returns_builtin_body(tmp_db: Path) -> None:
    p = get_persona("default", db_path=tmp_db)
    assert p is not None
    assert p.body.strip() != ""
    assert p.is_builtin is True


def test_get_persona_unknown_slug_returns_none(tmp_db: Path) -> None:
    assert get_persona("does-not-exist-xyz", db_path=tmp_db) is None


# ---------------------------------------------------------------------------
# get_active_body fallback behaviour
# ---------------------------------------------------------------------------

def test_get_active_body_none_resolves_to_default(tmp_db: Path) -> None:
    body = get_active_body(None, db_path=tmp_db)
    default_body = get_persona("default", db_path=tmp_db)
    assert default_body is not None
    assert body == default_body.body


def test_get_active_body_unknown_slug_falls_back_to_default(tmp_db: Path) -> None:
    body = get_active_body("zzz-nonexistent", db_path=tmp_db)
    default_body = get_persona("default", db_path=tmp_db)
    assert default_body is not None
    assert body == default_body.body


# ---------------------------------------------------------------------------
# DB shadow / upsert / delete / reset
# ---------------------------------------------------------------------------

def test_upsert_shadows_builtin(tmp_db: Path) -> None:
    upsert_persona("supportive", "Supportive (edited)", "custom body", db_path=tmp_db)
    p = get_persona("supportive", db_path=tmp_db)
    assert p is not None
    assert p.body == "custom body"
    assert p.is_builtin is True
    assert p.is_customized is True

    metas = list_personas(db_path=tmp_db)
    sup = next(m for m in metas if m.slug == "supportive")
    assert sup.is_customized is True


def test_reset_restores_builtin(tmp_db: Path) -> None:
    upsert_persona("supportive", "Supportive (edited)", "custom body", db_path=tmp_db)
    restored = reset_persona("supportive", db_path=tmp_db)
    assert restored is not None
    assert restored.body != "custom body"
    assert restored.is_customized is False

    p = get_persona("supportive", db_path=tmp_db)
    assert p is not None
    assert p.is_customized is False


def test_reset_returns_none_for_nonexistent_builtin(tmp_db: Path) -> None:
    create_persona("My Custom", "Some body", db_path=tmp_db)
    # "my-custom" slug has no builtin — reset should return None
    result = reset_persona("my-custom", db_path=tmp_db)
    assert result is None


def test_delete_removes_custom_persona(tmp_db: Path) -> None:
    p = create_persona("Temp CEO", "body text", db_path=tmp_db)
    assert get_persona(p.slug, db_path=tmp_db) is not None
    deleted = delete_persona(p.slug, db_path=tmp_db)
    assert deleted is True
    assert get_persona(p.slug, db_path=tmp_db) is None


def test_delete_builtin_shadow_reveals_builtin(tmp_db: Path) -> None:
    upsert_persona("supportive", "Sup", "custom body", db_path=tmp_db)
    delete_persona("supportive", db_path=tmp_db)
    p = get_persona("supportive", db_path=tmp_db)
    assert p is not None
    assert p.is_customized is False
    assert p.body != "custom body"


# ---------------------------------------------------------------------------
# create_persona slug uniqueness
# ---------------------------------------------------------------------------

def test_create_persona_derives_unique_slug(tmp_db: Path) -> None:
    p1 = create_persona("My CEO", "body one", db_path=tmp_db)
    p2 = create_persona("My CEO", "body two", db_path=tmp_db)
    assert p1.slug != p2.slug
    assert p2.slug.startswith("my-ceo")


# ---------------------------------------------------------------------------
# cache_manager substitution
# ---------------------------------------------------------------------------

def test_build_system_blocks_substitutes_voice_persona(tmp_db: Path) -> None:
    body = get_active_body("default", db_path=tmp_db)
    blocks = build_system_blocks(voice_persona_body=body)
    assert len(blocks) >= 1
    assert "{VOICE_PERSONA}" not in blocks[0]["text"]
    assert body in blocks[0]["text"]


def test_build_system_blocks_no_persona_body_clears_placeholder(tmp_db: Path) -> None:
    blocks = build_system_blocks(voice_persona_body=None)
    assert "{VOICE_PERSONA}" not in blocks[0]["text"]


def test_build_system_blocks_appends_when_placeholder_absent() -> None:
    override = "Custom prompt with no placeholder here."
    custom_body = "speak like a pirate"
    blocks = build_system_blocks(persona_override=override, voice_persona_body=custom_body)
    text = blocks[0]["text"]
    assert custom_body in text
    assert "{VOICE_PERSONA}" not in text


def test_default_persona_body_equals_original_voice_section(tmp_db: Path) -> None:
    """Regression: with default persona, assembled prompt equals the pre-feature prompt.

    We compare the Voice section content rather than the full prompt because the
    placeholder wrapper text ('Adopt the voice…') is new structural copy.
    The key invariant is that the default.md body (the actual bullets) makes it
    into the final prompt unchanged.
    """
    default_body = get_active_body(None, db_path=tmp_db)
    blocks = build_system_blocks(voice_persona_body=default_body)
    assembled = blocks[0]["text"]
    # Every bullet from the default body must appear verbatim in the assembled prompt.
    for line in default_body.splitlines():
        stripped = line.strip()
        if stripped:
            assert stripped in assembled, f"Missing from assembled prompt: {stripped!r}"


# ---------------------------------------------------------------------------
# overrides round-trip
# ---------------------------------------------------------------------------

def test_voice_persona_slug_round_trips_through_overrides(tmp_db: Path) -> None:
    from openexecutive.agents.overrides import (
        clear_override,
        get_override,
        initialize_overrides_db,
        set_override,
    )

    initialize_overrides_db(db_path=tmp_db)
    set_override(
        "executive",
        voice_persona_slug="supportive",
        voice_persona_slug_set=True,
        db_path=tmp_db,
    )
    ov = get_override("executive", db_path=tmp_db)
    assert ov is not None
    assert ov.voice_persona_slug == "supportive"

    clear_override("executive", db_path=tmp_db)
    assert get_override("executive", db_path=tmp_db) is None


def test_voice_persona_slug_in_history(tmp_db: Path) -> None:
    from openexecutive.agents.overrides import (
        initialize_overrides_db,
        list_history,
        set_override,
    )

    initialize_overrides_db(db_path=tmp_db)
    set_override(
        "executive",
        voice_persona_slug="default",
        voice_persona_slug_set=True,
        db_path=tmp_db,
    )
    set_override(
        "executive",
        voice_persona_slug="supportive",
        voice_persona_slug_set=True,
        db_path=tmp_db,
    )
    history = list_history("executive", db_path=tmp_db)
    assert len(history) >= 1
    assert history[0].voice_persona_slug == "default"


def test_rollback_restores_voice_persona_slug(tmp_db: Path) -> None:
    from openexecutive.agents.overrides import (
        get_override,
        initialize_overrides_db,
        list_history,
        rollback_to,
        set_override,
    )

    initialize_overrides_db(db_path=tmp_db)
    set_override(
        "executive",
        voice_persona_slug="default",
        voice_persona_slug_set=True,
        db_path=tmp_db,
    )
    set_override(
        "executive",
        voice_persona_slug="analytical",
        voice_persona_slug_set=True,
        db_path=tmp_db,
    )
    history = list_history("executive", db_path=tmp_db)
    entry_id = history[0].id
    rollback_to(entry_id, db_path=tmp_db)
    ov = get_override("executive", db_path=tmp_db)
    assert ov is not None
    assert ov.voice_persona_slug == "default"
