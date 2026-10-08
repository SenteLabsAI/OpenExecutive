"""Chat consults keep deep reasoning off unless opted in."""

from __future__ import annotations

from openexecutive.agents.base import CHAT_SPECIALIST_ACTOR
from openexecutive.agents.strategy import StrategyAgent


def test_chat_actor_defaults_deep_reasoning_off() -> None:
    agent = StrategyAgent()
    assert agent.use_deep_reasoning is True
    assert (
        agent.resolve_use_deep_reasoning(
            deep_reasoning_override=None,
            actor=CHAT_SPECIALIST_ACTOR,
            specialist_chat_deep_reasoning=False,
        )
        is False
    )


def test_workflow_actor_keeps_class_default() -> None:
    agent = StrategyAgent()
    assert (
        agent.resolve_use_deep_reasoning(
            deep_reasoning_override=None,
            actor="specialist_workflow",
            specialist_chat_deep_reasoning=False,
        )
        is True
    )


def test_env_opt_in_restores_chat_deep_reasoning() -> None:
    agent = StrategyAgent()
    assert (
        agent.resolve_use_deep_reasoning(
            deep_reasoning_override=None,
            actor=CHAT_SPECIALIST_ACTOR,
            specialist_chat_deep_reasoning=True,
        )
        is True
    )


def test_explicit_override_wins() -> None:
    agent = StrategyAgent()
    assert (
        agent.resolve_use_deep_reasoning(
            deep_reasoning_override=True,
            actor=CHAT_SPECIALIST_ACTOR,
            specialist_chat_deep_reasoning=False,
        )
        is True
    )
