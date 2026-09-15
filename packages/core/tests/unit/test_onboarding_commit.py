"""Persisting a reviewed onboarding draft: people + the ADDITIVE department reconcile.

The department half is the reason this file exists. ``cli/fixture_loader``
seeds departments by DELETing every row first — correct for swapping in a demo
company, and destructive here: it would drop the eight defaults along with
their ``specialist_key`` wiring, which ``create_department`` (always
``specialist_key = NULL``) cannot restore. These tests pin the additive
behaviour so nobody "simplifies" it into the fixture seeder later.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from openexecutive.departments import store as dept_store
from openexecutive.onboarding.commit import (
    derive_org_structure,
    reconcile_onboarding_departments,
    save_onboarding_people,
)
from openexecutive.onboarding.interview import (
    CompanyDraft,
    DepartmentDraft,
    PersonDraft,
)
from openexecutive.people import store as people_store


@pytest.fixture()
def dept_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "departments.db"
    monkeypatch.setattr(dept_store, "DB_PATH", path)
    dept_store.initialize_db(path)
    dept_store.seed_default_departments(path)
    monkeypatch.setattr("openexecutive.departments.registry.invalidate", lambda: None)
    return path


@pytest.fixture()
def people_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "people.db"
    monkeypatch.setattr(people_store, "DB_PATH", path)
    people_store.initialize_db(path)
    return path


# ── people ───────────────────────────────────────────────────────────────────


def test_saves_roster_and_grants_wildcard_to_the_principal_only(
    people_db: Path,
) -> None:
    ids = save_onboarding_people(
        [
            PersonDraft(full_name="Dana Reyes", role="CEO", is_principal=True),
            PersonDraft(full_name="Sam Okafor", role="Head of Ops"),
        ]
    )
    assert set(ids) == {"Dana Reyes", "Sam Okafor"}

    principal = people_store.find_principal_person(db_path=people_db)
    assert principal is not None
    assert principal.full_name == "Dana Reyes"
    # Contacts are never auto-imported.
    assert principal.email is None
    assert principal.slack_user_id is None

    assert [s.value for s in principal.authority_scope] == ["wildcard"]
    # Everyone else gets nothing — onboarding grants authority to the user only.
    sam = people_store.get_person(ids["Sam Okafor"], db_path=people_db)
    assert sam is not None
    assert sam.authority_scope == []


def test_blank_names_are_skipped(people_db: Path) -> None:
    assert save_onboarding_people([PersonDraft(full_name="   ")]) == {}


def test_people_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A people error must never cost the user the profile they just saved."""
    monkeypatch.setattr(
        "openexecutive.people.store.initialize_db",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk on fire")),
    )
    assert save_onboarding_people([PersonDraft(full_name="Dana", is_principal=True)]) == {}


# ── departments ──────────────────────────────────────────────────────────────


def test_matching_department_is_updated_not_recreated(dept_db: Path) -> None:
    before = {d.config.slug: d.config for d in dept_store.list_departments(dept_db)}
    assert "finance" in before, "expected the seeded defaults"
    original_specialist = before["finance"].specialist_key

    counts = reconcile_onboarding_departments(
        [DepartmentDraft(title="Finance", mission="Own the model and the runway.")],
        {},
    )
    assert counts == {"updated": 1, "created": 0}

    after = {d.config.slug: d.config for d in dept_store.list_departments(dept_db)}
    assert len(after) == len(before), "reconcile must not add or remove rows here"
    assert after["finance"].charter.mission == "Own the model and the runway."
    # The specialist wiring survives — create_department could never restore it.
    assert after["finance"].specialist_key == original_specialist


def test_unmatched_department_is_created(dept_db: Path) -> None:
    before = len(dept_store.list_departments(dept_db))
    counts = reconcile_onboarding_departments(
        [DepartmentDraft(title="Customer Success", mission="Keep accounts alive.")], {}
    )
    assert counts == {"updated": 0, "created": 1}

    after = {d.config.slug: d.config for d in dept_store.list_departments(dept_db)}
    assert len(after) == before + 1
    assert after["customer-success"].charter.mission == "Keep accounts alive."


def test_unmentioned_departments_are_never_deleted(dept_db: Path) -> None:
    before = {d.config.slug for d in dept_store.list_departments(dept_db)}
    reconcile_onboarding_departments([DepartmentDraft(title="Finance")], {})
    after = {d.config.slug for d in dept_store.list_departments(dept_db)}
    assert before <= after


