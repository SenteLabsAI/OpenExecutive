"""Thin BaseAgent subclass so the Executive can participate in the Agent Council.

The ExecutiveProxy is NOT registered in SPECIALIST_REGISTRY and is never used for
routing. Its only purpose is to give the agents API route a uniform interface for
building AgentMeta / AgentDetail and to surface effective_*() override resolution
for the Executive's prompt and model.

The test-box endpoint calls proxy.analyze() directly — this makes a single-turn
completion, which is the correct "does this prompt produce sensible output?" check.
"""
from __future__ import annotations

from openexecutive.agents.base import BaseAgent


class ExecutiveProxy(BaseAgent):
    name = "executive"
    domain = "orchestration"
    model = "claude-sonnet-5"  # matches DEFAULT_MODEL default
    use_deep_reasoning = False

    def get_system_prompt(self) -> str:
        # The built-in persona for this install's workspace mode (team / solo).
        from openexecutive.memory.workspace_settings import get_workspace
        from openexecutive.prompts.executive_persona import default_persona

        return default_persona(get_workspace().mode)
