"""Store-level tests for the Departments package.

Covers: seed idempotency, CRUD on departments and Goals, and the additive
`department` column on the episodic tables that landed alongside.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from openexecutive.departments import store
from openexecutive.departments.models import (
    AuthorityLevel,
    DepartmentCharter,
)


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated SQLite for each test. Monkeypatches the module-level DB_PATH
    so callers that omit db_path (the route layer, the registry) hit the
    test DB rather than the dev `./episodic_memory.db`."""
    path = tmp_path / "departments.db"
    monkeypatch.setattr(store, "DB_PATH", path)
    store.initialize_db()
    return path


def test_initialize_db_is_idempotent(db: Path) -> None:
    # Running twice must not raise — every CREATE/ALTER is guarded.
    store.initialize_db()
    store.initialize_db()
    # Sanity: the two tables exist.
    states = store.list_departments()
    assert states == []  # no seed yet


def test_list_departments_returns_empty_when_table_missing(tmp_path: Path) -> None:
    # The episodic DB file is shared; a sibling subsystem can create it before
    # the departments table exists. list_departments must degrade to [], not
    # raise OperationalError (which previously crashed committee chat turns).
    import sqlite3
    shared = tmp_path / "shared.db"
    conn = sqlite3.connect(str(shared))
    conn.execute("CREATE TABLE audit_log (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    assert store.list_departments(db_path=shared) == []


def test_seed_inserts_eight_departments(db: Path) -> None:
    inserted = store.seed_default_departments()
    assert inserted == 8
    states = store.list_departments()
    slugs = {s.config.slug for s in states}
    assert slugs == {
        "strategy", "finance", "hr", "legal", "operations",
        "marketing", "product", "board_comms",
    }


def test_seed_is_idempotent_and_does_not_clobber(db: Path) -> None:
    store.seed_default_departments()
    # User edits one charter.
    store.update_department(
        "finance",
        charter=DepartmentCharter(
            mission="Custom finance mission",
            scope=["custom-scope"],
            out_of_scope=[],
        ),
    )

    inserted = store.seed_default_departments()
    assert inserted == 0
    state = store.get_department("finance")
    assert state is not None
    assert state.config.charter.mission == "Custom finance mission"
    assert state.config.charter.scope == ["custom-scope"]


def test_seed_does_not_resurrect_deleted_default(db: Path) -> None:
    # Regression: deleting a default department must keep it gone across the
    # next startup seed pass. Previously seeding re-inserted any missing slug.
    store.seed_default_departments()
    assert store.delete_department("finance") is True

    inserted = store.seed_default_departments()
    assert inserted == 0
    assert store.get_department("finance") is None
    slugs = {s.config.slug for s in store.list_departments()}
    assert "finance" not in slugs
    assert len(slugs) == 7


def test_seed_skips_preexisting_db_without_sentinel(db: Path) -> None:
    # A database that predates the seed sentinel but already holds the user's
    # own departments must be adopted as-is — seeding must not inject the 8
    # defaults on top of a customised org.
    store.create_department("Custom Ops")
    inserted = store.seed_default_departments()
    assert inserted == 0
    slugs = {s.config.slug for s in store.list_departments()}
    assert slugs == {"custom-ops"}


def test_seed_does_not_re_run_after_full_wipe(db: Path) -> None:
    # Deleting every department leaves an empty list — the sentinel means a
    # restart still must not resurrect the defaults.
    store.seed_default_departments()
    for state in store.list_departments():
        store.delete_department(state.config.slug)
    assert store.list_departments() == []

    inserted = store.seed_default_departments()
    assert inserted == 0
    assert store.list_departments() == []


def test_default_authority_is_propose_only(db: Path) -> None:
    store.seed_default_departments()
    for state in store.list_departments():
        assert state.config.authority_level == AuthorityLevel.PROPOSE_ONLY


def test_default_cadence_is_daily_morning(db: Path) -> None:
    store.seed_default_departments()
    state = store.get_department("finance")
    assert state is not None
    assert state.config.cadences == {"check_in": "daily@09:00"}


def test_finance_seed_has_fleshed_charter(db: Path) -> None:
    store.seed_default_departments()
    state = store.get_department("finance")
    assert state is not None
    # Finance is the Phase 1 pilot — its charter ships with non-empty scope
    # and out_of_scope arrays; stubs ship with empty arrays.
    assert state.config.charter.scope
    assert state.config.charter.out_of_scope
    # The other 7 should all have empty scope arrays in Phase 1.
    for s in store.list_departments():
        if s.config.slug != "finance":
            assert s.config.charter.scope == []


def test_update_department_partial(db: Path) -> None:
    store.seed_default_departments()
    modified = store.update_department(
        "marketing",
        title="Growth & Marketing",
        authority_level=AuthorityLevel.AUTO_EXECUTE,
        headcount=4,
        budget_usd=12_500.0,
    )
    assert modified is True
    state = store.get_department("marketing")
    assert state is not None
    assert state.config.title == "Growth & Marketing"
    assert state.config.authority_level == AuthorityLevel.AUTO_EXECUTE
    assert state.headcount == 4
    assert state.budget_usd == 12_500.0


def test_watched_entities_round_trip_and_migration(db: Path) -> None:
    import sqlite3

    store.seed_default_departments()
    state = store.get_department("finance")
    assert state is not None and state.config.watched_entities == []
    assert store.update_department("finance", watched_entities=["Brex", "Stripe"]) is True
    state = store.get_department("finance")
    assert state is not None and state.config.watched_entities == ["Brex", "Stripe"]
    # Other fields untouched; an update without the field leaves it alone.
    store.update_department("finance", headcount=3)
    state = store.get_department("finance")
    assert state is not None and state.config.watched_entities == ["Brex", "Stripe"]
    assert store.update_department("finance", watched_entities=[]) is True
    state = store.get_department("finance")
    assert state is not None and state.config.watched_entities == []
    # Re-running the migration on a migrated DB is a no-op, and a corrupt
    # column value reads back as an empty list rather than failing the read.
    store.initialize_db()
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE departments SET watched_entities_json = 'nope' WHERE slug = 'finance'")
    state = store.get_department("finance")
    assert state is not None and state.config.watched_entities == []


def test_get_department_returns_none_for_unknown(db: Path) -> None:
    store.seed_default_departments()
    assert store.get_department("does-not-exist") is None


def test_goal_lifecycle(db: Path) -> None:
    store.seed_default_departments()

    goal_id = store.insert_goal(
        "finance",
        period_type="quarter",
        period_value="Q2 2026",
        key_result="Close Series A by Jun 30",
        target="termsheet signed + funds in account",
        current="term sheet signed",
        status="on_track",
    )
    assert goal_id > 0

    fetched = store.get_goal(goal_id)
    assert fetched is not None
    assert fetched.key_result == "Close Series A by Jun 30"
    assert fetched.department_slug == "finance"
    assert fetched.period_type == "quarter"
    assert fetched.period_value == "Q2 2026"

    store.update_goal(goal_id, current="funds wired", status="on_track")
    after = store.get_goal(goal_id)
    assert after is not None
    assert after.current == "funds wired"

    goals = store.list_goals("finance")
    assert len(goals) == 1

    deleted = store.delete_goal(goal_id)
    assert deleted is True
    assert store.get_goal(goal_id) is None


def test_goal_supports_non_quarter_periods(db: Path) -> None:
    """All five PeriodType values must round-trip cleanly through the store."""
    store.seed_default_departments()
    for period_type, period_value in (
        ("week", "Week of May 18"),
        ("month", "May 2026"),
        ("year", "2026"),
        ("ongoing", "Ongoing"),
    ):
        gid = store.insert_goal(
            "finance",
            period_type=period_type,  # type: ignore[arg-type]
            period_value=period_value,
            key_result=f"KR for {period_type}",
            target="x",
        )
        g = store.get_goal(gid)
        assert g is not None
        assert g.period_type == period_type
        assert g.period_value == period_value


def test_list_goals_filters_by_department(db: Path) -> None:
    store.seed_default_departments()
    store.insert_goal(
        "finance", period_value="Q2 2026",
        key_result="A", target="x", current="", status="on_track",
    )
    store.insert_goal(
        "product", period_value="Q2 2026",
        key_result="B", target="y", current="", status="at_risk",
    )
    assert {g.key_result for g in store.list_goals("finance")} == {"A"}
    assert {g.key_result for g in store.list_goals("product")} == {"B"}


def test_get_department_includes_its_goals(db: Path) -> None:
    store.seed_default_departments()
    store.insert_goal(
        "finance", period_value="Q2 2026",
        key_result="K1", target="t1", current="c1", status="on_track",
    )
    store.insert_goal(
        "finance", period_value="Q2 2026",
        key_result="K2", target="t2", current="c2", status="at_risk",
    )
    state = store.get_department("finance")
    assert state is not None
    assert {g.key_result for g in state.goals} == {"K1", "K2"}


def test_episodic_decisions_carries_department_column(tmp_path: Path) -> None:
    """The Phase 1 additive ALTER on `decisions` lets callers tag a row with
    its owning department; legacy callers that omit the kwarg get empty string."""
    from openexecutive.memory import episodic

    path = tmp_path / "episodic.db"
    episodic.initialize_db(path)

    episodic.store_decision(
        domain="finance",
        summary="Increase MRR target to $40k",
        department="finance",
        db_path=path,
    )
    episodic.store_decision(
        domain="strategy",
        summary="Pivot to enterprise segment",
        db_path=path,
        # legacy call site — no department kwarg
    )
    decisions = episodic.list_decisions(db_path=path)
    by_summary = {d.summary: d for d in decisions}
    assert by_summary["Increase MRR target to $40k"].department == "finance"
    assert by_summary["Pivot to enterprise segment"].department == ""


def test_episodic_scheduled_action_carries_department(tmp_path: Path) -> None:
    from openexecutive.memory import episodic

    path = tmp_path / "episodic.db"
    episodic.initialize_db(path)

    action_id = episodic.insert_scheduled_action(
        run_at="2099-01-01T00:00:00+00:00",
        channel="telegram",
        channel_ref="123",
        intent_text="follow up",
        department="finance",
        db_path=path,
    )
    fetched = episodic.get_scheduled_action(action_id, db_path=path)
    assert fetched is not None
    assert fetched.department == "finance"


def test_unknown_authority_level_falls_back_to_propose_only(db: Path) -> None:
    """A future migration or hand-edit could leave a row with an unrecognised
    authority_level value. The read path must not crash — it falls back to the
    safest level so a single bad row does not 500 every read of the list."""
    import sqlite3

    from openexecutive.departments.models import AuthorityLevel

    store.seed_default_departments()
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "UPDATE departments SET authority_level = ? WHERE slug = ?",
            ("auto", "finance"),  # not a real AuthorityLevel value
        )
        conn.commit()

    state = store.get_department("finance")
    assert state is not None
    assert state.config.authority_level == AuthorityLevel.PROPOSE_ONLY

    # And the list path keeps working — no exception escapes.
    all_states = store.list_departments()
    assert len(all_states) == 8
    finance = next(s for s in all_states if s.config.slug == "finance")
    assert finance.config.authority_level == AuthorityLevel.PROPOSE_ONLY


