"""Quality presets for the Agent Council: Fast, Balanced and Thorough.

A preset sets the model and deep-reasoning flag of every agent at once. It
is not stored anywhere: applying one writes ordinary per-agent overrides
(``agents.overrides.set_override`` / ``clear_override``), so history,
rollback and later per-agent edits keep working, and the active preset is
worked out from those overrides when the Council asks.

Presets change quality only. They never cap spend.

Model names are resolved at request time against the install's model
allowlist (``providers.allowed_models_for``), taking the first preferred
model the install can serve. A preset whose model this install can't reach
is reported as unavailable rather than silently picking something else.

This module is pure: callers pass each agent's defaults and current
overrides in as ``AgentState`` and apply the returned ``AgentWrite`` list.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from openexecutive.providers import model_supports_deep_reasoning

PresetId = Literal["fast", "balanced", "thorough"]

PRESET_IDS: tuple[PresetId, ...] = ("fast", "balanced", "thorough")
# Order ``preset_status`` tries presets in; Balanced first so it wins ties.
_STATUS_ORDER: tuple[PresetId, ...] = ("balanced", "fast", "thorough")

# Preferred models, best first. The first one the install allows wins.
_FAST_MODELS: tuple[str, ...] = ("claude-haiku-5-5", "claude-haiku-4-5")
_THOROUGH_MODELS: tuple[str, ...] = ("claude-opus-5-5", "claude-opus-5")


@dataclass(frozen=True)
class PresetSpec:
    id: PresetId
    label: str
    description: str


PRESETS: dict[PresetId, PresetSpec] = {
    "fast": PresetSpec(
        id="fast",
        label="Fast",
        description=(
            "A small, fast model for every agent, with deep reasoning off. "
            "Quickest answers."
        ),
    ),
    "balanced": PresetSpec(
        id="balanced",
        label="Balanced",
        description="Each agent's built-in model and deep-reasoning setting.",
    ),
    "thorough": PresetSpec(
        id="thorough",
        label="Thorough",
        description=(
            "The largest model for the Executive and the specialists, with "
            "deep reasoning on where the model supports it. Background "
            "helpers keep their built-in settings."
        ),
    ),
}


@dataclass(frozen=True)
class AgentState:
    """One agent's defaults and current overrides (None = no override)."""

    agent_id: str
    model_default: str
    deep_default: bool
    model_override: str | None
    deep_override: bool | None
    # The Executive or a domain specialist (BaseAgent.visibility == "core").
    # Thorough moves core agents up and leaves the rest on their defaults.
    core: bool = False
    # Whether the agent's own calls honour the deep-reasoning flag. The
    # Executive's turn never sends thinking fields, so a preset leaves its
    # flag alone.
    uses_deep_reasoning: bool = True

    @property
    def model(self) -> str:
        return self.model_override if self.model_override is not None else self.model_default

    @property
    def deep(self) -> bool:
        return self.deep_override if self.deep_override is not None else self.deep_default


@dataclass(frozen=True)
class AgentTarget:
    model: str
    deep: bool


@dataclass(frozen=True)
class AgentWrite:
    """Override values to store so the agent matches its preset target.

    ``None`` clears the field back to the agent's default.
    """

    agent_id: str
    model: str | None
    deep: bool | None


def _first_allowed(preferred: tuple[str, ...], allowed: list[str]) -> str | None:
    allowed_set = set(allowed)
    return next((m for m in preferred if m in allowed_set), None)


def preset_model(preset_id: PresetId, allowed: list[str]) -> str | None:
    """The model a preset moves agents to, or None when it keeps defaults
    (Balanced) or when the install allows none of its preferred models."""
    if preset_id == "fast":
        return _first_allowed(_FAST_MODELS, allowed)
    if preset_id == "thorough":
        return _first_allowed(_THOROUGH_MODELS, allowed)
    return None


def preset_available(preset_id: PresetId, allowed: list[str]) -> bool:
    return preset_id == "balanced" or preset_model(preset_id, allowed) is not None


def target_for(preset_id: PresetId, agent: AgentState, *, allowed: list[str]) -> AgentTarget:
    """What ``agent`` should run on under ``preset_id``.

    Raises ``ValueError`` when the preset is unavailable on this install.
    """
    default = AgentTarget(model=agent.model_default, deep=agent.deep_default)
    if preset_id == "balanced":
        return default
    model = preset_model(preset_id, allowed)
    if model is None:
        raise ValueError(f"Preset {preset_id!r} is not available on this install")
    if preset_id == "fast":
        deep = False if agent.uses_deep_reasoning else agent.deep_default
        return AgentTarget(model=model, deep=deep)
    # Thorough: core agents move up; background helpers keep their defaults.
    if not agent.core:
        return default
    deep = (
        model_supports_deep_reasoning(model)
        if agent.uses_deep_reasoning
        else agent.deep_default
    )
    return AgentTarget(model=model, deep=deep)


def matches(agent: AgentState, target: AgentTarget) -> bool:
    return agent.model == target.model and agent.deep == target.deep


def writes_for(
    preset_id: PresetId, agents: list[AgentState], *, allowed: list[str]
) -> list[AgentWrite]:
    """Override writes that bring every agent onto ``preset_id``.

    Agents already on their target are left out, so applying the active
    preset again writes nothing. A field whose target equals the agent's
    default is cleared rather than stored, so Balanced leaves no model or
    deep-reasoning override behind.
    """
    out: list[AgentWrite] = []
    for agent in agents:
        target = target_for(preset_id, agent, allowed=allowed)
        if matches(agent, target):
            continue
        out.append(
            AgentWrite(
                agent_id=agent.agent_id,
                model=None if target.model == agent.model_default else target.model,
                deep=None if target.deep == agent.deep_default else target.deep,
            )
        )
    return out


@dataclass(frozen=True)
class PresetStatus:
    """Which preset the council is on.

    ``active`` is the preset every agent matches, or None ("Custom").
    ``base`` is the available preset the most agents match (Balanced on a
    tie), and ``custom_agents`` lists the agents that differ from it.
    """

    active: PresetId | None
    base: PresetId
    custom_agents: list[str]


def preset_status(agents: list[AgentState], *, allowed: list[str]) -> PresetStatus:
    best: tuple[int, PresetId, list[str]] | None = None
    for preset_id in _STATUS_ORDER:
        if not preset_available(preset_id, allowed):
            continue
        diverging = [
            a.agent_id
            for a in agents
            if not matches(a, target_for(preset_id, a, allowed=allowed))
        ]
        if best is None or len(diverging) < best[0]:
            best = (len(diverging), preset_id, diverging)
    assert best is not None  # Balanced is always available.
    _, base, diverging = best
    return PresetStatus(
        active=base if not diverging else None,
        base=base,
        custom_agents=diverging,
    )
