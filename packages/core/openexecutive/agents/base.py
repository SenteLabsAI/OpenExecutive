from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from openexecutive.config import get_settings
from openexecutive.providers import get_provider, model_supports_deep_reasoning

_SPECIALIST_TIMEOUT = 180.0

logger = logging.getLogger(__name__)


class BaseAgent(ABC):
    name: str
    domain: str
    model: str
    use_deep_reasoning: bool = False

    @abstractmethod
    def get_system_prompt(self) -> str: ...

    def effective_system_prompt(self) -> str:
        """System prompt with runtime override applied, if any."""
        from openexecutive.agents.overrides import get_override

        ov = get_override(self.name)
        if ov is not None and ov.prompt is not None:
            return ov.prompt
        return self.get_system_prompt()

    def effective_model(self) -> str:
        from openexecutive.agents.overrides import get_override

        ov = get_override(self.name)
        if ov is not None and ov.model is not None:
            return ov.model
        return self.model

    def effective_use_deep_reasoning(self) -> bool:
        from openexecutive.agents.overrides import get_override

        ov = get_override(self.name)
        if ov is not None and ov.use_deep_reasoning is not None:
            return ov.use_deep_reasoning
        return self.use_deep_reasoning

    async def analyze(
        self,
        query: str,
        context: str = "",
        retrieved_knowledge: str = "",
        episodic_context: str = "",
        failure_cases: str = "",
        department_memory: str = "",
        *,
        system_prompt_override: str | None = None,
        model_override: str | None = None,
        deep_reasoning_override: bool | None = None,
    ) -> str:
        settings = get_settings()

        # Resolution order: explicit kwarg (for sandbox/test calls) → DB
        # override → class default. Keeping the override read inside this
        # method means a fresh DB row takes effect on the next request
        # without any process restart.
        system_prompt = (
            system_prompt_override
            if system_prompt_override is not None
            else self.effective_system_prompt()
        )
        model = (
            model_override
            if model_override is not None
            else self.effective_model()
        )
        use_deep = (
            deep_reasoning_override
            if deep_reasoning_override is not None
            else self.effective_use_deep_reasoning()
        )

        user_content = query
        if context:
            user_content = f"<conversation_context>\n{context}\n</conversation_context>\n\n{query}"
        if retrieved_knowledge:
            user_content = (
                f"<relevant_knowledge>\n{retrieved_knowledge}\n</relevant_knowledge>\n\n{user_content}"
            )
        if failure_cases:
            user_content = (
                f"<failure_cases>\n{failure_cases}\n</failure_cases>\n\n{user_content}"
            )
        if episodic_context:
            user_content = (
                f"<past_decisions>\n{episodic_context}\n</past_decisions>\n\n{user_content}"
            )
        if department_memory:
            # Placed adjacent to past_decisions so the specialist sees both
            # forms of institutional context together: the structured ledger
            # (past_decisions, from SQL) and the dept peer's synthesized
            # voice (department_memory, from Honcho). Order is intentional —
            # past_decisions stays closest to the query for cache stability
            # across turns; department_memory wraps it.
            user_content = (
                f"<department_memory>\n{department_memory}\n</department_memory>\n\n{user_content}"
            )

        create_kwargs: dict = {
            "model": model,
            "max_tokens": 4096,
            # Per-request timeout — keeps cancellation semantics aligned with
            # the previous per-client timeout, but the provider singleton no
            # longer needs to recreate the SDK client to set it.
            "timeout": _SPECIALIST_TIMEOUT,
            "system": [
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [{"role": "user", "content": user_content}],
        }

        if use_deep and model_supports_deep_reasoning(model):
            # Adaptive thinking with effort capped via
            # output_config.effort. Default "low" — adaptive at default
            # effort routinely burned 20k+ thinking tokens. "low" still
            # leaves room for nuanced reasoning while being ~3x faster.
            # Tune via SPECIALIST_EFFORT (low / medium / high / xhigh / max).
            create_kwargs["thinking"] = {"type": "adaptive"}
            create_kwargs["output_config"] = {"effort": settings.specialist_effort}
            create_kwargs["max_tokens"] = 16000

        # Resolve provider per-call by model so a Council UI override that
        # flips this agent to a non-Anthropic slug routes correctly. Today
        # the registry always returns the Anthropic provider; OpenRouter
        # wiring lands in the next commit.
        provider = get_provider(model)
        message = await provider.messages_create(**create_kwargs)

        text_blocks = [b for b in message.content if b.type == "text"]
        if not text_blocks:
            # A reasoning model can spend the whole max_tokens budget thinking
            # and return no text; the Executive would then synthesize as if
            # this specialist had nothing to say. Make that visible.
            logger.warning(
                "specialist %s (%s) returned no text block (stop_reason=%s, "
                "deep_reasoning=%s); its analysis will be empty",
                self.name,
                model,
                getattr(message, "stop_reason", None),
                use_deep,
            )
        return text_blocks[0].text if text_blocks else ""

    async def analyze_with_tools(
        self,
        user_content: str,
        *,
        tools: list[dict[str, Any]],
        system_addendum: str = "",
        max_tokens: int = 4096,
        timeout_seconds: float = _SPECIALIST_TIMEOUT,
        model_override: str | None = None,
        deep_reasoning_override: bool | None = None,
    ) -> Any:
        """Tool-use variant of ``analyze`` — returns the raw provider Message.

        Used by workflows that need structured output from a specialist
        (e.g. the watchlist research workflow's
        ``propose_watchlist_entries`` tool). Differs from ``analyze``:

          - ``tools`` is required; the model is expected to call one of
            them rather than emit prose.
          - The user_content is passed verbatim — no
            ``<conversation_context>`` / ``<retrieved_knowledge>`` /
            ``<past_decisions>`` wrappers (those are chat-time
            primitives). The caller owns context formatting.
          - Returns the raw ``Message`` instead of joined text so the
            caller can pick out the tool_use block it expects.

        Honours every per-instance effective_* override (system prompt,
        model, deep-reasoning) so a Council override applies here too.

        ``model_override`` / ``deep_reasoning_override`` let a caller pin the
        model and reasoning tier for this one turn (e.g. the executive_research
        fan-out routes specialists to a cheaper RESEARCH_MODEL with deep
        reasoning off) without disturbing the agent's chat-time defaults. When
        None, the per-instance effective_* values apply, matching prior
        behavior.
        """
        settings = get_settings()
        system_prompt = self.effective_system_prompt() + (
            system_addendum or ""
        )
        model = (
            model_override
            if model_override is not None
            else self.effective_model()
        )
        use_deep = (
            deep_reasoning_override
            if deep_reasoning_override is not None
            else self.effective_use_deep_reasoning()
        )

        create_kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "timeout": timeout_seconds,
            "system": [
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "tools": tools,
            "messages": [{"role": "user", "content": user_content}],
        }

        if use_deep and model_supports_deep_reasoning(model):
            create_kwargs["thinking"] = {"type": "adaptive"}
            create_kwargs["output_config"] = {"effort": settings.specialist_effort}
            create_kwargs["max_tokens"] = max(max_tokens, 16000)

        provider = get_provider(model)
        return await provider.messages_create(**create_kwargs)
