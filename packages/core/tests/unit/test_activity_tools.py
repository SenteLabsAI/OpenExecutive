"""list_my_activity: the Executive reads its own record of what it did on its
own (11-13: asked what it did overnight, it had no way to look)."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from openexecutive.api.routes import today
from openexecutive.orchestrator import activity_tools, tool_groups


def _item(**kw: Any) -> today.ActivityItem:
    base = {"kind": "followup_fired", "summary": "Sent the follow-up", "actor": "executive",
            "target": None, "department": None, "at": datetime.now(UTC).isoformat()}
    return today.ActivityItem(**{**base, **kw})


@pytest.fixture(autouse=True)
def _on_the_team() -> Any:
    from openexecutive.orchestrator.schedule_tools import current_session
    from openexecutive.orchestrator.session import Session

    token = current_session.set(Session(caller_person_id=1))
    yield
    current_session.reset(token)


def test_it_reads_the_shared_feed_for_the_window_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake(limit: int, since: datetime | None = None, **_: Any) -> today.ActivityResponse:
        seen.update(limit=limit, since=since)
        return today.ActivityResponse(items=[
            _item(summary="Checked in on Marketing", department="marketing", detail="3 goals graded",
                  mail_subject="never shown"),
            _item(summary="Nudged Sam", target="Sam Okafor"),
        ])

    monkeypatch.setattr(today, "_build_activity", fake)
    out = json.loads(asyncio.run(activity_tools.handle_list_my_activity({"hours": 12, "limit": 5})))
    assert seen["limit"] == 5
    assert abs((datetime.now(UTC) - timedelta(hours=12) - seen["since"]).total_seconds()) < 5
    assert out["hours"] == 12 and out["count"] == 2
    first, second = out["items"]
    assert first["what"] == "Checked in on Marketing" and first["department"] == "marketing"
    assert first["detail"] == "3 goals graded" and "never shown" not in json.dumps(out)
    assert second["for"] == "Sam Okafor"


def test_it_bounds_what_it_is_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake(limit: int, since: datetime | None = None, **_: Any) -> today.ActivityResponse:
        seen.update(limit=limit)
        return today.ActivityResponse(items=[])

    monkeypatch.setattr(today, "_build_activity", fake)
    out = json.loads(asyncio.run(activity_tools.handle_list_my_activity({"hours": 99999, "limit": "lots"})))
    assert out["hours"] == 720 and seen["limit"] == 30


def test_a_failed_read_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_: Any, **__: Any) -> today.ActivityResponse:
        raise RuntimeError("db locked")

    monkeypatch.setattr(today, "_build_activity", broken)
    out = json.loads(asyncio.run(activity_tools.handle_list_my_activity({})))
    assert "couldn't be read" in out["error"]


def test_it_is_offered_in_a_tool_group() -> None:
    from openexecutive.orchestrator import executive

    assert tool_groups.DEFERRED["list_my_activity"] == "watch_and_alerts"
    assert "list_my_activity" in executive._ALL_SKILL_HANDLERS


def test_only_the_team_and_its_own_runs_read_it(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.orchestrator import outside_content
    from openexecutive.orchestrator.schedule_tools import current_session
    from openexecutive.orchestrator.session import Session

    monkeypatch.setattr(today, "_build_activity", lambda *a, **k: today.ActivityResponse(items=[_item()]))

    def ask() -> dict[str, Any]:
        return json.loads(asyncio.run(activity_tools.handle_list_my_activity({})))

    for session, allowed in (
        (None, False),  # nothing says who is asking
        (Session(caller_person_id=None), False),  # an MCP caller off the roster
        (Session(caller_person_id=7), True),  # someone on the team
        (Session(unattended=True), True),  # its own scheduled work
    ):
        token = current_session.set(session)
        try:
            assert ("error" not in ask()) is allowed, session
        finally:
            current_session.reset(token)

    monkeypatch.setattr(outside_content, "answers_off_team_mail", lambda: True)
    assert "not on it" in ask()["error"]
    assert "list_my_activity" in outside_content.SENDER_WITHHELD_TOOLS
