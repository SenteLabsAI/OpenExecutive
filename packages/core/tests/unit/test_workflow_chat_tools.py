"""Chat tools in workflow steps (workflows/chat_tools.py).

Pins the parity contract (every tool chat offers is labelled, so a new chat
tool can't silently skip the workflow decision), and that a step runs a chat
tool the way an unattended chat turn would: the same handler, an unattended
session, the workspace-mode filter, the creator's department gate, and no
workflow starting itself.
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import Any

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-used")

from openexecutive.orchestrator.schedule_tools import current_session  # noqa: E402
from openexecutive.workflows import action_step as act  # noqa: E402
from openexecutive.workflows import chat_tools as ct  # noqa: E402
from openexecutive.workflows import tool_catalog as tc  # noqa: E402
from openexecutive.workflows.dynamic_models import ActionStepSpec  # noqa: E402

# ── Parity ───────────────────────────────────────────────────────────────────


def test_every_chat_tool_is_labelled() -> None:
    """A new chat tool must say whether workflows get it (ACCESS)."""
    missing = sorted(set(ct.chat_tool_definitions()) - set(ct.ACCESS))
    assert missing == [], f"label these in workflows/chat_tools.ACCESS: {missing}"


def test_no_stale_labels() -> None:
    stale = sorted(set(ct.ACCESS) - set(ct.chat_tool_definitions()) - {ct.WEB_SEARCH})
    assert stale == [], f"these are no longer chat tools: {stale}"


def test_every_usable_tool_has_a_chat_handler() -> None:
    handlers = ct._chat_handlers()
    usable = [n for n, (lbl, _) in ct.ACCESS.items() if lbl == ct.USE and n != ct.WEB_SEARCH]
    assert sorted(n for n in usable if n not in handlers) == []


def test_usable_tools_are_offered_to_workflows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "openexecutive.orchestrator.schedule_tools.filter_tools_for_configured_channels",
        lambda tools, settings: tools,
    )
    names = {t.name for t in tc.builtin_tools()}
    for chat_name, (lbl, _) in ct.ACCESS.items():
        if lbl == ct.USE and chat_name != ct.WEB_SEARCH:
            assert ct.workflow_name_for(chat_name) in names, chat_name
    for chat_name, (lbl, _) in ct.ACCESS.items():
        if lbl != ct.USE:
            assert ct.workflow_name_for(chat_name) not in names, chat_name


@pytest.mark.asyncio
async def test_workflow_tool_names_from_before_run_the_chat_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """oe__message_person / oe__create_alert are the chat tools now, under the
    same names; oe__read_file (saved workflows may list it) runs read_document
    but isn't offered for new steps."""
    ran: list[tuple[str, dict[str, Any]]] = []

    async def run_chat_tool(name: str, tool_input: dict[str, Any]) -> str:
        ran.append((name, tool_input))
        return "ok"

    monkeypatch.setattr(ct, "run_chat_tool", run_chat_tool)
    for name in ("oe__message_person", "oe__create_alert", "oe__read_file"):
        handler = tc.builtin_handler(name)
        assert handler is not None, name
        await handler({"x": name})
    assert ran == [
        ("message_person", {"x": "oe__message_person"}),
        ("create_alert", {"x": "oe__create_alert"}),
        ("read_document", {"x": "oe__read_file"}),
    ]
    offered = {t.name for t in tc.builtin_tools()}
    assert "oe__read_file" not in offered and "oe__read_document" in offered
    assert (await tc.resolve(["oe__read_file"]))["oe__read_file"].read_only is True


def test_long_running_tools_get_a_longer_timeout() -> None:
    specs = ct.workflow_specs()
    assert specs["oe__run_workflow"][0].timeout_s == 900.0
    assert specs["oe__run_executive_research"][0].timeout_s == 900.0
    assert specs["oe__list_people"][0].timeout_s is None


def test_web_search_is_a_server_tool_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "openexecutive.orchestrator.web_search_tool.build_web_search_tool",
        lambda **_: {"type": "web_search_20250305", "name": "web_search"},
    )
    assert ct.workflow_specs()["oe__web_search"][0].source == "server"
    monkeypatch.setattr(
        "openexecutive.orchestrator.web_search_tool.build_web_search_tool", lambda **_: None
    )
    assert "oe__web_search" not in ct.workflow_specs()


