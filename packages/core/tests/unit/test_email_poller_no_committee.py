"""The email poller must invoke the Executive on the standard path, like
the Slack and Discord adapters — it must not force ``committee_review=True``.

Committee review (draft + three critiques + revision, plus a deeper Honcho
prefetch) is a per-request opt-in on ``/chat``. Forcing it on every inbound
email multiplied model calls for every message, including ones from
off-roster senders the gateway will never let the Executive reply to.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import openexecutive.integrations.email_poller as poller


class _CapturingExecutive:
    def __init__(self, **_kwargs: Any) -> None:
        self.chat_kwargs: dict[str, Any] | None = None

    async def chat(self, **kwargs: Any) -> str:
        self.chat_kwargs = kwargs
        return "ok"


def _settings() -> Any:
    return SimpleNamespace(
        exec_email_address="exec@example.com",
        email_poll_interval_seconds=60,
    )


def test_run_executive_does_not_force_committee_review() -> None:
    captured: list[_CapturingExecutive] = []

    def _factory(**kwargs: Any) -> _CapturingExecutive:
        ex = _CapturingExecutive(**kwargs)
        captured.append(ex)
        return ex

    with (
        patch("openexecutive.orchestrator.executive.Executive", new=_factory),
        patch(
            "openexecutive.onboarding.profile_builder.load_or_create_profile",
            return_value=SimpleNamespace(is_empty=lambda: True),
        ),
        patch(
            "openexecutive.knowledge.retriever.retrieve",
            new=lambda **_k: "",
        ),
        patch(
            "openexecutive.memory.episodic.format_for_prompt",
            new=lambda: "",
        ),
        patch.object(poller, "get_settings", return_value=_settings()),
    ):
        gateway = AsyncMock()
        asyncio.run(
            poller._run_executive(
                gateway=gateway,
                raw_email="Subject: test\nFrom: alice@example.com\n\nbody",
                message_id="m1",
                thread_id="t1",
                from_addr="alice@example.com",
            )
        )

    assert len(captured) == 1
    kwargs = captured[0].chat_kwargs
    assert kwargs is not None
    assert kwargs.get("committee_review", False) is False, (
        "Email poller must use the standard chat path like the other "
        "channel adapters; committee review is a per-request /chat opt-in."
    )
