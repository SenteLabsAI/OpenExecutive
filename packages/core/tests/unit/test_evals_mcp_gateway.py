"""An mcp eval run reaches the MCP tools: the in-app runner gives its
Executive the API's running gateway (evals/runner.py), reads it again for each
scenario and fails one when there is none, and a chat run keeps running
without one. evals/run_evals.py --mcp starts its own gateway, makes it the
active one, and closes it and resets the active one on every way out."""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace

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


def _recording_executive(made: list[object]) -> type:
    """A fake Executive that records the gateway each one was built with."""

    class Recording:
        def __init__(self, mcp_gateway: object = None) -> None:
            made.append(mcp_gateway)

        async def chat(self, **_kwargs: object) -> str:
            return "answer"

    return Recording


def _drain(queue: asyncio.Queue[dict[str, object] | None]) -> list[dict[str, object]]:
    events = []
    while not queue.empty():
        event = queue.get_nowait()
        if event is not None and event["type"] != "scenario_start":
            events.append(event)
    return events


async def test_an_mcp_scenario_with_no_running_gateway_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    # Judged without tools, it would measure nothing.
    from openexecutive.evals import runner
    from openexecutive.orchestrator import executive as executive_module

    monkeypatch.setattr(executive_module, "Executive", _recording_executive([]))
    monkeypatch.setattr("openexecutive.orchestrator.mcp_gateway.get_active_gateway", lambda: None)
    queue: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()
    run_one = runner._make_chat_runner(asyncio.Semaphore(1), queue, [0], 1, None, with_mcp=True)
    await run_one(0, {"id": "s0", "query": "q"})
    assert [(e["type"], e["error"]) for e in _drain(queue)] == [("scenario_error", runner.NO_MCP_GATEWAY)]


async def test_each_mcp_scenario_reads_the_running_gateway_again(monkeypatch: pytest.MonkeyPatch) -> None:
    # A gateway the API replaces during a long run is the one the next scenario uses.
    from openexecutive.evals import runner
    from openexecutive.orchestrator import executive as executive_module

    async def judged(*_args: object, **_kwargs: object) -> dict[str, float]:
        return {"overall": 5.0}

    first, second = object(), object()
    active = [first]
    made: list[object] = []
    monkeypatch.setattr(executive_module, "Executive", _recording_executive(made))
    monkeypatch.setattr("openexecutive.orchestrator.mcp_gateway.get_active_gateway", lambda: active[0])
    monkeypatch.setattr(runner, "judge_chat", judged)
    queue: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()
    run_one = runner._make_chat_runner(asyncio.Semaphore(1), queue, [0], 3, None, with_mcp=True)
    await run_one(0, {"id": "s0", "query": "q"})
    active[0] = second
    await run_one(1, {"id": "s1", "query": "q"})
    await run_one(2, {"id": "s2", "query": "q"})
    # Built for the first gateway, again when it was replaced, and reused after.
    assert made == [first, second]
    assert [e["type"] for e in _drain(queue)] == ["scenario_done"] * 3


class _Gateway:
    """A fake MCPGateway: records start and close, and can fail to start."""

    def __init__(self, fail: BaseException | None = None) -> None:
        self.fail = fail
        self.started = False
        self.closed = False

    async def start(self, config_path: object) -> None:
        if self.fail is not None:
            raise self.fail
        self.started = True

    async def close(self) -> None:
        self.closed = True


def _run_evals(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, config: bool) -> ModuleType:
    """evals/run_evals.py, with the MCP config at a path that exists only when ``config``."""
    config_path = tmp_path / "mcp_servers.json"
    if config:
        config_path.write_text("{}")
    monkeypatch.setattr(
        "openexecutive.config.get_settings", lambda: SimpleNamespace(mcp_servers_config_path=config_path)
    )
    script = Path(__file__).resolve().parents[4] / "evals" / "run_evals.py"
    spec = importlib.util.spec_from_file_location("run_evals_under_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_the_mcp_run_starts_the_gateway_makes_it_active_and_closes_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from openexecutive.orchestrator import mcp_gateway

    gateway = _Gateway()
    monkeypatch.setattr(mcp_gateway, "MCPGateway", lambda: gateway)
    run_evals = _run_evals(monkeypatch, tmp_path, config=True)
    async with run_evals._eval_gateway(True) as given:
        assert given is gateway and gateway.started
        assert mcp_gateway.get_active_gateway() is gateway
    assert gateway.closed
    assert mcp_gateway.get_active_gateway() is None


async def test_a_failing_run_still_closes_the_gateway(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from openexecutive.orchestrator import mcp_gateway

    gateway = _Gateway()
    monkeypatch.setattr(mcp_gateway, "MCPGateway", lambda: gateway)
    run_evals = _run_evals(monkeypatch, tmp_path, config=True)
    with pytest.raises(RuntimeError, match="scenario blew up"):
        async with run_evals._eval_gateway(True):
            raise RuntimeError("scenario blew up")
    assert gateway.closed
    assert mcp_gateway.get_active_gateway() is None


@pytest.mark.parametrize("failure", [RuntimeError("server 3 failed"), TimeoutError()])
async def test_a_start_that_fails_part_way_closes_what_it_spawned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: BaseException
) -> None:
    # Servers spawned before the failure or the timeout would otherwise leak.
    from openexecutive.orchestrator import mcp_gateway

    gateway = _Gateway(fail=failure)
    monkeypatch.setattr(mcp_gateway, "MCPGateway", lambda: gateway)
    run_evals = _run_evals(monkeypatch, tmp_path, config=True)
    with pytest.raises(type(failure)):
        async with run_evals._eval_gateway(True):
            pytest.fail("the run must not start")
    assert gateway.closed
    assert mcp_gateway.get_active_gateway() is None


async def test_an_mcp_run_with_no_config_exits_before_any_gateway(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from openexecutive.orchestrator import mcp_gateway

    made: list[_Gateway] = []

    def make() -> _Gateway:
        made.append(_Gateway())
        return made[-1]

    monkeypatch.setattr(mcp_gateway, "MCPGateway", make)
    run_evals = _run_evals(monkeypatch, tmp_path, config=False)
    with pytest.raises(SystemExit, match="--mcp needs an MCP config"):
        async with run_evals._eval_gateway(True):
            pytest.fail("the run must not start")
    assert made == []


async def test_a_chat_run_gets_no_gateway(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from openexecutive.orchestrator import mcp_gateway

    def make() -> None:
        pytest.fail("a chat run starts no gateway")

    monkeypatch.setattr(mcp_gateway, "MCPGateway", make)
    run_evals = _run_evals(monkeypatch, tmp_path, config=True)
    async with run_evals._eval_gateway(False) as given:
        assert given is None