def test_title_match_is_case_insensitive(dept_db: Path) -> None:
    counts = reconcile_onboarding_departments(
        [DepartmentDraft(title="fInAnCe", mission="Lowercase heresy.")], {}
    )
    assert counts["created"] == 0
    assert counts["updated"] == 1


def test_head_person_is_attached_when_known(dept_db: Path) -> None:
    reconcile_onboarding_departments(
        [DepartmentDraft(title="Finance", head_person_name="Dana Reyes")],
        {"Dana Reyes": 42},
    )
    finance = dept_store.get_department("finance", dept_db)
    assert finance is not None
    assert finance.config.head_person_id == 42


def test_draft_without_a_head_leaves_an_existing_head_alone(dept_db: Path) -> None:
    """An empty head_person_name means "unknown", not "clear it"."""
    dept_store.update_department("finance", head_person_id=7, db_path=dept_db)
    reconcile_onboarding_departments([DepartmentDraft(title="Finance")], {})
    finance = dept_store.get_department("finance", dept_db)
    assert finance is not None
    assert finance.config.head_person_id == 7


def test_empty_mission_preserves_the_existing_charter(dept_db: Path) -> None:
    before = dept_store.get_department("finance", dept_db)
    assert before is not None
    reconcile_onboarding_departments([DepartmentDraft(title="Finance")], {})
    after = dept_store.get_department("finance", dept_db)
    assert after is not None
    assert after.config.charter.mission == before.config.charter.mission
    assert after.config.charter.scope == before.config.charter.scope


def test_authority_level_is_applied(dept_db: Path) -> None:
    reconcile_onboarding_departments(
        [DepartmentDraft(title="Finance", authority_level="auto_execute")], {}
    )
    finance = dept_store.get_department("finance", dept_db)
    assert finance is not None
    assert finance.config.authority_level.value == "auto_execute"


def test_blank_titles_are_skipped(dept_db: Path) -> None:
    assert reconcile_onboarding_departments([DepartmentDraft(title="  ")], {}) == {
        "updated": 0,
        "created": 0,
    }


def test_department_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "openexecutive.departments.store.initialize_db",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk on fire")),
    )
    assert reconcile_onboarding_departments([DepartmentDraft(title="Finance")], {}) == {
        "updated": 0,
        "created": 0,
    }


# ── derived org structure ────────────────────────────────────────────────────


def _draft() -> CompanyDraft:
    return CompanyDraft.model_validate(
        {
            "profile": {"name": "Northwind Tools"},
            "people": [
                {"full_name": "Dana Reyes", "role": "CEO", "is_principal": True},
                {"full_name": "Sam Okafor", "role": "Head of Ops"},
            ],
            "departments": [{"title": "Operations"}, {"title": "Finance"}],
        }
    )


def test_org_structure_is_derived_from_people_and_departments() -> None:
    d = _draft()
    out = derive_org_structure(d.profile, d.people, d.departments)
    assert out.org_structure.departments == ["Operations", "Finance"]
    assert out.org_structure.leadership_team == [
        "Dana Reyes, CEO",
        "Sam Okafor, Head of Ops",
    ]
    # Everything else is untouched.
    assert out.name == "Northwind Tools"


def test_person_without_a_role_renders_as_a_bare_name() -> None:
    d = _draft()
    d.people[1].role = ""
    out = derive_org_structure(d.profile, d.people, d.departments)
    assert out.org_structure.leadership_team == ["Dana Reyes, CEO", "Sam Okafor"]


def test_degenerate_title_matches_the_slug_the_store_assigns(dept_db: Path) -> None:
    """A title that slugifies to nothing must not create a new row every run.

    departments.store falls back to "department" for such a title. Onboarding
    has to predict the SAME slug to find the row again — before slugify was
    unified these disagreed ("" vs "department"), so a second pass created
    `department-2` instead of updating.
    """
    first = reconcile_onboarding_departments(
        [DepartmentDraft(title="###", mission="Odd but non-empty.")], {}
    )
    assert first == {"updated": 0, "created": 1}

    second = reconcile_onboarding_departments(
        [DepartmentDraft(title="###", mission="Odd but non-empty.")], {}
    )
    assert second == {"updated": 1, "created": 0}, "second pass must update, not re-create"

    slugs = [d.config.slug for d in dept_store.list_departments(dept_db)]
    assert slugs.count("department") == 1
    assert "department-2" not in slugs


# ── re-running setup (regressions from adversarial review) ───────────────────


