"""The Executive's chat may act through one sandboxed script (``run_script``).

Pins that a chat script is only a faster way to make the same ``call_tool``
uses: every call goes through the gateway (which keeps its own discovery,
deny-list and recipient gates), gets the same chip and audit row, and is
recorded as made via run_script; a turn private to the principal is never
offered it and is refused if it asks anyway; and the tool list stays
constant, so the cached prefix does not move.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch

import pytest

from openexecutive.orchestrator.executive import Executive
from openexecutive.workflows import step_script


class _TextBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _ToolUseBlock:
    type = "tool_use"

    def __init__(self, id_: str, name: str, input_: dict[str, Any]) -> None:
        self.id = id_
        self.name = name
        self.input = input_


class _FinalMsg:
    usage = None

    def __init__(self, content: list[Any], stop_reason: str) -> None:
        self.content = content
        self.stop_reason = stop_reason


class _FakeStream:
    def __init__(self, final_msg: _FinalMsg) -> None:
        self._final_msg = final_msg

    async def __aenter__(self) -> _FakeStream:
        return self

    async def __aexit__(self, *_a: Any) -> None:
        return None

    def __aiter__(self) -> _FakeStream:
        return self

    async def __anext__(self) -> Any:
        raise StopAsyncIteration

    async def get_final_message(self) -> _FinalMsg:
        return self._final_msg


class _ScriptedProvider:
    def __init__(self, final_msgs: list[_FinalMsg]) -> None:
        self._final_msgs = list(final_msgs)
        self.calls: list[dict[str, Any]] = []

    def messages_stream(self, **kwargs: Any) -> _FakeStream:
        self.calls.append(kwargs)
        return _FakeStream(self._final_msgs.pop(0))


class _Gateway:
    """Stands in for MCPGateway: it, not the script, decides what may run."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def search_tools(self, _input: dict[str, Any]) -> str:
        return json.dumps({"tools": []})

    async def call_tool(self, tool_input: dict[str, Any]) -> str:
        self.calls.append(tool_input)
        if tool_input["name"] == "drive__list_items":
            return json.dumps([{"id": "f1"}, {"id": "f2"}, {"id": "f3"}])
        if tool_input["name"] == "slack__post_message":
            return "Error: tool slack__post_message has not been discovered in this session"
        return json.dumps({"ok": True})

    async def load_mcp_server(self, _input: dict[str, Any]) -> str:
        return json.dumps({"ok": True})


@pytest.fixture(autouse=True)
def audit(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "openexecutive.orchestrator.executive.audit_log",
        lambda event_type, summary, **kw: rows.append({"event_type": event_type, "summary": summary, **kw}),
    )
    return rows


def _run(provider: _ScriptedProvider, gateway: _Gateway) -> list[Any]:
    exec_ = Executive(mcp_gateway=gateway)  # type: ignore[arg-type]

    async def go() -> list[Any]:
        items: list[Any] = []
        with patch("openexecutive.orchestrator.executive.get_provider", return_value=provider):
            async for item in exec_._stream_agent_loop(
                system_blocks=[],
                messages=[{"role": "user", "content": "file the scans"}],
                model="claude-test",
            ):
                items.append(item)
        return items

    return asyncio.run(go())


def _result(provider: _ScriptedProvider, call_index: int, use_id: str) -> str:
    turn = provider.calls[call_index]["messages"][-1]
    return {b["tool_use_id"]: b["content"] for b in turn["content"]}[use_id]


SCRIPT = """
moved = 0
for f in drive__list_items(folder="Inbox scans"):
    drive__move_file(file_id=f["id"], folder_id="Finance")
    moved += 1
moved
"""


def _script_turn(script: str = SCRIPT) -> _ScriptedProvider:
    return _ScriptedProvider([
        _FinalMsg([_ToolUseBlock("tu-s", "run_script", {"script": script})], "tool_use"),
        _FinalMsg([_TextBlock("Done.")], "end_turn"),
    ])


