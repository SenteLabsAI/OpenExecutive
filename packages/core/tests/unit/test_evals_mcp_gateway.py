"""An mcp eval run reaches the MCP tools: the in-app runner gives its
Executive the API's running gateway (evals/runner.py), and a chat run keeps
running without one."""
from __future__ import annotations

import asyncio

import pytest


def test_an_mcp_run_gets_the_running_gateway_and_a_chat_run_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # Without a gateway the Executive offers no MCP tools, so an mcp
    # scenario would be judged on a turn that can't reach them.
    from openexecutive.evals import runner
    from openexecutive.orchestrator import executive as executive_module

    given: list[object] = []

    class Recording:
        def __init__(self, mcp_gateway: object = None) -> None:
            given.append(mcp_gateway)

    gateway = object()
    monkeypatch.setattr(executive_module, "Executive", Recording)
    monkeypatch.setattr("openexecutive.orchestrator.mcp_gateway.get_active_gateway", lambda: gateway)
    queue: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()
    for with_mcp in (True, False):
        runner._make_chat_runner(asyncio.Semaphore(1), queue, [0], 1, None, with_mcp=with_mcp)
    assert given == [gateway, None]


def test_the_run_picks_the_runner_by_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.evals import runner

    asked: list[bool] = []
    monkeypatch.setattr(
        runner, "_make_chat_runner", lambda *_a, with_mcp=False, **_k: asked.append(with_mcp)
    )
    queue: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()
    for kind in ("mcp", "chat"):
        runner._make_run_one(
            kind=kind, store=None, sem=asyncio.Semaphore(1), queue=queue, passed=[0], total=1, cancel_event=None
        )
    assert asked == [True, False]
