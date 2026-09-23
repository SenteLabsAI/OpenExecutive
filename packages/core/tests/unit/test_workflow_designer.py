"""Conversational workflow design: the loop behind the /jobs/new wizard.

Pins the two-tool contract (constant + sorted array, tool_choice flipping only
at the budget), the cached constant system block, where the live context and
the previous draft go (user turns, never the system block), the one repair
retry — including a clash with a saved custom workflow — and that every error
raised to the route is a fixed string.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openexecutive.agents.workflow_designer import (
    WORKFLOW_DESIGNER_AGENT_ID,
    WORKFLOW_DESIGNER_SYSTEM,
)
from openexecutive.onboarding.interview import Turn
from openexecutive.workflows import designer as wd
from openexecutive.workflows import dynamic_store
from openexecutive.workflows.dynamic_models import DynamicWorkflowDef

_REAL_NAME_TAKEN = wd._name_taken


def _definition(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "weekly_competitor_digest",
        "title": "Weekly competitor digest",
        "description": "What competitors shipped this week.",
        "section": "Growth & GTM",
        "estimated_minutes": 5,
        "input_fields": [{"name": "competitors", "label": "Competitors"}],
        "steps": [
            {
                "kind": "specialist",
                "id": "scan",
                "title": "Scan competitors",
                "specialist": "cso",
                "goal": "Summarize what {competitors} shipped.",
            },
            {"kind": "synthesis", "id": "assemble", "title": "Assemble"},
        ],
    }
    base.update(overrides)
    return base


def _emit(**overrides: Any) -> dict[str, Any]:
    return {
        "definition": _definition(**overrides),
        "summary": "A weekly digest of competitor moves.",
        "assumptions": ["Delivered as a memo."],
    }


def _tool_response(name: str, payload: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", name=name, input=payload)]
    )


class _ScriptedProvider:
    """Returns queued responses in order, recording every call's kwargs."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def messages_create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("provider called more times than scripted")
        nxt = self._responses.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    """No usage rows in the default DB; no saved custom workflows by default."""
    monkeypatch.setattr("openexecutive.audit.usage.log_model_usage", lambda *a, **k: None)
    monkeypatch.setattr(wd, "_name_taken", lambda name: False)


def _install(monkeypatch: pytest.MonkeyPatch, provider: Any) -> None:
    monkeypatch.setattr("openexecutive.providers.registry.get_provider", lambda model: provider)


def _opening(text: str = "I want a weekly competitor digest.") -> list[Turn]:
    return [Turn(role="user", text=text)]


@pytest.mark.asyncio
async def test_asks_then_emits(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _ScriptedProvider(
        [
            _tool_response(
                wd.ASK_TOOL_NAME,
                {"question": "Which competitors?", "hint": "Names.", "options": ["Acme", "Globex"]},
            ),
            _tool_response(wd.EMIT_TOOL_NAME, _emit()),
        ]
    )
    _install(monkeypatch, provider)

    transcript = _opening()
    first = await wd.advance(transcript, questions_asked=0)
    assert isinstance(first, wd.DesignerQuestion)
    assert first.question == "Which competitors?"
    assert first.options == ["Acme", "Globex"]

    transcript += [
        Turn(role="assistant", text=first.question),
        Turn(role="user", text="Acme and Globex."),
    ]
    second = await wd.advance(transcript, questions_asked=1)
    assert isinstance(second, wd.WorkflowDraft)
    assert second.definition.name == "weekly_competitor_digest"
    assert second.assumptions == ["Delivered as a memo."]
    assert [m["content"] for m in provider.calls[1]["messages"]] == [
        "I want a weekly competitor digest.",
        "Which competitors?",
        "Acme and Globex.",
    ]


@pytest.mark.asyncio
async def test_system_prompt_is_the_constant_and_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _ScriptedProvider([_tool_response(wd.EMIT_TOOL_NAME, _emit())])
    _install(monkeypatch, provider)
    await wd.advance(_opening(), context_block="CTX roster")

    system = provider.calls[0]["system"]
    assert len(system) == 1
    assert system[0]["text"] == WORKFLOW_DESIGNER_SYSTEM
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert "CTX roster" not in system[0]["text"]


@pytest.mark.asyncio
async def test_tools_are_constant_and_sorted(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _ScriptedProvider(
        [
            _tool_response(wd.ASK_TOOL_NAME, {"question": "How often?"}),
            _tool_response(wd.EMIT_TOOL_NAME, _emit()),
        ]
    )
    _install(monkeypatch, provider)
    await wd.advance(_opening(), questions_asked=0)
    await wd.advance(_opening(), questions_asked=wd.MAX_QUESTIONS)
    for call in provider.calls:
        assert [t["name"] for t in call["tools"]] == [wd.ASK_TOOL_NAME, wd.EMIT_TOOL_NAME]
        assert call["tools"] is wd.TOOLS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "forced"),
    [
        ({"questions_asked": 2}, False),
        ({"questions_asked": wd.MAX_QUESTIONS}, True),
        ({"questions_asked": 0, "force_draft": True}, True),
    ],
)
async def test_tool_choice_flips_only_at_budget_or_force(
    monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, Any], forced: bool
) -> None:
    provider = _ScriptedProvider([_tool_response(wd.EMIT_TOOL_NAME, _emit())])
    _install(monkeypatch, provider)
    await wd.advance(_opening(), **kwargs)
    expected = {"type": "tool", "name": wd.EMIT_TOOL_NAME} if forced else {"type": "any"}
    assert provider.calls[0]["tool_choice"] == expected