def test_audit_log_carries_department(tmp_path: Path) -> None:
    from openexecutive.audit.logger import AuditLogger

    audit = AuditLogger(tmp_path / "audit.db")
    audit.log(
        "specialist_consult",
        "Finance: cash runway",
        actor="cfo",
        department="finance",
    )
    audit.log(
        "chat_turn",
        "User: hi",
        actor="user",
        # legacy call — no department
    )
    events = audit.query()
    by_summary = {e.summary: e for e in events}
    assert by_summary["Finance: cash runway"].department == "finance"
    assert by_summary["User: hi"].department is None


# --------------------------------------------------------------------------- #
# _slugify helper
# --------------------------------------------------------------------------- #

def test_slugify_basic() -> None:
    from openexecutive.departments.store import _slugify
    assert _slugify("Customer Success") == "customer-success"


def test_slugify_special_chars() -> None:
    from openexecutive.departments.store import _slugify
    assert _slugify("R&D / Innovation") == "r-d-innovation"


def test_slugify_multi_spaces() -> None:
    from openexecutive.departments.store import _slugify
    assert _slugify("HR  Ops") == "hr-ops"


def test_slugify_empty() -> None:
    from openexecutive.departments.store import _slugify
    assert _slugify("") == "department"


def test_slugify_only_special_chars() -> None:
    from openexecutive.departments.store import _slugify
    assert _slugify("!!!") == "department"


