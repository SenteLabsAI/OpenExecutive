"""Tests for authority-gate integration in the scheduler runner."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openexecutive.alerts import store as alert_store
from openexecutive.departments import registry as dept_registry
from openexecutive.departments import store as dept_store
from openexecutive.departments.models import AuthorityLevel
from openexecutive.memory import episodic
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store
from openexecutive.people.models import AuthorityScope, AvailabilityWindow
from openexecutive.scheduler.runner import _execute_action


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "test.db"
    monkeypatch.setattr(dept_store, "DB_PATH", db)
    monkeypatch.setattr(people_store, "DB_PATH", db)
    monkeypatch.setattr(episodic, "DB_PATH", db)
    monkeypatch.setattr(alert_store, "DB_PATH", db)
    # Dispatch writes a "delivered" audit row; keep it out of the default
    # ./episodic_memory.db, where it leaks into other modules' assertions.
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **k: None)

    dept_registry.invalidate()
    people_registry.invalidate()

    dept_store.initialize_db(db)
    people_store.initialize_db(db)
    alert_store.initialize_db(db)
    episodic.initialize_db(db)

    yield

    dept_registry.invalidate()
    people_registry.invalidate()


def _now() -> datetime:
    return datetime.now(UTC)


def _make_action(
    *,
    channel: str = "slack_dm",
    channel_ref: str = "U123",
    department: str = "",
    kind: str = "ad_hoc",
    run_at_offset_seconds: int = -10,
) -> episodic.ScheduledAction:
    run_at = (_now() + timedelta(seconds=run_at_offset_seconds)).isoformat()
    episodic.insert_scheduled_action(
        run_at=run_at,
        channel=channel,
        channel_ref=channel_ref,
        intent_text="Renegotiate vendor contract over $15K",
        department=department,
        kind=kind,
    )
    # Claim the action (transition to 'running') so _execute_action can be
    # called directly with a valid ScheduledAction instance.
    actions = episodic.claim_due_actions(_now())
    assert len(actions) == 1
    return actions[0]


# ---------------------------------------------------------------------------
# propose_only → alert routed to CFO, no outbound message
# ---------------------------------------------------------------------------

def test_propose_only_creates_alert_no_dispatch() -> None:
    dept_store.seed_default_departments()
    dept_store.update_department("finance", authority_level=AuthorityLevel.PROPOSE_ONLY)
    # Principal with WILDCARD is the approver the gate finds when no
    # required_scope is specified (current default = WILDCARD query).
    approver_id = people_store.upsert_person(
        full_name="Founder", role="CEO", is_principal=True,
    )
    people_store.set_authority_scope(approver_id, [AuthorityScope.WILDCARD])
    dept_registry.invalidate()
    people_registry.invalidate()

    action = _make_action(department="finance", kind="ad_hoc")

    with patch(
        "openexecutive.orchestrator.executive.Executive.chat",
        new_callable=AsyncMock,
    ) as mock_chat:
        asyncio.run(_execute_action(action, gateway=None))

    # Executive.chat must NOT have been called.
    mock_chat.assert_not_called()

    # Alert should have been created and routed to the principal approver.
    alerts = alert_store.list_alerts()
    assert len(alerts) == 1
    assert alerts[0].routed_to_person_id == approver_id
    assert "department:finance" in alerts[0].topic_tags
    # The "If you approve:" line is now a concrete executive action,
    # not the old circular "Review and approve or reject this action."
    assert alerts[0].suggested_action.startswith("Send this")
    assert "review and approve" not in alerts[0].suggested_action.lower()

    # Action should be marked done (not failed or pending).
    updated = episodic.get_scheduled_action(action.id)
    assert updated is not None
    assert updated.status == "done"


# ---------------------------------------------------------------------------
# propose_only, approver outside window → deferred (rescheduled)
# ---------------------------------------------------------------------------

def test_propose_only_outside_window_reschedules() -> None:
    dept_store.seed_default_departments()
    dept_store.update_department("finance", authority_level=AuthorityLevel.PROPOSE_ONLY)
    # Approver with WILDCARD — gate finds them. They're only available Sundays.
    approver_id = people_store.upsert_person(full_name="Sarah", slack_user_id="U_CFO")
    people_store.set_authority_scope(approver_id, [AuthorityScope.WILDCARD])
    # Available only on Sundays 09-10 UTC; today is Tuesday (weekday 1).
    people_store.set_availability(approver_id, [
        AvailabilityWindow(weekdays=[6], start_local="09:00", end_local="10:00", timezone="UTC")
    ])
    dept_registry.invalidate()
    people_registry.invalidate()

    action = _make_action(department="finance")

    with patch(
        "openexecutive.orchestrator.executive.Executive.chat",
        new_callable=AsyncMock,
    ):
        asyncio.run(_execute_action(action, gateway=None))

    # Action should be rescheduled back to pending with a future run_at.
    updated = episodic.get_scheduled_action(action.id)
    assert updated is not None
    assert updated.status == "pending"
    assert updated.run_at > action.run_at


# ---------------------------------------------------------------------------
# __internal__ channel → no dispatch, action done
# ---------------------------------------------------------------------------

def test_internal_channel_bypasses_dispatch() -> None:
    action = _make_action(channel="__internal__", channel_ref="")

    with patch(
        "openexecutive.orchestrator.executive.Executive.chat",
        new_callable=AsyncMock,
    ) as mock_chat:
        asyncio.run(_execute_action(action, gateway=None))

    mock_chat.assert_not_called()

    updated = episodic.get_scheduled_action(action.id)
    assert updated is not None
    assert updated.status == "done"


# ---------------------------------------------------------------------------
# auto_execute → dispatches normally
# ---------------------------------------------------------------------------

def test_auto_execute_dispatches() -> None:
    dept_store.seed_default_departments()
    dept_store.update_department("operations", authority_level=AuthorityLevel.AUTO_EXECUTE)
    dept_registry.invalidate()

    action = _make_action(department="operations", channel="slack_dm", channel_ref="U123")

    with patch(
        "openexecutive.orchestrator.executive.Executive.chat",
        new_callable=AsyncMock,
    ) as mock_chat, patch(
        "openexecutive.onboarding.profile_builder.load_or_create_profile",
        return_value=MagicMock(is_empty=lambda: True),
    ), patch(
        "openexecutive.knowledge.retriever.retrieve",
        return_value="",
    ), patch(
        "openexecutive.memory.episodic.format_for_prompt",
        return_value="",
    ):
        asyncio.run(_execute_action(action, gateway=None))

    # Executive.chat should have been called (auto_execute dispatches).
    mock_chat.assert_called_once()


# ---------------------------------------------------------------------------
# escalate → urgent alert created, nothing dispatched until someone approves
# ---------------------------------------------------------------------------

def test_escalate_creates_urgent_alert_and_holds() -> None:
    dept_store.seed_default_departments()
    dept_store.update_department("legal", authority_level=AuthorityLevel.ESCALATE)
    principal_id = people_store.upsert_person(
        full_name="Founder", is_principal=True
    )
    people_store.set_authority_scope(principal_id, [AuthorityScope.WILDCARD])
    dept_registry.invalidate()
    people_registry.invalidate()

    action = _make_action(department="legal", channel="slack_dm", channel_ref="U123")

    with patch(
        "openexecutive.orchestrator.executive.Executive.chat",
        new_callable=AsyncMock,
    ) as mock_chat:
        asyncio.run(_execute_action(action, gateway=None))

    # Nothing is sent: "Escalates to a human" promises the specialist will not act.
    mock_chat.assert_not_called()

    # An urgent card went to the approver instead.
    alerts = alert_store.list_alerts()
    assert len(alerts) == 1
    assert alerts[0].headline.startswith("[ESCALATION]")
    assert alerts[0].severity == "high"  # ranks above routine proposals
    assert alerts[0].routed_to_person_id == principal_id
    assert "department:legal" in alerts[0].topic_tags
    # Escalation phrasing is the concrete action with an urgency tail — what
    # approving the card will do, not something already done.
    assert alerts[0].suggested_action.startswith("Send this")
    assert alerts[0].suggested_action.endswith("right away.")

    updated = episodic.get_scheduled_action(action.id)
    assert updated is not None
    assert updated.status == "done"


def test_escalate_is_not_deferred_to_approver_window() -> None:
    """Unlike propose, an escalation reaches the approver now, even off-hours."""
    dept_store.seed_default_departments()
    dept_store.update_department("legal", authority_level=AuthorityLevel.ESCALATE)
    approver_id = people_store.upsert_person(full_name="Sarah", slack_user_id="U_GC")
    people_store.set_authority_scope(approver_id, [AuthorityScope.WILDCARD])
    # Available only on Sundays 09-10 UTC. Whether or not that is now, an
    # escalation must not be rescheduled to the approver's next window.
    people_store.set_availability(approver_id, [
        AvailabilityWindow(weekdays=[6], start_local="09:00", end_local="10:00", timezone="UTC")
    ])
    dept_registry.invalidate()
    people_registry.invalidate()

    action = _make_action(department="legal")

    with patch(
        "openexecutive.orchestrator.executive.Executive.chat",
        new_callable=AsyncMock,
    ) as mock_chat:
        asyncio.run(_execute_action(action, gateway=None))

    mock_chat.assert_not_called()
    alerts = alert_store.list_alerts()
    assert len(alerts) == 1
    assert alerts[0].routed_to_person_id == approver_id
    updated = episodic.get_scheduled_action(action.id)
    assert updated is not None
    assert updated.status == "done"  # not rescheduled back to pending


def test_escalate_with_nobody_to_ask_files_an_unrouted_card() -> None:
    """No approver and no principal: the held action must still reach the
    Briefing (which lists unrouted cards), never vanish with a log line."""
    dept_store.seed_default_departments()
    dept_store.update_department("legal", authority_level=AuthorityLevel.ESCALATE)
    dept_registry.invalidate()
    people_registry.invalidate()

    action = _make_action(department="legal")

    with patch(
        "openexecutive.orchestrator.executive.Executive.chat",
        new_callable=AsyncMock,
    ) as mock_chat:
        asyncio.run(_execute_action(action, gateway=None))

    mock_chat.assert_not_called()
    alerts = alert_store.list_alerts()
    assert len(alerts) == 1
    assert alerts[0].headline.startswith("[ESCALATION]")
    assert alerts[0].routed_to_person_id is None
    assert not any(t.startswith("person:") for t in alerts[0].topic_tags)
    updated = episodic.get_scheduled_action(action.id)
    assert updated is not None
    assert updated.status == "done"


def _escalate(intent_text: str) -> None:
    episodic.insert_scheduled_action(
        run_at=(_now() - timedelta(seconds=10)).isoformat(),
        channel="slack_dm",
        channel_ref="U123",
        intent_text=intent_text,
        department="legal",
    )
    (action,) = episodic.claim_due_actions(_now())
    with patch(
        "openexecutive.orchestrator.executive.Executive.chat",
        new_callable=AsyncMock,
    ) as mock_chat:
        asyncio.run(_execute_action(action, gateway=None))
    mock_chat.assert_not_called()


def test_escalations_sharing_an_opening_are_separate_cards() -> None:
    """With nothing sent before approval, the card is the action's only trace:
    two escalations whose first 60 characters match must not dedup."""
    dept_store.seed_default_departments()
    dept_store.update_department("legal", authority_level=AuthorityLevel.ESCALATE)
    principal_id = people_store.upsert_person(full_name="Founder", is_principal=True)
    people_store.set_authority_scope(principal_id, [AuthorityScope.WILDCARD])
    dept_registry.invalidate()
    people_registry.invalidate()
    opening = "Send the NDA redline follow-up to Acme's counsel regarding section "

    _escalate(opening + "4")
    _escalate(opening + "7")
    _escalate(opening + "7")  # an identical repeat folds into the open card

    bodies = sorted(a.body for a in alert_store.list_alerts())
    assert bodies == [opening + "4", opening + "7"]


def test_repeat_escalation_after_its_card_was_handled_gets_a_new_card() -> None:
    dept_store.seed_default_departments()
    dept_store.update_department("legal", authority_level=AuthorityLevel.ESCALATE)
    principal_id = people_store.upsert_person(full_name="Founder", is_principal=True)
    people_store.set_authority_scope(principal_id, [AuthorityScope.WILDCARD])
    dept_registry.invalidate()
    people_registry.invalidate()

    _escalate("Department check-in: Legal")
    (first,) = alert_store.list_alerts()
    alert_store.set_status(first.id, "ack")  # the principal approved it

    _escalate("Department check-in: Legal")  # next time: must not be swallowed

    cards = alert_store.list_alerts()
    assert len(cards) == 2
    assert sorted(c.status for c in cards) == ["ack", "unread"]


def test_escalation_is_retried_not_marked_done_when_its_card_cannot_be_filed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dept_store.seed_default_departments()
    dept_store.update_department("legal", authority_level=AuthorityLevel.ESCALATE)
    principal_id = people_store.upsert_person(full_name="Founder", is_principal=True)
    people_store.set_authority_scope(principal_id, [AuthorityScope.WILDCARD])
    dept_registry.invalidate()
    people_registry.invalidate()

    def _store_down(**_kwargs: object) -> int:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(alert_store, "insert_alert", _store_down)
    action = _make_action(department="legal")

    with patch(
        "openexecutive.orchestrator.executive.Executive.chat",
        new_callable=AsyncMock,
    ) as mock_chat:
        asyncio.run(_execute_action(action, gateway=None))

    mock_chat.assert_not_called()
    updated = episodic.get_scheduled_action(action.id)
    assert updated is not None
    assert updated.status == "pending"  # rescheduled with backoff, not lost
    assert updated.attempts == 1
    assert "escalation card could not be filed" in updated.last_error


# ---------------------------------------------------------------------------
# No department → dispatch proceeds normally (gate is bypassed)
# ---------------------------------------------------------------------------

def test_no_department_bypasses_gate() -> None:
    action = _make_action(department="", channel="slack_dm", channel_ref="U123")

    with patch(
        "openexecutive.orchestrator.executive.Executive.chat",
        new_callable=AsyncMock,
    ) as mock_chat, patch(
        "openexecutive.onboarding.profile_builder.load_or_create_profile",
        return_value=MagicMock(is_empty=lambda: True),
    ), patch(
        "openexecutive.knowledge.retriever.retrieve",
        return_value="",
    ), patch(
        "openexecutive.memory.episodic.format_for_prompt",
        return_value="",
    ):
        asyncio.run(_execute_action(action, gateway=None))

    mock_chat.assert_called_once()
