"""The three general voices (Direct, Supportive, Analytical), legacy named
voices, and playing a voice per session for evals."""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from openexecutive.evals.scenarios import scenario_voice_persona, validate_scenario_yaml
from openexecutive.memory import episodic
from openexecutive.orchestrator.executive import Executive
from openexecutive.orchestrator.session import Session
from openexecutive.personas import loader
from openexecutive.personas.loader import (
    get_active_body,
    get_persona,
    invalidate_builtin_cache,
    list_personas,
)

GENERAL = ("default", "supportive", "analytical")
_BUILTIN_DIR = Path(loader.__file__).parent / "builtin"
# Numeric response-length ceilings belong in prompts/executive_persona.py.
_LENGTH_RULE_RE = re.compile(
    r"\b\d+\s*(?:words?|sentences?|paragraphs?|bullets?|lines?|characters?)\b"
    r"|\bunder\s+\d+\b|\bat most\s+\d+\b",
    re.IGNORECASE,
)


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "voice.db"
    monkeypatch.setattr(episodic, "DB_PATH", db)
    monkeypatch.setattr(loader, "DB_PATH", db)
    invalidate_builtin_cache()
    return db


def test_general_voices_load_with_picker_copy(_isolated: Path) -> None:
    metas = {m.slug: m for m in list_personas(db_path=_isolated)}
    assert {s: metas[s].display_name for s in GENERAL} == {
        "default": "Direct",
        "supportive": "Supportive",
        "analytical": "Analytical",
    }
    for slug in GENERAL:
        assert metas[slug].is_legacy is False
        assert metas[slug].description
        assert metas[slug].sample
        assert get_active_body(slug, db_path=_isolated)


def test_named_voices_are_legacy_but_still_resolve(_isolated: Path) -> None:
    legacy = [m for m in list_personas(db_path=_isolated) if m.is_legacy]
    assert {m.slug for m in legacy} >= {"tim-cook", "satya-nadella", "jensen-huang"}
    assert not {m.slug for m in legacy} & set(GENERAL)
    body = get_active_body("tim-cook", db_path=_isolated)
    assert body and body != get_active_body(None, db_path=_isolated)


def test_customized_builtin_keeps_its_picker_metadata(_isolated: Path) -> None:
    loader.upsert_persona("tim-cook", "Tim", "Custom body", db_path=_isolated)
    p = get_persona("tim-cook", db_path=_isolated)
    assert p is not None and p.is_legacy is True and p.body == "Custom body"
    loader.upsert_persona("supportive", "Kind", "Warm body", db_path=_isolated)
    meta = next(m for m in list_personas(db_path=_isolated) if m.slug == "supportive")
    assert meta.is_customized and meta.description and not meta.is_legacy


@pytest.mark.parametrize("path", sorted(_BUILTIN_DIR.glob("*.md")), ids=lambda p: p.stem)
def test_voice_bodies_carry_no_length_rules(path: Path) -> None:
    body = loader._parse_md(path)["body"]
    hits = _LENGTH_RULE_RE.findall(body)
    assert not hits, f"{path.name} sets a length rule: {hits}"


def test_scenario_voice_persona() -> None:
    assert scenario_voice_persona({}) is None
    assert scenario_voice_persona({"voice_persona": "supportive"}) == "supportive"
    with pytest.raises(ValueError):
        scenario_voice_persona({"voice_persona": "supportve"})
    with pytest.raises(ValueError):
        validate_scenario_yaml("id: x\nquery: hi\nvoice_persona: nope\n")


def test_shipped_voice_scenarios_name_real_voices() -> None:
    scenarios = Path(loader.__file__).parents[1] / "evals" / "_scenarios"
    for name, slug in (("voice_supportive_001", "supportive"), ("voice_analytical_001", "analytical")):
        s = validate_scenario_yaml((scenarios / f"{name}.yaml").read_text())
        assert scenario_voice_persona(s) == slug


def test_session_voice_wins_over_the_install_voice(_isolated: Path) -> None:
    """Evals play a voice on the session; the Executive override is untouched."""
    from openexecutive.agents import overrides as ov_mod

    monkey_db = _isolated
    with patch.object(ov_mod, "DB_PATH", monkey_db):
        ov_mod.invalidate_cache()
        ov_mod.set_override("executive", voice_persona_slug="analytical", voice_persona_slug_set=True)
        bodies: list[Any] = []

        def _blocks(*_a: Any, **kw: Any) -> list[dict[str, Any]]:
            bodies.append(kw.get("voice_persona_body"))
            return [{"type": "text", "text": "persona"}]

        async def _loop(*_a: Any, **_kw: Any):  # type: ignore[no-untyped-def]
            yield "Hello."

        async def _drain(session: Session) -> None:
            async for _ in Executive().stream_chat(user_message="hi", session=session):
                pass

        with (
            patch("openexecutive.orchestrator.executive.build_system_blocks", new=_blocks),
            patch.object(Executive, "_stream_agent_loop", new=_loop),
            patch("openexecutive.orchestrator.executive.audit_log", lambda *a, **k: None),
            patch("openexecutive.audit.log_event", lambda *a, **k: None),
        ):
            asyncio.run(_drain(Session()))
            asyncio.run(_drain(Session(voice_persona_slug="supportive")))
        ov_mod.invalidate_cache()
    assert bodies == [
        get_active_body("analytical", db_path=_isolated),
        get_active_body("supportive", db_path=_isolated),
    ]
