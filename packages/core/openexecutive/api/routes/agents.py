from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel

from openexecutive.agents.overrides import (
    EXECUTIVE_AGENT_ID as EXECUTIVE_ID,
)
from openexecutive.agents.overrides import (
    AgentHistoryEntry,
    clear_override,
    get_override,
    list_history,
    rollback_to,
    set_override,
)
from openexecutive.audit.usage import log_model_usage

logger = logging.getLogger(__name__)

router = APIRouter()

# Models the Agent Council UI is allowed to switch agents to. Sourced
# from ``providers.allowed_models()`` so the dropdown always matches
# what the runtime can actually serve — when OPENROUTER_ENABLED is on,
# the curated OpenRouter set folds in automatically.
from openexecutive.providers import allowed_models_for as _allowed_models_for  # noqa: E402


def _allowed(agent_id: str | None = None) -> list[str]:
    return _allowed_models_for(agent_id)


class AgentMeta(BaseModel):
    name: str
    role: str
    model: str
    deep_reasoning: bool
    domains: list[str]
    has_override: bool = False


class AgentDetail(BaseModel):
    name: str
    role: str
    role_default: str
    model: str
    model_default: str
    deep_reasoning: bool
    deep_reasoning_default: bool
    prompt: str
    prompt_default: str
    domains: list[str]
    has_override: bool
    overridden_fields: list[str]
    updated_at: str | None = None
    voice_persona_slug: str | None = None
    # Per-specialist research-mode focus tail (effective value + code
    # default). null for agents that have no research scope (executive /
    # utility knobs); the UI shows the editor only when a default exists.
    research_focus: str | None = None
    research_focus_default: str | None = None


class AgentPatch(BaseModel):
    prompt: str | None = None
    model: str | None = None
    use_deep_reasoning: bool | None = None
    role: str | None = None
    voice_persona_slug: str | None = None
    research_focus: str | None = None


class AgentTestRequest(BaseModel):
    query: str
    prompt: str | None = None
    model: str | None = None
    use_deep_reasoning: bool | None = None


class AgentTestResponse(BaseModel):
    response: str


def _role_default(name: str) -> str:
    if name == "executive":
        return "Executive"
    if name == "quality_judge":
        return "Committee · Quality Judge"
    if name == "utility_fast":
        return "Utility · Fast model (gates, titles, decision parsing)"
    if name == "research":
        return "Utility · Research model (executive_research fan-out)"
    if name == "fixture_generator":
        return "Utility · Company fixture generator"
    if name == "engagement_intake":
        return "Utility · Client engagement intake (grounded company drafts)"
    if name == "onboarding_interviewer":
        return "Utility · Company setup interviewer (conversational onboarding)"
    from openexecutive.orchestrator.router import SPECIALIST_DESCRIPTIONS

    description = SPECIALIST_DESCRIPTIONS.get(name, name)
    return description.split(" — ")[0] if " — " in description else description


_executive_proxy: Any = None
_quality_judge_agent: Any = None
_utility_fast_agent: Any = None
_research_council_agent: Any = None
_fixture_generator_agent: Any = None
_engagement_intake_agent: Any = None
_onboarding_interviewer_agent: Any = None


