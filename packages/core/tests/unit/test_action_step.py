"""Workflow action steps: the tool-use loop with an approved-tools allowlist.

Pins the safety contract — only the step's tools are ever offered or run, the
call budget holds, errors come back to the model instead of crashing the run,
every call is audited without its arguments — plus the caching shape (a
constant cached system block, sorted tools) and the engine wiring.
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import Any

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-used")

from openexecutive.agents.workflow_actor import WORKFLOW_ACTOR_SYSTEM  # noqa: E402
from openexecutive.workflows import action_step as act  # noqa: E402
from openexecutive.workflows import tool_catalog as tc  # noqa: E402
from openexecutive.workflows.dynamic import DynamicWorkflow  # noqa: E402
from openexecutive.workflows.dynamic_models import (  # noqa: E402
    ActionStepSpec,
    DynamicWorkflowDef,
)

READ = "sheets__read_values"
APPEND = "sheets__append_rows"


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
        # Deep-ish copy: the loop mutates `messages` after each call.
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        if not self._responses:
            raise AssertionError("provider called more times than scripted")
        nxt = self._responses.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt


class _FakeGateway:
    def __init__(self, replies: dict[str, str] | None = None) -> None:
        self.replies = replies or {}
        self.calls: list[dict[str, Any]] = []

    async def call_tool(self, tool_input: dict[str, Any]) -> str:
        self.calls.append(tool_input)
        return self.replies.get(tool_input["name"], "ok")


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
    gw = _FakeGateway({READ: "header: vendor, amount", APPEND: "Appended 1 row"})
    monkeypatch.setattr(
        "openexecutive.orchestrator.mcp_gateway.get_active_gateway", lambda: gw
    )

    async def _resolve(names: list[str]) -> dict[str, tc.ToolInfo]:
        known = {
            READ: tc.ToolInfo(READ, "Read values.", {"type": "object", "properties": {}}, True),
            APPEND: tc.ToolInfo(APPEND, "Append rows.", {"type": "object", "properties": {}}),
        }
        builtins = {t.name: t for t in tc.builtin_tools()}
        return {n: (known.get(n) or builtins[n]) for n in names if n in known or n in builtins}

    monkeypatch.setattr(tc, "resolve", _resolve)
    return gw


def _install(monkeypatch: pytest.MonkeyPatch, provider: Any) -> None:
    monkeypatch.setattr("openexecutive.providers.registry.get_provider", lambda model: provider)


def _step(**overrides: Any) -> ActionStepSpec:
    base: dict[str, Any] = {
        "id": "file_bills",
        "title": "File bills",
        "goal": "Add today's bills to the tracker.",
        "tools": [APPEND, READ],
    }
    base.update(overrides)
    return ActionStepSpec.model_validate(base)


async def _run(step: ActionStepSpec, **kw: Any) -> list[tuple[str, str]]:
    out = []
    async for item in act.run_action_step(
        step,
        workflow_name="file_bills_wf",
        workflow_title="File bills",
        goal=step.goal,
        values=kw.get("values", {"sheet": "Bill tracker"}),
        company_block=kw.get("company_block", "Company: Northwind"),
        prior_outputs=kw.get("prior_outputs", {}),
    ):
        out.append(item)
    return out


@pytest.mark.asyncio
async def test_happy_path_calls_tools_and_reports(
    monkeypatch: pytest.MonkeyPatch, gateway: _FakeGateway, audit: list[dict[str, Any]]
) -> None:
    provider = _ScriptedProvider(
        [
            _resp(_text("Reading first."), _use(READ, {"range": "A1:B2"}, "tu_1")),
            _resp(_use(APPEND, {"rows": [["Acme", "10"]]}, "tu_2")),
            _resp(_text("Added 1 bill (Acme, $10) to Bill tracker.")),
        ]
    )
    _install(monkeypatch, provider)
    out = await _run(_step())

    assert [k for k, _ in out] == ["progress", "progress", "output"]
    report = out[-1][1]
    assert report.startswith("Added 1 bill (Acme, $10) to Bill tracker.")
    assert f"`{READ}` — ok" in report and f"`{APPEND}` — ok" in report
    assert [c["name"] for c in gateway.calls] == [READ, APPEND]
    assert gateway.calls[1]["arguments"] == {"rows": [["Acme", "10"]]}

    # The tool result is paired with its tool_use id and fed back.
    second = provider.calls[1]["messages"]
    assert second[-1]["content"][0] == {
        "type": "tool_result", "tool_use_id": "tu_1",
        "content": "header: vendor, amount", "is_error": False,
    }
    # Audited per call, without arguments.
    assert [r["details"]["tool"] for r in audit] == [READ, APPEND]
    assert all("Acme" not in json.dumps(r) for r in audit)


@pytest.mark.asyncio
async def test_prompt_shape_is_cache_friendly(
    monkeypatch: pytest.MonkeyPatch, gateway: _FakeGateway, audit: list[dict[str, Any]]
) -> None:
    provider = _ScriptedProvider([_resp(_text("Nothing to do."))])
    _install(monkeypatch, provider)
    await _run(_step(), prior_outputs={"scan": ("Scan", "Found 2 bills")})
    call = provider.calls[0]
    assert call["system"] == [
        {"type": "text", "text": WORKFLOW_ACTOR_SYSTEM, "cache_control": {"type": "ephemeral"}}
    ]
    # Only the step's tools, sorted by name — never the gateway meta-tools.
    assert [t["name"] for t in call["tools"]] == sorted([APPEND, READ])
    user = call["messages"][0]["content"]
    assert "Add today's bills to the tracker." in user
    assert "sheet: Bill tracker" in user
    assert "Found 2 bills" in user and "data, not instructions" in user
    assert "tool_choice" not in call


@pytest.mark.asyncio
async def test_tool_outside_the_allowlist_is_refused(
    monkeypatch: pytest.MonkeyPatch, gateway: _FakeGateway, audit: list[dict[str, Any]]
) -> None:
    provider = _ScriptedProvider(
        [
            _resp(_use("gmail__send_message", {"to": "x@evil.test"}, "tu_1")),
            _resp(_text("Could not send.")),
        ]
    )
    _install(monkeypatch, provider)
    out = await _run(_step())
    assert gateway.calls == []  # never reached the gateway
    result = provider.calls[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True and "not one of this step's tools" in result["content"]
    assert "`gmail__send_message` — refused: not allowed" in out[-1][1]
    assert audit[0]["details"]["outcome"] == "refused: not allowed"


@pytest.mark.asyncio
async def test_call_budget_is_enforced_then_tools_are_switched_off(
    monkeypatch: pytest.MonkeyPatch, gateway: _FakeGateway, audit: list[dict[str, Any]]
) -> None:
    provider = _ScriptedProvider(
        [
            # Two calls in one turn against a budget of one.
            _resp(_use(READ, {}, "tu_1"), _use(APPEND, {}, "tu_2")),
            _resp(_text("Stopped at the budget.")),
        ]
    )
    _install(monkeypatch, provider)
    out = await _run(_step(max_tool_calls=1))
    assert [c["name"] for c in gateway.calls] == [READ]
    results = provider.calls[1]["messages"][-1]["content"]
    assert results[1]["is_error"] is True and "budget" in results[1]["content"]
    assert provider.calls[1]["tool_choice"] == {"type": "none"}
    assert out[-1][0] == "output"


@pytest.mark.asyncio
async def test_tool_errors_go_back_to_the_model(
    monkeypatch: pytest.MonkeyPatch, gateway: _FakeGateway, audit: list[dict[str, Any]]
) -> None:
    gateway.replies[APPEND] = "Tool error: range not found"
    provider = _ScriptedProvider(
        [_resp(_use(APPEND, {"range": "Z9"}, "tu_1")), _resp(_text("The sheet has no Z9."))]
    )
    _install(monkeypatch, provider)
    out = await _run(_step())
    assert provider.calls[1]["messages"][-1]["content"][0]["is_error"] is True
    assert f"`{APPEND}` — error" in out[-1][1]


@pytest.mark.asyncio
async def test_builtin_tools_run_locally(
    monkeypatch: pytest.MonkeyPatch, gateway: _FakeGateway, audit: list[dict[str, Any]]
) -> None:
    seen: list[dict[str, Any]] = []

    async def _fake_read(tool_input: dict[str, Any]) -> str:
        seen.append(tool_input)
        return "Invoice total 42"

    monkeypatch.setattr(tc, "builtin_handler", lambda name: _fake_read if name == "oe__read_file" else None)
    provider = _ScriptedProvider(
        [_resp(_use("oe__read_file", {"path": "/x/bill.pdf"}, "tu_1")), _resp(_text("Read it."))]
    )
    _install(monkeypatch, provider)
    await _run(_step(tools=["oe__read_file"]))
    assert seen == [{"path": "/x/bill.pdf"}]
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_unresolvable_tool_fails_before_any_model_call(
    monkeypatch: pytest.MonkeyPatch, gateway: _FakeGateway, audit: list[dict[str, Any]]
) -> None:
    provider = _ScriptedProvider([])
    _install(monkeypatch, provider)
    out = await _run(_step(tools=[READ, "srv__gone"]))
    assert out == [("error", out[0][1])] and "srv__gone" in out[0][1]
    assert provider.calls == []


@pytest.mark.asyncio
async def test_missing_gateway_is_a_tool_error_not_a_crash(
    monkeypatch: pytest.MonkeyPatch, gateway: _FakeGateway, audit: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr("openexecutive.orchestrator.mcp_gateway.get_active_gateway", lambda: None)
    provider = _ScriptedProvider([_resp(_use(READ, {}, "tu_1")), _resp(_text("Gateway down."))])
    _install(monkeypatch, provider)
    out = await _run(_step())
    result = provider.calls[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True and "gateway is not running" in result["content"]
    assert out[-1][0] == "output"


@pytest.mark.asyncio
async def test_provider_failure_is_a_fixed_error(
    monkeypatch: pytest.MonkeyPatch, gateway: _FakeGateway, audit: list[dict[str, Any]]
) -> None:
    provider = _ScriptedProvider([RuntimeError("zz_secret_request_body")])
    _install(monkeypatch, provider)
    out = await _run(_step())
    assert out[0][0] == "error" and "zz_secret" not in out[0][1]


def test_looks_like_error() -> None:
    assert act.looks_like_error('{"error": "no"}')
    assert act.looks_like_error("Error: Unknown tool 'x'")
    assert act.looks_like_error("Error calling tool 'x': boom")
    assert act.looks_like_error("Tool error: boom")
    assert not act.looks_like_error("Appended 1 row")
    assert not act.looks_like_error('{"result": "ok"}')
    # Successful results that merely mention errors are NOT failures — a false
    # positive invites the model to retry a write that already happened.
    assert not act.looks_like_error("Errors column: none")
    assert not act.looks_like_error("Error report Q3 — 4 rows")
    assert not act.looks_like_error('{"values": [1], "error": null}')
    assert not act.looks_like_error('{"error": ""}')


@pytest.mark.asyncio
async def test_empty_text_blocks_are_not_sent_back(
    monkeypatch: pytest.MonkeyPatch, gateway: _FakeGateway, audit: list[dict[str, Any]]
) -> None:
    """The API rejects an empty text block in a replayed assistant turn."""
    provider = _ScriptedProvider(
        [_resp(_text(""), _use(READ, {}, "tu_1")), _resp(_text("Done."))]
    )
    _install(monkeypatch, provider)
    await _run(_step())
    assistant = provider.calls[1]["messages"][1]
    assert assistant["role"] == "assistant"
    assert all(b["type"] != "text" for b in assistant["content"])


@pytest.mark.asyncio
async def test_running_out_of_turns_is_a_failure_that_lists_what_was_done(
    monkeypatch: pytest.MonkeyPatch, gateway: _FakeGateway, audit: list[dict[str, Any]]
) -> None:
    """A model that never reports (e.g. keeps naming refused tools) must not
    be recorded as a successful step."""
    step = _step(max_tool_calls=1)
    turns = step.max_tool_calls + act._EXTRA_TURNS
    provider = _ScriptedProvider(
        [_resp(_use(READ, {}, "tu_0"))]
        + [_resp(_use("srv__forbidden", {}, f"tu_{i}")) for i in range(1, turns)]
    )
    _install(monkeypatch, provider)
    out = await _run(step)
    assert out[-1][0] == "error"
    assert "turn limit" in out[-1][1] and f"{READ} (ok)" in out[-1][1]


# --- engine wiring -----------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_with_action_step_produces_an_artifact(
    monkeypatch: pytest.MonkeyPatch, gateway: _FakeGateway, audit: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(
        "openexecutive.workflows.dynamic.load_or_create_profile",
        lambda: SimpleNamespace(name="Northwind", is_empty=lambda: True),
    )
    provider = _ScriptedProvider(
        [_resp(_use(APPEND, {"rows": [["Acme", "10"]]}, "tu_1")), _resp(_text("Filed 1 bill."))]
    )
    _install(monkeypatch, provider)
    defn = DynamicWorkflowDef.model_validate(
        {
            "name": "file_bills_wf",
            "title": "File bills",
            "steps": [
                {"kind": "action", "id": "file_bills", "title": "File bills",
                 "goal": "File them.", "tools": [APPEND]},
                {"kind": "synthesis", "id": "assemble", "title": "Assemble"},
            ],
        }
    )
    wf = DynamicWorkflow(defn)
    events = [e async for e in wf.run(wf.input_model()(), store=None)]  # type: ignore[arg-type]
    types = [e.type for e in events]
    assert "progress" in types and "error" not in types
    progress = next(e for e in events if e.type == "progress")
    assert progress.step_id == "file_bills" and APPEND in (progress.summary or "")
    artifact = next(e for e in events if e.type == "artifact").content or ""
    assert "Filed 1 bill." in artifact and f"`{APPEND}` — ok" in artifact


@pytest.mark.asyncio
async def test_engine_surfaces_an_action_step_error(
    monkeypatch: pytest.MonkeyPatch, gateway: _FakeGateway, audit: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(
        "openexecutive.workflows.dynamic.load_or_create_profile",
        lambda: SimpleNamespace(name="Northwind", is_empty=lambda: True),
    )
    _install(monkeypatch, _ScriptedProvider([]))
    defn = DynamicWorkflowDef.model_validate(
        {
            "name": "file_bills_wf",
            "title": "File bills",
            "steps": [
                {"kind": "action", "id": "file_bills", "title": "File bills",
                 "goal": "File them.", "tools": ["srv__gone"]},
                {"kind": "synthesis", "id": "assemble", "title": "Assemble"},
            ],
        }
    )
    wf = DynamicWorkflow(defn)
    events = [e async for e in wf.run(wf.input_model()(), store=None)]  # type: ignore[arg-type]
    assert events[-1].type == "error" and "srv__gone" in (events[-1].message or "")
    assert not any(e.type == "artifact" for e in events)


@pytest.mark.asyncio
async def test_preflight_fails_before_any_step_acts(
    monkeypatch: pytest.MonkeyPatch, gateway: _FakeGateway, audit: list[dict[str, Any]]
) -> None:
    """A missing tool in a LATER step must stop the run before an earlier
    step sends or writes anything."""
    monkeypatch.setattr(
        "openexecutive.workflows.dynamic.load_or_create_profile",
        lambda: SimpleNamespace(name="Northwind", is_empty=lambda: True),
    )
    provider = _ScriptedProvider([])
    _install(monkeypatch, provider)
    defn = DynamicWorkflowDef.model_validate(
        {
            "name": "two_actions",
            "title": "Two actions",
            "steps": [
                {"kind": "action", "id": "first", "title": "First", "goal": "Append.", "tools": [APPEND]},
                {"kind": "action", "id": "second", "title": "Second", "goal": "Go.", "tools": ["srv__gone"]},
                {"kind": "synthesis", "id": "assemble", "title": "Assemble"},
            ],
        }
    )
    wf = DynamicWorkflow(defn)
    events = [e async for e in wf.run(wf.input_model()(), store=None)]  # type: ignore[arg-type]
    assert [e.type for e in events] == ["error"]
    assert "srv__gone" in (events[0].message or "") and "did not start" in (events[0].message or "")
    assert provider.calls == [] and gateway.calls == []


# --- resume after a gate -------------------------------------------------------


def _gated_defn(before_tools: list[str], after_tools: list[str]) -> DynamicWorkflowDef:
    return DynamicWorkflowDef.model_validate(
        {
            "name": "gated_actions",
            "title": "Gated actions",
            "steps": [
                {"kind": "action", "id": "before", "title": "Before", "goal": "Go.", "tools": before_tools},
                {"kind": "approval_gate", "id": "gate", "title": "Approve", "person_id": 7, "question": "OK?"},
                {"kind": "action", "id": "after", "title": "After", "goal": "Go.", "tools": after_tools},
                {"kind": "synthesis", "id": "assemble", "title": "Assemble"},
            ],
        }
    )


async def _resume(defn: DynamicWorkflowDef, decision: str = "approve") -> list[Any]:
    from openexecutive.workflows.dynamic import _steps_fingerprint
    from openexecutive.workflows.wait_for_human import (
        WaitForHumanResolution,
        WorkflowResumeState,
    )

    wf = DynamicWorkflow(defn)
    state = WorkflowResumeState(
        workflow_name=defn.name,
        gate_step_id="gate",
        gate_step_index=1,
        steps_fingerprint=_steps_fingerprint(defn),
        outputs={"before": ("Before", "did it")},
    )
    resolution = WaitForHumanResolution(
        reply_text="yes", source_channel="slack",
        parsed_decision={"decision": decision}, person_id=7,
    )
    return [
        e
        async for e in wf.resume(
            inputs=wf.input_model()(), state=state, resolution=resolution, store=None  # type: ignore[arg-type]
        )
    ]


@pytest.fixture()
def _profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "openexecutive.workflows.dynamic.load_or_create_profile",
        lambda: SimpleNamespace(name="Northwind", is_empty=lambda: True),
    )


@pytest.mark.asyncio
async def test_resume_ignores_tools_of_steps_that_already_ran(
    monkeypatch: pytest.MonkeyPatch, gateway: _FakeGateway, audit: list[dict[str, Any]], _profile: None
) -> None:
    """The pre-gate step already acted; its tool vanishing must not fail the
    remaining steps."""
    provider = _ScriptedProvider([_resp(_use(APPEND, {}, "tu_1")), _resp(_text("Done after."))])
    _install(monkeypatch, provider)
    events = await _resume(_gated_defn(["srv__gone"], [APPEND]))
    assert not any(e.type == "error" for e in events)
    artifact = next(e for e in events if e.type == "artifact").content or ""
    assert "did it" in artifact and "Done after." in artifact


@pytest.mark.asyncio
async def test_resume_checks_remaining_tools_after_the_decision(
    monkeypatch: pytest.MonkeyPatch, gateway: _FakeGateway, audit: list[dict[str, Any]], _profile: None
) -> None:
    _install(monkeypatch, _ScriptedProvider([]))
    events = await _resume(_gated_defn([READ], ["srv__gone"]))
    assert events[-1].type == "error"
    assert "after the approval did not run" in (events[-1].message or "")
    # A rejection is reported as a rejection, not as a missing tool.
    rejected = await _resume(_gated_defn([READ], ["srv__gone"]), decision="reject")
    assert "srv__gone" not in (rejected[-1].message or "")


@pytest.mark.asyncio
async def test_last_turn_always_switches_tools_off(
    monkeypatch: pytest.MonkeyPatch, gateway: _FakeGateway, audit: list[dict[str, Any]]
) -> None:
    """Even with budget left, the final turn must let the model report rather
    than act and then be failed without one."""
    step = _step(max_tool_calls=5)
    turns = step.max_tool_calls + act._EXTRA_TURNS
    provider = _ScriptedProvider(
        [_resp(_use("srv__forbidden", {}, f"tu_{i}")) for i in range(turns - 1)]
        + [_resp(_text("Could not do it with these tools."))]
    )
    _install(monkeypatch, provider)
    out = await _run(step)
    assert provider.calls[-1]["tool_choice"] == {"type": "none"}
    assert all("tool_choice" not in c for c in provider.calls[:-1])
    assert out[-1][0] == "output"


@pytest.mark.asyncio
async def test_write_calls_audit_their_targets_but_never_content(
    monkeypatch: pytest.MonkeyPatch, gateway: _FakeGateway, audit: list[dict[str, Any]]
) -> None:
    """A write redirected by injected content must be visible in the audit."""
    provider = _ScriptedProvider(
        [
            _resp(_use(READ, {"spreadsheet_id": "sheet-READ"}, "tu_1")),
            _resp(_use(APPEND, {"spreadsheet_id": "sheet-XYZ", "rows": [["Acme", "10"]]}, "tu_2")),
            _resp(_text("Done.")),
        ]
    )
    _install(monkeypatch, provider)
    await _run(_step())
    read_row, write_row = audit
    assert "targets" not in read_row["details"]  # read-only tool: nothing logged
    assert write_row["details"]["targets"] == {"spreadsheet_id": "sheet-XYZ"}
    assert "Acme" not in json.dumps(write_row)
