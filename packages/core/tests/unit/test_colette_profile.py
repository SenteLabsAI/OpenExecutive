"""Tests for departments/colette_profile.py (the Colette operating overlay)."""
from __future__ import annotations

from pathlib import Path

import pytest

from openexecutive.departments import colette_profile as cp
from openexecutive.departments import registry as dept_registry
from openexecutive.departments import store as dept_store
from openexecutive.departments.cadence import bootstrap_cadences
from openexecutive.departments.charters import DEFAULT_CHECK_IN_CADENCE
from openexecutive.departments.models import DepartmentCharter
from openexecutive.memory import episodic
from openexecutive.people import store as people_store
from openexecutive.people.models import AuthorityScope


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "profile.db"
    monkeypatch.setattr(dept_store, "DB_PATH", path)
    monkeypatch.setattr(episodic, "DB_PATH", path)
    monkeypatch.setattr(people_store, "DB_PATH", path)
    # No roster.yaml leaks in from the developer's real company/ directory.
    monkeypatch.setattr(cp, "_roster_overrides", lambda: {})
    dept_registry.invalidate()
    episodic.initialize_db(path)
    people_store.initialize_db(path)
    dept_store.initialize_db(path)
    dept_store.seed_default_departments(db_path=path)
    yield path
    dept_registry.invalidate()


def _people(db: Path) -> dict[str, object]:
    return {p.full_name: p for p in people_store.list_people(db_path=db)}


def test_apply_creates_roster_with_principal_and_scopes(db: Path) -> None:
    report = cp.apply_colette_profile(db_path=db)

    assert set(report.people_created) == {"West", "Wajnberg", "Dingus"}
    people = _people(db)
    assert people["West"].is_principal is True
    assert AuthorityScope.WILDCARD in people["West"].authority_scope
    assert people["Wajnberg"].is_principal is False
    assert people["Wajnberg"].reports_to_person_id == people["West"].id
    assert AuthorityScope.VENDOR_ONBOARDING in people["Dingus"].authority_scope


def test_apply_sets_owners_specialists_cadences_and_goals(db: Path) -> None:
    cp.apply_colette_profile(db_path=db)
    people = _people(db)

    finance = dept_store.get_department("finance", db_path=db)
    ops = dept_store.get_department("operations", db_path=db)
    marketing = dept_store.get_department("marketing", db_path=db)
    product = dept_store.get_department("product", db_path=db)
    strategy = dept_store.get_department("strategy", db_path=db)
    assert all(d is not None for d in (finance, ops, marketing, product, strategy))

    assert finance.config.head_person_id == people["West"].id
    assert marketing.config.head_person_id == people["Wajnberg"].id
    assert product.config.head_person_id == people["Dingus"].id
    # The humans own marketing/product; the partner seats do not replace them.
    assert marketing.config.specialist_key == "cmo"
    assert product.config.specialist_key == "cpo"
    # Strategy moved off the unregistered generic CSO onto the CEO advisor.
    assert strategy.config.specialist_key == "ceo_advisor"

    assert finance.config.cadences["check_in"] == "weekly@mon-13:00"
    assert ops.config.cadences["check_in"] == "weekly@thu-13:00"
    assert finance.config.charter.scope != []
    assert "cost-to-serve" in " ".join(finance.config.charter.scope)
    assert len(finance.goals) == 4
    assert len(ops.goals) == 6
    assert all(g.status == "on_track" and g.current == "" for g in ops.goals)


def test_unrunnable_departments_stop_checking_in(db: Path) -> None:
    before = dept_store.get_department("hr", db_path=db)
    assert before.config.cadences["check_in"] == DEFAULT_CHECK_IN_CADENCE

    report = cp.apply_colette_profile(db_path=db)

    assert set(report.cadences_cleared) == {"hr", "board_comms"}
    assert "check_in" not in dept_store.get_department("hr", db_path=db).config.cadences
    assert "check_in" not in dept_store.get_department("board_comms", db_path=db).config.cadences
    # Every department that still has a cadence points at a registered seat.
    registered = cp._registered_specialists()
    for state in dept_store.list_departments(db_path=db):
        if state.config.cadences.get("check_in"):
            assert state.config.specialist_key in registered, state.config.slug


