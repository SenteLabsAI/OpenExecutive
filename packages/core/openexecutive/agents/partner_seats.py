"""Colette's owner-seat and partner-seat agents.

Registered in ``orchestrator/router.py`` under the same keys the department
seeds already point at (``cfo``, ``coo``, ``cmo``, ``cpo``) plus ``ceo_advisor``
for the Strategy department. Keeping the keys stable means the department →
specialist mapping, knowledge-domain aliases and audit rows need no migration.

* ``cfo`` / ``coo`` — the upstream agents with an *operating mandate* overlay
  appended to the system prompt (they own outcomes; no human holds the role).
* ``cmo`` / ``cpo`` — partner seats for the humans who own marketing and
  product.
* ``ceo_advisor`` — the challenger-first partner to the CEO.

The overlays are plain string concatenation of module constants, so the
resulting system prompt is byte-stable across calls (prompt caching).
"""
from __future__ import annotations

from openexecutive.agents.base import BaseAgent
from openexecutive.agents.finance import FinanceAgent
from openexecutive.agents.operations import OperationsAgent
from openexecutive.config import get_settings


class OperatingCFOAgent(FinanceAgent):
    """CFO seat that owns the financial operating system."""

    def get_system_prompt(self) -> str:
        from openexecutive.prompts.domain_prompts import CFO_PROMPT
        from openexecutive.prompts.partner_prompts import CFO_OPERATING_ADDENDUM

        return CFO_PROMPT + CFO_OPERATING_ADDENDUM


class OperatingCOOAgent(OperationsAgent):
    """COO seat that owns the operating system behind growth."""

    def get_system_prompt(self) -> str:
        from openexecutive.prompts.domain_prompts import COO_PROMPT
        from openexecutive.prompts.partner_prompts import COO_OPERATING_ADDENDUM

        return COO_PROMPT + COO_OPERATING_ADDENDUM


class CMOPartnerAgent(BaseAgent):
    """Direct partner to the human CMO."""

    name = "cmo"
    domain = "marketing"

    @property
    def model(self) -> str:  # type: ignore[override]
        return get_settings().default_model

    def get_system_prompt(self) -> str:
        from openexecutive.prompts.partner_prompts import CMO_PARTNER_PROMPT

        return CMO_PARTNER_PROMPT


class CPOPartnerAgent(BaseAgent):
    """Direct partner (VP of Product) to the human CPO."""

    name = "cpo"
    domain = "product"

    @property
    def model(self) -> str:  # type: ignore[override]
        return get_settings().default_model

    def get_system_prompt(self) -> str:
        from openexecutive.prompts.partner_prompts import CPO_PARTNER_PROMPT

        return CPO_PARTNER_PROMPT


class CEOAdvisorAgent(BaseAgent):
    """Challenger-first strategic partner to the CEO."""

    name = "ceo_advisor"
    domain = "strategy"
    use_deep_reasoning = True

    @property
    def model(self) -> str:  # type: ignore[override]
        return get_settings().deep_reasoning_model

    def get_system_prompt(self) -> str:
        from openexecutive.prompts.partner_prompts import CEO_ADVISOR_PROMPT

        return CEO_ADVISOR_PROMPT
