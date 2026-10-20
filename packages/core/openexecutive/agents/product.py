from openexecutive.agents.base import BaseAgent
from openexecutive.config import get_settings


class ProductAgent(BaseAgent):
    name = "cpo"
    domain = "product"
    visibility = "core"

    @property
    def model(self) -> str:  # type: ignore[override]
        return get_settings().default_model

    def get_system_prompt(self) -> str:
        from openexecutive.prompts.domain_prompts import CPO_PROMPT

        return CPO_PROMPT