def _agent_registry() -> dict[str, Any]:
    """Map of agent_id → agent instance for the Council UI.

    Includes the Executive proxy, all consult-routable specialists, the
    Committee's Quality Judge, the ``utility_fast`` virtual agent that
    controls the model used by non-specialist Haiku call sites (Discord
    response gate / title gen, wait_for_human parser, inbound resolver
    disambiguation), and the ``fixture_generator`` that authors company
    simulator fixtures, the ``onboarding_interviewer`` that runs the
    conversational company-setup flow, and the ``research`` virtual agent
    whose model + deep-reasoning drive the executive_research fan-out. These
    live here (not in SPECIALIST_REGISTRY) because we want them overridable
    through the Council but NOT callable via the ``consult_specialist`` tool.
    """
    global _executive_proxy, _quality_judge_agent, _utility_fast_agent
    global _research_council_agent, _fixture_generator_agent
    global _engagement_intake_agent, _onboarding_interviewer_agent
    if _executive_proxy is None:
        from openexecutive.agents.executive_proxy import ExecutiveProxy
        _executive_proxy = ExecutiveProxy()
    if _quality_judge_agent is None:
        from openexecutive.agents.quality_judge import QualityJudgeAgent
        _quality_judge_agent = QualityJudgeAgent()
    if _utility_fast_agent is None:
        from openexecutive.agents.utility_fast import UtilityFastAgent
        _utility_fast_agent = UtilityFastAgent()
    if _research_council_agent is None:
        from openexecutive.agents.research_council import ResearchCouncilAgent
        _research_council_agent = ResearchCouncilAgent()
    if _fixture_generator_agent is None:
        from openexecutive.agents.fixture_generator import FixtureGeneratorAgent
        _fixture_generator_agent = FixtureGeneratorAgent()
    if _engagement_intake_agent is None:
        from openexecutive.agents.engagement_intake import EngagementIntakeAgent
        _engagement_intake_agent = EngagementIntakeAgent()
    if _onboarding_interviewer_agent is None:
        from openexecutive.agents.onboarding_interviewer import (
            OnboardingInterviewerAgent,
        )
        _onboarding_interviewer_agent = OnboardingInterviewerAgent()
    from openexecutive.orchestrator.router import SPECIALIST_REGISTRY

    return {
        "executive": _executive_proxy,
        **SPECIALIST_REGISTRY,
        "quality_judge": _quality_judge_agent,
        "utility_fast": _utility_fast_agent,
        "research": _research_council_agent,
        "fixture_generator": _fixture_generator_agent,
        "engagement_intake": _engagement_intake_agent,
        "onboarding_interviewer": _onboarding_interviewer_agent,
    }


class _ExecutiveDefaults:
    """Defaults for the synthetic 'executive' agent entry.

    The Executive isn't a BaseAgent — it has no analyze() and its system
    prompt is assembled by cache_manager. We expose just enough surface
    for the Council UI to read/edit it like a specialist.
    """

    # Matches _role_default("executive") so the proxy-based path (main) and
    # the synthetic-defaults path agree on what the UI should display.
    role: str = "Executive"
    use_deep_reasoning: bool = False

    @staticmethod
    def model() -> str:
        from openexecutive.config import get_settings

        return get_settings().default_model

    @staticmethod
    def prompt() -> str:
        from openexecutive.prompts.executive_persona import EXECUTIVE_PERSONA_PROMPT

        return EXECUTIVE_PERSONA_PROMPT


def _build_executive_meta() -> AgentMeta:
    ov = get_override(EXECUTIVE_ID)
    role = (
        ov.role if ov is not None and ov.role is not None else _ExecutiveDefaults.role
    )
    model = (
        ov.model
        if ov is not None and ov.model is not None
        else _ExecutiveDefaults.model()
    )
    deep = (
        ov.use_deep_reasoning
        if ov is not None and ov.use_deep_reasoning is not None
        else _ExecutiveDefaults.use_deep_reasoning
    )
    return AgentMeta(
        name=EXECUTIVE_ID,
        role=role,
        model=model,
        deep_reasoning=deep,
        domains=[],
        has_override=ov is not None,
    )


def _build_executive_detail() -> AgentDetail:
    ov = get_override(EXECUTIVE_ID)
    overridden: list[str] = []
    if ov is not None:
        if ov.prompt is not None:
            overridden.append("prompt")
        if ov.model is not None:
            overridden.append("model")
        if ov.use_deep_reasoning is not None:
            overridden.append("use_deep_reasoning")
        if ov.role is not None:
            overridden.append("role")
        if ov.voice_persona_slug is not None:
            overridden.append("voice_persona_slug")
    role_default = _ExecutiveDefaults.role
    model_default = _ExecutiveDefaults.model()
    prompt_default = _ExecutiveDefaults.prompt()
    return AgentDetail(
        name=EXECUTIVE_ID,
        role=(ov.role if ov and ov.role is not None else role_default),
        role_default=role_default,
        model=(ov.model if ov and ov.model is not None else model_default),
        model_default=model_default,
        deep_reasoning=(
            ov.use_deep_reasoning
            if ov and ov.use_deep_reasoning is not None
            else _ExecutiveDefaults.use_deep_reasoning
        ),
        deep_reasoning_default=_ExecutiveDefaults.use_deep_reasoning,
        prompt=(ov.prompt if ov and ov.prompt is not None else prompt_default),
        prompt_default=prompt_default,
        domains=[],
        has_override=ov is not None,
        overridden_fields=overridden,
        updated_at=ov.updated_at if ov else None,
        voice_persona_slug=ov.voice_persona_slug if ov else None,
    )


