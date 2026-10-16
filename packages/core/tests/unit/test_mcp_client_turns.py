"""A turn another AI app asks over Open Executive's MCP server
(``Session.from_mcp_client``, set by ``mcp_server.ask_executive``) reads and
advises but never sends, posts, books, schedules, runs or changes anything:
the chat loop neither offers nor runs ``MCP_CLIENT_WITHHELD_TOOLS``, Act as
me or extension tools, and of the gateway tools only the reads in
``MCP_CLIENT_MCP_TOOLS``."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from openexecutive.audit import logger as audit_logger
from openexecutive.audit.logger import AuditLogger, log_event
from openexecutive.departments import registry as dept_registry
from openexecutive.departments import store as dept_store
from openexecutive.memory import episodic
from openexecutive.orchestrator.schedule_tools import (
    MCP_CLIENT_MCP_TOOLS,
    MCP_CLIENT_REFUSAL,
    MCP_CLIENT_WITHHELD_TOOLS,
    PRIVATE_TURN_MCP_TOOLS,
    PRIVATE_TURN_WITHHELD_TOOLS,
    UNATTENDED_WITHHELD_TOOLS,
    current_session,
)
from openexecutive.orchestrator.session import Session
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store
from tests.unit.offered_tools import capture_offered

EXEC = "exec@example.com"
OWNER_EMAIL = "olivia@co.example"
TEAM_EMAIL = "ben@co.example"


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "mcp_client.db"
    monkeypatch.setattr(people_store, "DB_PATH", path)
    monkeypatch.setattr(episodic, "DB_PATH", path)
    monkeypatch.setattr(dept_store, "DB_PATH", path)
    episodic.initialize_db(path)
    people_store.initialize_db()
    dept_store.initialize_db()
    people_registry.invalidate()
    dept_registry.invalidate()
    monkeypatch.setattr(audit_logger, "_default_logger", AuditLogger(db_path=path))
    token = current_session.set(None)
    yield path
    current_session.reset(token)
    people_registry.invalidate()
    dept_registry.invalidate()


@pytest.fixture
def roster() -> SimpleNamespace:
    principal = people_store.upsert_person(
        full_name="Olivia Owner", is_principal=True, email=OWNER_EMAIL, slack_user_id="U_OWNER"
    )
    teammate = people_store.upsert_person(
        full_name="Ben Teammate", role="Ops", email=TEAM_EMAIL, slack_user_id="U_BEN"
    )
    people_registry.invalidate()
    return SimpleNamespace(principal=principal, teammate=teammate)


def _mcp_turn(person_id: int | None) -> Session:
    return Session(from_mcp_client=True, caller_person_id=person_id)


class _ScriptedStreams:
    def __init__(self, finals: list[Any]) -> None:
        self._finals = list(finals)
        self.calls: list[dict[str, Any]] = []
        self.offered: list[list[str]] = []

    def messages_stream(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        final = self._finals.pop(0)

        class _Stream:
            async def __aenter__(self) -> _Stream:
                return self

            async def __aexit__(self, *_a: Any) -> None:
                return None

            def __aiter__(self) -> _Stream:
                return self

            async def __anext__(self) -> Any:
                raise StopAsyncIteration

            async def get_final_message(self) -> Any:
                return final

        return _Stream()


def _gateway() -> Any:
    gateway = AsyncMock()
    gateway.load_mcp_server = AsyncMock(return_value=json.dumps({"ok": True}))
    return gateway


def _run_loop(session: Session, tool_uses: list[Any], gateway: Any = None) -> _ScriptedStreams:
    from openexecutive.orchestrator.executive import Executive
    from openexecutive.orchestrator.schedule_tools import set_session

    finals = []
    if tool_uses:
        finals.append(SimpleNamespace(content=tool_uses, stop_reason="tool_use", usage=None))
    finals.append(
        SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ok")], stop_reason="end_turn", usage=None
        )
    )
    provider = _ScriptedStreams(finals)

    async def _go() -> None:
        with (
            capture_offered() as offered,
            patch("openexecutive.orchestrator.executive.get_provider", return_value=provider),
            patch("openexecutive.orchestrator.executive.audit_log", log_event),
            set_session(session),
        ):
            async for _item in Executive(mcp_gateway=gateway)._stream_agent_loop(
                system_blocks=[],
                messages=[{"role": "user", "content": "x"}],
                model="claude-test",
                workspace_mode="team",
                principal_role_tag="",
                turn_id="t-mcp",
            ):
                pass
            provider.offered = offered

    asyncio.run(_go())
    return provider


def _results(provider: _ScriptedStreams) -> dict[str, str]:
    return {r["tool_use_id"]: r["content"] for r in provider.calls[1]["messages"][-1]["content"]}


def _refusals() -> list[Any]:
    return [
        e
        for e in audit_logger.get_audit_logger().query(event_type="tool_invocation", limit=200)
        if e.details.get("refused") == "mcp_client"
    ]


# --- the sets ---------------------------------------------------------------


def test_the_withheld_set_starts_from_the_private_and_unattended_sets() -> None:
    assert PRIVATE_TURN_WITHHELD_TOOLS | UNATTENDED_WITHHELD_TOOLS <= MCP_CLIENT_WITHHELD_TOOLS
    for name in (
        "message_person", "send_slack_dm", "send_discord_dm", "send_telegram_message",
        "create_calendar_event", "cancel_calendar_event", "ack_alert", "upsert_person",
        "archive_person", "set_department_head", "close_open_loop", "create_alert",
    ):
        assert name in MCP_CLIENT_WITHHELD_TOOLS


def test_every_withheld_name_is_a_real_tool() -> None:
    from openexecutive.orchestrator.executive import _ALL_SKILL_HANDLERS, _ALL_SKILL_TOOLS
    from openexecutive.orchestrator.mcp_gateway import MCP_TOOL_NAMES
    from openexecutive.workflows.step_script import LIST_SAVED_TOOLS_TOOL, RUN_SCRIPT_TOOL

    gateway_side = MCP_TOOL_NAMES | {RUN_SCRIPT_TOOL, LIST_SAVED_TOOLS_TOOL}
    assert {t["name"] for t in _ALL_SKILL_TOOLS} | gateway_side >= MCP_CLIENT_WITHHELD_TOOLS
    assert set(_ALL_SKILL_HANDLERS) | gateway_side >= MCP_CLIENT_WITHHELD_TOOLS


def test_every_tool_left_offered_only_reads() -> None:
    """Pins what such a turn may still run: a new tool that writes must be
    added to the withheld set, not slip in."""
    from openexecutive.orchestrator.executive import _ALL_SKILL_TOOLS

    offered = {t["name"] for t in _ALL_SKILL_TOOLS} - MCP_CLIENT_WITHHELD_TOOLS
    assert offered == {
        "ask_about_person", "find_alerts", "get_artifact", "list_artifacts",
        "list_department_goals", "list_followups", "list_open_loops", "list_people",
        "list_watchlist", "list_workflows", "load_skill", "lookup_person",
        "read_user_guide", "search_skills",
    }


def test_the_gateway_allow_list_holds_no_send_or_draft() -> None:
    assert MCP_CLIENT_MCP_TOOLS < PRIVATE_TURN_MCP_TOOLS
    for name in MCP_CLIENT_MCP_TOOLS:
        assert "send" not in name and "draft" not in name


# --- the chat loop ----------------------------------------------------------


def test_an_mcp_client_turn_is_not_offered_the_withheld_tools(roster: SimpleNamespace) -> None:
    mcp_turn = _run_loop(_mcp_turn(roster.teammate), [], _gateway())
    offered = mcp_turn.offered[0]
    normal = _run_loop(Session(caller_person_id=roster.teammate), [], _gateway()).offered[0]
    assert not MCP_CLIENT_WITHHELD_TOOLS & set(offered)
    # Withheld before the sort: the normal list minus the set, still sorted.
    assert offered == sorted(offered)
    assert offered == [n for n in normal if n not in MCP_CLIENT_WITHHELD_TOOLS]
    assert {"consult_specialist", "search_tools", "call_tool", "list_people"} <= set(offered)
    # Stable: the same prefix on the next such turn.
    assert _run_loop(_mcp_turn(roster.teammate), [], _gateway()).offered[0] == offered
    tools = mcp_turn.calls[0]["tools"]
    client = [t for t in tools if "input_schema" in t]
    assert "cache_control" in client[-1]


@pytest.mark.parametrize(
    "tool",
    ["message_person", "send_slack_dm", "create_calendar_event", "ack_alert", "upsert_person"],
)
def test_an_mcp_client_turn_refuses_a_withheld_tool_the_model_emits_anyway(
    roster: SimpleNamespace, tool: str
) -> None:
    from openexecutive.orchestrator import executive as executive_module

    handler = AsyncMock(return_value=json.dumps({"status": "sent"}))
    tool_use = SimpleNamespace(type="tool_use", id="tu-1", name=tool, input={"text": "hi"})
    with patch.dict(executive_module._ALL_SKILL_HANDLERS, {tool: handler}):
        provider = _run_loop(_mcp_turn(roster.principal), [tool_use])
    handler.assert_not_awaited()
    error = json.loads(_results(provider)["tu-1"])["error"]
    assert tool in error and MCP_CLIENT_REFUSAL in error
    assert [e.details["tool"] for e in _refusals()] == [tool]


def test_an_mcp_client_turn_refuses_through_use_tool(roster: SimpleNamespace) -> None:
    from openexecutive.orchestrator import executive as executive_module

    handler = AsyncMock(return_value=json.dumps({"status": "sent"}))
    use = SimpleNamespace(
        type="tool_use", id="tu-1", name="use_tool",
        input={"name": "send_company_broadcast", "input": {"text": "hi"}},
    )
    with patch.dict(executive_module._ALL_SKILL_HANDLERS, {"send_company_broadcast": handler}):
        provider = _run_loop(_mcp_turn(roster.principal), [use])
    handler.assert_not_awaited()
    assert MCP_CLIENT_REFUSAL in json.loads(_results(provider)["tu-1"])["error"]


def test_an_mcp_client_turn_gets_no_act_as_me_or_extension_tools(
    roster: SimpleNamespace,
) -> None:
    from openexecutive.delegation import settings as delegation_settings
    from openexecutive.orchestrator.delegation_tools import DELEGATION_TOOL_HANDLERS

    # Even with Act as me on and pinned as offered, the turn is not offered it.
    pinned = SimpleNamespace(offered=True)
    with (
        patch("openexecutive.orchestrator.executive.turn_delegation", return_value=pinned),
        patch(
            "openexecutive.orchestrator.executive.extensions.offered_tools",
            side_effect=AssertionError("extensions are not offered"),
        ),
    ):
        offered = _run_loop(_mcp_turn(roster.principal), []).offered[0]
    assert not set(DELEGATION_TOOL_HANDLERS) & set(offered)
    assert "recall_history" not in offered
    # And Act as me is never pinned as offered on such a turn.
    assert delegation_settings._offered(_mcp_turn(roster.principal), None, True) is False


def test_an_mcp_client_turn_refuses_load_mcp_server(roster: SimpleNamespace) -> None:
    gateway = _gateway()
    tool_use = SimpleNamespace(
        type="tool_use", id="tu-1", name="load_mcp_server",
        input={"name": "x", "url": "https://collector.example/mcp"},
    )
    provider = _run_loop(_mcp_turn(roster.principal), [tool_use], gateway)
    gateway.load_mcp_server.assert_not_awaited()
    assert "load_mcp_server" not in provider.offered[0]
    assert MCP_CLIENT_REFUSAL in json.loads(_results(provider)["tu-1"])["error"]


# --- gateway tools: reads only ----------------------------------------------

_SEARCH_TEXT = (
    "Found 3 matching tool(s). Use `call_tool` with the tool name and arguments "
    "to invoke one.\n\n"
    + "\n".join(
        "\n".join([f"## {name}", f"**Description:** {desc}", "**Parameters:**",
                   '```json\n{"type": "object"}\n```', "**Similarity:** 0.800", ""])
        for name, desc in [
            ("google_workspace__send_gmail_message", "Send an email."),
            ("google_workspace__search_drive_files", "Search Drive."),
            ("slack__post_message", "Post to Slack."),
        ]
    )
)


def _real_gateway() -> tuple[Any, list[tuple[str, dict[str, Any]]]]:
    from openexecutive.orchestrator.mcp_gateway import MCPGateway

    reached: list[tuple[str, dict[str, Any]]] = []

    async def _call(name: str, args: dict[str, Any]) -> Any:
        reached.append((name, args))
        text = _SEARCH_TEXT if name == "search_tools" else json.dumps({"ok": True})
        return SimpleNamespace(content=[SimpleNamespace(text=text)])

    gateway = MCPGateway()
    gateway._session = SimpleNamespace(call_tool=_call)
    return gateway, reached


def test_an_mcp_client_turn_reaches_gateway_reads_only(roster: SimpleNamespace) -> None:
    import openexecutive.orchestrator.mcp_gateway as gw_module

    def _call(i: int, tool: str, arguments: dict[str, Any]) -> Any:
        return SimpleNamespace(
            type="tool_use", id=f"tu-{i}", name="call_tool",
            input={"name": tool, "arguments": arguments},
        )

    uses = [
        SimpleNamespace(type="tool_use", id="tu-1", name="search_tools", input={"query": "x"}),
        _call(2, "google_workspace__send_gmail_message",
              {"to": OWNER_EMAIL, "subject": "s", "body": "b"}),
        _call(3, "google_workspace__draft_gmail_message",
              {"to": OWNER_EMAIL, "subject": "s", "body": "b"}),
        _call(4, "slack__post_message", {"channel": "#all", "text": "hi"}),
        _call(5, "google_workspace__search_drive_files", {"query": "plan", "user_google_email": EXEC}),
    ]
    gateway, reached = _real_gateway()
    with patch.object(
        gw_module, "get_settings", return_value=SimpleNamespace(exec_email_address=EXEC)
    ):
        provider = _run_loop(_mcp_turn(roster.principal), uses, gateway)
    results = _results(provider)
    # A search shows only the reads.
    assert "google_workspace__search_drive_files" in results["tu-1"]
    assert "send_gmail_message" not in results["tu-1"] and "slack__" not in results["tu-1"]
    for refused in ("tu-2", "tu-3", "tu-4"):
        assert MCP_CLIENT_REFUSAL in json.loads(results[refused])["error"]
    called = [args["tool_name"] for name, args in reached if name == "call_tool"]
    assert called == ["google_workspace__search_drive_files"]


def test_the_gateway_itself_refuses_a_send_on_an_mcp_client_turn(roster: SimpleNamespace) -> None:
    from openexecutive.orchestrator.schedule_tools import set_session

    gateway, reached = _real_gateway()

    async def _go() -> str:
        with set_session(_mcp_turn(roster.principal)):
            return str(await gateway.call_tool({
                "name": "google_workspace__send_gmail_message",
                "arguments": {"to": OWNER_EMAIL, "subject": "s", "body": "b"},
            }))

    out = asyncio.run(_go())
    assert MCP_CLIENT_REFUSAL in json.loads(out)["error"]
    assert reached == []


# --- a scheduled run a teammate queued (Session.run_for_person_id) ----------


def _teammate_run(person_id: int) -> Session:
    return Session(unattended=True, run_for_person_id=person_id)


def test_a_teammates_scheduled_run_is_read_only(roster: SimpleNamespace) -> None:
    """The runner sends what such a run writes; the run itself sends,
    schedules and writes nothing (`schedule_tools.read_only_turn`)."""
    from openexecutive.orchestrator.schedule_tools import read_only_turn

    assert read_only_turn(_teammate_run(roster.teammate))
    assert not read_only_turn(Session(unattended=True))
    offered = set(_run_loop(_teammate_run(roster.teammate), [], _gateway()).offered[0])
    assert not MCP_CLIENT_WITHHELD_TOOLS & offered
    assert not {"message_person", "send_slack_dm", "schedule_followup", "suggest_workflow"} & offered


def test_a_teammates_scheduled_run_refuses_a_send_the_model_emits(
    roster: SimpleNamespace,
) -> None:
    from openexecutive.orchestrator import executive as executive_module

    handler = AsyncMock(return_value=json.dumps({"status": "sent"}))
    tool_use = SimpleNamespace(
        type="tool_use", id="tu-1", name="message_person", input={"text": "Q3 numbers"}
    )
    with patch.dict(executive_module._ALL_SKILL_HANDLERS, {"message_person": handler}):
        provider = _run_loop(_teammate_run(roster.teammate), [tool_use])
    handler.assert_not_awaited()
    assert "message_person" in json.loads(_results(provider)["tu-1"])["error"]


def test_the_gateway_refuses_a_send_on_a_teammates_scheduled_run(
    roster: SimpleNamespace,
) -> None:
    from openexecutive.orchestrator.schedule_tools import set_session

    gateway, reached = _real_gateway()

    async def _go() -> str:
        with set_session(_teammate_run(roster.teammate)):
            return str(await gateway.call_tool({
                "name": "google_workspace__send_gmail_message",
                "arguments": {"to": OWNER_EMAIL, "subject": "s", "body": "b"},
            }))

    assert "error" in json.loads(asyncio.run(_go()))
    assert reached == []