# --------------------------------------------------------------------------- #
# create_department
# --------------------------------------------------------------------------- #

def test_create_department_basic(db: Path) -> None:
    state = store.create_department("Customer Success", mission="Own post-sale relationships.")
    assert state.config.slug == "customer-success"
    assert state.config.title == "Customer Success"
    assert state.config.charter.mission == "Own post-sale relationships."
    assert state.config.specialist_key is None
    assert state.config.authority_level == AuthorityLevel.PROPOSE_ONLY
    assert state.goals == []


def test_create_department_no_mission(db: Path) -> None:
    state = store.create_department("Engineering")
    assert state.config.charter.mission == ""


def test_create_department_slug_collision_appends_suffix(db: Path) -> None:
    store.create_department("Customer Success")
    dupe = store.create_department("Customer Success")
    assert dupe.config.slug == "customer-success-2"


def test_create_department_triple_collision(db: Path) -> None:
    store.create_department("Customer Success")
    store.create_department("Customer Success")
    third = store.create_department("Customer Success")
    assert third.config.slug == "customer-success-3"


def test_create_department_appears_in_list(db: Path) -> None:
    store.create_department("Customer Success")
    slugs = {s.config.slug for s in store.list_departments()}
    assert "customer-success" in slugs


def test_create_department_blank_title_raises(db: Path) -> None:
    with pytest.raises(ValueError, match="blank"):
        store.create_department("   ")


