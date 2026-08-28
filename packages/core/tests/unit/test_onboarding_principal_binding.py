"""Onboarding must bind the bootstrap principal to the verified UI email."""
from __future__ import annotations

from pathlib import Path

import pytest

from openexecutive.api.authorization import require_principal
from openexecutive.onboarding import profile_builder
from openexecutive.people import store as people_store
from openexecutive.people.models import AuthorityScope


@pytest.fixture()
def people_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "people.db"
    monkeypatch.setattr(people_store, "DB_PATH", db_path)
    people_store.initialize_db()
    return db_path


def test_first_onboarding_principal_is_bound_to_verified_caller_email(
    people_db: Path,
) -> None:
    answers = {"principal_identity": "Alex Rivera, CEO"}

    profile_builder._save_wizard_people(
        answers, principal_email="Alex.Rivera@Example.com"
    )

    principal = people_store.find_principal_person()
    assert principal is not None
    assert principal.email == "alex.rivera@example.com"
    # The same header stamped by the UI proxy now authorizes principal-only
    # endpoints such as the Codex connection flow.
    require_principal("alex.rivera@example.com")


def test_legacy_principal_is_recovered_by_email_binding(people_db: Path) -> None:
    people_store.upsert_person(
        full_name="Alex Rivera", is_principal=True, email="stale@example.com"
    )

    profile_builder._save_wizard_people(
        {"principal_identity": "Alex Rivera, CEO"},
        principal_email="alex@example.com",
    )

    principal = people_store.find_principal_person()
    assert principal is not None
    assert principal.email == "alex@example.com"
    assert AuthorityScope.WILDCARD in principal.authority_scope


def test_store_rejects_concurrent_second_principal(people_db: Path) -> None:
    people_store.upsert_person(full_name="Alex Rivera", is_principal=True)
    with pytest.raises(ValueError, match="principal"):
        people_store.upsert_person(full_name="Mallory", is_principal=True)


def test_repeat_onboarding_cannot_mint_a_second_principal(people_db: Path) -> None:
    profile_builder._save_wizard_people(
        {"principal_identity": "Alex Rivera, CEO"},
        principal_email="alex@example.com",
    )
    profile_builder._save_wizard_people(
        {"principal_identity": "Alex Rivera, CEO"},
        principal_email="alex@example.com",
    )

    principals = [person for person in people_store.list_people() if person.is_principal]
    assert len(principals) == 1
    assert principals[0].email == "alex@example.com"