def test_rerun_updates_people_instead_of_duplicating(people_db: Path) -> None:
    """save_onboarding_people must be a real upsert.

    upsert_person only UPDATEs when given a person_id; without name matching a
    second run inserted a whole second roster, including a second
    is_principal row.
    """
    first = save_onboarding_people(
        [
            PersonDraft(full_name="Dana Reyes", role="CEO", is_principal=True),
            PersonDraft(full_name="Sam Okafor", role="Head of Ops"),
        ]
    )
    second = save_onboarding_people(
        [
            PersonDraft(full_name="Dana Reyes", role="Founder & CEO", is_principal=True),
            PersonDraft(full_name="Sam Okafor", role="COO"),
        ]
    )
    assert first == second, "the same names must resolve to the same rows"

    everyone = people_store.list_people(db_path=people_db)
    assert len(everyone) == 2, f"expected no duplicates, got {[p.full_name for p in everyone]}"
    assert {p.role for p in everyone} == {"Founder & CEO", "COO"}


def test_rerun_with_a_new_principal_demotes_the_old_one(people_db: Path) -> None:
    """find_principal_person tie-breaks on oldest id, so a stale principal wins.

    The old founder must lose both the flag and WILDCARD, or they keep blanket
    approval authority and keep receiving the principal's routing.
    """
    save_onboarding_people([PersonDraft(full_name="Dana Reyes", is_principal=True)])
    save_onboarding_people([PersonDraft(full_name="Bo Nakamura", is_principal=True)])

    principal = people_store.find_principal_person(db_path=people_db)
    assert principal is not None
    assert principal.full_name == "Bo Nakamura"

    flagged = [p for p in people_store.list_people(db_path=people_db) if p.is_principal]
    assert [p.full_name for p in flagged] == ["Bo Nakamura"]

    dana = next(
        p for p in people_store.list_people(db_path=people_db) if p.full_name == "Dana Reyes"
    )
    assert dana.authority_scope == [], "the old principal must lose WILDCARD"


def test_rerun_preserves_contact_details(people_db: Path) -> None:
    """upsert_person's UPDATE writes every column — contacts must be carried."""
    ids = save_onboarding_people([PersonDraft(full_name="Dana Reyes", is_principal=True)])
    people_store.update_person(
        ids["Dana Reyes"],
        email="dana@example.com",
        slack_user_id="U123",
        db_path=people_db,
    )

    save_onboarding_people(
        [PersonDraft(full_name="Dana Reyes", role="CEO", is_principal=True)]
    )
    dana = people_store.get_person(ids["Dana Reyes"], db_path=people_db)
    assert dana is not None
    assert dana.email == "dana@example.com", "re-running setup must not wipe contacts"
    assert dana.slack_user_id == "U123"
    assert dana.role == "CEO"


def test_demotion_keeps_non_wildcard_scopes(people_db: Path) -> None:
    from openexecutive.people.models import AuthorityScope

    ids = save_onboarding_people([PersonDraft(full_name="Dana Reyes", is_principal=True)])
    people_store.set_authority_scope(
        ids["Dana Reyes"],
        [AuthorityScope.WILDCARD, AuthorityScope.LEGAL_SIGN],
        db_path=people_db,
    )
    save_onboarding_people([PersonDraft(full_name="Bo Nakamura", is_principal=True)])

    dana = people_store.get_person(ids["Dana Reyes"], db_path=people_db)
    assert dana is not None
    assert [s.value for s in dana.authority_scope] == ["legal_sign"]


def test_two_drafts_colliding_on_one_slug_create_one_department(dept_db: Path) -> None:
    """The lookup maps used to be built once, so the second draft made `-2`."""
    before = len(dept_store.list_departments(dept_db))
    counts = reconcile_onboarding_departments(
        [
            DepartmentDraft(title="Growth", mission="A"),
            DepartmentDraft(title="growth", mission="B"),
        ],
        {},
    )
    assert counts == {"updated": 0, "created": 1}
    after = dept_store.list_departments(dept_db)
    assert len(after) == before + 1
    assert "growth-2" not in {d.config.slug for d in after}


def test_two_drafts_matching_one_existing_department_apply_once(dept_db: Path) -> None:
    counts = reconcile_onboarding_departments(
        [
            DepartmentDraft(title="Finance", mission="First"),
            DepartmentDraft(title="finance", mission="Second"),
        ],
        {},
    )
    assert counts == {"updated": 1, "created": 0}, "counts must not claim two updates"
    finance = dept_store.get_department("finance", dept_db)
    assert finance is not None
    assert finance.config.charter.mission == "First"


