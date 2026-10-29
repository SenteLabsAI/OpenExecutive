"""alerts.dispatcher.dispatch_slack_dm: a high or urgent alert to its person's
own chat app (Slack, Discord or Telegram DM, in their reach order)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from openexecutive.alerts import dispatcher
from openexecutive.alerts.models import PRIVATE_ALERT_TAG, Alert
from openexecutive.people import channel as people_channel
from openexecutive.people import store as people_store
from openexecutive.scheduler import runner
from openexecutive.scheduler.runner import PrincipalDelivery

OWNER = SimpleNamespace(id=1, full_name="Olivia Owner", archived=False)
BEN = SimpleNamespace(id=2, full_name="Ben Teammate", archived=False)


def _alert(severity: str = "high", routed: int | None = None, tags: list[str] | None = None) -> Alert:
    return Alert(
        id=7, source="email", external_id="x-1", severity=severity, headline="Vendor outage",
        body="Acme is down.", suggested_action="Call Acme", created_at="2026-10-29T00:00:00+00:00",
        routed_to_person_id=routed, topic_tags=tags or [],
    )


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, str, bool]]:
    calls: list[tuple[Any, str, bool]] = []

    async def deliver(person: Any, text: str, *, label: str = "Update", chat_only: bool = False) -> PrincipalDelivery:
        calls.append((person, text, chat_only))
        return PrincipalDelivery(True, "sent", "sent", "telegram")

    monkeypatch.setattr(runner, "deliver_to_person", deliver)
    monkeypatch.setattr(people_store, "find_principal_person", lambda: OWNER)
    monkeypatch.setattr(people_store, "get_person", lambda pid: {1: OWNER, 2: BEN}.get(pid))
    monkeypatch.setattr(people_channel, "is_within_availability", lambda person, **kw: True)
    return calls


def test_a_high_alert_goes_to_the_principals_chat_app(sent: list[Any]) -> None:
    assert asyncio.run(dispatcher.dispatch_slack_dm(_alert())) is True
    assert sent == [(OWNER, "Vendor outage\n\nAcme is down.\n\nSuggested action: Call Acme", True)]


def test_a_routed_alert_goes_to_its_person_unless_private(sent: list[Any]) -> None:
    asyncio.run(dispatcher.dispatch_slack_dm(_alert(routed=2)))
    asyncio.run(dispatcher.dispatch_slack_dm(_alert(routed=2, tags=[PRIVATE_ALERT_TAG])))
    assert [p for p, _, _ in sent] == [BEN, OWNER]


def test_a_medium_alert_waits_on_home(sent: list[Any]) -> None:
    assert asyncio.run(dispatcher.dispatch_slack_dm(_alert("medium"))) is False
    assert sent == []


def test_nothing_sent_is_not_delivered(monkeypatch: pytest.MonkeyPatch, sent: list[Any]) -> None:
    async def none(person: Any, text: str, **kw: Any) -> PrincipalDelivery:
        return PrincipalDelivery(False, "no chat app reaches them", "no_channel")

    monkeypatch.setattr(runner, "deliver_to_person", none)
    assert asyncio.run(dispatcher.dispatch_slack_dm(_alert("urgent"))) is False


def test_quiet_hours_hold_a_high_alert_but_let_an_urgent_one_through(
    monkeypatch: pytest.MonkeyPatch, sent: list[Any],
) -> None:
    asked: list[bool] = []

    def quiet(person: Any, *, now: Any, quiet_hours: bool = False, urgent: bool = False) -> bool:
        asked.append(urgent)
        return urgent

    monkeypatch.setattr(people_channel, "is_within_availability", quiet)
    assert asyncio.run(dispatcher.dispatch_slack_dm(_alert("high"))) is False
    assert asyncio.run(dispatcher.dispatch_slack_dm(_alert("urgent"))) is True
    assert asked == [False, True] and len(sent) == 1
