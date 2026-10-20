"""Tests for the agent override layer used by the Agent Council UI."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-used")

from openexecutive.agents import overrides as ov_mod  # noqa: E402
from openexecutive.agents.base import BaseAgent  # noqa: E402


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    db = tmp_path / "test.db"
    ov_mod.invalidate_cache()
    yield db
    ov_mod.invalidate_cache()


def test_get_override_returns_none_when_no_row(tmp_db: Path) -> None:
    assert ov_mod.get_override("cso", db_path=tmp_db) is None


def test_set_override_creates_row_and_persists(tmp_db: Path) -> None:
    ov_mod.set_override(
        "cso",
        prompt="new prompt",
        prompt_set=True,
        db_path=tmp_db,
    )
    ov_mod.invalidate_cache()  # force re-read from DB
    result = ov_mod.get_override("cso", db_path=tmp_db)
    assert result is not None
    assert result.prompt == "new prompt"
    assert result.model is None
    assert result.role is None


def test_set_override_partial_update_preserves_other_fields(tmp_db: Path) -> None:
    ov_mod.set_override(
        "cfo",
        prompt="p1",
        model="claude-opus-4-7",
        prompt_set=True,
        model_set=True,
        db_path=tmp_db,
    )
    ov_mod.set_override(
        "cfo",
        role="New Role",
        role_set=True,
        db_path=tmp_db,
    )
    result = ov_mod.get_override("cfo", db_path=tmp_db)
    assert result is not None
    assert result.prompt == "p1"
    assert result.model == "claude-opus-4-7"
    assert result.role == "New Role"


def test_set_override_records_history_on_update(tmp_db: Path) -> None:
    ov_mod.set_override("cmo", prompt="v1", prompt_set=True, db_path=tmp_db)
    ov_mod.set_override("cmo", prompt="v2", prompt_set=True, db_path=tmp_db)
    history = ov_mod.list_history("cmo", db_path=tmp_db)
    assert len(history) == 1
    assert history[0].prompt == "v1"


def test_clear_override_removes_row_and_records_history(tmp_db: Path) -> None:
    ov_mod.set_override("gc", prompt="x", prompt_set=True, db_path=tmp_db)
    removed = ov_mod.clear_override("gc", db_path=tmp_db)
    assert removed is True
    assert ov_mod.get_override("gc", db_path=tmp_db) is None
    assert len(ov_mod.list_history("gc", db_path=tmp_db)) == 1


def test_clear_override_noop_when_no_row(tmp_db: Path) -> None:
    assert ov_mod.clear_override("missing", db_path=tmp_db) is False


def test_rollback_restores_prior_version(tmp_db: Path) -> None:
    ov_mod.set_override("cpo", prompt="v1", prompt_set=True, db_path=tmp_db)
    ov_mod.set_override("cpo", prompt="v2", prompt_set=True, db_path=tmp_db)
    history = ov_mod.list_history("cpo", db_path=tmp_db)
    # The first history entry is the older one (after second write).
    # Restore the v1 version.
    target = next(h for h in history if h.prompt == "v1")
    ov_mod.rollback_to(target.id, db_path=tmp_db)
    current = ov_mod.get_override("cpo", db_path=tmp_db)
    assert current is not None
    assert current.prompt == "v1"


def test_rollback_unknown_id_returns_none(tmp_db: Path) -> None:
    assert ov_mod.rollback_to(99999, db_path=tmp_db) is None


# ---------------------------------------------------------------------------
# research_focus field (per-specialist research-mode focus override)
# ---------------------------------------------------------------------------


def test_research_focus_round_trips(tmp_db: Path) -> None:
    ov_mod.set_override(
        "cso",
        research_focus="watch only acme.com",
        research_focus_set=True,
        db_path=tmp_db,
    )
    ov_mod.invalidate_cache()
    result = ov_mod.get_override("cso", db_path=tmp_db)
    assert result is not None
    assert result.research_focus == "watch only acme.com"
    # Other fields untouched.
    assert result.prompt is None
    assert result.model is None


def test_research_focus_partial_update_preserves_other_fields(tmp_db: Path) -> None:
    ov_mod.set_override(
        "cfo", model="claude-opus-4-7", model_set=True, db_path=tmp_db
    )
    ov_mod.set_override(
        "cfo", research_focus="tickers only", research_focus_set=True, db_path=tmp_db
    )
    result = ov_mod.get_override("cfo", db_path=tmp_db)
    assert result is not None
    assert result.model == "claude-opus-4-7"
    assert result.research_focus == "tickers only"


def test_research_focus_in_history_and_rollback(tmp_db: Path) -> None:
    ov_mod.set_override("cmo", research_focus="v1", research_focus_set=True, db_path=tmp_db)
    ov_mod.set_override("cmo", research_focus="v2", research_focus_set=True, db_path=tmp_db)
    history = ov_mod.list_history("cmo", db_path=tmp_db)
    assert history[0].research_focus == "v1"
    ov_mod.rollback_to(history[0].id, db_path=tmp_db)
    current = ov_mod.get_override("cmo", db_path=tmp_db)
    assert current is not None
    assert current.research_focus == "v1"


def test_add_column_migrates_preexisting_db_without_research_focus(
    tmp_path: Path,
) -> None:
    """A DB created before research_focus existed gets the column added
    idempotently on initialize — no crash, NULL for old rows."""
    import sqlite3

    db = tmp_path / "legacy.db"
    # Build a legacy schema WITHOUT research_focus, with one row.
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE agent_overrides (
            agent_id TEXT PRIMARY KEY, prompt TEXT, model TEXT,
            use_deep_reasoning INTEGER, role TEXT, voice_persona_slug TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE agent_override_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL,
            prompt TEXT, model TEXT, use_deep_reasoning INTEGER, role TEXT,
            voice_persona_slug TEXT, created_at TEXT NOT NULL
        );
        INSERT INTO agent_overrides (agent_id, prompt, updated_at)
        VALUES ('cso', 'legacy', '2026-01-01T00:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()

    ov_mod.invalidate_cache()
    ov_mod.initialize_overrides_db(db)  # runs the additive migration
    cols = {
        row[1]
        for row in sqlite3.connect(db)
        .execute("PRAGMA table_info(agent_overrides)")
        .fetchall()
    }
    assert "research_focus" in cols
    # Legacy row still readable; new column is NULL.
    result = ov_mod.get_override("cso", db_path=db)
    assert result is not None
    assert result.prompt == "legacy"
    assert result.research_focus is None


# ---------------------------------------------------------------------------
# BaseAgent override resolution
# ---------------------------------------------------------------------------


class _Dummy(BaseAgent):
    name = "dummy_override_test"
    domain = "strategy"
    model = "claude-default-model"
    use_deep_reasoning = False

    def get_system_prompt(self) -> str:
        return "DEFAULT_PROMPT"


def _patch_settings_and_anthropic(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Patch the provider registry and capture messages_create kwargs."""
    create_mock = AsyncMock(
        return_value=SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")])
    )
    fake_provider = SimpleNamespace(messages_create=create_mock)
    monkeypatch.setattr(
        "openexecutive.agents.base.get_provider",
        lambda _model: fake_provider,
    )
    return create_mock