def test_each_script_call_is_a_gateway_call_with_its_own_audit_row(
    audit: list[dict[str, Any]],
) -> None:
    gateway = _Gateway()
    provider = _script_turn()
    _run(provider, gateway)

    assert json.loads(_any_result(provider, "tu-s"))["result"] == 3
    assert [c["name"] for c in gateway.calls] == [
        "drive__list_items", "drive__move_file", "drive__move_file", "drive__move_file"
    ]
    assert gateway.calls[1] == {"name": "drive__move_file", "arguments": {"file_id": "f1", "folder_id": "Finance"}}
    rows = [r for r in audit if r["details"].get("via") == "run_script"]
    assert [r["details"]["tool"] for r in rows] == [c["name"] for c in gateway.calls]
    assert all(r["details"]["kind"] == "mcp" for r in rows)


def test_the_gateway_still_refuses_what_it_would_refuse(audit: list[dict[str, Any]]) -> None:
    gateway = _Gateway()
    provider = _script_turn("slack__post_message(channel='#all', text='hi')")
    _run(provider, gateway)
    body = json.loads(_any_result(provider, "tu-s"))
    assert body["error"] == "the script failed" and "has not been discovered" in body["detail"]
    assert body["calls"] == [{"tool": "slack__post_message", "ok": False}]


def test_the_tool_is_offered_with_a_gateway_and_is_constant() -> None:
    gateway = _Gateway()
    provider = _ScriptedProvider([_FinalMsg([_TextBlock("Hi.")], "end_turn")])
    _run(provider, gateway)
    tools = [t for t in provider.calls[0]["tools"] if t.get("name") == "run_script"]
    assert len(tools) == 1
    assert {k: v for k, v in tools[0].items() if k != "cache_control"} == step_script.CHAT_TOOL_DEFINITION


def test_no_gateway_no_script_tool() -> None:
    provider = _ScriptedProvider([_FinalMsg([_TextBlock("Hi.")], "end_turn")])
    exec_ = Executive()

    async def go() -> None:
        with patch("openexecutive.orchestrator.executive.get_provider", return_value=provider):
            async for _ in exec_._stream_agent_loop(
                system_blocks=[], messages=[{"role": "user", "content": "hi"}], model="claude-test",
            ):
                pass

    asyncio.run(go())
    assert "run_script" not in [t.get("name") for t in provider.calls[0]["tools"]]


def test_off_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHAT_SCRIPTS", "false")
    gateway = _Gateway()
    provider = _ScriptedProvider([_FinalMsg([_TextBlock("Hi.")], "end_turn")])
    _run(provider, gateway)
    assert "run_script" not in [t.get("name") for t in provider.calls[0]["tools"]]