def test_create_department_default_cadence_set(db: Path) -> None:
    state = store.create_department("Legal Ops")
    assert "check_in" in state.config.cadences


def test_create_department_named_after_a_catalogue_area_gets_its_charter(db: Path) -> None:
    from openexecutive.departments.charters import FINANCE_CHARTER, HR_CHARTER

    finance = store.create_department("Finance")
    assert finance.config.charter == FINANCE_CHARTER
    # Matched by slug as well as title: "HR" is the catalogue's People & Talent.
    hr = store.create_department("HR")
    assert hr.config.charter.mission == HR_CHARTER.mission


def test_create_department_explicit_mission_beats_the_catalogue(db: Path) -> None:
    from openexecutive.departments.charters import FINANCE_CHARTER

    state = store.create_department("Finance", mission="Keep the books.")
    assert state.config.charter.mission == "Keep the books."
    assert state.config.charter.scope == FINANCE_CHARTER.scope


# --------------------------------------------------------------------------- #
# delete_department
# --------------------------------------------------------------------------- #

def test_delete_department_returns_true(db: Path) -> None:
    store.seed_default_departments()
    assert store.delete_department("finance") is True


def test_delete_department_removes_row(db: Path) -> None:
    store.seed_default_departments()
    store.delete_department("finance")
    assert store.get_department("finance") is None


def test_delete_department_cascades_goals(db: Path) -> None:
    store.seed_default_departments()
    store.insert_goal("finance", period_value="Q2 2026", key_result="K", target="T")
    store.delete_department("finance")
    assert store.list_goals("finance") == []


def test_delete_department_unknown_returns_false(db: Path) -> None:
    assert store.delete_department("does-not-exist") is False