# ── Running a chat tool ──────────────────────────────────────────────────────


@pytest.fixture()
def fake_session(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    modes: list[str] = []

    def _session(mode: str) -> Any:
        modes.append(mode)
        return SimpleNamespace(session_id="wf-session", unattended=True)

    monkeypatch.setattr(ct, "_session", _session)
    monkeypatch.setattr(
        "openexecutive.memory.workspace_settings.effective_workspace_mode", lambda _s: "team"
    )
    return modes


def _handlers(monkeypatch: pytest.MonkeyPatch, **handlers: Any) -> None:
    monkeypatch.setattr(ct, "_chat_handlers", lambda: handlers)


@pytest.mark.asyncio
async def test_runs_the_chat_handler_in_an_unattended_session(
    monkeypatch: pytest.MonkeyPatch, fake_session: list[str]
) -> None:
    seen: list[Any] = []

    async def list_people(tool_input: dict[str, Any]) -> str:
        seen.append((tool_input, current_session.get()))
        return "Ana, Ben"

    _handlers(monkeypatch, list_people=list_people)
    assert await ct.run_chat_tool("list_people", {"q": "a"}) == "Ana, Ben"
    (tool_input, session), = seen
    assert tool_input == {"q": "a"}
    assert session.unattended is True
    assert fake_session == ["team"]
    assert current_session.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["remember_fact", "ghostwrite_email", "consult_specialist", "nope"])
async def test_tools_not_usable_in_workflows_are_refused(
    monkeypatch: pytest.MonkeyPatch, fake_session: list[str], name: str
) -> None:
    called: list[str] = []

    async def handler(tool_input: dict[str, Any]) -> str:
        called.append(name)
        return "ran"

    _handlers(monkeypatch, **{name: handler})
    out = json.loads(await ct.run_chat_tool(name, {}))
    assert "not available in workflows" in out["error"]
    assert called == []


@pytest.mark.asyncio
async def test_solo_mode_withholds_team_only_tools(
    monkeypatch: pytest.MonkeyPatch, fake_session: list[str]
) -> None:
    monkeypatch.setattr(
        "openexecutive.memory.workspace_settings.effective_workspace_mode", lambda _s: "solo"
    )

    async def handler(tool_input: dict[str, Any]) -> str:
        raise AssertionError("must not run")

    _handlers(monkeypatch, send_department_message=handler)
    out = json.loads(await ct.run_chat_tool("send_department_message", {}))
    assert "solo mode" in out["error"]


@pytest.mark.asyncio
async def test_a_workflow_cannot_start_itself_or_nest_too_deep(
    monkeypatch: pytest.MonkeyPatch, fake_session: list[str]
) -> None:
    ran: list[str] = []

    async def run_workflow(tool_input: dict[str, Any]) -> str:
        ran.append(tool_input["name"])
        return "started"

    _handlers(monkeypatch, run_workflow=run_workflow)
    with ct.in_workflow("weekly_report", None):
        loop = json.loads(await ct.run_chat_tool("run_workflow", {"name": "weekly_report"}))
        assert "already running" in loop["error"]
        assert await ct.run_chat_tool("run_workflow", {"name": "other"}) == "started"
        with ct.in_workflow("b", None), ct.in_workflow("c", None):
            deep = json.loads(await ct.run_chat_tool("run_workflow", {"name": "d"}))
            assert "at most" in deep["error"]
    assert ran == ["other"]
    assert ct._chain.get() == ()


@pytest.mark.asyncio
async def test_a_team_members_workflow_goes_through_their_department_gate(
    monkeypatch: pytest.MonkeyPatch, fake_session: list[str]
) -> None:
    member = SimpleNamespace(id=7, is_principal=False, kind="team")
    monkeypatch.setattr(
        "openexecutive.people.store.get_person", lambda pid: member if pid == 7 else None
    )
    gated: list[Any] = []

    def gated_handlers(handlers: dict[str, Any], person: Any, *, session_id: str | None = None) -> Any:
        gated.append((person, session_id))

        async def refused(tool_input: dict[str, Any]) -> str:
            return "held for the department head"

        return {name: refused for name in handlers}

    monkeypatch.setattr(
        "openexecutive.orchestrator.department_gate.gated_handlers", gated_handlers
    )

    async def message_person(tool_input: dict[str, Any]) -> str:
        return "sent"

    _handlers(monkeypatch, message_person=message_person)
    with ct.in_workflow("wf", 7):
        assert await ct.run_chat_tool("message_person", {}) == "held for the department head"
    assert gated == [(member, "wf-session")]
    # The principal's own workflow (no creator on record) is not gated.
    assert await ct.run_chat_tool("message_person", {}) == "sent"
    assert len(gated) == 1