def test_analyze_uses_default_when_no_override(tmp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ov_mod, "DB_PATH", tmp_db)
    monkeypatch.setattr("openexecutive.agents.base.get_settings", lambda: SimpleNamespace(anthropic_api_key="x", specialist_effort="low"))
    create_mock = _patch_settings_and_anthropic(monkeypatch)
    import asyncio

    asyncio.run(_Dummy().analyze(query="hi"))
    kw = create_mock.await_args.kwargs
    assert kw["model"] == "claude-default-model"
    assert kw["system"][0]["text"] == "DEFAULT_PROMPT"


def test_analyze_honors_db_override(tmp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ov_mod, "DB_PATH", tmp_db)
    monkeypatch.setattr("openexecutive.agents.base.get_settings", lambda: SimpleNamespace(anthropic_api_key="x", specialist_effort="low"))
    create_mock = _patch_settings_and_anthropic(monkeypatch)
    ov_mod.set_override(
        "dummy_override_test",
        prompt="OVERRIDE_PROMPT",
        model="claude-opus-4-7",
        prompt_set=True,
        model_set=True,
        db_path=tmp_db,
    )
    import asyncio

    asyncio.run(_Dummy().analyze(query="hi"))
    kw = create_mock.await_args.kwargs
    assert kw["model"] == "claude-opus-4-7"
    assert kw["system"][0]["text"] == "OVERRIDE_PROMPT"


def test_analyze_explicit_override_beats_db(tmp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ov_mod, "DB_PATH", tmp_db)
    monkeypatch.setattr("openexecutive.agents.base.get_settings", lambda: SimpleNamespace(anthropic_api_key="x", specialist_effort="low"))
    create_mock = _patch_settings_and_anthropic(monkeypatch)
    ov_mod.set_override(
        "dummy_override_test",
        prompt="DB_PROMPT",
        prompt_set=True,
        db_path=tmp_db,
    )
    import asyncio

    asyncio.run(
        _Dummy().analyze(
            query="hi",
            system_prompt_override="EXPLICIT_PROMPT",
            model_override="claude-sonnet-4-6",
        )
    )
    kw = create_mock.await_args.kwargs
    assert kw["model"] == "claude-sonnet-4-6"
    assert kw["system"][0]["text"] == "EXPLICIT_PROMPT"


