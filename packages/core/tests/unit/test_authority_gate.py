"""Tests for the authority gate (departments/authority.py)."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from openexecutive.departments import registry as dept_registry
from openexecutive.departments import store as dept_store
from openexecutive.departments.authority import gate_action, propose_via_alert
from openexecutive.departments.models import AuthorityLevel
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store
from openexecutive.people.models import AuthorityScope, AvailabilityWindow

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every test its own SQLite DB and a clean registry cache."""
    db = tmp_path / "test.db"
    monkeypatch.setattr(dept_store, "DB_PATH", db)
    monkeypatch.setattr(people_store, "DB_PATH", db)

    from openexecutive.alerts import store as alert_store
    monkeypatch.setattr(alert_store, "DB_PATH", db)

    dept_registry.invalidate()
    people_registry.invalidate()

    dept_store.initialize_db(db)
    people_store.initialize_db(db)
    alert_store.initialize_db(db)

    yield

    dept_registry.invalidate()
    people_registry.invalidate()


_NOW = datetime(2026, 5, 19, 10, 0, tzinfo=UTC)  # Tuesday 10:00 UTC


def _set_dept_level(slug: str, level: AuthorityLevel) -> None:
    """Seed all departments (idempotent) and set the authority level for one."""
    dept_store.seed_default_departments()
    dept_store.update_department(slug, authority_level=level)
    dept_registry.invalidate()


# ---------------------------------------------------------------------------
# AUTO_EXECUTE
# ---------------------------------------------------------------------------

def test_auto_execute_returns_execute() -> None:
    _set_dept_level("operations", AuthorityLevel.AUTO_EXECUTE)
    decision = gate_action("operations", "ad_hoc", now=_NOW)
    assert decision.action == "execute"
    assert decision.allowed is True


def test_has_user_consent_bypasses_gate() -> None:
    """Even PROPOSE_ONLY departments execute immediately when user gave consent."""
    _set_dept_level("finance", AuthorityLevel.PROPOSE_ONLY)
    decision = gate_action("finance", "ad_hoc", has_user_consent=True, now=_NOW)
    assert decision.action == "execute"
    assert decision.allowed is True


# ---------------------------------------------------------------------------
# PROPOSE_ONLY
# ---------------------------------------------------------------------------

def test_propose_only_routes_to_correct_person() -> None:
    _set_dept_level("finance", AuthorityLevel.PROPOSE_ONLY)

    cfo_id = people_store.upsert_person(full_name="Sarah Chen", role="CFO (fractional)")
    people_store.set_authority_scope(cfo_id, [AuthorityScope.SPEND_GT_10K])

    decision = gate_action(
        "finance", "ad_hoc",
        required_scope=AuthorityScope.SPEND_GT_10K,
        now=_NOW,
    )
    assert decision.action == "propose"
    assert decision.allowed is False
    assert decision.assignee_person_id == cfo_id


def test_propose_only_no_approvers_falls_back_to_principal() -> None:
    _set_dept_level("finance", AuthorityLevel.PROPOSE_ONLY)
    principal_id = people_store.upsert_person(
        full_name="Founder", role="CEO", is_principal=True
    )
    people_store.set_authority_scope(principal_id, [AuthorityScope.WILDCARD])

    decision = gate_action(
        "finance", "ad_hoc",
        required_scope=AuthorityScope.SPEND_GT_10K,
        now=_NOW,
    )
    assert decision.action == "propose"
    assert decision.assignee_person_id == principal_id


def test_propose_only_approver_inside_window_has_no_deliver_at() -> None:
    """When the approver is available right now, deliver_at should be None."""
    _set_dept_level("finance", AuthorityLevel.PROPOSE_ONLY)
    cfo_id = people_store.upsert_person(full_name="Sarah")
    people_store.set_authority_scope(cfo_id, [AuthorityScope.SPEND_GT_10K])
    # Tuesday 09:00-13:00 UTC — _NOW is Tuesday 10:00 UTC, inside window
    people_store.set_availability(cfo_id, [
        AvailabilityWindow(weekdays=[1], start_local="09:00", end_local="13:00", timezone="UTC")
    ])
    people_registry.invalidate()

    decision = gate_action(
        "finance", "ad_hoc",
        required_scope=AuthorityScope.SPEND_GT_10K,
        now=_NOW,
    )
    assert decision.action == "propose"
    assert decision.deliver_at is None


def test_propose_only_approver_outside_window_sets_deliver_at() -> None:
    """When the approver is outside their window, deliver_at is set to next slot."""
    _set_dept_level("finance", AuthorityLevel.PROPOSE_ONLY)
    cfo_id = people_store.upsert_person(full_name="Sarah")
    people_store.set_authority_scope(cfo_id, [AuthorityScope.SPEND_GT_10K])
    # Tuesday only, 09:00-10:00 UTC. _NOW is 10:00 — just AFTER window.
    people_store.set_availability(cfo_id, [
        AvailabilityWindow(weekdays=[1], start_local="09:00", end_local="10:00", timezone="UTC")
    ])
    people_registry.invalidate()

    decision = gate_action(
        "finance", "ad_hoc",
        required_scope=AuthorityScope.SPEND_GT_10K,
        now=_NOW,
    )
    assert decision.action == "propose"
    assert decision.deliver_at is not None
    assert decision.deliver_at > _NOW


# ---------------------------------------------------------------------------
# ESCALATE
# ---------------------------------------------------------------------------