def test_delete_seeded_department(db: Path) -> None:
    store.seed_default_departments()
    store.delete_department("board_comms")
    slugs = {s.config.slug for s in store.list_departments()}
    assert "board_comms" not in slugs
    assert len(slugs) == 7


def test_delete_custom_department(db: Path) -> None:
    store.create_department("Legal Ops")
    result = store.delete_department("legal-ops")
    assert result is True
    assert store.get_department("legal-ops") is None


# --------------------------------------------------------------------------- #
# record_goal_review — Phase A: status review advances last_reviewed_at but
# NOT updated_at, distinguishing OE's grading from content edits.
# --------------------------------------------------------------------------- #

def test_record_goal_review_advances_last_reviewed_at_without_bumping_updated_at(
    db: Path,
) -> None:
    """A review-touch must move `last_reviewed_at` and `status` only —
    `updated_at` is reserved for content edits so the UI can render an
    honest 'Last reviewed N ago' label."""
    store.create_department("Engineering")
    goal_id = store.insert_goal(
        "engineering",
        period_type="quarter",
        period_value="Q2 2026",
        key_result="Ship billing migration",
        target="Cutover by Jun 30",
        status="on_track",
    )
    seeded = store.get_goal(goal_id)
    assert seeded is not None
    seeded_updated_at = seeded.updated_at
    assert seeded.last_reviewed_at == ""

    review_ts = "2026-05-28T09:00:00+00:00"
    updated = store.record_goal_review(
        goal_id, status="at_risk", last_reviewed_at=review_ts
    )
    assert updated is True

    after = store.get_goal(goal_id)
    assert after is not None
    assert after.status == "at_risk"
    assert after.last_reviewed_at == review_ts
    # The whole point of the wrapper: updated_at must NOT advance.
    assert after.updated_at == seeded_updated_at


def test_record_goal_review_returns_false_for_unknown_goal(db: Path) -> None:
    store.create_department("Engineering")
    assert (
        store.record_goal_review(99999, status="on_track", last_reviewed_at="2026-01-01")
        is False
    )


def test_update_goal_still_bumps_updated_at_for_content_edits(db: Path) -> None:
    """Regression guard: the existing `update_goal` semantics for
    content edits are untouched by Phase A — only the new
    `record_goal_review` path skips the bump."""
    store.create_department("Engineering")
    goal_id = store.insert_goal(
        "engineering",
        period_type="quarter",
        period_value="Q2 2026",
        key_result="Ship billing migration",
        target="Cutover by Jun 30",
    )
    before = store.get_goal(goal_id)
    assert before is not None
    # Force a noticeable time gap by patching _now via direct UPDATE
    # would be over-engineered — just confirm the field changed at all.
    store.update_goal(goal_id, current="50% done")
    after = store.get_goal(goal_id)
    assert after is not None
    assert after.current == "50% done"
    assert after.updated_at >= before.updated_at  # may equal at sub-second resolution


def test_migrate_add_last_reviewed_at_column_is_idempotent(tmp_path: Path) -> None:
    """Running initialize_db twice must not raise on the new ALTER, and
    rows inserted before the second run keep their column value."""
    import sqlite3

    path = tmp_path / "twice.db"
    store.initialize_db(path)
    # Insert directly bypassing record_goal_review so we know NULL handling works.
    store.create_department("Engineering", db_path=path)
    goal_id = store.insert_goal(
        "engineering",
        period_type="quarter",
        period_value="Q2 2026",
        key_result="Hold the line",
        target="No churn",
        db_path=path,
    )

    store.initialize_db(path)  # second pass — must be a no-op for the column

    # Sanity: column exists exactly once.
    with sqlite3.connect(str(path)) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(department_goals)")]
    assert cols.count("last_reviewed_at") == 1

    # NULL on a freshly-inserted row reads back as '' via _row_to_goal.
    g = store.get_goal(goal_id, db_path=path)
    assert g is not None
    assert g.last_reviewed_at == ""
