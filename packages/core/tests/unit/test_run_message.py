"""A scheduled workflow run tells its reader what came of it in a few plain
lines, or nothing; its full report stays on the run's page."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from openexecutive.workflows import run_message
from openexecutive.workflows.action_step import plain_tool_name

REPORT = (
    "# Daily Meeting Prep\n\n## Find upcoming meetings\n\nI searched the primary "
    "calendar from 2026-10-23 00:00 UTC and it returned no events.\n\n"
    "**Actions taken**\n\n- Google Workspace: get events — ok"
)


class _Provider:
    def __init__(self, text: str, stop: str = "end_turn", fail: bool = False) -> None:
        self.text, self.stop, self.fail = text, stop, fail
        self.calls: list[dict[str, Any]] = []

    async def messages_create(self, **kw: Any) -> object:
        self.calls.append(kw)
        if self.fail:
            raise RuntimeError("provider down")
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.text)], stop_reason=self.stop)


def _write(monkeypatch: pytest.MonkeyPatch, provider: _Provider) -> str:
    monkeypatch.setattr("openexecutive.providers.get_provider", lambda _m: provider)
    monkeypatch.setattr("openexecutive.agents.utility_fast.get_fast_model", lambda: "claude-test")
    monkeypatch.setattr("openexecutive.audit.usage.log_model_usage", lambda *a, **k: None)
    monkeypatch.setattr("openexecutive.briefing.reader.person_reader", lambda pid: None)
    return asyncio.run(run_message.message_for_run(
        title="Daily Meeting Prep", description="Emails a prep for tomorrow's meetings.",
        artifact=REPORT, person_id=3,
    ))


def test_a_run_with_nothing_for_the_reader_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _Provider("NOTHING")
    assert _write(monkeypatch, provider) == ""
    prompt = provider.calls[0]
    assert REPORT in prompt["messages"][0]["content"]
    assert "Now:" in prompt["messages"][0]["content"]
    assert prompt["system"] == run_message.RUN_MESSAGE_SYSTEM


def test_the_message_is_what_the_model_wrote_with_tool_names_in_plain_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = _write(monkeypatch, _Provider("Sent your prep for the 2 PM call (via `google_workspace__send_gmail_message`)."))
    assert text == "Sent your prep for the 2 PM call (via Google Workspace: send gmail message)."


@pytest.mark.parametrize("provider", [_Provider("", fail=True), _Provider("Sent your pr", stop="max_tokens"), _Provider("")])
def test_a_message_that_could_not_be_written_points_at_the_report(
    monkeypatch: pytest.MonkeyPatch, provider: _Provider,
) -> None:
    assert _write(monkeypatch, provider) == run_message.fallback_message("Daily Meeting Prep")


def test_an_empty_report_sends_nothing() -> None:
    assert asyncio.run(run_message.message_for_run(
        title="t", description="", artifact="  ", person_id=3,
    )) == ""


@pytest.mark.parametrize(("name", "plain"), [
    ("google_workspace__get_events", "Google Workspace: get events"),
    ("hubspot-crm__create_deal", "Hubspot CRM: create deal"),
    ("run_script", "Script"),
    ("oe__send_telegram_message", "Send telegram message"),
])
def test_tool_names_read_in_plain_words(name: str, plain: str) -> None:
    assert plain_tool_name(name) == plain


@pytest.mark.parametrize(("message", "expected"), [
    ("Sent your prep for tomorrow's 2 PM call.", ["Sent your prep for tomorrow's 2 PM call."]),
    ("", []),
])
def test_a_scheduled_run_sends_its_message_not_its_report(
    monkeypatch: pytest.MonkeyPatch, message: str, expected: list[str],
) -> None:
    from openexecutive.scheduler import runner
    from openexecutive.workflows import dynamic_store, persistence
    from openexecutive.workflows.base import WorkflowEvent

    class _Run:
        title = "Daily Meeting Prep"

        def input_model(self) -> Any:
            return lambda: SimpleNamespace(model_dump=dict)

        async def run(self, inputs: Any, store: Any) -> Any:
            yield WorkflowEvent(type="artifact", content=REPORT)

    defn = SimpleNamespace(
        name="meeting_prep", title="Daily Meeting Prep", description="Preps meetings.",
        is_active=True, steps=[],
    )
    monkeypatch.setattr(dynamic_store, "get_definition", lambda name: defn)
    monkeypatch.setattr("openexecutive.workflows.get_workflow", lambda name: _Run())
    monkeypatch.setattr("openexecutive.delegation.workflow_reads.owns_runs", lambda name: None)
    monkeypatch.setattr(persistence, "create_run", lambda *a, **k: None)
    stored: list[str] = []
    monkeypatch.setattr(persistence, "complete_run", lambda rid, art: stored.append(art))
    monkeypatch.setattr("openexecutive.knowledge.store.ChromaDBStore", lambda **k: None)
    monkeypatch.setattr(runner, "mark_action_done", lambda aid: None)
    monkeypatch.setattr(
        "openexecutive.workflows.dynamic_cadence.schedule_dynamic_workflow_cadence", lambda d, after=None: None
    )
    written: list[dict[str, Any]] = []

    async def _message(**kw: Any) -> str:
        written.append(kw)
        return message

    monkeypatch.setattr(run_message, "message_for_run", _message)
    sent: list[str] = []

    async def _dm(payload: dict[str, Any]) -> str:
        sent.append(payload["text"])
        return "{}"

    monkeypatch.setattr("openexecutive.orchestrator.schedule_tools.handle_message_person", _dm)
    action = SimpleNamespace(id=3, channel_ref="meeting_prep", assigned_to_person_id=3, scope_key=None)

    asyncio.run(runner._run_dynamic_workflow(action, now=__import__("datetime").datetime.now()))  # type: ignore[arg-type]

    assert stored == [REPORT]  # the full report stays on the run
    assert written[0]["artifact"] == REPORT and written[0]["description"] == "Preps meetings."
    assert sent == expected
