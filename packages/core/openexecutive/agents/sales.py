from openexecutive.agents.base import BaseAgent
from openexecutive.config import get_settings


class SalesAgent(BaseAgent):
    name = "sales"
    domain = "sales"

    @property
    def model(self) -> str:  # type: ignore[override]
        return get_settings().default_model

    def get_system_prompt(self) -> str:
        from openexecutive.prompts.domain_prompts import SALES_PROMPT

        return SALES_PROMPT
