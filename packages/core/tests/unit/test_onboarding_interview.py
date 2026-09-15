"""Conversational onboarding: the interview loop.

Covers the two-tool contract (constant + sorted array, tool_choice flipping
only at the budget), the one repair retry, referential-integrity validation,
and — most importantly — that no failure path ever echoes the user's input
back, because these transcripts carry ARR, burn, and runway.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from openexecutive.agents.onboarding_interviewer import (
    ONBOARDING_INTERVIEWER_SYSTEM,
)
from openexecutive.memory.company_profile import CompanyProfile
from openexecutive.onboarding import interview as iv


def _draft_dict(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "profile": {
            "name": "Northwind Tools",
            "industry": "Industrial supply",
            "stage": "Bootstrapped",
            "mission": "Get the right tool to the job site the same day.",
            "vendors": ["Acme Freight"],
            "tickers": ["FAST"],
        },
        "people": [
            {"full_name": "Dana Reyes", "role": "CEO", "is_principal": True},
            {"full_name": "Sam Okafor", "role": "Head of Ops"},
        ],
        "departments": [
            {"title": "Operations", "mission": "Fulfilment", "head_person_name": "Sam Okafor"},
        ],
        "confidence_notes": ["Monthly burn not stated."],
        "summary": "A bootstrapped industrial supplier.",
    }
    base.update(overrides)
    return base


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
        return self._responses.pop(0)


@pytest.fixture(autouse=True)
def _no_usage_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    """log_model_usage writes to the default episodic DB; keep tests isolated."""
    monkeypatch.setattr(
        "openexecutive.audit.usage.log_model_usage", lambda *a, **k: None
    )


def _install(monkeypatch: pytest.MonkeyPatch, provider: Any) -> None:
    monkeypatch.setattr(
        "openexecutive.providers.registry.get_provider", lambda model: provider
    )


def _opening(text: str = "We sell industrial tools.") -> list[iv.Turn]:
    return [iv.Turn(role="user", text=text)]


@pytest.mark.asyncio
async def test_asks_then_emits(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _ScriptedProvider(
        [
            _tool_response(iv.ASK_TOOL_NAME, {"question": "Who runs ops?", "hint": "A name."}),
            _tool_response(iv.EMIT_TOOL_NAME, _draft_dict()),
        ]
    )
    _install(monkeypatch, provider)

    transcript = _opening()
    first = await iv.advance(transcript, questions_asked=0)
    assert isinstance(first, iv.Question)
    assert first.question == "Who runs ops?"

    transcript += [
        iv.Turn(role="assistant", text=first.question),
        iv.Turn(role="user", text="Sam Okafor runs ops."),
    ]
    second = await iv.advance(transcript, questions_asked=1)
    assert isinstance(second, iv.CompanyDraft)
    assert second.profile.name == "Northwind Tools"

    # Second call replays the whole conversation, not just the latest turn.
    replayed = provider.calls[1]["messages"]
    assert [m["content"] for m in replayed] == [
        "We sell industrial tools.",
        "Who runs ops?",
        "Sam Okafor runs ops.",
    ]


@pytest.mark.asyncio
async def test_system_prompt_is_the_constant_and_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _ScriptedProvider([_tool_response(iv.EMIT_TOOL_NAME, _draft_dict())])
    _install(monkeypatch, provider)
    await iv.advance(_opening())

    system = provider.calls[0]["system"]
    assert len(system) == 1
    assert system[0]["text"] == ONBOARDING_INTERVIEWER_SYSTEM
    assert system[0]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_tools_are_constant_and_sorted_on_every_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Varying the tool array mid-conversation would break the cached prefix."""
    provider = _ScriptedProvider(
        [
            _tool_response(iv.ASK_TOOL_NAME, {"question": "How many people?"}),
            _tool_response(iv.EMIT_TOOL_NAME, _draft_dict()),
        ]
    )
    _install(monkeypatch, provider)

    await iv.advance(_opening(), questions_asked=0)
    await iv.advance(_opening(), questions_asked=iv.MAX_QUESTIONS)

    for call in provider.calls:
        assert [t["name"] for t in call["tools"]] == [
            iv.ASK_TOOL_NAME,
            iv.EMIT_TOOL_NAME,
        ]


