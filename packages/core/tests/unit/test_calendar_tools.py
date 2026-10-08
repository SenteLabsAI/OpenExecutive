"""Unit tests for calendar_tools: caps, idempotency, gate routing, and propose vs execute paths."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openexecutive.orchestrator.calendar_tools import (
    handle_cancel_calendar_event,
    handle_create_calendar_event,
    handle_create_instant_meeting,
)
from openexecutive.people import store as people_store


# A weekday at 10:00 UTC, at least 3 days out. The calendar validator rejects
# weekends and off-hours, so snap forward to Mon–Fri — otherwise these tests
# flake whenever `now + 3 days` happens to land on a Saturday or Sunday.
def _next_weekday_at_10(base: datetime) -> datetime:
    dt = base.replace(hour=10, minute=0, second=0, microsecond=0)
    while dt.weekday() >= 5:  # Saturday=5, Sunday=6
        dt += timedelta(days=1)
    return dt


FUTURE = _next_weekday_at_10(datetime.now(UTC) + timedelta(days=3))
FUTURE_END = FUTURE.replace(hour=11)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from openexecutive.alerts import store as alert_store
    from openexecutive.memory import episodic
    db = tmp_path / "test.db"
    monkeypatch.setattr(episodic, "DB_PATH", db)
    monkeypatch.setattr(people_store, "DB_PATH", db)
    # The propose path now surfaces a companion briefing alert; wire the alerts
    # store to the same isolated DB so it lands in-test (and is assertable).
    monkeypatch.setattr(alert_store, "DB_PATH", db)
    episodic.initialize_db(db)
    people_store.initialize_db()
    alert_store.initialize_db(db)
    return db


def _settings(**kw: Any) -> Any:
    defaults = dict(
        calendar_booking_enabled=True,
        mcp_enabled=True,
        calendar_business_hours_start="09:00",
        calendar_business_hours_end="18:00",
        calendar_horizon_days=30,
        calendar_max_events_per_day=10,
        calendar_max_attendees=8,
        calendar_meet_links_enabled=True,
        calendar_instant_meeting_minutes=30,
        calendar_post_meeting_followup_enabled=True,
        # No DM channel tokens by default → post-meeting recap resolver finds no
        # reachable attendee and skips (keeps unrelated tests side-effect-free).
        slack_bot_token=None,
        discord_bot_token=None,
        telegram_bot_token=None,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _add_person(
    name: str, email: str, *, is_principal: bool = False
) -> int:
    return people_store.upsert_person(
        full_name=name, email=email, is_principal=is_principal
    )


def _fake_gateway(event_id: str = "evt-001") -> Any:
    gw = MagicMock()
    gw.call_tool = AsyncMock(return_value=json.dumps({"id": event_id}))
    return gw


def _call(tool_input: dict[str, Any], settings: Any | None = None, gateway: Any = None) -> dict[str, Any]:
    s = settings or _settings()
    gw = gateway or _fake_gateway()

    with (
        patch("openexecutive.config.get_settings", return_value=s),
        patch("openexecutive.orchestrator.mcp_gateway.get_active_gateway", return_value=gw),
        patch("openexecutive.departments.authority.gate_action") as mock_gate,
    ):
        mock_gate.return_value = SimpleNamespace(
            action="propose",
            assignee_person_id=None,
        )
        raw = asyncio.run(handle_create_calendar_event(tool_input))
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Disabled / unconfigured
# ---------------------------------------------------------------------------

def test_disabled_returns_error() -> None:
    _add_person("Alice", "alice@example.com")
    result = _call(
        {"title": "Sync", "start": _iso(FUTURE), "end": _iso(FUTURE_END),
         "attendee_person_ids": [1], "confidence": 0.9},
        settings=_settings(calendar_booking_enabled=False),
    )
    assert "error" in result


def test_no_gateway_returns_error() -> None:
    _add_person("Alice", "alice@example.com")
    s = _settings()
    with (
        patch("openexecutive.config.get_settings", return_value=s),
        patch("openexecutive.orchestrator.mcp_gateway.get_active_gateway", return_value=None),
    ):
        raw = asyncio.run(handle_create_calendar_event(
            {"title": "Sync", "start": _iso(FUTURE), "end": _iso(FUTURE_END),
             "attendee_person_ids": [1], "confidence": 0.9}
        ))
    assert "error" in json.loads(raw)


# ---------------------------------------------------------------------------
# Parsing / validation
# ---------------------------------------------------------------------------

def test_missing_required_field_returns_error() -> None:
    result = _call({"start": _iso(FUTURE), "end": _iso(FUTURE_END),
                    "attendee_person_ids": [1], "confidence": 0.8})
    assert "error" in result
    assert "title" in result["error"].lower() or "missing" in result["error"].lower()


def test_past_start_rejected() -> None:
    _add_person("Alice", "alice@example.com")
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    result = _call({
        "title": "Sync", "start": past, "end": _iso(FUTURE_END),
        "attendee_person_ids": [1], "confidence": 0.8,
    })
    assert "error" in result
    assert "future" in result["error"].lower()


def test_end_before_start_rejected() -> None:
    _add_person("Alice", "alice@example.com")
    result = _call({
        "title": "Sync", "start": _iso(FUTURE_END), "end": _iso(FUTURE),
        "attendee_person_ids": [1], "confidence": 0.8,
    })
    assert "error" in result
    assert "end" in result["error"].lower()


# ---------------------------------------------------------------------------
# Cap: horizon
# ---------------------------------------------------------------------------

def test_beyond_horizon_rejected() -> None:
    _add_person("Alice", "alice@example.com")
    far = (datetime.now(UTC) + timedelta(days=60)).replace(hour=10)
    result = _call({
        "title": "Sync", "start": _iso(far), "end": _iso(far.replace(hour=11)),
        "attendee_person_ids": [1], "confidence": 0.8,
    }, settings=_settings(calendar_horizon_days=30))
    assert "error" in result
    assert "30 days" in result["error"]


# ---------------------------------------------------------------------------
# Cap: business hours
# ---------------------------------------------------------------------------

def test_weekend_rejected() -> None:
    _add_person("Alice", "alice@example.com")
    # Find next Saturday, starting from tomorrow so the time-of-day replace is safe
    d = datetime.now(UTC) + timedelta(days=1)
    while d.weekday() != 5:
        d += timedelta(days=1)
    sat = d.replace(hour=10, minute=0, second=0, microsecond=0)
    result = _call({
        "title": "Sync", "start": _iso(sat), "end": _iso(sat.replace(hour=11)),
        "attendee_person_ids": [1], "confidence": 0.8,
    })
    assert "error" in result
    assert "weekend" in result["error"].lower() or "business" in result["error"].lower()


def test_outside_business_hours_rejected() -> None:
    _add_person("Alice", "alice@example.com")
    # 20:00 UTC — outside 09:00–18:00
    late = FUTURE.replace(hour=20)
    result = _call({
        "title": "Sync", "start": _iso(late), "end": _iso(late.replace(hour=21)),
        "attendee_person_ids": [1], "confidence": 0.8,
    })
    assert "error" in result
    assert "business" in result["error"].lower()


# ---------------------------------------------------------------------------
# Cap: roster gate
# ---------------------------------------------------------------------------

def test_unknown_person_id_rejected() -> None:
    result = _call({
        "title": "Sync", "start": _iso(FUTURE), "end": _iso(FUTURE_END),
        "attendee_person_ids": [9999], "confidence": 0.8,
    })
    assert "error" in result
    assert "9999" in result["error"]


def test_person_without_email_rejected() -> None:
    pid = people_store.upsert_person(full_name="No Email", email=None)
    result = _call({
        "title": "Sync", "start": _iso(FUTURE), "end": _iso(FUTURE_END),
        "attendee_person_ids": [pid], "confidence": 0.8,
    })
    assert "error" in result
    assert "email" in result["error"].lower()


def test_too_many_attendees_rejected() -> None:
    for i in range(9):
        _add_person(f"Person {i}", f"p{i}@example.com")
    all_ids = [p.id for p in people_store.list_people() if p.id]
    result = _call({
        "title": "Sync", "start": _iso(FUTURE), "end": _iso(FUTURE_END),
        "attendee_person_ids": all_ids[:9], "confidence": 0.8,
    }, settings=_settings(calendar_max_attendees=8))
    assert "error" in result
    assert "attendee" in result["error"].lower()


# ---------------------------------------------------------------------------
# Cap: principal protection
# ---------------------------------------------------------------------------

def test_principal_without_include_flag_rejected() -> None:
    pid = _add_person("Principal", "ceo@example.com", is_principal=True)
    result = _call({
        "title": "Sync", "start": _iso(FUTURE), "end": _iso(FUTURE_END),
        "attendee_person_ids": [pid], "confidence": 0.9,
        "include_principal": False,
    })
    assert "error" in result
    assert "principal" in result["error"].lower()


def test_principal_with_flag_allowed() -> None:
    pid = _add_person("Principal", "ceo@example.com", is_principal=True)
    result = _call({
        "title": "Sync", "start": _iso(FUTURE), "end": _iso(FUTURE_END),
        "attendee_person_ids": [pid], "confidence": 0.9,
        "include_principal": True,
    })
    assert "error" not in result
    assert result["status"] == "proposed"


# ---------------------------------------------------------------------------
# Happy path: propose-only
# ---------------------------------------------------------------------------

def test_happy_path_propose(tmp_path: Path) -> None:
    pid = _add_person("Alice", "alice@example.com")
    result = _call({
        "title": "Weekly sync", "start": _iso(FUTURE), "end": _iso(FUTURE_END),
        "attendee_person_ids": [pid], "confidence": 0.85,
    })
    assert result["status"] == "proposed"
    assert "decision_instance_id" in result


def test_propose_does_not_call_mcp() -> None:
    pid = _add_person("Alice", "alice@example.com")
    gw = _fake_gateway()
    _call({
        "title": "Sync", "start": _iso(FUTURE), "end": _iso(FUTURE_END),
        "attendee_person_ids": [pid], "confidence": 0.9,
    }, gateway=gw)
    # propose path must NOT call the MCP
    gw.call_tool.assert_not_awaited()


# ---------------------------------------------------------------------------
# Auto-execute path (Build 3 — gate returns "execute" + class_mode auto_execute)
# ---------------------------------------------------------------------------

def test_auto_execute_creates_event_and_marks_executed() -> None:
    pid = _add_person("Alice", "alice@example.com")
    gw = _fake_gateway("evt-auto")
    s = _settings()

    with (
        patch("openexecutive.config.get_settings", return_value=s),
        patch("openexecutive.orchestrator.mcp_gateway.get_active_gateway", return_value=gw),
        patch("openexecutive.departments.authority.gate_action") as mock_gate,
        patch("openexecutive.memory.decision_ledger.get_class_mode", return_value="auto_execute"),
    ):
        mock_gate.return_value = SimpleNamespace(action="execute", assignee_person_id=None)
        raw = asyncio.run(handle_create_calendar_event({
            "title": "Auto Sync", "start": _iso(FUTURE), "end": _iso(FUTURE_END),
            "attendee_person_ids": [pid], "confidence": 0.95,
        }))

    result = json.loads(raw)
    assert result["status"] == "created"
    assert "decision_instance_id" in result
    gw.call_tool.assert_awaited_once()


def test_auto_execute_marks_failed_on_mcp_error() -> None:
    pid = _add_person("Alice", "alice@example.com")
    gw = MagicMock()
    gw.call_tool = AsyncMock(return_value=json.dumps({"error": "google API down"}))
    s = _settings()

    with (
        patch("openexecutive.config.get_settings", return_value=s),
        patch("openexecutive.orchestrator.mcp_gateway.get_active_gateway", return_value=gw),
        patch("openexecutive.departments.authority.gate_action") as mock_gate,
        patch("openexecutive.memory.decision_ledger.get_class_mode", return_value="auto_execute"),
    ):
        mock_gate.return_value = SimpleNamespace(action="execute", assignee_person_id=None)
        raw = asyncio.run(handle_create_calendar_event({
            "title": "Fail Sync", "start": _iso(FUTURE), "end": _iso(FUTURE_END),
            "attendee_person_ids": [pid], "confidence": 0.9,
        }))

    result = json.loads(raw)
    assert "error" in result
    # The stranded-row fix: ledger row must be marked failed, not left proposed.
    from openexecutive.memory.decision_ledger import list_instances
    instances = list_instances("meeting_scheduling")
    assert len(instances) == 1
    assert instances[0].status == "failed"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def test_identical_proposal_returns_existing(tmp_path: Path) -> None:
    pid = _add_person("Alice", "alice@example.com")
    tool_input = {
        "title": "Sync", "start": _iso(FUTURE), "end": _iso(FUTURE_END),
        "attendee_person_ids": [pid], "confidence": 0.9,
    }
    r1 = _call(tool_input)
    r2 = _call(tool_input)
    assert r1["status"] == "proposed"
    assert r2["status"] == "already_proposed"
    assert r1["decision_instance_id"] == r2["decision_instance_id"]


# ---------------------------------------------------------------------------
# Bridge: propose surfaces a linked briefing alert
# ---------------------------------------------------------------------------

def _call_with_gate(
    tool_input: dict[str, Any], *, action: str, assignee_person_id: int | None
) -> dict[str, Any]:
    """Like _call but lets the test control the authority-gate decision."""
    s = _settings()
    gw = _fake_gateway()
    with (
        patch("openexecutive.config.get_settings", return_value=s),
        patch("openexecutive.orchestrator.mcp_gateway.get_active_gateway", return_value=gw),
        patch("openexecutive.departments.authority.gate_action") as mock_gate,
    ):
        mock_gate.return_value = SimpleNamespace(
            action=action, assignee_person_id=assignee_person_id
        )
        raw = asyncio.run(handle_create_calendar_event(tool_input))
    return json.loads(raw)


def test_a_booking_its_approver_already_approved_is_booked_now() -> None:
    """An invite approved on an action card (delegation.action_cards) by the
    person the gate would ask is booked, not proposed to them again."""
    from openexecutive.orchestrator.calendar_tools import approved_by

    pid = _add_person("Alice", "alice@example.com")
    approver = _add_person("Boss", "boss@example.com")
    with approved_by(approver):
        result = _call_with_gate(
            {"title": "Pilot kickoff", "start": _iso(FUTURE), "end": _iso(FUTURE_END),
             "attendee_person_ids": [pid], "confidence": 1.0},
            action="propose", assignee_person_id=approver,
        )
    assert result["status"] == "created"


def test_a_booking_someone_else_approved_still_goes_to_its_approver() -> None:
    from openexecutive.orchestrator.calendar_tools import approved_by

    pid = _add_person("Alice", "alice@example.com")
    approver = _add_person("Boss", "boss@example.com")
    teammate = _add_person("Ben", "ben@example.com")
    with approved_by(teammate):
        result = _call_with_gate(
            {"title": "Pilot kickoff", "start": _iso(FUTURE), "end": _iso(FUTURE_END),
             "attendee_person_ids": [pid], "confidence": 1.0},
            action="propose", assignee_person_id=approver,
        )
    assert result["status"] == "proposed"


def test_propose_creates_linked_briefing_alert(_isolated: Path) -> None:
    from openexecutive.alerts.store import get_alert_by_external

    pid = _add_person("Alice", "alice@example.com")
    result = _call({
        "title": "Weekly sync", "start": _iso(FUTURE), "end": _iso(FUTURE_END),
        "attendee_person_ids": [pid], "confidence": 0.85,
    })
    iid = result["decision_instance_id"]

    alert = get_alert_by_external("decision_scheduling", f"decision:{iid}", db_path=_isolated)
    assert alert is not None
    assert alert.status == "unread"
    assert "Weekly sync" in alert.headline
    assert f"decision_instance:{iid}" in alert.topic_tags
    assert "decision_class:meeting_scheduling" in alert.topic_tags
    # _call's gate has no assignee → unrouted; still surfaces (principal-owns).
    assert alert.routed_to_person_id is None


def test_propose_routes_alert_to_approver(_isolated: Path) -> None:
    from openexecutive.alerts.store import get_alert_by_external

    pid = _add_person("Alice", "alice@example.com")
    approver = _add_person("Boss", "boss@example.com")
    result = _call_with_gate(
        {
            "title": "Budget review", "start": _iso(FUTURE), "end": _iso(FUTURE_END),
            "attendee_person_ids": [pid], "confidence": 0.9,
        },
        action="propose",
        assignee_person_id=approver,
    )
    iid = result["decision_instance_id"]
    alert = get_alert_by_external("decision_scheduling", f"decision:{iid}", db_path=_isolated)
    assert alert is not None
    assert alert.routed_to_person_id == approver


def test_already_proposed_does_not_duplicate_alert(_isolated: Path) -> None:
    from openexecutive.alerts.store import list_alerts

    pid = _add_person("Alice", "alice@example.com")
    tool_input = {
        "title": "Sync", "start": _iso(FUTURE), "end": _iso(FUTURE_END),
        "attendee_person_ids": [pid], "confidence": 0.9,
    }
    _call(tool_input)
    _call(tool_input)  # idempotent — already_proposed
    decision_alerts = [
        a for a in list_alerts(db_path=_isolated) if a.source == "decision_scheduling"
    ]
    assert len(decision_alerts) == 1


def test_auto_execute_creates_no_decision_alert(_isolated: Path) -> None:
    from openexecutive.alerts.store import list_alerts

    pid = _add_person("Alice", "alice@example.com")
    gw = _fake_gateway("evt-auto")
    s = _settings()
    with (
        patch("openexecutive.config.get_settings", return_value=s),
        patch("openexecutive.orchestrator.mcp_gateway.get_active_gateway", return_value=gw),
        patch("openexecutive.departments.authority.gate_action") as mock_gate,
        patch("openexecutive.memory.decision_ledger.get_class_mode", return_value="auto_execute"),
    ):
        mock_gate.return_value = SimpleNamespace(action="execute", assignee_person_id=None)
        raw = asyncio.run(handle_create_calendar_event({
            "title": "Auto Sync", "start": _iso(FUTURE), "end": _iso(FUTURE_END),
            "attendee_person_ids": [pid], "confidence": 0.95,
        }))
    assert json.loads(raw)["status"] == "created"
    decision_alerts = [
        a for a in list_alerts(db_path=_isolated) if a.source == "decision_scheduling"
    ]
    assert decision_alerts == []


# ---------------------------------------------------------------------------
# cancel_calendar_event
# ---------------------------------------------------------------------------

def test_cancel_event_marks_reversed() -> None:
    from openexecutive.memory.decision_ledger import mark_resolved
    from openexecutive.memory.episodic import DB_PATH

    pid = _add_person("Alice", "alice@example.com")
    r = _call({
        "title": "Sync", "start": _iso(FUTURE), "end": _iso(FUTURE_END),
        "attendee_person_ids": [pid], "confidence": 0.9,
    })
    iid = r["decision_instance_id"]
    mark_resolved(iid, "approved_unchanged", external_event_id="evt-999", db_path=DB_PATH)

    gw = _fake_gateway()
    with (
        patch("openexecutive.orchestrator.mcp_gateway.get_active_gateway", return_value=gw),
    ):
        raw = asyncio.run(handle_cancel_calendar_event({"decision_instance_id": iid}))
    result = json.loads(raw)
    assert result["status"] == "cancelled"
    gw.call_tool.assert_awaited_once()


def test_cancel_nonexistent_returns_error() -> None:
    with patch("openexecutive.orchestrator.mcp_gateway.get_active_gateway",
               return_value=_fake_gateway()):
        raw = asyncio.run(handle_cancel_calendar_event({"decision_instance_id": 9999}))
    assert "error" in json.loads(raw)


def _instance(decision_class: str, payload: dict[str, Any]) -> int:
    from openexecutive.memory.decision_ledger import create_decision_instance

    return create_decision_instance(
        decision_class=decision_class, department="", originating_session_id=None,
        proposed_payload=payload, idempotency_key=None, gate_mode="propose",
        approver_person_id=None, confidence=0.9,
    )


def test_cancel_touches_only_meeting_bookings() -> None:
    """Another class (a reply drafted in the owner's own Gmail) reads exactly
    like a missing id: the tool can't close it, nor tell that it exists."""
    from openexecutive.memory.decision_ledger import get_decision_instance, mark_reversed

    iid = _instance("delegation_reply", {"private": True, "draft_id": "d1"})
    gw = _fake_gateway()
    with patch("openexecutive.orchestrator.mcp_gateway.get_active_gateway", return_value=gw):
        raw = asyncio.run(handle_cancel_calendar_event({"decision_instance_id": iid}))
    assert json.loads(raw) == {"error": f"decision_instance {iid} not found"}
    assert get_decision_instance(iid).status == "proposed"  # type: ignore[union-attr]
    gw.call_tool.assert_not_awaited()
    # And the ledger itself refuses a reversal of the wrong class.
    assert mark_reversed(iid, reason="x", decision_class="meeting_scheduling") is False
    assert get_decision_instance(iid).status == "proposed"  # type: ignore[union-attr]


def test_a_private_booking_is_the_owners_to_cancel() -> None:
    from openexecutive.memory.decision_ledger import get_decision_instance
    from openexecutive.orchestrator.people_tools import grant_contact_egress

    iid = _instance("meeting_scheduling", {"private": True, "title": "Sync"})
    with patch("openexecutive.orchestrator.mcp_gateway.get_active_gateway", return_value=_fake_gateway()):
        # Not the owner's own verified turn: it doesn't exist.
        raw = asyncio.run(handle_cancel_calendar_event({"decision_instance_id": iid}))
        assert json.loads(raw) == {"error": f"decision_instance {iid} not found"}
        assert get_decision_instance(iid).status == "proposed"  # type: ignore[union-attr]
        with grant_contact_egress():
            raw = asyncio.run(handle_cancel_calendar_event({"decision_instance_id": iid}))
    assert json.loads(raw)["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Google Meet links
# ---------------------------------------------------------------------------

def _fake_gateway_with_meet(
    event_id: str = "evt-meet", link: str = "https://meet.google.com/abc-defg-hij"
) -> Any:
    gw = MagicMock()
    gw.call_tool = AsyncMock(return_value=json.dumps({
        "id": event_id,
        "conferenceData": {
            "entryPoints": [{"entryPointType": "video", "uri": link}],
        },
    }))
    return gw


def _last_manage_event_args(gw: Any) -> dict[str, Any]:
    return gw.call_tool.call_args.args[0]["arguments"]


def test_meet_link_requested_and_surfaced_on_auto_execute() -> None:
    pid = _add_person("Alice", "alice@example.com")
    gw = _fake_gateway_with_meet()
    s = _settings()
    with (
        patch("openexecutive.config.get_settings", return_value=s),
        patch("openexecutive.orchestrator.mcp_gateway.get_active_gateway", return_value=gw),
        patch("openexecutive.departments.authority.gate_action") as mock_gate,
        patch("openexecutive.memory.decision_ledger.get_class_mode", return_value="auto_execute"),
    ):
        mock_gate.return_value = SimpleNamespace(action="execute", assignee_person_id=None)
        raw = asyncio.run(handle_create_calendar_event({
            "title": "Synced", "start": _iso(FUTURE), "end": _iso(FUTURE_END),
            "attendee_person_ids": [pid], "confidence": 0.95,
        }))
    result = json.loads(raw)
    assert result["status"] == "created"
    assert result["meet_link"] == "https://meet.google.com/abc-defg-hij"
    # add_google_meet must have been requested on the MCP call.
    assert _last_manage_event_args(gw).get("add_google_meet") is True


def test_add_google_meet_false_omits_request() -> None:
    pid = _add_person("Alice", "alice@example.com")
    # No-conference gateway: with add_google_meet omitted, the real MCP returns
    # no conferenceData, so there is no link to surface.
    gw = _fake_gateway("evt-novid")
    s = _settings()
    with (
        patch("openexecutive.config.get_settings", return_value=s),
        patch("openexecutive.orchestrator.mcp_gateway.get_active_gateway", return_value=gw),
        patch("openexecutive.departments.authority.gate_action") as mock_gate,
        patch("openexecutive.memory.decision_ledger.get_class_mode", return_value="auto_execute"),
    ):
        mock_gate.return_value = SimpleNamespace(action="execute", assignee_person_id=None)
        raw = asyncio.run(handle_create_calendar_event({
            "title": "In person", "start": _iso(FUTURE), "end": _iso(FUTURE_END),
            "attendee_person_ids": [pid], "confidence": 0.9, "add_google_meet": False,
        }))
    result = json.loads(raw)
    # The booking still succeeds; it just carries no video link.
    assert result["status"] == "created"
    assert result.get("meet_link") is None
    assert "add_google_meet" not in _last_manage_event_args(gw)


# ---------------------------------------------------------------------------
# create_instant_meeting
# ---------------------------------------------------------------------------

def _call_instant(
    tool_input: dict[str, Any], settings: Any | None = None, gateway: Any = None
) -> tuple[dict[str, Any], Any]:
    s = settings or _settings()
    gw = gateway or _fake_gateway_with_meet()
    with (
        patch("openexecutive.config.get_settings", return_value=s),
        patch("openexecutive.orchestrator.mcp_gateway.get_active_gateway", return_value=gw),
    ):
        raw = asyncio.run(handle_create_instant_meeting(tool_input))
    return json.loads(raw), gw


def test_instant_meeting_auto_executes_now_with_meet_link() -> None:
    pid = _add_person("Alice", "alice@example.com")
    result, gw = _call_instant({"attendee_person_ids": [pid], "confidence": 0.9})
    assert result["status"] == "created"
    assert result["meet_link"] == "https://meet.google.com/abc-defg-hij"
    assert result["event_id"] == "evt-meet"
    gw.call_tool.assert_awaited_once()
    assert _last_manage_event_args(gw).get("add_google_meet") is True
    # No proposal left pending — it was executed, not proposed.
    from openexecutive.memory.decision_ledger import list_instances
    instances = list_instances("meeting_scheduling")
    assert len(instances) == 1
    assert instances[0].status == "executed"


def test_instant_meeting_ignores_business_hours_and_horizon() -> None:
    # Even at an hour/day the scheduled tool would reject, instant books fine.
    pid = _add_person("Alice", "alice@example.com")
    result, _ = _call_instant({"attendee_person_ids": [pid], "confidence": 0.8})
    assert result["status"] == "created"


def test_instant_meeting_principal_protection() -> None:
    pid = _add_person("Boss", "boss@example.com", is_principal=True)
    result, _ = _call_instant({"attendee_person_ids": [pid], "confidence": 0.9})
    assert "error" in result
    assert "principal" in result["error"].lower()


def test_instant_meeting_max_attendees() -> None:
    ids = [_add_person(f"P{i}", f"p{i}@example.com") for i in range(3)]
    result, _ = _call_instant(
        {"attendee_person_ids": ids, "confidence": 0.9},
        settings=_settings(calendar_max_attendees=2),
    )
    assert "error" in result
    assert "attendees" in result["error"].lower()


def test_instant_meeting_daily_cap() -> None:
    pid = _add_person("Alice", "alice@example.com")
    result, _ = _call_instant(
        {"attendee_person_ids": [pid], "confidence": 0.9},
        settings=_settings(calendar_max_events_per_day=0),
    )
    assert "error" in result
    assert "cap" in result["error"].lower()


# ---------------------------------------------------------------------------
# Post-meeting recap follow-up
# ---------------------------------------------------------------------------

def test_post_meeting_followup_scheduled_for_reachable_attendee() -> None:
    from openexecutive.memory.episodic import list_pending_scheduled_actions

    pid = people_store.upsert_person(
        full_name="Alice", email="alice@example.com", discord_user_id="123456789"
    )
    s = _settings(discord_bot_token="x")
    _call_instant({"attendee_person_ids": [pid], "confidence": 0.9}, settings=s)

    pending = list_pending_scheduled_actions()
    followups = [a for a in pending if (a.scope_key or "").startswith("meeting_followup:")]
    assert len(followups) == 1
    fu = followups[0]
    assert fu.channel == "discord_dm"
    assert fu.channel_ref == "123456789"
    assert fu.kind == "ad_hoc"
    # The recap ask must actually request decisions + action items.
    low = fu.intent_text.lower()
    assert "recap" in low
    assert "decision" in low and "action item" in low
    # Fires ~5 min after the (now + 1 min + 30 min) instant meeting ends, i.e.
    # comfortably in the future — never before the meeting.
    run_at = datetime.fromisoformat(fu.run_at)
    assert run_at > datetime.now(UTC) + timedelta(minutes=30)


def test_post_meeting_followup_skipped_when_no_dm_channel() -> None:
    from openexecutive.memory.episodic import list_pending_scheduled_actions

    # Email-only attendee, no DM tokens configured → nothing to DM, skip.
    pid = _add_person("Alice", "alice@example.com")
    _call_instant({"attendee_person_ids": [pid], "confidence": 0.9})

    pending = list_pending_scheduled_actions()
    assert [a for a in pending if (a.scope_key or "").startswith("meeting_followup:")] == []


def test_post_meeting_followup_deduped_on_same_event() -> None:
    from openexecutive.memory.episodic import list_pending_scheduled_actions

    pid = people_store.upsert_person(
        full_name="Alice", email="alice@example.com", discord_user_id="123456789"
    )
    s = _settings(discord_bot_token="x")
    # Same fake gateway → same event_id → same meeting_followup scope_key, so a
    # second booking of the "same" event must not schedule a duplicate recap.
    gw = _fake_gateway_with_meet()
    _call_instant({"attendee_person_ids": [pid], "confidence": 0.9}, settings=s, gateway=gw)
    _call_instant({"attendee_person_ids": [pid], "confidence": 0.9}, settings=s, gateway=gw)

    pending = list_pending_scheduled_actions()
    followups = [a for a in pending if (a.scope_key or "").startswith("meeting_followup:")]
    assert len(followups) == 1


# ---------------------------------------------------------------------------
# Tool gating (proactive + chat share the same filter)
# ---------------------------------------------------------------------------

def test_instant_meeting_tool_gated_on_calendar_configured() -> None:
    from openexecutive.orchestrator.calendar_tools import CALENDAR_TOOLS
    from openexecutive.orchestrator.schedule_tools import (
        filter_tools_for_configured_channels,
    )

    with patch(
        "openexecutive.orchestrator.mcp_gateway.get_active_gateway",
        return_value=_fake_gateway(),
    ):
        on = filter_tools_for_configured_channels(list(CALENDAR_TOOLS), _settings())
        off = filter_tools_for_configured_channels(
            list(CALENDAR_TOOLS), _settings(calendar_booking_enabled=False)
        )
    on_names = {t["name"] for t in on}
    off_names = {t["name"] for t in off}
    assert "create_instant_meeting" in on_names
    assert "create_calendar_event" in on_names
    assert "create_instant_meeting" not in off_names


# ---------------------------------------------------------------------------
# Provider switch: add_video_link (+ legacy add_google_meet alias) and the
# Microsoft backend behind the same typed tool
# ---------------------------------------------------------------------------

def _fake_gateway_with_teams(
    event_id: str = "evt-teams", link: str = "https://teams.microsoft.com/l/meetup-join/abc"
) -> Any:
    gw = MagicMock()
    gw.call_tool = AsyncMock(return_value=json.dumps({
        "id": event_id, "onlineMeeting": {"joinUrl": link},
    }))
    return gw


def test_add_video_link_false_omits_meet_request() -> None:
    pid = _add_person("Alice", "alice@example.com")
    gw = _fake_gateway("evt-novid2")
    s = _settings()
    with (
        patch("openexecutive.config.get_settings", return_value=s),
        patch("openexecutive.orchestrator.mcp_gateway.get_active_gateway", return_value=gw),
        patch("openexecutive.departments.authority.gate_action") as mock_gate,
        patch("openexecutive.memory.decision_ledger.get_class_mode", return_value="auto_execute"),
    ):
        mock_gate.return_value = SimpleNamespace(action="execute", assignee_person_id=None)
        raw = asyncio.run(handle_create_calendar_event({
            "title": "In person", "start": _iso(FUTURE), "end": _iso(FUTURE_END),
            "attendee_person_ids": [pid], "confidence": 0.9, "add_video_link": False,
        }))
    result = json.loads(raw)
    assert result["status"] == "created"
    assert result.get("meet_link") is None
    assert "add_google_meet" not in _last_manage_event_args(gw)


def test_tool_schema_exposes_add_video_link_not_the_old_key() -> None:
    from openexecutive.orchestrator.calendar_tools import CREATE_CALENDAR_EVENT_TOOL

    props = CREATE_CALENDAR_EVENT_TOOL["input_schema"]["properties"]
    assert "add_video_link" in props
    assert "add_google_meet" not in props
    assert "Google Meet" not in CREATE_CALENDAR_EVENT_TOOL["description"].replace(
        "Google Meet or Microsoft Teams", ""
    )


def test_microsoft_provider_books_a_teams_meeting() -> None:
    pid = _add_person("Alice", "alice@example.com")
    gw = _fake_gateway_with_teams()
    s = _settings(calendar_provider="microsoft")
    with (
        patch("openexecutive.config.get_settings", return_value=s),
        patch("openexecutive.orchestrator.mcp_gateway.get_active_gateway", return_value=gw),
        patch("openexecutive.departments.authority.gate_action") as mock_gate,
        patch("openexecutive.memory.decision_ledger.get_class_mode", return_value="auto_execute"),
    ):
        mock_gate.return_value = SimpleNamespace(action="execute", assignee_person_id=None)
        raw = asyncio.run(handle_create_calendar_event({
            "title": "Synced", "start": _iso(FUTURE), "end": _iso(FUTURE_END),
            "attendee_person_ids": [pid], "confidence": 0.95,
        }))
    result = json.loads(raw)
    assert result["status"] == "created"
    assert result["event_id"] == "evt-teams"
    assert result["meet_link"] == "https://teams.microsoft.com/l/meetup-join/abc"
    call = gw.call_tool.call_args.args[0]
    assert call["name"] == "microsoft_365__create-calendar-event"
    body = call["arguments"]["body"]
    assert body["isOnlineMeeting"] is True
    assert body["onlineMeetingProvider"] == "teamsForBusiness"
    assert body["attendees"] == [{"emailAddress": {"address": "alice@example.com"}, "type": "required"}]


def test_microsoft_provider_cancels_through_delete_calendar_event() -> None:
    from openexecutive.memory.decision_ledger import (
        create_decision_instance,
        get_decision_instance,
        mark_executed,
    )
    from openexecutive.orchestrator.calendar_tools import handle_cancel_calendar_event

    iid = create_decision_instance(
        decision_class="meeting_scheduling", department="operations",
        originating_session_id=None, proposed_payload={"title": "x"},
        idempotency_key="idem-ms-cancel", gate_mode="execute",
        approver_person_id=None, confidence=0.9,
    )
    mark_executed(iid, external_event_id="evt-ms", final_payload={"title": "x"})
    gw = MagicMock()
    gw.call_tool = AsyncMock(return_value=json.dumps({"success": True}))
    with (
        patch("openexecutive.config.get_settings", return_value=_settings(calendar_provider="microsoft")),
        patch("openexecutive.orchestrator.mcp_gateway.get_active_gateway", return_value=gw),
    ):
        raw = asyncio.run(handle_cancel_calendar_event({"decision_instance_id": iid}))
    assert json.loads(raw)["status"] == "cancelled"
    assert gw.call_tool.call_args.args[0] == {
        "name": "microsoft_365__delete-calendar-event", "arguments": {"eventId": "evt-ms"},
    }
    assert get_decision_instance(iid).status == "reversed"
