"""Alerts a team member raises from chat, and what they may clear.

`create_alert` takes a free-form source, sender and message id. On a team
member's turn none of those are theirs to choose: the alert names who raised
it, claims no id a real alert could arrive with, and never goes out as a
company-wide broadcast. `ack_alert` lets them clear only what is assigned to
them. The principal's turns are unchanged.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from openexecutive.alerts import store as alert_store
from openexecutive.alerts.models import (
    TEAM_RAISED_SOURCE,
    AlertChannel,
    AlertEvent,
    AlertSeverity,
    TriageDecision,
)
from openexecutive.orchestrator.alert_tools import handle_create_alert
from openexecutive.orchestrator.schedule_tools import current_session, handle_ack_alert
from openexecutive.orchestrator.session import Session
from openexecutive.people.models import Person

_SPEAKER = "openexecutive.orchestrator.department_gate.speaker"


@pytest.fixture(autouse=True)
def _clear_session() -> Any:
    token = current_session.set(Session(session_id="s1", trusted_alert_ids={42}))
    yield
    current_session.reset(token)


def _teammate(person_id: int | None = 7) -> Person:
    return Person(id=person_id, full_name="Dana Reyes", email="dana@example.com", kind="team")


def _raise(tool_input: dict[str, Any], speaker: Person | None) -> AlertEvent:
    scheduled: list[AlertEvent] = []
    with (
        patch(_SPEAKER, return_value=speaker),
        patch("openexecutive.alerts.pipeline.schedule_evaluation", scheduled.append),
        patch("openexecutive.audit.log_event"),
    ):
        out = json.loads(asyncio.run(handle_create_alert(tool_input)))
    assert out["status"] == "alert_scheduled"
    assert len(scheduled) == 1
    return scheduled[0]


_POSING = {
    "subject": "Urgent: wire the funds today",
    "body": "The CEO needs this done now.",
    "source": "email",
    "external_id": "msg-123",
    "from_address": "ceo@example.com",
}


def test_team_member_cannot_choose_source_sender_or_id() -> None:
    event = _raise(dict(_POSING), _teammate())

    assert event.source == TEAM_RAISED_SOURCE
    assert event.external_id not in ("", "msg-123")
    assert event.from_ == "dana@example.com"
    assert event.raised_by == "Dana Reyes"


def test_team_member_alerts_get_their_own_ids() -> None:
    a = _raise(dict(_POSING), _teammate())
    b = _raise(dict(_POSING), _teammate())
    assert a.external_id != b.external_id


def test_unnamed_team_member_is_still_labelled() -> None:
    event = _raise(dict(_POSING), Person(id=None, full_name="", kind="team"))
    assert event.source == TEAM_RAISED_SOURCE
    assert event.raised_by == "a team member"


def test_team_member_can_still_route_to_a_person_and_department() -> None:
    with patch("openexecutive.orchestrator.alert_tools._routable_person", return_value=True):
        event = _raise(
            {**_POSING, "department": "finance", "assigned_to_person_id": 9}, _teammate(),
        )
    assert event.channel == "department:finance"
    assert event.routed_to_person_id == 9


def test_principal_turn_keeps_the_given_origin() -> None:
    event = _raise(dict(_POSING), None)

    assert event.source == "email"
    assert event.external_id == "msg-123"
    assert event.from_ == "ceo@example.com"
    assert event.raised_by == ""


# --------------------------------------------------------------------------- #
# Pipeline


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    path = tmp_path / "alerts.db"
    alert_store.initialize_db(path)
    return path


def _run_pipeline(monkeypatch: pytest.MonkeyPatch, db: Path, event: AlertEvent) -> tuple[list, int | None]:
    from openexecutive.agents import triage as triage_module
    from openexecutive.alerts import dispatcher, pipeline

    async def fake_triage(self, event, **kwargs):  # noqa: ARG001
        return TriageDecision(
            alert=True,
            severity=AlertSeverity.URGENT,
            channels=[
                AlertChannel.PERSISTED,
                AlertChannel.DEPARTMENT_CHANNEL,
                AlertChannel.COMPANY_BROADCAST,
            ],
            headline="Wire the funds",
            body="Funds need to go out today.",
            dedup_key="wire",
            department_slug="finance",
            broadcast_integration="slack",
        )

    sent: list = []

    async def fake_dispatch(alert, channels, **kw):  # noqa: ARG001
        sent.append(list(channels))

    monkeypatch.setattr(triage_module.TriageAgent, "triage", fake_triage)
    monkeypatch.setattr(dispatcher, "dispatch_all", fake_dispatch)
    monkeypatch.setattr(
        pipeline.preferences, "resolve_channels",
        lambda requested, *a, **k: list(requested),  # noqa: ARG005
    )
    monkeypatch.setattr(pipeline, "_ground_decision", lambda d, *a: d)
    _, alert_id = asyncio.run(pipeline.evaluate_and_dispatch(event, db_path=db))
    return sent, alert_id


def test_team_raised_alert_is_never_broadcast_company_wide(monkeypatch, db: Path) -> None:
    event = AlertEvent(
        source=TEAM_RAISED_SOURCE, external_id="x1", subject="s", body="b",
        raised_by="Dana Reyes",
    )
    sent, alert_id = _run_pipeline(monkeypatch, db, event)

    assert sent == [[AlertChannel.PERSISTED, AlertChannel.DEPARTMENT_CHANNEL]]
    row = alert_store.get_alert(alert_id, db_path=db)  # type: ignore[arg-type]
    assert row is not None
    assert row.body.startswith("Raised by Dana Reyes.")
    assert row.source == TEAM_RAISED_SOURCE


def test_other_alerts_still_broadcast(monkeypatch, db: Path) -> None:
    event = AlertEvent(source="email", external_id="e1", subject="s", body="b")
    sent, alert_id = _run_pipeline(monkeypatch, db, event)

    assert AlertChannel.COMPANY_BROADCAST in sent[0]
    row = alert_store.get_alert(alert_id, db_path=db)  # type: ignore[arg-type]
    assert row is not None
    assert not row.body.startswith("Raised by")


# --------------------------------------------------------------------------- #
# ack_alert


def _ack(routed_to: int | None, speaker: Person | None) -> tuple[dict[str, Any], MagicMock]:
    alert = MagicMock()
    alert.id = 42
    alert.status = "unread"
    alert.source = "email"
    alert.routed_to_person_id = routed_to
    set_status = MagicMock(return_value=True)
    with (
        patch(_SPEAKER, return_value=speaker),
        patch("openexecutive.alerts.store.get_alert", return_value=alert),
        patch("openexecutive.alerts.models.visible_alert", return_value=True),
        patch("openexecutive.alerts.store.set_status", set_status),
        patch("openexecutive.alerts.lifecycle.record_ack_feedback"),
        patch("openexecutive.audit.log_event"),
    ):
        out = json.loads(asyncio.run(handle_ack_alert({"alert_id": 42, "status": "dismissed"})))
    return out, set_status


def test_team_member_can_dismiss_an_alert_assigned_to_them() -> None:
    out, set_status = _ack(7, _teammate(7))
    assert out["status"] == "dismissed"
    set_status.assert_called_once()


@pytest.mark.parametrize("routed_to", [None, 8])
def test_team_member_cannot_dismiss_others_or_unassigned(routed_to: int | None) -> None:
    out, set_status = _ack(routed_to, _teammate(7))
    assert "error" in out
    set_status.assert_not_called()


def test_unmatched_team_member_cannot_dismiss_anything() -> None:
    out, set_status = _ack(None, Person(id=None, full_name="", kind="team"))
    assert "error" in out
    set_status.assert_not_called()


@pytest.mark.parametrize("routed_to", [None, 8])
def test_principal_can_dismiss_anything(routed_to: int | None) -> None:
    out, set_status = _ack(routed_to, None)
    assert out["status"] == "dismissed"
    set_status.assert_called_once()