@pytest.mark.asyncio
async def test_tool_choice_is_any_under_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _ScriptedProvider(
        [_tool_response(iv.ASK_TOOL_NAME, {"question": "What stage?"})]
    )
    _install(monkeypatch, provider)
    await iv.advance(_opening(), questions_asked=2)
    assert provider.calls[0]["tool_choice"] == {"type": "any"}


@pytest.mark.asyncio
async def test_question_budget_forces_emit(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _ScriptedProvider([_tool_response(iv.EMIT_TOOL_NAME, _draft_dict())])
    _install(monkeypatch, provider)
    result = await iv.advance(_opening(), questions_asked=iv.MAX_QUESTIONS)
    assert isinstance(result, iv.CompanyDraft)
    assert provider.calls[0]["tool_choice"] == {
        "type": "tool",
        "name": iv.EMIT_TOOL_NAME,
    }


@pytest.mark.asyncio
async def test_force_draft_forces_emit(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _ScriptedProvider([_tool_response(iv.EMIT_TOOL_NAME, _draft_dict())])
    _install(monkeypatch, provider)
    await iv.advance(_opening(), force_draft=True, questions_asked=0)
    assert provider.calls[0]["tool_choice"] == {
        "type": "tool",
        "name": iv.EMIT_TOOL_NAME,
    }


@pytest.mark.asyncio
async def test_transcript_budget_forces_emit(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _ScriptedProvider([_tool_response(iv.EMIT_TOOL_NAME, _draft_dict())])
    _install(monkeypatch, provider)
    huge = [iv.Turn(role="user", text="x" * (iv.MAX_TRANSCRIPT_CHARS + 1))]
    await iv.advance(huge, questions_asked=0)
    assert provider.calls[0]["tool_choice"] == {
        "type": "tool",
        "name": iv.EMIT_TOOL_NAME,
    }


@pytest.mark.asyncio
async def test_validation_error_triggers_one_repair_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad = _draft_dict()
    bad["profile"] = {**bad["profile"], "founding_year": "two thousand"}
    provider = _ScriptedProvider(
        [
            _tool_response(iv.EMIT_TOOL_NAME, bad),
            _tool_response(iv.EMIT_TOOL_NAME, _draft_dict()),
        ]
    )
    _install(monkeypatch, provider)

    result = await iv.advance(_opening())
    assert isinstance(result, iv.CompanyDraft)
    assert len(provider.calls) == 2
    # The repair turn tells the model what to fix and forces the emit tool.
    repair = provider.calls[1]["messages"][-1]["content"]
    assert "not usable" in repair
    assert iv.EMIT_TOOL_NAME in repair
    assert provider.calls[1]["tool_choice"] == {
        "type": "tool",
        "name": iv.EMIT_TOOL_NAME,
    }


@pytest.mark.asyncio
async def test_referential_error_triggers_repair_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad = _draft_dict(
        departments=[{"title": "Ops", "head_person_name": "Nobody At All"}]
    )
    provider = _ScriptedProvider(
        [
            _tool_response(iv.EMIT_TOOL_NAME, bad),
            _tool_response(iv.EMIT_TOOL_NAME, _draft_dict()),
        ]
    )
    _install(monkeypatch, provider)

    result = await iv.advance(_opening())
    assert isinstance(result, iv.CompanyDraft)
    assert "Nobody At All" in provider.calls[1]["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_double_validation_failure_does_not_echo_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The core privacy regression: a ValidationError's str() embeds values."""
    bad = _draft_dict()
    bad["profile"] = {**bad["profile"], "annual_revenue_arr": "SECRETARR"}
    provider = _ScriptedProvider(
        [
            _tool_response(iv.EMIT_TOOL_NAME, bad),
            _tool_response(iv.EMIT_TOOL_NAME, bad),
        ]
    )
    _install(monkeypatch, provider)

    with pytest.raises(iv.InterviewError) as excinfo:
        await iv.advance(_opening("Our ARR is SECRETARR and burn is SECRETBURN."))
    assert "SECRETARR" not in str(excinfo.value)
    assert "SECRETBURN" not in str(excinfo.value)


@pytest.mark.asyncio
async def test_provider_exception_does_not_echo_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Boom:
        async def messages_create(self, **kwargs: Any) -> Any:
            raise RuntimeError("400 bad request: {'burn': 'SECRETBURN'}")

    _install(monkeypatch, Boom())
    with pytest.raises(iv.InterviewError) as excinfo:
        await iv.advance(_opening("burn is SECRETBURN"))
    assert "SECRETBURN" not in str(excinfo.value)


@pytest.mark.asyncio
async def test_existing_profile_context_goes_in_user_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caching invariant: dynamic content never enters the cached system block."""
    provider = _ScriptedProvider([_tool_response(iv.EMIT_TOOL_NAME, _draft_dict())])
    _install(monkeypatch, provider)

    existing = CompanyProfile(name="Acme Widgets", industry="Widgets")
    await iv.advance(_opening(), existing_profile=existing)

    call = provider.calls[0]
    assert "Acme Widgets" in call["messages"][0]["content"]
    assert "Acme Widgets" not in call["system"][0]["text"]
    # And the user's own opening text survives the prepend.
    assert "We sell industrial tools." in call["messages"][0]["content"]


@pytest.mark.asyncio
async def test_empty_existing_profile_is_not_prepended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _ScriptedProvider([_tool_response(iv.EMIT_TOOL_NAME, _draft_dict())])
    _install(monkeypatch, provider)
    await iv.advance(_opening(), existing_profile=CompanyProfile())
    assert provider.calls[0]["messages"][0]["content"] == "We sell industrial tools."


@pytest.mark.asyncio
async def test_people_draft_drops_contact_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OE never auto-imports contacts, even if the model emits them anyway."""
    leaky = _draft_dict(
        people=[
            {
                "full_name": "Dana Reyes",
                "role": "CEO",
                "is_principal": True,
                "email": "dana@example.com",
                "slack_user_id": "U123",
            }
        ],
        departments=[],
    )
    provider = _ScriptedProvider([_tool_response(iv.EMIT_TOOL_NAME, leaky)])
    _install(monkeypatch, provider)

    draft = await iv.advance(_opening())
    assert isinstance(draft, iv.CompanyDraft)
    person = draft.people[0]
    assert not hasattr(person, "email")
    dumped = person.model_dump()
    assert "email" not in dumped
    assert "slack_user_id" not in dumped


@pytest.mark.asyncio
async def test_timeout_raises_interview_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Slow:
        async def messages_create(self, **kwargs: Any) -> Any:
            await asyncio.sleep(5)

    _install(monkeypatch, Slow())
    monkeypatch.setattr(
        "openexecutive.onboarding.interview.get_settings",
        lambda: SimpleNamespace(chat_stream_timeout_s=0.01),
    )
    with pytest.raises(iv.InterviewTimeout):
        await iv.advance(_opening())


@pytest.mark.asyncio
async def test_no_tool_call_is_an_interview_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    textonly = SimpleNamespace(content=[SimpleNamespace(type="text", text="hello")])
    _install(monkeypatch, _ScriptedProvider([textonly]))
    with pytest.raises(iv.InterviewError):
        await iv.advance(_opening())


@pytest.mark.asyncio
async def test_usage_is_logged_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    logged: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "openexecutive.audit.usage.log_model_usage",
        lambda message, **kw: logged.append(kw),
    )
    provider = _ScriptedProvider([_tool_response(iv.EMIT_TOOL_NAME, _draft_dict())])
    _install(monkeypatch, provider)
    await iv.advance(_opening())
    assert len(logged) == 1
    assert logged[0]["actor"] == "onboarding_interviewer"


@pytest.mark.asyncio
async def test_empty_transcript_is_an_interview_error() -> None:
    with pytest.raises(iv.InterviewError):
        await iv.advance([])


# ── validate_draft ───────────────────────────────────────────────────────────


def _draft(**overrides: Any) -> iv.CompanyDraft:
    return iv.CompanyDraft.model_validate(_draft_dict(**overrides))


def _details(errors: list[iv.DraftError]) -> list[str]:
    """The model-facing rendering — the one that may quote input."""
    return [e.detail for e in errors]


def _safes(errors: list[iv.DraftError]) -> list[str]:
    """The client-facing rendering — the one the route puts in a 422."""
    return [e.safe for e in errors]


def test_validate_draft_accepts_a_good_draft() -> None:
    assert iv.validate_draft(_draft()) == []


def test_validate_draft_requires_exactly_one_principal() -> None:
    none_flagged = _draft(
        people=[{"full_name": "Dana Reyes", "role": "CEO"}], departments=[]
    )
    assert any("is_principal" in e for e in _details(iv.validate_draft(none_flagged)))

    two_flagged = _draft(
        people=[
            {"full_name": "Dana Reyes", "is_principal": True},
            {"full_name": "Sam Okafor", "is_principal": True},
        ],
        departments=[],
    )
    assert any("is_principal" in e for e in _details(iv.validate_draft(two_flagged)))


def test_validate_draft_rejects_duplicate_person_names() -> None:
    dupes = _draft(
        people=[
            {"full_name": "Dana Reyes", "is_principal": True},
            {"full_name": "Dana Reyes", "role": "COO"},
        ],
        departments=[],
    )
    assert any("duplicate full_name" in e for e in _details(iv.validate_draft(dupes)))


def test_validate_draft_catches_unknown_department_head() -> None:
    orphan = _draft(departments=[{"title": "Ops", "head_person_name": "Ghost"}])
    errors = _details(iv.validate_draft(orphan))
    assert any("not in the roster" in e for e in errors)


def test_validate_draft_allows_a_headless_department() -> None:
    assert iv.validate_draft(_draft(departments=[{"title": "Ops"}])) == []


def test_validate_draft_rejects_colliding_department_titles() -> None:
    colliding = _draft(
        departments=[{"title": "Customer Success"}, {"title": "customer success"}]
    )
    assert any("duplicate department title" in e for e in _details(iv.validate_draft(colliding)))


def test_validate_draft_requires_a_company_name() -> None:
    nameless = _draft()
    nameless.profile.name = "   "
    assert any("profile.name" in e for e in _details(iv.validate_draft(nameless)))


def test_validate_draft_requires_at_least_one_person() -> None:
    assert any(
        "at least one person" in e
        for e in _details(iv.validate_draft(_draft(people=[], departments=[])))
    )


def test_emit_schema_covers_vendors_tickers_and_org_structure() -> None:
    """The fixture emit tool omits all three; vendors/tickers drive watch policy."""
    props = iv._EMIT_TOOL["input_schema"]["properties"]["profile"]["properties"]
    for key in ("vendors", "tickers", "org_structure"):
        assert key in props


def test_department_authority_level_defaults_to_propose_only() -> None:
    draft = _draft(departments=[{"title": "Ops"}])
    assert draft.departments[0].authority_level.value == "propose_only"


def test_agent_in_council_registry_but_not_specialist_registry() -> None:
    from openexecutive.api.routes.agents import _agent_registry
    from openexecutive.orchestrator.router import SPECIALIST_REGISTRY

    assert "onboarding_interviewer" in _agent_registry()
    assert "onboarding_interviewer" not in SPECIALIST_REGISTRY


def test_degenerate_titles_collide_like_the_store_would() -> None:
    """Two titles that both slugify to nothing land on one slug on insert."""
    colliding = _draft(departments=[{"title": "###"}, {"title": "!!!"}])
    assert any("duplicate department title" in e for e in _details(iv.validate_draft(colliding)))


def test_validate_draft_bounds_name_role_and_mission_lengths() -> None:
    """Bounds live here, not in Field(max_length=), so 422s can't echo input."""
    long_person = _draft(
        people=[{"full_name": "SECRETNAME" * 30, "is_principal": True}], departments=[]
    )
    assert any("name is too long" in e for e in _safes(iv.validate_draft(long_person)))

    long_role = _draft(
        people=[{"full_name": "Dana", "role": "R" * 500, "is_principal": True}],
        departments=[],
    )
    assert any("role is too long" in e for e in _safes(iv.validate_draft(long_role)))

    long_title = _draft(departments=[{"title": "T" * 300}])
    assert any(
        "department name is too long" in e for e in _safes(iv.validate_draft(long_title))
    )

    long_mission = _draft(departments=[{"title": "Ops", "mission": "m" * 2500}])
    assert any(
        "description is too long" in e for e in _safes(iv.validate_draft(long_mission))
    )


def test_validate_draft_bounds_profile_text() -> None:
    """The profile renders into the CACHED system prompt on every turn, so an
    unbounded field is a permanent per-request cost, not one big row."""
    huge = _draft()
    huge.profile.mission = "m" * (iv.MAX_PROFILE_TEXT_CHARS + 1)
    assert any("mission is too long" in e for e in _safes(iv.validate_draft(huge)))

    huge2 = _draft()
    huge2.profile.name = "n" * (iv.MAX_PROFILE_TEXT_CHARS + 1)
    assert any("name is too long" in e for e in _safes(iv.validate_draft(huge2)))


def test_safe_error_strings_never_quote_input() -> None:
    """The route returns .safe; only the model's repair turn sees .detail."""
    leaky = _draft(
        people=[
            {"full_name": "SECRETPERSON", "is_principal": True},
            {"full_name": "SECRETPERSON"},
        ],
        departments=[{"title": "SECRETDEPT", "head_person_name": "SECRETHEAD"}],
    )
    errors = iv.validate_draft(leaky)
    assert errors
    blob = " ".join(_safes(errors))
    for secret in ("SECRETPERSON", "SECRETDEPT", "SECRETHEAD"):
        assert secret not in blob, f"{secret} leaked into a client-facing string"
    # The model, by contrast, must get the specifics to repair the draft.
    assert "SECRETHEAD" in " ".join(_details(errors))


def test_duplicate_person_names_are_matched_case_insensitively() -> None:
    """save_onboarding_people upserts on a case-folded name, so a case-only
    difference is the SAME person — a case-sensitive check let both through and
    the upsert then collapsed them, potentially leaving no principal at all."""
    variants = _draft(
        people=[
            {"full_name": "JANE DOE", "role": "CEO", "is_principal": True},
            {"full_name": "Jane Doe", "role": "Advisor"},
        ],
        departments=[],
    )
    assert any("duplicate full_name" in e for e in _details(iv.validate_draft(variants)))


def test_department_head_is_matched_case_insensitively() -> None:
    ok = _draft(
        people=[{"full_name": "Dana Reyes", "is_principal": True}],
        departments=[{"title": "Ops", "head_person_name": "dana reyes"}],
    )
    assert iv.validate_draft(ok) == []


def test_transcript_budget_allows_a_full_legal_start() -> None:
    """A 20k description plus the documented 8 attachments is ~140k. A lower
    ceiling locked the user out of the conversation on their first turn."""
    from openexecutive.api.models import ONBOARD_MESSAGE_MAX_CHARS
    from openexecutive.api.intake_uploads import (
        _INTAKE_GEN_CHARS_PER_FILE,
        _INTAKE_MAX_FILES,
    )

    largest_legal_start = (
        ONBOARD_MESSAGE_MAX_CHARS + _INTAKE_MAX_FILES * _INTAKE_GEN_CHARS_PER_FILE
    )
    assert iv.MAX_TRANSCRIPT_CHARS > largest_legal_start


@pytest.mark.asyncio
async def test_transcript_never_ends_on_an_assistant_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A draft records itself as an assistant turn, so "ask me more" then
    "draft again" left one trailing — a prefill, which the API rejects
    alongside a forced tool_choice."""
    provider = _ScriptedProvider([_tool_response(iv.EMIT_TOOL_NAME, _draft_dict())])
    _install(monkeypatch, provider)

    transcript = [
        iv.Turn(role="user", text="We are Northwind Tools, 40 people."),
        iv.Turn(role="assistant", text="A bootstrapped industrial supplier."),
    ]
    await iv.advance(transcript, force_draft=True)

    roles = [m["role"] for m in provider.calls[0]["messages"]]
    assert roles[-1] == "user", f"trailing assistant prefill: {roles}"
    assert all(a != b for a, b in zip(roles, roles[1:]))
