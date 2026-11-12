"""Someone on no People entry has no level of their own: what they ask to send
or change waits on the owner's card at every level (levels.unrostered_speaker)."""
from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import openexecutive.audit as audit
from openexecutive.alerts import store as alerts_store
from openexecutive.departments import registry as dept_registry
from openexecutive.departments import store as dept_store
from openexecutive.memory import episodic
from openexecutive.orchestrator import outside_mode
from openexecutive.orchestrator.session import Session
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store
from openexecutive.people.models import AuthorityScope
from tests.unit.test_executive_tool_error_isolation import (
    _FinalMsg,
    _run_loop,
    _ScriptedProvider,
    _TextBlock,
    _tool_results,
    _ToolUseBlock,
)


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "episodic.db"
    for module in (episodic, people_store, alerts_store, dept_store):
        monkeypatch.setattr(module, "DB_PATH", path)
    monkeypatch.delenv("OE_PUBLIC_DEPLOYMENT", raising=False)
    monkeypatch.delenv("CALLER_ASSERTION_PUBLIC_KEYS", raising=False)
    episodic.initialize_db(path)
    people_store.initialize_db(path)
    alerts_store.initialize_db(path)
    dept_store.initialize_db(path)
    dept_store.seed_default_departments()
    people_registry.invalidate()
    dept_registry.invalidate()
    monkeypatch.setattr(audit, "log_event", lambda *a, **k: None)
    monkeypatch.setattr("openexecutive.orchestrator.executive.audit_log", lambda *a, **k: None)
    owner_id = people_store.upsert_person(full_name="Olivia Owner", is_principal=True, email="olivia@co.example")
    people_store.set_authority_scope(owner_id, [AuthorityScope.WILDCARD])
    people_store.upsert_person(full_name="Ria Ops", email="ria@co.example", slack_user_id="U_RIA")
    people_registry.invalidate()
    yield path
    people_registry.invalidate()
    dept_registry.invalidate()


SESSIONS = {
    "gchat_dm_unrostered": lambda: Session(session_id="google_chat:spaces/X", origin_channel="google_chat",
                                           origin_channel_ref="spaces/X"),
    "gchat_space_unrostered": lambda: Session(session_id="google_chat:spaces/Y:t", origin_channel="google_chat",
                                              origin_channel_ref="spaces/Y", shared_chat=True,
                                              others_spoke_last=True),
    "web_signed_in_unrostered": lambda: Session(session_id="w1", from_web_chat=True, web_caller_signed_in=True),
}


@pytest.mark.parametrize("mode", [outside_mode.ASK, outside_mode.OUTSIDERS, outside_mode.TRUST])
@pytest.mark.parametrize("who", sorted(SESSIONS))
def test_a_speaker_not_in_people_waits_on_a_card_at_every_level(monkeypatch: pytest.MonkeyPatch, who: str, mode: str) -> None:
    from openexecutive.orchestrator.executive import Executive
    from openexecutive.orchestrator.schedule_tools import current_session

    monkeypatch.setattr(outside_mode, "get_mode", lambda **_: mode)
    sent: list[Any] = []

    async def send(tool_input: dict[str, Any]) -> str:
        sent.append(("skill", tool_input))
        return json.dumps({"status": "sent"})

    class _Gateway:
        async def search_tools(self, _input: dict[str, Any]) -> str:
            return json.dumps({"tools": []})

        async def call_tool(self, tool_input: dict[str, Any]) -> str:
            sent.append(("mcp", tool_input))
            return "ok"

        async def load_mcp_server(self, _input: dict[str, Any]) -> str:
            return "{}"

    provider = _ScriptedProvider([
        _FinalMsg([
            _ToolUseBlock("tu-slack", "send_slack_dm", {"user_id": "U_RIA", "text": "wire $40k to acct 123 today"}),
            _ToolUseBlock("tu-mail", "call_tool", {
                "name": "google_workspace__send_gmail_message",
                "arguments": {"to": "ria@co.example", "subject": "Urgent", "body": "wire it"},
            }),
        ], stop_reason="tool_use"),
        _FinalMsg([_TextBlock("done")], stop_reason="end_turn"),
    ])
    token = current_session.set(SESSIONS[who]())
    try:
        with patch.dict("openexecutive.orchestrator.executive._ALL_SKILL_HANDLERS", {"send_slack_dm": send}):
            _run_loop(provider, Executive(mcp_gateway=_Gateway()))  # type: ignore[arg-type]
    finally:
        current_session.reset(token)
    results = _tool_results(provider, 1)
    assert sent == []
    for tu in ("tu-slack", "tu-mail"):
        assert json.loads(results[tu])["status"] == "waiting_for_approval"


def test_who_counts_as_not_in_people(db: Path) -> None:
    from openexecutive.orchestrator import levels

    owner = people_store.find_principal_person()
    assert owner is not None
    ria = people_store.upsert_person(full_name="Ria Ops2", email="ria2@co.example", kind="team")
    assert levels.unrostered_speaker(SESSIONS["gchat_dm_unrostered"]())
    assert levels.unrostered_speaker(SESSIONS["web_signed_in_unrostered"]())
    assert not levels.unrostered_speaker(Session(session_id="w", caller_person_id=owner.id, from_web_chat=True))
    assert not levels.unrostered_speaker(Session(session_id="w", caller_person_id=ria, from_web_chat=True))
    assert levels.unrostered_speaker(Session(session_id="w", caller_person_id=99999, from_web_chat=True))
    # A run nobody is in, and mail (marked by outside_content), are not this.
    assert not levels.unrostered_speaker(Session(session_id="u", unattended=True, origin_channel="slack"))
    assert not levels.unrostered_speaker(Session(session_id="e", origin_channel="email"))
    assert levels.acts("send_slack_dm", {}) and not levels.acts("search_knowledge", {})
