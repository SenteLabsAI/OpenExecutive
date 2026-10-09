"""The less common tools sit behind open_tools / use_tool (orchestrator.tool_groups).

The tool list is the start of the cached prefix, so it must be the same on
every call of every turn: a group the model opens comes back as a tool
result, never as a change to the list. A use_tool call is rewritten into the
tool's own call before dispatch, so it runs the same handler, guards and
audit rows as a direct call would.

Also pinned here: the specialist lookups take their result counts from
settings, and a zero synced count skips the synced-source queries.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openexecutive.orchestrator import tool_groups
from tests.unit.offered_tools import capture_offered


@pytest.fixture(autouse=True)
def _no_audit_writes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        "openexecutive.orchestrator.executive.audit_log",
        lambda *a, **_k: rows.append(a),
    )
    return rows


# --------------------------------------------------------------------- #
# The groups
# --------------------------------------------------------------------- #


def test_every_deferred_name_is_a_real_tool_with_a_handler() -> None:
    from openexecutive.orchestrator.executive import _ALL_SKILL_HANDLERS, _ALL_SKILL_TOOLS

    names = {t["name"] for t in _ALL_SKILL_TOOLS}
    assert set(tool_groups.DEFERRED) <= names
    assert set(tool_groups.DEFERRED) <= set(_ALL_SKILL_HANDLERS)


def test_each_tool_sits_in_one_group() -> None:
    listed = [t for _, tools in tool_groups.GROUPS.values() for t in tools]
    assert len(listed) == len(set(listed))


@pytest.mark.parametrize("name", ["consult_specialist", "propose_form_values", "message_person"])
def test_the_everyday_tools_stay_direct(name: str) -> None:
    assert name not in tool_groups.DEFERRED


def test_open_tools_lists_every_group() -> None:
    desc = tool_groups.open_tools_tool(tool_groups.DEFERRED)["description"]
    for group, (_, tools) in tool_groups.GROUPS.items():
        assert group in desc
        assert all(t in desc for t in tools)


def test_open_tools_names_only_the_offered_tools() -> None:
    tool = tool_groups.open_tools_tool(["create_goal", "send_slack_dm"])
    assert "create_goal" in tool["description"] and "send_slack_dm" in tool["description"]
    assert "send_company_broadcast" not in tool["description"]
    assert "calendar" not in tool["description"]
    assert tool["input_schema"]["properties"]["group"]["enum"] == ["goals", "messaging"]
    # The same offer gives the same definition (one cached list per kind of turn).
    assert tool == tool_groups.open_tools_tool(["send_slack_dm", "create_goal"])


def test_split_keeps_the_direct_list_the_same_whatever_is_opened() -> None:
    tools = [{"name": n, "input_schema": {}} for n in ("consult_specialist", "create_goal", "list_watchlist")]
    direct, deferred = tool_groups.split(tools)
    assert [t["name"] for t in direct] == ["consult_specialist", "open_tools", "use_tool"]
    assert set(deferred) == {"create_goal", "list_watchlist"}
    # Nothing deferred on the turn: no open_tools / use_tool either.
    direct, deferred = tool_groups.split(tools[:1])
    assert [t["name"] for t in direct] == ["consult_specialist"] and deferred == {}


def test_open_result_gives_only_the_offered_tools() -> None:
    deferred = {"create_goal": {"name": "create_goal", "description": "d", "input_schema": {"type": "object"}}}
    out = json.loads(tool_groups.open_result({"group": "goals"}, deferred))
    assert out["call_with"] == "use_tool"
    assert [t["name"] for t in out["tools"]] == ["create_goal"]
    assert "error" in json.loads(tool_groups.open_result({"group": "calendar"}, deferred))
    assert "error" in json.loads(tool_groups.open_result({"group": "nope"}, deferred))
    assert "error" in json.loads(tool_groups.open_result("goals", deferred))


def test_unwrap() -> None:
    call, err = tool_groups.unwrap(
        {"id": "tu-1", "name": "use_tool", "input": {"name": "create_goal", "input": {"title": "t"}}}
    )
    assert err is None and call == {"id": "tu-1", "name": "create_goal", "input": {"title": "t"}}
    # JSON text for the input is accepted.
    call, _ = tool_groups.unwrap({"id": "tu-2", "name": "use_tool",
                                  "input": {"name": "create_goal", "input": '{"title": "t"}'}})
    assert call is not None and call["input"] == {"title": "t"}
    # A direct tool, an unknown name, or a missing input object is refused.
    for bad in (
        {"name": "consult_specialist", "input": {}},
        {"name": "use_tool", "input": {}},
        {"name": "create_goal", "input": "not json"},
        {"name": "create_goal"},
        "create_goal",
    ):
        call, err = tool_groups.unwrap({"id": "tu-3", "name": "use_tool", "input": bad})
        assert call is None and err and "error" in json.loads(err)


# --------------------------------------------------------------------- #
# In the loop
# --------------------------------------------------------------------- #


class _Provider:
    def __init__(self, finals: list[Any]) -> None:
        self._finals = list(finals)
        self.calls: list[dict[str, Any]] = []

    def messages_stream(self, **kwargs: Any) -> Any:
        import copy

        self.calls.append(copy.deepcopy(kwargs))
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


def _use(id_: str, name: str, input_: Any) -> Any:
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=input_)


def _run(rounds: list[list[Any]]) -> tuple[_Provider, list[Any], list[list[str]]]:
    from openexecutive.orchestrator.executive import Executive
    from openexecutive.orchestrator.schedule_tools import set_session
    from openexecutive.orchestrator.session import Session

    finals = [SimpleNamespace(content=r, stop_reason="tool_use", usage=None) for r in rounds]
    finals.append(SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")],
                                  stop_reason="end_turn", usage=None))
    provider = _Provider(finals)
    items: list[Any] = []

    async def _go() -> list[list[str]]:
        with (
            capture_offered() as offered,
            patch("openexecutive.orchestrator.executive.get_provider", return_value=provider),
            set_session(Session(session_id="web:groups")),
        ):
            async for item in Executive()._stream_agent_loop(
                system_blocks=[], messages=[{"role": "user", "content": "x"}],
                model="claude-test", workspace_mode="team", principal_role_tag="",
                turn_id="t-groups",
            ):
                items.append(item)
        return offered

    offered = asyncio.run(_go())
    return provider, items, offered


def _results(provider: _Provider, call: int) -> dict[str, str]:
    return {b["tool_use_id"]: b["content"] for b in provider.calls[call]["messages"][-1]["content"]}


def test_the_request_carries_open_tools_and_no_deferred_tool() -> None:
    provider, _, offered = _run([])
    names = [t["name"] for t in provider.calls[0]["tools"] if "input_schema" in t]
    assert {"open_tools", "use_tool"} <= set(names)
    assert not set(names) & set(tool_groups.DEFERRED)
    assert names == sorted(names)
    # ...while the turn still offers them behind open_tools.
    assert {"create_goal", "list_watchlist"} <= set(offered[0])


def test_opening_a_group_returns_its_tools_and_keeps_the_list_unchanged() -> None:
    provider, _, _ = _run([[_use("tu-1", "open_tools", {"group": "goals"})]])
    out = json.loads(_results(provider, 1)["tu-1"])
    assert out["group"] == "goals"
    names = {t["name"] for t in out["tools"]}
    assert names == set(tool_groups.GROUPS["goals"][1])
    assert all(t["input_schema"] for t in out["tools"])
    # The cached prefix is byte-for-byte the same on the next call.
    assert provider.calls[1]["tools"] == provider.calls[0]["tools"]


def test_use_tool_runs_the_tools_own_handler(_no_audit_writes: list[tuple[Any, ...]]) -> None:
    from openexecutive.orchestrator import executive as executive_module

    handler = AsyncMock(return_value=json.dumps({"status": "ok"}))
    with patch.dict(executive_module._ALL_SKILL_HANDLERS, {"list_watchlist": handler}):
        provider, items, _ = _run(
            [[_use("tu-1", "use_tool", {"name": "list_watchlist", "input": {"limit": 5}})]]
        )
    handler.assert_awaited_once_with({"limit": 5})
    assert json.loads(_results(provider, 1)["tu-1"]) == {"status": "ok"}
    # The assistant turn keeps the model's own use_tool block, so the result
    # pairs with the id it sent.
    assistant = provider.calls[1]["messages"][-2]["content"]
    assert any(b.get("name") == "use_tool" and b.get("id") == "tu-1" for b in assistant)
    # The audit row and progress event name the tool itself.
    assert any("list_watchlist" in str(row[1]) for row in _no_audit_writes if len(row) > 1)
    assert "use_tool" not in json.dumps([i for i in items if isinstance(i, dict)], default=str)


def test_use_tool_on_a_direct_tool_or_without_input_is_an_error() -> None:
    provider, _, _ = _run([[
        _use("tu-1", "use_tool", {"name": "consult_specialist", "input": {}}),
        _use("tu-2", "use_tool", {"name": "create_goal"}),
    ]])
    results = _results(provider, 1)
    assert "error" in json.loads(results["tu-1"])
    assert "error" in json.loads(results["tu-2"])


# --------------------------------------------------------------------- #
# Specialist lookups
# --------------------------------------------------------------------- #


def test_specialist_retrieval_uses_the_settings_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.orchestrator import router

    monkeypatch.setattr(
        "openexecutive.config.get_settings",
        lambda: SimpleNamespace(specialist_builtin_n_results=4, specialist_company_n_results=1),
    )
    seen: dict[str, Any] = {}

    def fake_retrieve(**kw: Any) -> str:
        seen.update(kw)
        return "ctx"

    monkeypatch.setattr("openexecutive.knowledge.retriever.retrieve", fake_retrieve)
    out = asyncio.run(router._retrieve_for_call({"query": "q", "specialist": "cfo"}))
    assert out == "ctx"
    assert (seen["n_builtin"], seen["n_company"], seen["n_synced"]) == (4, 1, 1)


def test_the_specialist_defaults_are_small() -> None:
    from openexecutive.config import Settings

    fields = Settings.model_fields
    assert fields["specialist_builtin_n_results"].default == 3
    assert fields["specialist_company_n_results"].default == 2


def test_no_synced_count_skips_the_synced_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    import openexecutive.knowledge.retriever as retriever_mod
    from openexecutive.knowledge.store import ChromaDBStore

    monkeypatch.setattr(retriever_mod, "_audit_log", lambda *a, **k: None)
    review_store = SimpleNamespace(
        get_withheld_keys=lambda _ct: set(),
        get_withheld_source_ids=lambda: set(),
        get_priority_map=lambda _ct: {},
        list_annotations=lambda domains=None, active_only=True: [],
    )
    store = MagicMock()
    store.query.return_value = []

    def collections() -> set[str]:
        return {c.kwargs["collection"] for c in store.query.call_args_list}

    synced = {
        ChromaDBStore.NOTION_COLLECTION,
        ChromaDBStore.DRIVE_COLLECTION,
        ChromaDBStore.ONEDRIVE_COLLECTION,
        ChromaDBStore.CONFLUENCE_COLLECTION,
    }
    retriever_mod.retrieve(query="How should we price the renewal?", store=store,
                           review_store=review_store, n_builtin=1, n_company=1, n_synced=0)
    assert not collections() & synced
    store.query.reset_mock()
    retriever_mod.retrieve(query="How should we price the renewal?", store=store,
                           review_store=review_store, n_builtin=1, n_company=1, n_synced=2)
    assert collections() >= synced
    assert all(c.kwargs["n_results"] == 2 for c in store.query.call_args_list
               if c.kwargs["collection"] in synced)