# ── In an action step ────────────────────────────────────────────────────────


class _Provider:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def messages_create(self, **kwargs: Any) -> Any:
        self.calls.append({**kwargs, "messages": [dict(m) for m in kwargs["messages"]]})
        return self._responses.pop(0)


class _Block(SimpleNamespace):
    def model_dump(self, exclude_none: bool = False) -> dict[str, Any]:
        return dict(vars(self))


@pytest.fixture()
def step_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **k: None)
    monkeypatch.setattr("openexecutive.audit.usage.log_model_usage", lambda *a, **k: None)
    monkeypatch.setattr(
        "openexecutive.orchestrator.web_search_tool.build_web_search_tool",
        lambda **_: {"type": "web_search_20250305", "name": "web_search"},
    )
    monkeypatch.setattr("openexecutive.workflows.dynamic_store.get_owner", lambda name: None)

    async def _resolve(names: list[str]) -> dict[str, tc.ToolInfo]:
        builtins = {t.name: t for t in tc.builtin_tools()}
        return {n: builtins[n] for n in names if n in builtins}

    monkeypatch.setattr(tc, "resolve", _resolve)


async def _run_step(monkeypatch: pytest.MonkeyPatch, provider: _Provider, tools: list[str]) -> list[Any]:
    monkeypatch.setattr("openexecutive.providers.registry.get_provider", lambda model: provider)
    step = ActionStepSpec.model_validate(
        {"id": "s", "title": "S", "goal": "Find the news.", "tools": tools}
    )
    return [
        item
        async for item in act.run_action_step(
            step, workflow_name="wf", workflow_title="WF", goal=step.goal,
            values={}, company_block="", prior_outputs={}, policy=None,
        )
    ]


@pytest.mark.asyncio
async def test_web_search_is_offered_as_the_models_own_tool(
    monkeypatch: pytest.MonkeyPatch, step_env: None
) -> None:
    search = _Block(type="server_tool_use", id="srv_1", name="web_search", input={"query": "q"})
    found = _Block(type="web_search_tool_result", tool_use_id="srv_1", content=[])
    provider = _Provider([
        SimpleNamespace(content=[search, found], stop_reason="pause_turn"),
        SimpleNamespace(content=[SimpleNamespace(type="text", text="Here's the news.")], stop_reason="end_turn"),
    ])
    out = await _run_step(monkeypatch, provider, ["oe__list_people", "oe__web_search"])
    tools = provider.calls[0]["tools"]
    names = [t["name"] for t in tools]
    assert "oe__web_search" not in names
    assert names[-1] == "web_search" and names[:-1] == sorted(names[:-1])
    assert tools[-1]["type"] == "web_search_20250305"
    # The paused search is sent back as-is so the model carries on from it.
    assert provider.calls[1]["messages"][-1] == {
        "role": "assistant",
        "content": [vars(search), vars(found)],
    }
    assert out[-1][0] == "output"
    assert "Here's the news." in out[-1][1]


@pytest.mark.asyncio
async def test_a_chat_tool_step_runs_inside_its_workflow(
    monkeypatch: pytest.MonkeyPatch, step_env: None, fake_session: list[str]
) -> None:
    chains: list[tuple[str, ...]] = []

    async def list_people(tool_input: dict[str, Any]) -> str:
        chains.append(ct._chain.get())
        return "Ana"

    _handlers(monkeypatch, list_people=list_people)
    provider = _Provider([
        SimpleNamespace(
            content=[SimpleNamespace(type="tool_use", id="tu_1", name="oe__list_people", input={})],
            stop_reason="tool_use",
        ),
        SimpleNamespace(content=[SimpleNamespace(type="text", text="Done.")], stop_reason="end_turn"),
    ])
    await _run_step(monkeypatch, provider, ["oe__list_people"])
    assert chains == [("wf",)]
    result = provider.calls[1]["messages"][-1]["content"][0]
    assert result["content"] == "Ana" and result["is_error"] is False