@pytest.mark.asyncio
async def test_transcript_budget_forces_emit(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _ScriptedProvider([_tool_response(wd.EMIT_TOOL_NAME, _emit())])
    _install(monkeypatch, provider)
    await wd.advance([Turn(role="user", text="x" * (wd.MAX_TRANSCRIPT_CHARS + 1))])
    assert provider.calls[0]["tool_choice"] == {"type": "tool", "name": wd.EMIT_TOOL_NAME}


@pytest.mark.asyncio
async def test_context_block_goes_in_first_user_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _ScriptedProvider([_tool_response(wd.EMIT_TOOL_NAME, _emit())])
    _install(monkeypatch, provider)
    transcript = _opening() + [
        Turn(role="assistant", text="Which competitors?"),
        Turn(role="user", text="Acme."),
    ]
    await wd.advance(transcript, context_block="CTX roster")
    messages = provider.calls[0]["messages"]
    assert messages[0]["role"] == "user"
    assert messages[0]["content"].startswith("CTX roster")
    assert messages[0]["content"].endswith("I want a weekly competitor digest.")
    assert "CTX roster" not in messages[-1]["content"]


@pytest.mark.asyncio
async def test_previous_draft_goes_in_latest_user_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refining edits the real draft, not the model's memory of a summary."""
    provider = _ScriptedProvider([_tool_response(wd.EMIT_TOOL_NAME, _emit())])
    _install(monkeypatch, provider)
    previous = DynamicWorkflowDef.model_validate(_definition(title="Old title"))
    transcript = _opening() + [
        Turn(role="assistant", text="A weekly digest."),
        Turn(role="user", text="Rename it."),
    ]
    await wd.advance(transcript, previous_draft=previous)
    messages = provider.calls[0]["messages"]
    assert "Old title" in messages[-1]["content"]
    assert messages[-1]["content"].endswith("Rename it.")
    assert "Old title" not in messages[0]["content"]
    # Server-owned fields never reach the model.
    assert "created_at" not in messages[-1]["content"]


@pytest.mark.asyncio
async def test_trailing_assistant_turn_gets_continue_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _ScriptedProvider([_tool_response(wd.EMIT_TOOL_NAME, _emit())])
    _install(monkeypatch, provider)
    transcript = _opening() + [Turn(role="assistant", text="A weekly digest.")]
    await wd.advance(transcript, force_draft=True)
    assert provider.calls[0]["messages"][-1]["role"] == "user"


@pytest.mark.asyncio
async def test_options_are_clamped_not_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _ScriptedProvider(
        [
            _tool_response(
                wd.ASK_TOOL_NAME,
                {"question": "When?", "options": ["a", "", 3, "b", "c", "d", "e" * 500]},
            )
        ]
    )
    _install(monkeypatch, provider)
    result = await wd.advance(_opening())
    assert isinstance(result, wd.DesignerQuestion)
    assert result.options == ["a", "b", "c", "d"]


@pytest.mark.asyncio
async def test_invalid_definition_gets_one_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    bad_steps = [
        {"kind": "synthesis", "id": "assemble", "title": "Assemble"},
        {"kind": "specialist", "id": "scan", "title": "Scan", "specialist": "cso", "goal": "Go."},
    ]
    provider = _ScriptedProvider(
        [
            _tool_response(wd.EMIT_TOOL_NAME, _emit(steps=bad_steps)),
            _tool_response(wd.EMIT_TOOL_NAME, _emit()),
        ]
    )
    _install(monkeypatch, provider)
    result = await wd.advance(_opening())
    assert isinstance(result, wd.WorkflowDraft)
    repair = provider.calls[1]
    assert repair["tool_choice"] == {"type": "tool", "name": wd.EMIT_TOOL_NAME}
    assert "synthesis" in repair["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_saved_custom_name_triggers_repair(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "episodic.db"
    dynamic_store.initialize_dynamic_workflows_db(db_path)
    monkeypatch.setattr(dynamic_store, "DB_PATH", db_path)
    dynamic_store.upsert_definition(DynamicWorkflowDef.model_validate(_definition()))
    monkeypatch.setattr(wd, "_name_taken", _REAL_NAME_TAKEN)

    provider = _ScriptedProvider(
        [
            _tool_response(wd.EMIT_TOOL_NAME, _emit()),
            _tool_response(wd.EMIT_TOOL_NAME, _emit(name="weekly_competitor_digest_v2")),
        ]
    )
    _install(monkeypatch, provider)
    result = await wd.advance(_opening())
    assert isinstance(result, wd.WorkflowDraft)
    assert result.definition.name == "weekly_competitor_digest_v2"
    assert "already used" in provider.calls[1]["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_two_bad_drafts_raise_fixed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "zz_secret_goal_zz"
    bad = _emit(steps=[{"kind": "specialist", "id": "scan", "title": "S", "specialist": "cso", "goal": secret}])
    provider = _ScriptedProvider(
        [_tool_response(wd.EMIT_TOOL_NAME, bad), _tool_response(wd.EMIT_TOOL_NAME, bad)]
    )
    _install(monkeypatch, provider)
    with pytest.raises(wd.WorkflowDesignerError) as exc:
        await wd.advance(_opening(secret))
    assert secret not in str(exc.value)


@pytest.mark.asyncio
async def test_server_owned_fields_are_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _ScriptedProvider(
        [
            _tool_response(
                wd.EMIT_TOOL_NAME,
                _emit(is_active=False, created_at="1999-01-01", updated_at="1999-01-01"),
            )
        ]
    )
    _install(monkeypatch, provider)
    result = await wd.advance(_opening())
    assert isinstance(result, wd.WorkflowDraft)
    assert result.definition.is_active is True
    assert result.definition.created_at == ""


@pytest.mark.asyncio
async def test_question_while_forced_retries_then_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _ScriptedProvider(
        [
            _tool_response(wd.ASK_TOOL_NAME, {"question": "One more?"}),
            _tool_response(wd.ASK_TOOL_NAME, {"question": "Another?"}),
        ]
    )
    _install(monkeypatch, provider)
    with pytest.raises(wd.WorkflowDesignerError):
        await wd.advance(_opening(), force_draft=True)
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_provider_error_is_fixed_string(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "zz_request_body_zz"
    provider = _ScriptedProvider([RuntimeError(secret)])
    _install(monkeypatch, provider)
    with pytest.raises(wd.WorkflowDesignerError) as exc:
        await wd.advance(_opening())
    assert secret not in str(exc.value)


@pytest.mark.asyncio
async def test_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Slow:
        async def messages_create(self, **kwargs: Any) -> Any:
            await asyncio.sleep(5)

    _install(monkeypatch, _Slow())
    monkeypatch.setattr(wd, "get_settings", lambda: SimpleNamespace(interview_timeout_s=0.01))
    with pytest.raises(wd.WorkflowDesignerTimeout):
        await wd.advance(_opening())


@pytest.mark.asyncio
async def test_empty_transcript_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _ScriptedProvider([]))
    with pytest.raises(wd.WorkflowDesignerError):
        await wd.advance([Turn(role="user", text="   ")])


def test_context_block_lists_specialists_roster_and_taken_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    person = SimpleNamespace(id=7, full_name="Dana Reyes", role="CEO", is_principal=True)
    monkeypatch.setattr("openexecutive.people.store.list_people", lambda: [person])
    monkeypatch.setattr(
        "openexecutive.workflows.dynamic_store.list_definitions",
        lambda active_only=True: [DynamicWorkflowDef.model_validate(_definition())],
    )
    block = wd.build_context_block()
    assert "- cfo:" in block
    assert "triage" not in block
    assert "person_id 7: Dana Reyes — CEO (the user)" in block
    assert "weekly_competitor_digest" in block
    assert "board_prep" in block  # built-ins are taken too
    assert "Growth & GTM" in block


def test_context_block_survives_a_broken_roster(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> list[Any]:
        raise RuntimeError("db locked")

    monkeypatch.setattr("openexecutive.people.store.list_people", _boom)
    monkeypatch.setattr(
        "openexecutive.workflows.dynamic_store.list_definitions", lambda active_only=True: []
    )
    assert "nobody yet" in wd.build_context_block()


def test_agent_is_in_council_but_not_consultable() -> None:
    from openexecutive.api.routes.agents import _agent_registry
    from openexecutive.orchestrator.router import SPECIALIST_REGISTRY

    assert WORKFLOW_DESIGNER_AGENT_ID in _agent_registry()
    assert WORKFLOW_DESIGNER_AGENT_ID not in SPECIALIST_REGISTRY
