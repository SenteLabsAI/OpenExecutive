"""Who an email address belongs to: aliases and the company's own domains.

A person has one primary address (their sign-in) and any number of aliases.
Mail matches its sender by either, exactly; on the company's own domains a
teammate also matches by local part (``+tag`` dropped, the company's domains
counted as one). The outbound allow-list (``RosterAllow``) follows the same
rule, so the Executive may email exactly the addresses mail would match.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from openexecutive.memory import episodic
from openexecutive.memory import workspace_settings as ws
from openexecutive.people import identity
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "identity.db"
    monkeypatch.setattr(people_store, "DB_PATH", path)
    monkeypatch.setattr(episodic, "DB_PATH", path)
    people_store.initialize_db()
    people_registry.invalidate()
    yield path
    people_registry.invalidate()


@pytest.fixture
def roster() -> SimpleNamespace:
    owner = people_store.upsert_person(
        full_name="Olivia Owner", is_principal=True, email="olivia@acme.com"
    )
    anna = people_store.upsert_person(full_name="Anna Smith", email="anna@acme.com")
    client = people_store.upsert_person(
        full_name="Carl Client", email="carl@acme.com", kind="contact"
    )
    return SimpleNamespace(owner=owner, anna=anna, client=client)


def _name(addr: str, **kw: bool) -> str | None:
    person = identity.resolve_email_sender(addr, **kw)
    return person.full_name if person is not None else None


def test_company_domains_come_from_the_principals_address(roster: SimpleNamespace) -> None:
    assert identity.company_domains() == frozenset({"acme.com"})


def test_a_free_mail_principal_has_no_company_domain() -> None:
    people_store.upsert_person(full_name="Solo", is_principal=True, email="solo@gmail.com")
    assert identity.company_domains() == frozenset()
    people_store.upsert_person(full_name="Sam", email="sam@gmail.com")
    # Nobody on gmail.com matches by local part: anyone can register sam+x.
    assert _name("sam+x@gmail.com") is None


def test_the_setting_overrides_the_derived_domains(roster: SimpleNamespace) -> None:
    ws.set_company_domains(["acme.com", "acme.io"])
    assert identity.company_domains() == frozenset({"acme.com", "acme.io"})
    # anna@acme.io is the Anna at anna@acme.com: the company's domains are one.
    assert _name("Anna@ACME.io") == "Anna Smith"


def test_plus_tags_are_dropped_only_on_a_company_domain(roster: SimpleNamespace) -> None:
    assert _name("anna+invoices@acme.com") == "Anna Smith"
    people_store.set_person_emails(roster.anna, ["anna.smith@gmail.com"])
    assert _name("anna.smith@gmail.com") == "Anna Smith"
    assert _name("anna.smith+x@gmail.com") is None


def test_an_unknown_local_part_on_the_company_domain_matches_nobody(roster: SimpleNamespace) -> None:
    # The domain alone never says who someone is.
    assert _name("annamarie@acme.com") is None


def test_the_domain_rule_never_matches_a_contact(roster: SimpleNamespace) -> None:
    assert _name("carl@acme.com", include_contacts=True) == "Carl Client"  # exact still does
    assert _name("carl+x@acme.com", include_contacts=True) is None


def test_two_teammates_with_one_local_part_match_nobody(roster: SimpleNamespace) -> None:
    ws.set_company_domains(["acme.com", "acme.io"])
    people_store.upsert_person(full_name="Anna Other", email="anna@acme.io")
    # Exact addresses still resolve; the ambiguous local-part match fails closed.
    assert _name("anna@acme.io") == "Anna Other"
    assert _name("anna+x@acme.com") is None


def test_an_alias_matches_mail_but_never_signs_in(roster: SimpleNamespace) -> None:
    people_store.set_person_emails(roster.anna, ["Anna.Smith@Gmail.com"])
    assert people_store.find_person_by_address("anna.smith@gmail.com").full_name == "Anna Smith"
    # find_person_by_email is the sign-in lookup: primary only.
    assert people_store.find_person_by_email("anna.smith@gmail.com") is None
    assert people_store.get_person(roster.anna).email_aliases == ["Anna.Smith@Gmail.com"]


def test_an_address_belongs_to_one_person(roster: SimpleNamespace) -> None:
    with pytest.raises(people_store.AddressInUseError):
        people_store.set_person_emails(roster.anna, ["olivia@acme.com"])
    people_store.set_person_emails(roster.anna, ["a@elsewhere.com"])
    with pytest.raises(people_store.AddressInUseError):
        people_store.add_person_email(roster.client, "A@elsewhere.com")
    assert people_store.add_person_email(roster.anna, "a@elsewhere.com") is False


def test_bad_aliases_are_refused_and_the_primary_dropped(roster: SimpleNamespace) -> None:
    assert people_store.clean_aliases(
        [" x@a.com", "X@A.com", "", "anna@acme.com"], primary="anna@acme.com"
    ) == ["x@a.com"]
    for bad in ["no-at-sign", "a@b", "a b@c.com", "<a@b.com>", "a@b.com, c@d.com"]:
        with pytest.raises(ValueError):
            people_store.clean_aliases([bad])
    with pytest.raises(ValueError):
        people_store.clean_aliases([f"a{i}@b.com" for i in range(11)])


def test_archiving_frees_the_aliases(roster: SimpleNamespace) -> None:
    people_store.set_person_emails(roster.anna, ["a@elsewhere.com"])
    people_store.archive_person(roster.anna)
    assert people_store.find_person_by_address("a@elsewhere.com") is None
    people_store.set_person_emails(roster.client, ["a@elsewhere.com"])


def test_the_allow_list_follows_the_same_rule(roster: SimpleNamespace) -> None:
    people_store.set_person_emails(roster.anna, ["anna.smith@gmail.com"])
    allow = identity.RosterAllow(people_store.list_people(), extra=["exec@acme.com"])
    assert "Anna+Q3@acme.com" in allow
    assert "anna.smith@gmail.com" in allow
    assert "exec@acme.com" in allow
    assert "annamarie@acme.com" not in allow
    assert "anna.smith+x@gmail.com" not in allow
    # Built from the team only (the gateway adds contacts on the principal's
    # own turn): a contact's address is refused like a stranger's.
    assert "carl@acme.com" not in allow
    with_contacts = identity.RosterAllow(people_store.list_people(include_contacts=True))
    assert "carl@acme.com" in with_contacts
    assert "carl+x@acme.com" not in with_contacts


def test_the_migration_adds_aliases_to_an_existing_roster(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sqlite3

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE people (id INTEGER PRIMARY KEY AUTOINCREMENT, full_name TEXT NOT NULL,"
            " role TEXT NOT NULL DEFAULT '', is_principal INTEGER NOT NULL DEFAULT 0,"
            " department_slugs_json TEXT NOT NULL DEFAULT '[]', email TEXT, slack_user_id TEXT,"
            " telegram_chat_id TEXT, preferred_channel TEXT NOT NULL DEFAULT 'any',"
            " response_sla_hours INTEGER NOT NULL DEFAULT 24, on_leave_until TEXT,"
            " reports_to_person_id INTEGER, archived INTEGER NOT NULL DEFAULT 0,"
            " created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO people (full_name, email, created_at, updated_at)"
            " VALUES ('Old Timer', 'old@acme.com', 'x', 'x')"
        )
    monkeypatch.setattr(people_store, "DB_PATH", path)
    people_store.initialize_db()
    people_store.initialize_db()  # idempotent
    person = people_store.find_person_by_address("OLD@acme.com")
    assert person is not None and person.email_aliases == []