def test_escalate_routes_and_is_not_allowed() -> None:
    """ESCALATE routes to an approver and is NOT allowed to execute: the level
    promises "the specialist will not act" until a person approves."""
    _set_dept_level("legal", AuthorityLevel.ESCALATE)
    principal_id = people_store.upsert_person(
        full_name="Founder", is_principal=True
    )
    people_store.set_authority_scope(principal_id, [AuthorityScope.WILDCARD])

    decision = gate_action("legal", "ad_hoc", now=_NOW)
    assert decision.action == "escalate"
    assert decision.allowed is False
    assert decision.assignee_person_id == principal_id


def test_escalate_with_no_approver_is_not_allowed() -> None:
    """With nobody to ask, an escalation still must not execute."""
    _set_dept_level("legal", AuthorityLevel.ESCALATE)

    decision = gate_action("legal", "ad_hoc", now=_NOW)
    assert decision.action == "escalate"
    assert decision.allowed is False
    assert decision.assignee_person_id is None


# ---------------------------------------------------------------------------
# Missing department
# ---------------------------------------------------------------------------

def test_unknown_department_falls_back_to_propose() -> None:
    """Gate must not raise for unknown departments — falls back to propose."""
    # No people seeded, no departments — gate should produce propose with None assignee.
    decision = gate_action("nonexistent", "ad_hoc", now=_NOW)
    assert decision.action == "propose"
    assert decision.assignee_person_id is None


def test_unknown_department_with_principal_routes_to_principal() -> None:
    principal_id = people_store.upsert_person(
        full_name="Founder", is_principal=True
    )
    people_store.set_authority_scope(principal_id, [AuthorityScope.WILDCARD])
    people_registry.invalidate()

    decision = gate_action("nonexistent", "ad_hoc", now=_NOW)
    assert decision.action == "propose"
    assert decision.assignee_person_id == principal_id


# ---------------------------------------------------------------------------
# Non-principal sorted before principal (wildcard)
# ---------------------------------------------------------------------------

def test_propose_prefers_delegated_human_over_principal() -> None:
    _set_dept_level("finance", AuthorityLevel.PROPOSE_ONLY)

    principal_id = people_store.upsert_person(
        full_name="Founder", role="CEO", is_principal=True
    )
    people_store.set_authority_scope(principal_id, [AuthorityScope.WILDCARD])

    cfo_id = people_store.upsert_person(
        full_name="Sarah CFO", role="CFO", is_principal=False, response_sla_hours=4
    )
    people_store.set_authority_scope(cfo_id, [AuthorityScope.SPEND_GT_10K])

    decision = gate_action(
        "finance", "ad_hoc",
        required_scope=AuthorityScope.SPEND_GT_10K,
        now=_NOW,
    )
    assert decision.assignee_person_id == cfo_id
    assert decision.assignee_person_id != principal_id


# ---------------------------------------------------------------------------
# propose_via_alert
# ---------------------------------------------------------------------------

def test_propose_via_alert_creates_alert() -> None:
    from openexecutive.alerts import store as alert_store

    _set_dept_level("finance", AuthorityLevel.PROPOSE_ONLY)
    cfo_id = people_store.upsert_person(full_name="Sarah")

    alert_id = propose_via_alert(
        department_slug="finance",
        person_id=cfo_id,
        summary="Vendor renegotiation over $10K",
        body="Finance dept proposes vendor renegotiation for $15K.",
        suggested_action="Approve or reject.",
    )
    assert alert_id is not None

    alert = alert_store.get_alert(alert_id)
    assert alert is not None
    assert "department:finance" in alert.topic_tags
    assert f"person:{cfo_id}" in alert.topic_tags
    assert alert.routed_to_person_id == cfo_id
    assert alert.severity == "medium"


def test_propose_via_alert_dedup() -> None:
    """A duplicate proposal (same dedup_key) should return None."""
    _set_dept_level("finance", AuthorityLevel.PROPOSE_ONLY)
    cfo_id = people_store.upsert_person(full_name="Sarah")

    first = propose_via_alert("finance", cfo_id, "Same summary", "body")
    assert first is not None
    second = propose_via_alert("finance", cfo_id, "Same summary", "body")
    assert second is None  # duplicate suppressed


def test_propose_via_alert_suffix_coalesces_then_reissues() -> None:
    """With an external_id_suffix the card is recurring: an open one is
    refreshed in place, an acknowledged one is left alone until the suffix
    changes."""
    from openexecutive.alerts import store as alert_store

    _set_dept_level("finance", AuthorityLevel.PROPOSE_ONLY)
    cfo_id = people_store.upsert_person(full_name="Sarah")

    first = propose_via_alert("finance", cfo_id, "Watch suggestions", "body one",
                              external_id_suffix="2026-W37")
    assert first is not None
    # Open card: refreshed, no new row.
    assert propose_via_alert("finance", cfo_id, "Watch suggestions", "body two",
                             external_id_suffix="2026-W37") is None
    refreshed = alert_store.get_alert(first)
    assert refreshed is not None and refreshed.body == "body two" and refreshed.occurrence_count == 2
    # Acknowledged: same week stays quiet, next week mints a new card.
    alert_store.set_status(first, "acknowledged")
    assert propose_via_alert("finance", cfo_id, "Watch suggestions", "body three",
                             external_id_suffix="2026-W37") is None
    nxt = propose_via_alert("finance", cfo_id, "Watch suggestions", "body four",
                            external_id_suffix="2026-W38")
    assert nxt is not None and nxt != first