def test_rerun_preserves_every_column_upsert_writes(people_db: Path) -> None:
    """Guards the whole carry-across, not just the two contacts above.

    ``upsert_person``'s UPDATE branch writes every column unconditionally, so
    any field ``save_onboarding_people`` forgets to pass is silently NULLed on
    a re-run. If someone adds a column there, this fails instead of quietly
    wiping user data.
    """
    from datetime import date

    ids = save_onboarding_people([PersonDraft(full_name="Dana Reyes", is_principal=True)])
    pid = ids["Dana Reyes"]
    people_store.update_person(
        pid,
        email="dana@example.com",
        slack_user_id="U1",
        telegram_chat_id="T1",
        discord_user_id="D1",
        preferred_channel="slack",
        response_sla_hours=4,
        on_leave_until=date(2030, 1, 1),
        department_slugs=["finance"],
        db_path=people_db,
    )
    before = people_store.get_person(pid, db_path=people_db)
    assert before is not None

    save_onboarding_people(
        [PersonDraft(full_name="Dana Reyes", role="CEO", is_principal=True)]
    )
    after = people_store.get_person(pid, db_path=people_db)
    assert after is not None

    # role is the one field the draft is allowed to change.
    for field in (
        "email",
        "slack_user_id",
        "telegram_chat_id",
        "discord_user_id",
        "preferred_channel",
        "response_sla_hours",
        "on_leave_until",
        "department_slugs",
    ):
        assert getattr(after, field) == getattr(before, field), f"{field} was not preserved"
    assert after.role == "CEO"


def test_drafts_resolving_to_one_row_by_title_apply_once(dept_db: Path) -> None:
    """The shipped `hr` department is titled "People & Talent", so slug !=
    slugify(title). Keying the dedupe on the drafted slug missed that: both
    drafts resolved to the same row and the second silently clobbered the
    first, while counts claimed two updates."""
    hr = dept_store.get_department("hr", dept_db)
    assert hr is not None and hr.config.title != "HR", "fixture assumption"

    before = len(dept_store.list_departments(dept_db))
    counts = reconcile_onboarding_departments(
        [
            DepartmentDraft(title="HR", mission="Recruiting and payroll"),
            DepartmentDraft(title=hr.config.title, mission="Culture and L&D"),
        ],
        {},
    )
    assert counts == {"updated": 1, "created": 0}, "must not claim two updates"
    assert len(dept_store.list_departments(dept_db)) == before

    after = dept_store.get_department("hr", dept_db)
    assert after is not None
    assert after.config.charter.mission == "Recruiting and payroll", (
        "the first draft must win, not be silently overwritten"
    )


def test_demoted_roster_member_loses_wildcard(people_db: Path) -> None:
    """Someone who stays on the roster but is no longer principal falls through
    _demote_stale_principals (they ARE in keep_ids) — they must lose WILDCARD
    in the main loop or they keep blanket approval authority."""
    from openexecutive.people.models import AuthorityScope

    ids = save_onboarding_people([PersonDraft(full_name="Alice", is_principal=True)])
    people_store.set_authority_scope(
        ids["Alice"],
        [AuthorityScope.WILDCARD, AuthorityScope.LEGAL_SIGN],
        db_path=people_db,
    )

    save_onboarding_people(
        [
            PersonDraft(full_name="Alice", role="Founder", is_principal=False),
            PersonDraft(full_name="Bob", role="CEO", is_principal=True),
        ]
    )

    alice = people_store.get_person(ids["Alice"], db_path=people_db)
    assert alice is not None
    assert alice.is_principal is False
    assert [s.value for s in alice.authority_scope] == ["legal_sign"]

    principal = people_store.find_principal_person(db_path=people_db)
    assert principal is not None and principal.full_name == "Bob"


def test_case_variant_names_collapse_onto_one_row_not_two(people_db: Path) -> None:
    """The upsert key is case-folded, so two spellings are one person. The
    snapshot is refreshed in the loop so the second never inserts a duplicate."""
    ids = save_onboarding_people(
        [
            PersonDraft(full_name="JANE DOE", role="CEO", is_principal=True),
            PersonDraft(full_name="Jane Doe", role="Advisor"),
        ]
    )
    assert len(set(ids.values())) == 1
    everyone = people_store.list_people(db_path=people_db)
    assert len(everyone) == 1, f"duplicate rows: {[p.full_name for p in everyone]}"