def _domains_for(name: str) -> list[str]:
    from openexecutive.knowledge.retriever import DOMAIN_ALIASES

    return DOMAIN_ALIASES.get(name, [])


def _build_meta(name: str) -> AgentMeta:
    agent = _agent_registry()[name]
    ov = get_override(name)
    return AgentMeta(
        name=name,
        role=(ov.role if ov and ov.role is not None else _role_default(name)),
        model=agent.effective_model(),
        deep_reasoning=agent.effective_use_deep_reasoning(),
        domains=_domains_for(name),
        has_override=ov is not None,
    )


def _build_detail(name: str) -> AgentDetail:
    from openexecutive.monitoring.research.prompts import default_research_focus

    agent = _agent_registry()[name]
    ov = get_override(name)
    overridden: list[str] = []
    if ov is not None:
        if ov.prompt is not None:
            overridden.append("prompt")
        if ov.model is not None:
            overridden.append("model")
        if ov.use_deep_reasoning is not None:
            overridden.append("use_deep_reasoning")
        if ov.role is not None:
            overridden.append("role")
        if ov.research_focus is not None:
            overridden.append("research_focus")
    # null (not "") for agents with no research scope, so the UI can decide
    # whether to render the research-focus editor at all.
    rf_default = default_research_focus(name) or None
    rf_effective = (
        ov.research_focus if ov and ov.research_focus is not None else rf_default
    )
    return AgentDetail(
        name=name,
        role=(ov.role if ov and ov.role is not None else _role_default(name)),
        role_default=_role_default(name),
        model=agent.effective_model(),
        model_default=agent.model,
        deep_reasoning=agent.effective_use_deep_reasoning(),
        deep_reasoning_default=agent.use_deep_reasoning,
        prompt=agent.effective_system_prompt(),
        prompt_default=agent.get_system_prompt(),
        domains=_domains_for(name),
        has_override=ov is not None,
        overridden_fields=overridden,
        updated_at=ov.updated_at if ov else None,
        research_focus=rf_effective,
        research_focus_default=rf_default,
    )


@router.get("/agents/models", response_model=list[str])
def list_models(agent_id: str | None = None) -> list[str]:
    """Return the model allowlist. ``agent_id`` is accepted (and forwarded)
    for call-site stability, but every agent currently gets the same list."""
    return _allowed(agent_id)


def _is_known_agent(agent_id: str) -> bool:
    return agent_id == EXECUTIVE_ID or agent_id in _agent_registry()


@router.get("/agents", response_model=list[AgentMeta])
async def list_agents() -> list[AgentMeta]:
    # Executive is rendered first so the Council UI puts it at the top of
    # the list. Specialists follow in registry order. The registry includes
    # "executive" as a key (for detail/lookup paths), so skip it here to
    # avoid duplicating the Executive card.
    return [_build_executive_meta()] + [
        _build_meta(name) for name in _agent_registry() if name != EXECUTIVE_ID
    ]


@router.get("/agents/{agent_id}", response_model=AgentDetail)
def get_agent(agent_id: str) -> AgentDetail:
    if agent_id == EXECUTIVE_ID:
        return _build_executive_detail()
    if agent_id not in _agent_registry():
        raise HTTPException(status_code=404, detail="Unknown agent")
    return _build_detail(agent_id)


@router.patch("/agents/{agent_id}", response_model=AgentDetail)
def patch_agent(agent_id: str, patch: AgentPatch) -> AgentDetail:
    if not _is_known_agent(agent_id):
        raise HTTPException(status_code=404, detail="Unknown agent")

    raw = patch.model_dump(exclude_unset=True)
    if patch.model is not None and patch.model not in _allowed(agent_id):
        raise HTTPException(
            status_code=400,
            detail=f"Model {patch.model!r} is not in the allowed list",
        )

    if "voice_persona_slug" in raw and patch.voice_persona_slug is not None:
        from openexecutive.personas.loader import persona_exists
        if not persona_exists(patch.voice_persona_slug):
            raise HTTPException(
                status_code=400,
                detail=f"Voice persona {patch.voice_persona_slug!r} does not exist",
            )

    set_override(
        agent_id,
        prompt=patch.prompt,
        model=patch.model,
        use_deep_reasoning=patch.use_deep_reasoning,
        role=patch.role,
        voice_persona_slug=patch.voice_persona_slug,
        research_focus=patch.research_focus,
        prompt_set="prompt" in raw,
        model_set="model" in raw,
        deep_set="use_deep_reasoning" in raw,
        role_set="role" in raw,
        voice_persona_slug_set="voice_persona_slug" in raw,
        research_focus_set="research_focus" in raw,
    )
    return (
        _build_executive_detail()
        if agent_id == EXECUTIVE_ID
        else _build_detail(agent_id)
    )


