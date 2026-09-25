"""The company stage reaches specialists in the USER turn, never the system prompt.

Specialists never see the company profile — it sits in the Executive's cached
system block — so a CFO asked about runway for a bootstrapped solo business
defaulted to venture benchmarks. The stage now rides in a `<company_stage>`
user-turn tag. The specialist's system block is a cached constant and must stay
byte-identical whatever the profile says.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from openexecutive.agents.base import BaseAgent
from openexecutive.memory.company_profile import CompanyProfile
from openexecutive.orchestrator import router
from openexecutive.orchestrator.executive import Executive
from openexecutive.orchestrator.schedule_tools import set_session
from openexecutive.orchestrator.session import Session
from openexecutive.prompts.domain_prompts import CFO_PROMPT, CSO_PROMPT, SALES_PROMPT

from ._agent_loop_fakes import FinalMsg, ScriptedProvider, TextBlock, ToolUseBlock


class _Agent(BaseAgent):
    name = "unit_stage_agent"
    domain = "finance"
    model = "claude-test"

    def get_system_prompt(self) -> str:
        return "STATIC SYSTEM PROMPT"


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture every provider request and keep usage logging out of the DB."""
    calls: list[dict[str, Any]] = []

    async def fake_create(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")])

    monkeypatch.setattr("openexecutive.agents.overrides.get_override", lambda _n: None)
    monkeypatch.setattr(
        "openexecutive.agents.base.get_provider",
        lambda _m: SimpleNamespace(messages_create=fake_create),
    )
    monkeypatch.setattr("openexecutive.agents.base.log_model_usage", lambda *a, **k: None)
    return calls


def _user_turn(call: dict[str, Any]) -> str:
    return call["messages"][0]["content"]


def test_stage_tag_leads_the_user_turn(captured: list[dict[str, Any]]) -> None:
    asyncio.run(
        _Agent().analyze(
            "How long is my runway?",
            context="ctx",
            episodic_context="EPISODIC",
            company_stage="  Bootstrapped,\n solo founder  ",
        )
    )

    user = _user_turn(captured[0])
    assert user.startswith("<company_stage>\nBootstrapped, solo founder\n</company_stage>\n\n")
    assert "<past_decisions>\nEPISODIC" in user
    assert user.endswith("How long is my runway?")


@pytest.mark.parametrize("stage", ["", "   ", "\n"])
def test_no_tag_when_stage_is_empty(captured: list[dict[str, Any]], stage: str) -> None:
    asyncio.run(_Agent().analyze("q", company_stage=stage))

    assert "<company_stage>" not in _user_turn(captured[0])
    assert _user_turn(captured[0]) == "q"


def test_system_prompt_bytes_do_not_depend_on_the_stage(captured: list[dict[str, Any]]) -> None:
    agent = _Agent()
    for stage in ("", "Series B", "Bootstrapped, solo founder"):
        asyncio.run(agent.analyze("q", company_stage=stage))

    systems = [call["system"] for call in captured]
    assert systems[0] == [
        {"type": "text", "text": "STATIC SYSTEM PROMPT", "cache_control": {"type": "ephemeral"}}
    ]
    assert systems[0] == systems[1] == systems[2]


def test_real_specialist_system_prompt_is_unchanged_by_the_stage(
    captured: list[dict[str, Any]],
) -> None:
    cfo = router.SPECIALIST_REGISTRY["cfo"]
    asyncio.run(cfo.analyze("q", company_stage=""))
    asyncio.run(cfo.analyze("q", company_stage="Bootstrapped"))

    assert captured[0]["system"][0]["text"] == CFO_PROMPT
    assert captured[0]["system"] == captured[1]["system"]


# ── router: where the stage comes from ───────────────────────────────────────


def _profile_on_disk(tmp_path, monkeypatch: pytest.MonkeyPatch, stage: str) -> None:
    path = tmp_path / "profile.yaml"
    CompanyProfile(name="Acme", stage=stage).save_to_yaml(path)
    monkeypatch.setattr(
        "openexecutive.onboarding.profile_builder.load_or_create_profile",
        lambda path=path: CompanyProfile.load_from_yaml(path),
    )


def test_load_company_stage_reads_the_profile_fresh(tmp_path, monkeypatch) -> None:
    _profile_on_disk(tmp_path, monkeypatch, "Seed")
    assert router.load_company_stage() == "Seed"

    CompanyProfile(name="Acme", stage="Bootstrapped").save_to_yaml(tmp_path / "profile.yaml")
    assert router.load_company_stage() == "Bootstrapped"


def test_load_company_stage_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> CompanyProfile:
        raise ValueError("bad yaml")

    monkeypatch.setattr("openexecutive.onboarding.profile_builder.load_or_create_profile", boom)
    assert router.load_company_stage() == ""


def test_route_to_specialist_reads_the_stage_when_not_given(tmp_path, monkeypatch) -> None:
    _profile_on_disk(tmp_path, monkeypatch, "Bootstrapped, solo founder")
    mock = AsyncMock(return_value="x")
    with patch.object(router.SPECIALIST_REGISTRY["cfo"], "analyze", mock):
        asyncio.run(router.route_to_specialist("cfo", "q"))

    assert mock.await_args.kwargs["company_stage"] == "Bootstrapped, solo founder"


def test_route_to_specialist_prefers_an_explicit_stage(monkeypatch) -> None:
    monkeypatch.setattr(router, "load_company_stage", lambda: pytest.fail("must not read disk"))
    mock = AsyncMock(return_value="x")
    with patch.object(router.SPECIALIST_REGISTRY["cfo"], "analyze", mock):
        asyncio.run(router.route_to_specialist("cfo", "q", company_stage=""))

    assert mock.await_args.kwargs["company_stage"] == ""


def test_route_parallel_sends_one_stage_to_every_specialist(monkeypatch) -> None:
    reads: list[int] = []

    def fake_load() -> str:
        reads.append(1)
        return "Self-funded"

    monkeypatch.setattr(router, "load_company_stage", fake_load)
    cfo, cso = AsyncMock(return_value="a"), AsyncMock(return_value="b")
    with (
        patch.object(router.SPECIALIST_REGISTRY["cfo"], "analyze", cfo),
        patch.object(router.SPECIALIST_REGISTRY["cso"], "analyze", cso),
    ):
        asyncio.run(
            router.route_parallel(
                [{"specialist": "cfo", "query": "q1"}, {"specialist": "cso", "query": "q2"}],
                retrieved_knowledge_map={},
            )
        )

    assert reads == [1], "read once per batch, not once per specialist"
    assert cfo.await_args.kwargs["company_stage"] == "Self-funded"
    assert cso.await_args.kwargs["company_stage"] == "Self-funded"


def test_route_parallel_uses_the_session_stage_when_given(monkeypatch) -> None:
    monkeypatch.setattr(router, "load_company_stage", lambda: pytest.fail("must not read disk"))
    cfo = AsyncMock(return_value="a")
    with patch.object(router.SPECIALIST_REGISTRY["cfo"], "analyze", cfo):
        asyncio.run(
            router.route_parallel(
                [{"specialist": "cfo", "query": "q"}],
                retrieved_knowledge_map={},
                company_stage="Series A",
            )
        )

    assert cfo.await_args.kwargs["company_stage"] == "Series A"


def _consult_once(session: Session | None, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Drive one real consult_specialist round; return route_parallel's kwargs."""
    seen: dict[str, Any] = {}

    async def fake_route_parallel(calls: list[dict[str, str]], **kwargs: Any) -> list[str]:
        seen.update(kwargs)
        return ["analysis"] * len(calls)

    monkeypatch.setattr("openexecutive.orchestrator.executive.route_parallel", fake_route_parallel)
    # The round writes a specialist_consult audit row; keep it out of the
    # default ./episodic_memory.db (CLAUDE.md: audit-log test pollution).
    monkeypatch.setattr("openexecutive.orchestrator.executive.audit_log", lambda *a, **k: None)
    provider = ScriptedProvider([
        FinalMsg(
            [ToolUseBlock("toolu_1", "consult_specialist", {"specialist": "cfo", "query": "runway?"})],
            "tool_use",
        ),
        FinalMsg([TextBlock("done")], "end_turn"),
    ])

    async def go() -> None:
        with patch("openexecutive.orchestrator.executive.get_provider", return_value=provider):
            async for _ in Executive()._stream_agent_loop(
                system_blocks=[],
                messages=[{"role": "user", "content": "go"}],
                model="claude-test",
            ):
                pass

    with set_session(session):
        asyncio.run(go())
    return seen


def test_executive_passes_the_session_profile_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    """The chat turn hands specialists the stage of the profile the Executive
    itself reasons over — which is also the one an eval scenario injects."""
    session = Session(company_profile=CompanyProfile(name="Solo Co", stage="Bootstrapped"))

    assert _consult_once(session, monkeypatch)["company_stage"] == "Bootstrapped"


def test_executive_without_a_session_profile_defers_to_the_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No session profile (onboarding not done when the session started):
    pass None so the router reads the profile from disk."""
    assert _consult_once(Session(company_profile=None), monkeypatch)["company_stage"] is None


# ── the prompts themselves ───────────────────────────────────────────────────


@pytest.mark.parametrize("prompt", [CFO_PROMPT, CSO_PROMPT, SALES_PROMPT])
def test_stage_guidance_is_static_and_names_the_tag(prompt: str) -> None:
    assert "<company_stage>" in prompt
    assert "{" not in prompt and "}" not in prompt, "cached constant: no template slots"


def test_cfo_guidance_puts_cash_before_fundraising() -> None:
    lower = CFO_PROMPT.lower()
    for phrase in ("bootstrapped", "runway at the current burn", "receivables", "owner pay",
                   "tax set-aside", "only if the founder wants"):
        assert phrase in lower, phrase
