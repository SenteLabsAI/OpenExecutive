from openexecutive.agents.base import BaseAgent
from openexecutive.config import get_settings


class StrategyAgent(BaseAgent):
    name = "cso"
    domain = "strategy"
    visibility = "core"
    use_deep_reasoning = True

    @property
    def model(self) -> str:  # type: ignore[override]
        return get_settings().deep_reasoning_model

    def get_system_prompt(self) -> str:
        from openexecutive.prompts.domain_prompts import CSO_PROMPT

        return CSO_PROMPT