# ---------------------------------------------------------------------------
# Additional instructions
# ---------------------------------------------------------------------------


def test_append_instructions_blank_leaves_prompt_unchanged() -> None:
    assert ov_mod.append_instructions("P", None) == "P"
    assert ov_mod.append_instructions("P", "") == "P"
    assert ov_mod.append_instructions("P", "  \n ") == "P"


def test_append_instructions_wraps_text_after_prompt() -> None:
    out = ov_mod.append_instructions("P", "  Use EUR.\n")
    assert out == "P\n\n<additional_instructions>\nUse EUR.\n</additional_instructions>"


def test_instructions_persist_history_and_rollback(tmp_db: Path) -> None:
    ov_mod.set_override(
        "cso", instructions="first", instructions_set=True, db_path=tmp_db
    )
    ov_mod.set_override(
        "cso", instructions="second", instructions_set=True, db_path=tmp_db
    )
    current = ov_mod.get_override("cso", db_path=tmp_db)
    assert current is not None and current.instructions == "second"
    # A partial update leaves instructions alone.
    ov_mod.set_override("cso", role="R", role_set=True, db_path=tmp_db)
    current = ov_mod.get_override("cso", db_path=tmp_db)
    assert current is not None and current.instructions == "second"

    history = ov_mod.list_history("cso", db_path=tmp_db)
    first = next(h for h in history if h.instructions == "first")
    restored = ov_mod.rollback_to(first.id, db_path=tmp_db)
    assert restored is not None and restored.instructions == "first"


def test_instructions_column_added_to_existing_db(tmp_db: Path) -> None:
    from openexecutive.memory.episodic import _get_conn

    with _get_conn(tmp_db) as conn:
        conn.executescript(
            """
            CREATE TABLE agent_overrides (
                agent_id TEXT PRIMARY KEY, prompt TEXT, model TEXT,
                use_deep_reasoning INTEGER, role TEXT, updated_at TEXT NOT NULL
            );
            CREATE TABLE agent_override_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL,
                prompt TEXT, model TEXT, use_deep_reasoning INTEGER, role TEXT,
                created_at TEXT NOT NULL
            );
            INSERT INTO agent_overrides (agent_id, prompt, updated_at)
                VALUES ('cfo', 'OLD', '2026-01-01');
            """
        )
    result = ov_mod.get_override("cfo", db_path=tmp_db)
    assert result is not None
    assert result.prompt == "OLD"
    assert result.instructions is None


def test_analyze_appends_instructions_to_builtin_prompt(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ov_mod, "DB_PATH", tmp_db)
    monkeypatch.setattr("openexecutive.agents.base.get_settings", lambda: SimpleNamespace(anthropic_api_key="x", specialist_effort="low"))
    create_mock = _patch_settings_and_anthropic(monkeypatch)
    ov_mod.set_override(
        "dummy_override_test",
        instructions="Use EUR.",
        instructions_set=True,
        db_path=tmp_db,
    )
    import asyncio

    asyncio.run(_Dummy().analyze(query="hi"))
    text = create_mock.await_args.kwargs["system"][0]["text"]
    assert text.startswith("DEFAULT_PROMPT\n\n<additional_instructions>")
    assert "Use EUR." in text
    # The editor's view of the prompt never includes the instructions.
    assert _Dummy().base_system_prompt() == "DEFAULT_PROMPT"


def test_analyze_appends_instructions_to_replaced_prompt(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ov_mod, "DB_PATH", tmp_db)
    monkeypatch.setattr("openexecutive.agents.base.get_settings", lambda: SimpleNamespace(anthropic_api_key="x", specialist_effort="low"))
    create_mock = _patch_settings_and_anthropic(monkeypatch)
    ov_mod.set_override(
        "dummy_override_test",
        prompt="OVERRIDE_PROMPT",
        instructions="Use EUR.",
        prompt_set=True,
        instructions_set=True,
        db_path=tmp_db,
    )
    import asyncio

    asyncio.run(_Dummy().analyze(query="hi"))
    text = create_mock.await_args.kwargs["system"][0]["text"]
    assert text == ov_mod.append_instructions("OVERRIDE_PROMPT", "Use EUR.")