@router.delete("/agents/{agent_id}/override", status_code=status.HTTP_204_NO_CONTENT)
def reset_agent(agent_id: str) -> Response:
    if not _is_known_agent(agent_id):
        raise HTTPException(status_code=404, detail="Unknown agent")
    clear_override(agent_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/agents/{agent_id}/history",
    response_model=list[AgentHistoryEntry],
)
def get_history(agent_id: str, limit: int = 50) -> list[AgentHistoryEntry]:
    if not _is_known_agent(agent_id):
        raise HTTPException(status_code=404, detail="Unknown agent")
    return list_history(agent_id, limit=limit)


@router.post(
    "/agents/{agent_id}/rollback/{history_id}",
    response_model=AgentDetail,
)
def rollback_agent(agent_id: str, history_id: int) -> AgentDetail:
    if not _is_known_agent(agent_id):
        raise HTTPException(status_code=404, detail="Unknown agent")
    restored = rollback_to(history_id)
    if restored is None or restored.agent_id != agent_id:
        raise HTTPException(status_code=404, detail="Unknown history entry")
    return (
        _build_executive_detail()
        if agent_id == EXECUTIVE_ID
        else _build_detail(agent_id)
    )


async def _test_executive(req: AgentTestRequest) -> str:
    """One-shot preview of the Executive's voice with a draft prompt.

    Bypasses the full orchestrator (tool use, specialists, RAG) — this is
    a single messages.create so the Council UI can preview how an edited
    persona reads, not a real chat turn.
    """
    from openexecutive.config import get_settings
    from openexecutive.prompts.executive_persona import EXECUTIVE_PERSONA_PROMPT
    from openexecutive.providers import get_provider

    settings = get_settings()
    ov = get_override(EXECUTIVE_ID)
    prompt = (
        req.prompt
        if req.prompt is not None
        else (
            ov.prompt
            if ov is not None and ov.prompt is not None
            else EXECUTIVE_PERSONA_PROMPT
        )
    )
    model = (
        req.model
        if req.model is not None
        else (
            ov.model
            if ov is not None and ov.model is not None
            else settings.default_model
        )
    )
    message = await get_provider(model).messages_create(
        model=model,
        max_tokens=2048,
        system=[
            {
                "type": "text",
                "text": prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": req.query}],
    )
    log_model_usage(message, model=model, actor="agent_test")
    text_blocks = [b for b in message.content if b.type == "text"]
    return text_blocks[0].text if text_blocks else ""


@router.post("/agents/{agent_id}/test", response_model=AgentTestResponse)
async def test_agent(agent_id: str, req: AgentTestRequest) -> AgentTestResponse:
    """Run a one-off analyze() with the draft config — never persisted."""
    if req.model is not None and req.model not in _allowed(agent_id):
        raise HTTPException(
            status_code=400, detail=f"Model {req.model!r} is not in the allowed list"
        )
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="query must not be empty")

    if agent_id == EXECUTIVE_ID:
        try:
            response = await _test_executive(req)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Executive test failed")
            raise HTTPException(status_code=502, detail="Agent call failed") from exc
        return AgentTestResponse(response=response)

    registry = _agent_registry()
    agent = registry.get(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Unknown agent")

    # Triage has its own non-`analyze` entry point; call analyze with the
    # draft anyway so the UI test box still works for it — it'll return a
    # plain text completion using the triage prompt.
    try:
        response = await agent.analyze(
            query=req.query,
            system_prompt_override=req.prompt,
            model_override=req.model,
            deep_reasoning_override=req.use_deep_reasoning,
            # Sandbox runs must not read as production specialist spend in
            # the /audit/usage by-source breakdown.
            actor="agent_test",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Agent test failed for %r", agent_id)
        raise HTTPException(status_code=502, detail="Agent call failed") from exc
    return AgentTestResponse(response=response)