def test_second_apply_is_skipped_and_preserves_team_edits(db: Path) -> None:
    cp.apply_colette_profile(db_path=db)
    dept_store.update_department(
        "finance",
        charter=DepartmentCharter(mission="Edited by the team", scope=["x"], out_of_scope=[]),
        cadences={"check_in": "weekly@tue-10:00"},
        db_path=db,
    )
    goal = dept_store.list_goals("finance", db_path=db)[0]
    dept_store.update_goal(goal.id, current="14 months", db_path=db)

    report = cp.apply_colette_profile(db_path=db)
    assert report.skipped is True

    forced = cp.apply_colette_profile(force=True, db_path=db)
    assert forced.skipped is False
    assert forced.people_created == []
    assert forced.goals_added == 0
    finance = dept_store.get_department("finance", db_path=db)
    assert finance.config.charter.mission == "Edited by the team"
    assert finance.config.cadences["check_in"] == "weekly@tue-10:00"
    assert dept_store.list_goals("finance", db_path=db)[0].current == "14 months"
    assert len(_people(db)) == 3


def test_existing_principal_is_adopted_not_duplicated(db: Path) -> None:
    people_store.upsert_person(full_name="Someone Else", role="Founder", is_principal=True, db_path=db)

    cp.apply_colette_profile(db_path=db)

    principals = [p for p in people_store.list_people(db_path=db) if p.is_principal]
    assert [p.full_name for p in principals] == ["Someone Else"]
    assert "West" not in _people(db)
    # Departments the CEO would own still get a head: the adopted principal.
    finance = dept_store.get_department("finance", db_path=db)
    assert finance.config.head_person_id == principals[0].id


def test_deleted_department_stays_deleted(db: Path) -> None:
    dept_store.delete_department("legal", db_path=db)

    cp.apply_colette_profile(db_path=db)

    assert dept_store.get_department("legal", db_path=db) is None


def test_roster_overrides_supply_contact_data_but_only_allowed_fields(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cp,
        "_roster_overrides",
        lambda: {
            "cmo": {
                "full_name": "Alex Wajnberg",
                "email": "cmo@example.com",
                "is_principal": True,  # must be ignored
                "authority_scope": ["wildcard"],  # must be ignored
            }
        },
    )

    cp.apply_colette_profile(db_path=db)

    cmo = _people(db)["Alex Wajnberg"]
    assert cmo.email == "cmo@example.com"
    assert cmo.is_principal is False
    assert AuthorityScope.WILDCARD not in cmo.authority_scope


def test_changed_cadence_cancels_stale_pending_occurrence(db: Path) -> None:
    # Upstream seeding enqueues the daily@09:00 occurrence first.
    assert bootstrap_cadences(db_path=db) > 0
    with episodic._get_conn(db) as conn:
        pending_before = conn.execute(
            "SELECT COUNT(*) AS n FROM scheduled_actions "
            "WHERE kind='dept_cadence' AND status='pending' AND department='finance'"
        ).fetchone()["n"]
    assert pending_before == 1

    cp.apply_colette_profile(db_path=db)
    bootstrap_cadences(db_path=db)

    with episodic._get_conn(db) as conn:
        rows = conn.execute(
            "SELECT run_at, status FROM scheduled_actions "
            "WHERE kind='dept_cadence' AND department='finance' ORDER BY id"
        ).fetchall()
    statuses = [r["status"] for r in rows]
    assert statuses.count("pending") == 1 and "cancelled" in statuses
    pending_run_at = next(r["run_at"] for r in rows if r["status"] == "pending")
    assert "T13:00" in pending_run_at  # weekly@mon-13:00, not the old 09:00
    # HR/board_comms have no pending cadence rows left.
    with episodic._get_conn(db) as conn:
        left = conn.execute(
            "SELECT COUNT(*) AS n FROM scheduled_actions WHERE kind='dept_cadence' "
            "AND status='pending' AND department IN ('hr','board_comms')"
        ).fetchone()["n"]
    assert left == 0


def test_profile_cadence_specs_are_parseable() -> None:
    from datetime import UTC, datetime

    from openexecutive.departments.cadence import _parse_cadence_spec

    now = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
    for profile in cp.PROFILES:
        assert _parse_cadence_spec(profile.check_in_cadence, now) is not None, profile.slug


def test_every_profile_seat_is_registered_and_owner_resolves() -> None:
    registered = cp._registered_specialists()
    roster_keys = {s.key for s in cp.ROSTER}
    for profile in cp.PROFILES:
        assert profile.specialist_key in registered, profile.slug
        assert profile.owner in roster_keys, profile.slug
        assert profile.goals, profile.slug