def test_a_private_turn_is_not_offered_it_and_is_refused(
    monkeypatch: pytest.MonkeyPatch, audit: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr("openexecutive.orchestrator.executive.turn_is_private_to_principal", lambda: True)
    gateway = _Gateway()
    provider = _script_turn()
    _run(provider, gateway)
    assert "run_script" not in [t.get("name") for t in provider.calls[0]["tools"]]
    assert "not available on this turn" in json.loads(_any_result(provider, "tu-s"))["error"]
    assert gateway.calls == []
    assert any(r["details"].get("refused") == "private_turn" for r in audit)


def test_a_turn_that_read_the_owners_mail_refuses_the_whole_script(
    monkeypatch: pytest.MonkeyPatch, audit: list[dict[str, Any]]
) -> None:
    from types import SimpleNamespace

    pinned = SimpleNamespace(offered=False, touched_mail=True, read_mail=True)
    monkeypatch.setattr("openexecutive.orchestrator.executive.turn_delegation", lambda _s: pinned)
    gateway = _Gateway()
    provider = _script_turn()
    _run(provider, gateway)
    assert "read the user's own mail" in json.loads(_any_result(provider, "tu-s"))["error"]
    assert gateway.calls == []
    assert any(r["details"].get("refused") == "mail_touched" for r in audit)


def test_the_script_itself_is_audited_with_its_source(audit: list[dict[str, Any]]) -> None:
    _run(_script_turn(), _Gateway())
    rows = [r for r in audit if r["details"].get("kind") == "script"]
    assert len(rows) == 1 and rows[0]["details"]["ok"] is True
    assert rows[0]["full"]["input"] == {"script": SCRIPT}


class _BrokenGateway(_Gateway):
    async def call_tool(self, tool_input: dict[str, Any]) -> str:
        raise ConnectionError("stdio pipe closed")


def test_a_raising_call_is_audited_as_failed(audit: list[dict[str, Any]]) -> None:
    provider = _script_turn("drive__list_items(folder='x')")
    _run(provider, _BrokenGateway())
    failed = [r for r in audit if r["details"].get("via") == "run_script" and r["details"].get("ok") is False]
    assert len(failed) == 1 and "FAILED: ConnectionError" in failed[0]["summary"]
    body = json.loads(_any_result(provider, "tu-s"))
    assert body["error"] == "the script failed"
    script_row = [r for r in audit if r["details"].get("kind") == "script"][0]
    assert script_row["details"]["ok"] is False


@pytest.fixture()
def saved_db(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.workflows import saved_tools

    monkeypatch.setattr(saved_tools, "DB_PATH", tmp_path / "saved.db")


def _any_result(provider: _ScriptedProvider, use_id: str) -> str:
    for message in provider.calls[-1]["messages"]:
        for block in message["content"] if isinstance(message["content"], list) else []:
            if isinstance(block, dict) and block.get("tool_use_id") == use_id:
                return str(block["content"])
    raise KeyError(use_id)


def test_chat_saves_a_script_and_lists_then_runs_it(
    saved_db: None, audit: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The principal speaking on a verified surface: the one place a save lands.
    monkeypatch.setattr("openexecutive.orchestrator.executive.principal_only_withheld", lambda _s: frozenset())
    gateway = _Gateway()
    provider = _ScriptedProvider([
        _FinalMsg([_ToolUseBlock("tu-s", "run_script", {
            "script": SCRIPT, "save_as": "file_scans", "description": "File the inbox scans.",
        })], "tool_use"),
        _FinalMsg([_ToolUseBlock("tu-l", "list_saved_tools", {})], "tool_use"),
        _FinalMsg([_ToolUseBlock("tu-r", "run_script", {"tool": "file_scans"})], "tool_use"),
        _FinalMsg([_TextBlock("Done.")], "end_turn"),
    ])
    _run(provider, gateway)
    saved = json.loads(_any_result(provider, "tu-s"))["saved"]
    assert saved == {"name": "file_scans", "version": 1,
                     "uses_tools": ["drive__list_items", "drive__move_file"], "enabled": True,
                     "workflows": "off until the owner turns it on in Settings → Advanced → Custom tools"}
    listed = json.loads(_any_result(provider, "tu-l"))["saved_tools"]
    assert [t["name"] for t in listed] == ["file_scans"]
    again = json.loads(_any_result(provider, "tu-r"))
    assert again["result"] == 3 and again["saved_tool"] == {"name": "file_scans", "version": 1}
    # Both runs went through the gateway, one call at a time.
    assert len(gateway.calls) == 8
    script_rows = [r for r in audit if r["details"].get("kind") == "script"]
    assert script_rows[1]["full"]["input"] == {"tool": "file_scans"}


def test_list_saved_tools_is_offered_beside_run_script(monkeypatch: pytest.MonkeyPatch) -> None:
    # Offered only while the principal is speaking (PRINCIPAL_ONLY_TOOLS).
    monkeypatch.setattr("openexecutive.orchestrator.executive.principal_only_withheld", lambda _s: frozenset())
    provider = _ScriptedProvider([_FinalMsg([_TextBlock("Hi.")], "end_turn")])
    _run(provider, _Gateway())
    [listed] = [t for t in provider.calls[0]["tools"] if t.get("name") == "list_saved_tools"]
    assert {k: v for k, v in listed.items() if k != "cache_control"} == step_script.LIST_SAVED_TOOLS_DEFINITION


def test_a_turn_that_is_not_the_principals_cannot_save(
    saved_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "openexecutive.orchestrator.executive.principal_only_withheld",
        lambda _s: frozenset({"load_mcp_server"}),
    )
    gateway = _Gateway()
    provider = _ScriptedProvider([
        _FinalMsg([_ToolUseBlock("tu-s", "run_script", {
            "script": SCRIPT, "save_as": "file_scans", "description": "File the inbox scans.",
        })], "tool_use"),
        _FinalMsg([_TextBlock("Done.")], "end_turn"),
    ])
    _run(provider, gateway)
    body = json.loads(_any_result(provider, "tu-s"))
    assert body["result"] == 3 and "only the principal" in body["save_error"]
    from openexecutive.workflows import saved_tools

    assert saved_tools.get("file_scans") is None


def test_someone_elses_turn_is_not_offered_or_run_saved_tools(
    saved_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.workflows import saved_tools

    saved_tools.save("file_scans", "File the scans.", SCRIPT, ["drive__list_items"], origin="chat")
    monkeypatch.setattr(
        "openexecutive.orchestrator.executive.principal_only_withheld",
        lambda _s: frozenset({"list_saved_tools", "load_mcp_server"}),
    )
    gateway = _Gateway()
    provider = _ScriptedProvider([
        _FinalMsg([
            _ToolUseBlock("tu-l", "list_saved_tools", {}),
            _ToolUseBlock("tu-r", "run_script", {"tool": "file_scans"}),
        ], "tool_use"),
        _FinalMsg([_TextBlock("Done.")], "end_turn"),
    ])
    _run(provider, gateway)
    assert "list_saved_tools" not in [t.get("name") for t in provider.calls[0]["tools"]]
    assert "only the principal" in json.loads(_any_result(provider, "tu-l"))["error"]
    assert "principal's own turns" in json.loads(_any_result(provider, "tu-r"))["error"]
    assert gateway.calls == []


def test_a_script_can_use_the_executives_own_tools(
    monkeypatch: pytest.MonkeyPatch, audit: list[dict[str, Any]]
) -> None:
    """create_alert from a script runs the turn's own handler, with the same
    chip and audit row as a direct call — no gateway involved."""
    from openexecutive.orchestrator import executive as executive_module

    made: list[dict[str, Any]] = []

    async def create_alert(tool_input: dict[str, Any]) -> str:
        made.append(tool_input)
        return json.dumps({"ok": True, "alert_id": len(made)})

    monkeypatch.setitem(executive_module._ALL_SKILL_HANDLERS, "create_alert", create_alert)
    gateway = _Gateway()
    provider = _script_turn(
        "for t in ['Renew lease', 'Pay invoice', 'Call bank']:\n"
        "    create_alert(title=t, severity='low')\n"
        "len(inputs)"
    )
    _run(provider, gateway)
    assert [m["title"] for m in made] == ["Renew lease", "Pay invoice", "Call bank"]
    assert gateway.calls == []
    rows = [r for r in audit if r["details"].get("via") == "run_script"]
    assert [(r["details"]["tool"], r["details"]["kind"]) for r in rows] == [("create_alert", "skill")] * 3
    assert json.loads(_any_result(provider, "tu-s"))["result"] == 0


def test_an_own_tool_the_turn_is_not_offered_is_not_a_function(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.orchestrator import executive as executive_module

    called: list[Any] = []

    async def create_alert(tool_input: dict[str, Any]) -> str:
        called.append(tool_input)
        return "{}"

    monkeypatch.setitem(executive_module._ALL_SKILL_HANDLERS, "create_alert", create_alert)
    monkeypatch.setattr(
        "openexecutive.orchestrator.executive.tools_withheld_in_mode", lambda _m: frozenset({"create_alert"})
    )
    provider = _script_turn("create_alert(title='x', severity='low')")
    _run(provider, _Gateway())
    body = json.loads(_any_result(provider, "tu-s"))
    assert body["error"] == "the script failed" and "NameError" in body["detail"]
    assert called == []


def test_every_own_tool_is_a_real_tool() -> None:
    from openexecutive.delegation.lockdown import MAIL_TOUCHED_WITHHELD_TOOLS
    from openexecutive.orchestrator.executive import _ALL_SKILL_HANDLERS

    assert set(step_script.CHAT_OWN_TOOLS) <= set(_ALL_SKILL_HANDLERS)
    # The whole script is withheld under the Act as me lockdown already, but
    # nothing mail-specific belongs on the list.
    assert "ghostwrite_email" not in step_script.CHAT_OWN_TOOLS
    assert "run_script" in MAIL_TOUCHED_WITHHELD_TOOLS


def test_own_tools_in_bulk_stop_at_their_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Messaging everyone on the roster is a broadcast, which scripts don't get."""
    from openexecutive.orchestrator import executive as executive_module

    sent: list[Any] = []

    async def message_person(tool_input: dict[str, Any]) -> str:
        sent.append(tool_input)
        return json.dumps({"ok": True})

    monkeypatch.setitem(executive_module._ALL_SKILL_HANDLERS, "message_person", message_person)
    provider = _script_turn(
        "n = 0\n"
        "for i in range(30):\n"
        "    try:\n"
        "        message_person(person_id=i, text='hi')\n"
        "        n += 1\n"
        "    except RuntimeError:\n"
        "        pass\n"
        "n"
    )
    _run(provider, _Gateway())
    cap = step_script.CHAT_OWN_TOOL_CAPS["message_person"]
    assert len(sent) == cap
    assert json.loads(_any_result(provider, "tu-s"))["result"] == cap


def test_a_turns_scripts_share_one_call_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHAT_SCRIPT_MAX_CALLS", "4")
    gateway = _Gateway()
    provider = _ScriptedProvider([
        _FinalMsg([_ToolUseBlock("tu-a", "run_script", {"script": "drive__list_items(folder='a')\n1"})], "tool_use"),
        _FinalMsg([_ToolUseBlock("tu-s", "run_script", {"script": SCRIPT})], "tool_use"),
        _FinalMsg([_TextBlock("Done.")], "end_turn"),
    ])
    _run(provider, gateway)
    # One call in the first script, three in the second, then the budget is spent.
    assert len(gateway.calls) == 4
    body = json.loads(_any_result(provider, "tu-s"))
    assert body["error"] == "the script failed" and "at most 4" in body["detail"]


def test_agent_activity_shows_the_built_tool_as_one_card() -> None:
    from openexecutive.orchestrator.debug_events import DebugCollector

    provider = _script_turn()
    exec_ = Executive(mcp_gateway=_Gateway())  # type: ignore[arg-type]
    collector = DebugCollector()

    async def go() -> list[Any]:
        items: list[Any] = []
        with patch("openexecutive.orchestrator.executive.get_provider", return_value=provider):
            async for item in exec_._stream_agent_loop(
                system_blocks=[], messages=[{"role": "user", "content": "file the scans"}],
                model="claude-test", debug_collector=collector,
            ):
                items.append(item)
        return items

    items = asyncio.run(go())
    [card] = [i for i in items if isinstance(i, dict) and i.get("kind") == "script_run"]
    assert card["data"]["ok"] is True and card["data"]["calls_made"] == 4
    assert {c["tool"] for c in card["data"]["calls"]} == {"drive__list_items", "drive__move_file"}


def _people_turn(people: int) -> tuple[_ScriptedProvider, Any]:
    from openexecutive.orchestrator import executive as executive_module

    async def list_people(_input: dict[str, Any]) -> str:
        return json.dumps({"people": [{"id": i, "name": f"P{i}"} for i in range(people)]})

    provider = _ScriptedProvider([
        _FinalMsg([_ToolUseBlock("tu-l", "list_people", {})], "tool_use"),
        _FinalMsg([_TextBlock("Done.")], "end_turn"),
    ])
    return provider, patch.dict(executive_module._ALL_SKILL_HANDLERS, {"list_people": list_people})


def _hinted(provider: _ScriptedProvider) -> bool:
    return any(
        isinstance(b, dict) and b.get("type") == "text" and b.get("text") == step_script.FANOUT_HINT
        for m in provider.calls[-1]["messages"] if isinstance(m["content"], list)
        for b in m["content"]
    )


def test_a_list_result_nudges_toward_one_built_tool() -> None:
    provider, handlers = _people_turn(6)
    with handlers:
        _run(provider, _Gateway())
    assert _hinted(provider)


def test_no_nudge_for_a_short_list_or_without_scripts(monkeypatch: pytest.MonkeyPatch) -> None:
    provider, handlers = _people_turn(3)
    with handlers:
        _run(provider, _Gateway())
    assert not _hinted(provider)
    monkeypatch.setenv("CHAT_SCRIPTS", "false")
    provider, handlers = _people_turn(8)
    with handlers:
        _run(provider, _Gateway())
    assert not _hinted(provider)


@pytest.mark.parametrize(
    ("text", "many"),
    [
        (json.dumps([1, 2, 3, 4, 5]), True),
        (json.dumps({"people": [{}] * 5}), True),
        (json.dumps({"result": {"files": list(range(7))}}), True),
        (json.dumps({"people": [{}] * 4}), False),
        (json.dumps({"error": "x", "items": list(range(9))}), False),
        ("plain text", False),
    ],
)
def test_lists_many(text: str, many: bool) -> None:
    assert step_script.lists_many(text) is many
