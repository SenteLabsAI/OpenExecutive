"""Tools and document collections an installed extension adds
(orchestrator/extensions.py).

An extension tool is offered only on a turn someone is in (never an
unattended run or a turn private to the principal), only where it says so,
and runs through the same dispatch as any tool. A collection's documents get
their own Documents filter, and the extension hears about archive, restore
and delete before they happen.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from openexecutive.orchestrator import extensions, tool_groups
from openexecutive.orchestrator.extensions import Collection, ExtensionError, ExtraTool
from tests.unit.offered_tools import capture_offered


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr("openexecutive.orchestrator.executive.audit_log", lambda *a, **_k: None)
    extensions._reset_for_tests()
    # No OPENEXECUTIVE_EXTENSIONS here: count the registry as loaded.
    monkeypatch.setattr(extensions, "_loaded", True)
    yield
    extensions._reset_for_tests()


def _definition(name: str = "make_widget") -> dict[str, Any]:
    return {
        "name": name,
        "description": "Make a widget.",
        "input_schema": {"type": "object", "properties": {"title": {"type": "string"}}},
    }


def _tool(name: str = "make_widget", **kwargs: Any) -> ExtraTool:
    handler = kwargs.pop("handler", AsyncMock(return_value=json.dumps({"status": "made", "id": "w1"})))
    return ExtraTool(definition=_definition(name), handler=handler, **kwargs)


# --------------------------------------------------------------------- #
# Registering
# --------------------------------------------------------------------- #


@pytest.mark.parametrize("name", ["consult_specialist", "draft_artifact", "open_tools", "call_tool", "web_search"])
def test_a_built_in_name_is_refused(name: str) -> None:
    with pytest.raises(ExtensionError, match="taken"):
        extensions.register_tool(_tool(name))


def test_a_name_is_registered_once() -> None:
    extensions.register_tool(_tool())
    with pytest.raises(ExtensionError, match="taken"):
        extensions.register_tool(_tool())


@pytest.mark.parametrize(
    "definition",
    [
        {"name": "Bad-Name", "description": "d", "input_schema": {"type": "object"}},
        {"name": "no_description", "description": " ", "input_schema": {"type": "object"}},
        {"name": "no_schema", "description": "d"},
        {"name": "not_object", "description": "d", "input_schema": {"type": "string"}},
    ],
)
def test_a_bad_definition_is_refused(definition: dict[str, Any]) -> None:
    with pytest.raises(ExtensionError):
        extensions.register_tool(ExtraTool(definition=definition, handler=AsyncMock()))


def test_a_tool_can_open_a_new_group_and_leaves_it_on_reset() -> None:
    extensions.register_tool(_tool(group="widgets", group_purpose="make and list widgets"))
    assert tool_groups.DEFERRED["make_widget"] == "widgets"
    assert "widgets: make and list widgets (make_widget)" in tool_groups.open_tools_tool(
        tool_groups.DEFERRED
    )["description"]
    extensions._reset_for_tests()
    assert "widgets" not in tool_groups.GROUPS and "make_widget" not in tool_groups.DEFERRED


def test_a_tool_can_join_an_existing_group() -> None:
    before = tool_groups.GROUPS["documents"]
    extensions.register_tool(_tool(group="documents"))
    assert tool_groups.GROUPS["documents"] == (before[0], (*before[1], "make_widget"))
    extensions._reset_for_tests()
    assert tool_groups.GROUPS["documents"] == before


def test_a_new_group_needs_a_purpose() -> None:
    with pytest.raises(ValueError, match="purpose"):
        extensions.register_tool(_tool(group="widgets"))


def test_a_collection_needs_a_good_name_and_a_label() -> None:
    with pytest.raises(ExtensionError):
        extensions.register_collection(Collection(name="Widgets", label="Widgets"))
    with pytest.raises(ExtensionError):
        extensions.register_collection(Collection(name="widgets", label=" "))
    extensions.register_collection(Collection(name="widgets", label="Widgets"))
    with pytest.raises(ExtensionError, match="already"):
        extensions.register_collection(Collection(name="widgets", label="Widgets"))


# --------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------- #


def test_load_imports_each_named_module_and_skips_a_broken_one(monkeypatch: pytest.MonkeyPatch) -> None:
    good = types.ModuleType("oe_ext_good")
    good.register = lambda: extensions.register_tool(_tool())  # type: ignore[attr-defined]
    broken = types.ModuleType("oe_ext_broken")

    def _boom() -> None:
        raise RuntimeError("boom")

    broken.register = _boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "oe_ext_good", good)
    monkeypatch.setitem(sys.modules, "oe_ext_broken", broken)
    monkeypatch.setattr(
        "openexecutive.config.get_settings",
        lambda: SimpleNamespace(extensions="oe_ext_broken,oe_ext_missing,oe_ext_good"),
    )
    monkeypatch.setattr(extensions, "_loaded", False)
    extensions.load()
    extensions.load()  # once per process
    assert [t.name for t in extensions.offered_tools(None)] == ["make_widget"]


def test_the_setting_keeps_only_module_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.config import Settings

    monkeypatch.setenv("OPENEXECUTIVE_EXTENSIONS", "acme.tools, bad name,../x,  other ")
    assert Settings().extensions == "acme.tools,other"
    monkeypatch.setenv("OPENEXECUTIVE_EXTENSIONS", " ")
    assert Settings().extensions is None


# --------------------------------------------------------------------- #
# Offered in the loop
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


def _run(session: Any, rounds: list[list[Any]]) -> tuple[_Provider, list[Any], list[list[str]]]:
    from openexecutive.orchestrator.executive import Executive
    from openexecutive.orchestrator.schedule_tools import set_session

    finals = [SimpleNamespace(content=r, stop_reason="tool_use", usage=None) for r in rounds]
    finals.append(SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")],
                                  stop_reason="end_turn", usage=None))
    provider = _Provider(finals)
    items: list[Any] = []

    async def _go() -> list[list[str]]:
        with (
            capture_offered() as offered,
            patch("openexecutive.orchestrator.executive.get_provider", return_value=provider),
            set_session(session),
        ):
            async for item in Executive()._stream_agent_loop(
                system_blocks=[], messages=[{"role": "user", "content": "x"}],
                model="claude-test", workspace_mode="team", principal_role_tag="",
                turn_id="t-ext",
            ):
                items.append(item)
        return offered

    offered = asyncio.run(_go())
    return provider, items, offered


def _results(provider: _Provider, call: int) -> dict[str, str]:
    return {b["tool_use_id"]: b["content"] for b in provider.calls[call]["messages"][-1]["content"]}


def _session(**kwargs: Any) -> Any:
    from openexecutive.orchestrator.session import Session

    return Session(session_id="web:ext", **kwargs)


def test_a_turn_someone_is_in_is_offered_the_tool_and_runs_it() -> None:
    tool = _tool(chip=lambda _i, parsed: {"summary": "Made a widget", "link": f"/widgets/{parsed['id']}"})
    extensions.register_tool(tool)
    provider, items, offered = _run(_session(), [[_use("tu-1", "make_widget", {"title": "A"})]])
    assert "make_widget" in offered[0]
    assert "make_widget" in [t["name"] for t in provider.calls[0]["tools"]]
    tool.handler.assert_awaited_once_with({"title": "A"})  # type: ignore[attr-defined]
    assert json.loads(_results(provider, 1)["tu-1"])["status"] == "made"
    chips = [i for i in items if isinstance(i, dict) and i.get("type") == "action_taken"]
    assert [(c["tool"], c["summary"], c["link"]) for c in chips] == [
        ("make_widget", "Made a widget", "/widgets/w1")
    ]


def test_a_grouped_tool_runs_through_use_tool() -> None:
    tool = _tool(group="widgets", group_purpose="widgets")
    extensions.register_tool(tool)
    provider, _, offered = _run(
        _session(), [[_use("tu-1", "use_tool", {"name": "make_widget", "input": {"title": "B"}})]]
    )
    assert "make_widget" in offered[0]
    assert "make_widget" not in [t["name"] for t in provider.calls[0]["tools"]]
    tool.handler.assert_awaited_once_with({"title": "B"})  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "session_kwargs",
    [{"unattended": True}, {"private_to_principal": True}],
    ids=["unattended", "private to the principal"],
)
def test_never_offered_or_run_on_an_unattended_or_private_turn(session_kwargs: dict[str, Any]) -> None:
    tool = _tool()
    extensions.register_tool(tool)
    provider, _, offered = _run(_session(**session_kwargs), [[_use("tu-1", "make_widget", {})]])
    assert "make_widget" not in offered[0]
    tool.handler.assert_not_awaited()  # type: ignore[attr-defined]
    assert "Unknown tool" in _results(provider, 1)["tu-1"]


def test_the_tool_decides_which_turns_it_is_offered_on() -> None:
    tool = _tool(offered=lambda session: bool(getattr(session, "from_web_chat", False)))
    extensions.register_tool(tool)
    _, _, offered = _run(_session(from_web_chat=True), [])
    assert "make_widget" in offered[0]
    _, _, offered = _run(_session(), [])
    assert "make_widget" not in offered[0]


def test_a_failing_offered_counts_as_not_offered() -> None:
    def _boom(_s: Any) -> bool:
        raise RuntimeError("boom")

    extensions.register_tool(_tool(offered=_boom))
    assert extensions.offered_tools(None) == []


def test_the_mail_lockdown_refuses_it_like_any_unclassified_tool() -> None:
    from openexecutive.delegation.lockdown import mail_touched_withholds

    assert mail_touched_withholds("make_widget", {}) is True


# --------------------------------------------------------------------- #
# Chips
# --------------------------------------------------------------------- #


def _chip(result: dict[str, Any], chip: Any) -> dict[str, Any] | None:
    from openexecutive.orchestrator.action_chips import summarize_action

    extensions.register_tool(_tool(chip=chip))
    return summarize_action(tool_name="make_widget", tool_input={}, tool_result=json.dumps(result))


def test_no_chip_without_a_chip_function() -> None:
    assert _chip({"status": "made"}, None) is None


def test_no_chip_for_an_error_or_a_refusal() -> None:
    chip = lambda _i, _p: {"summary": "Made"}  # noqa: E731
    assert _chip({"error": "no"}, chip) is None
    extensions._reset_for_tests()
    assert _chip({"status": "refused"}, chip) is None


@pytest.mark.parametrize(
    "link, kept",
    [
        ("/artifacts/alert:1", "/artifacts/alert:1"),
        ("https://evil.example/x", None),
        ("//evil.example/x", None),
        ("/\\evil.example", None),
        ("javascript:alert(1)", None),
        ("/a b", None),
        (None, None),
    ],
)
def test_a_chip_link_stays_inside_the_app(link: Any, kept: str | None) -> None:
    out = _chip({"status": "made"}, lambda _i, _p: {"summary": "Made", "target": "W", "link": link})
    assert out is not None and out["link"] == kept and out["summary"] == "Made" and out["target"] == "W"


def test_a_chip_that_raises_or_has_no_summary_gives_none() -> None:
    def _boom(_i: Any, _p: Any) -> dict[str, Any]:
        raise RuntimeError("boom")

    assert _chip({"status": "made"}, _boom) is None
    extensions._reset_for_tests()
    assert _chip({"status": "made"}, lambda _i, _p: {"summary": ""}) is None


# --------------------------------------------------------------------- #
# Collections
# --------------------------------------------------------------------- #


def test_collection_for_tags_finds_only_registered_collections() -> None:
    extensions.register_collection(Collection(name="widgets", label="Widgets"))
    assert extensions.collection_tag("widgets") == "collection:widgets"
    found = extensions.collection_for_tags(["artifact", "collection:widgets"])
    assert found is not None and found.label == "Widgets"
    assert extensions.collection_for_tags(["artifact", "collection:other"]) is None
    assert extensions.collection_for_tags(None) is None


def test_notify_passes_the_change_and_its_error() -> None:
    seen: list[tuple[str, str]] = []

    async def on_change(artifact_id: str, change: str) -> None:
        seen.append((artifact_id, change))
        if change == "deleted":
            raise RuntimeError("can't")

    extensions.register_collection(Collection(name="widgets", label="Widgets", on_change=on_change))
    asyncio.run(extensions.notify("widgets", "alert:1", "archived"))
    asyncio.run(extensions.notify(None, "alert:2", "archived"))
    asyncio.run(extensions.notify("unknown", "alert:3", "archived"))
    with pytest.raises(RuntimeError):
        asyncio.run(extensions.notify("widgets", "alert:1", "deleted"))
    assert seen == [("alert:1", "archived"), ("alert:1", "deleted")]
