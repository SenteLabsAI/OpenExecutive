"""Act as me: a contact the person names goes on the roster after a mail read,
and only that (delegation/lockdown.py ``speaker_named_contact``)."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from openexecutive.delegation import lockdown
from openexecutive.delegation.settings import TurnDelegation
from openexecutive.memory import episodic
from openexecutive.orchestrator.schedule_tools import current_session
from openexecutive.orchestrator.session import Session
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store

BOTH = (lockdown.mail_touched_withholds,)


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", path)
    monkeypatch.setattr(people_store, "DB_PATH", path)
    episodic.initialize_db(path)
    people_store.initialize_db(path)
    people_registry.invalidate()
    yield path
    people_registry.invalidate()


@pytest.fixture
def said() -> Iterator[Any]:
    """Bind a turn that read mail, with ``said(text)`` as the speaker's words."""
    session = Session()
    pinned = TurnDelegation(
        offered=True, touched_mail=True, read_mail=True, session_id=session.session_id,
        mail_senders={"jamie@firm.example": "Jamie Rivera", "billing@evil.example": "Accounts"},
    )
    session.turn_delegation = pinned  # type: ignore[attr-defined]
    token = current_session.set(session)

    def say(text: str) -> None:
        pinned.speaker_text = text

    try:
        yield say
    finally:
        current_session.reset(token)


def _add(**fields: Any) -> dict[str, Any]:
    return {"full_name": "Jamie Rivera", "kind": "contact", "email": "jamie@firm.example", **fields}


def test_a_contact_the_speaker_named_is_added_after_a_mail_read(said: Any) -> None:
    said("Can you add jamie as a contact?")
    for withholds in BOTH:
        assert not withholds("upsert_person", _add())
        assert not withholds("upsert_person", _add(role="Paralegal, Firm LLP"))


@pytest.mark.parametrize("call", [
    _add(kind="team"),  # a seat and a sign-in
    {"full_name": "Jamie Rivera", "email": "jamie@firm.example"},  # kind left to the default
    _add(authority_scopes=["spend_lt_2k"]),
    _add(telegram_chat_id="123"),
    _add(slack_user_id="U9"),
    _add(email_aliases=["other@firm.example"]),
    _add(department_slugs=["legal"]),
    _add(preferred_channel="telegram"),
    _add(full_name="Morgan Blake"),  # a name the speaker never typed
    "not a dict",
])
def test_anything_beyond_a_named_contact_stays_refused(said: Any, call: Any) -> None:
    said("Can you add jamie as a contact?")
    for withholds in BOTH:
        assert withholds("upsert_person", call), call


def test_a_message_with_an_attachment_names_no_one(said: Any) -> None:
    said("add jamie as a contact [Attached: notes.pdf] Jamie Rivera")
    assert lockdown.mail_touched_withholds("upsert_person", _add())


def test_a_name_only_the_mail_carried_is_refused(said: Any) -> None:
    # The speaker said "add her"; the name came from the conversation.
    said("yes add her as a contact")
    assert lockdown.mail_touched_withholds("upsert_person", _add())


def test_updating_a_contact_cant_redirect_their_address(said: Any) -> None:
    pid = people_store.upsert_person(full_name="Jamie Rivera", kind="contact", email="jamie@firm.example")
    team = people_store.upsert_person(full_name="Jamie Team", kind="team", email="jt@co.example")
    people_registry.invalidate()
    said("update jamie's role to partner")
    assert not lockdown.mail_touched_withholds("upsert_person", {"person_id": pid, "full_name": "Jamie Rivera", "role": "Partner"})
    # Same address, or none given: fine. A new one the speaker didn't type: refused.
    assert not lockdown.mail_touched_withholds("upsert_person", {"person_id": pid, "full_name": "Jamie Rivera", "email": "Jamie@firm.example"})
    assert lockdown.mail_touched_withholds("upsert_person", {"person_id": pid, "full_name": "Jamie Rivera", "email": "jamie@evil.example"})
    said("jamie's new address is jamie@newfirm.example")
    assert not lockdown.mail_touched_withholds("upsert_person", {"person_id": pid, "full_name": "Jamie Rivera", "email": "jamie@newfirm.example"})
    # Never a team member, never a contact turned into one.
    assert lockdown.mail_touched_withholds("upsert_person", {"person_id": team, "full_name": "Jamie Team"})
    assert lockdown.mail_touched_withholds("upsert_person", {"person_id": pid, "full_name": "Jamie Rivera", "kind": "team"})
    assert lockdown.mail_touched_withholds("upsert_person", {"person_id": 999, "full_name": "Jamie Rivera"})


def test_the_stored_name_must_be_the_one_they_named(said: Any) -> None:
    pid = people_store.upsert_person(full_name="Morgan Blake", kind="contact", email="m@firm.example")
    people_registry.invalidate()
    said("add jamie as a contact")
    assert lockdown.mail_touched_withholds("upsert_person", {"person_id": pid, "full_name": "Jamie Rivera"})


def test_everyday_words_the_speaker_typed_name_no_one(said: Any) -> None:
    said("Find Priya's email and add Priya as a contact")
    for name in ("Email Billing", "Contact Desk", "Add Find", "As Is"):
        assert lockdown.mail_touched_withholds("upsert_person", _add(full_name=name)), name
    assert not lockdown.mail_touched_withholds("upsert_person", _add(full_name="Priya Shah", email=None))



def test_a_name_without_an_ask_to_add_anyone_is_refused(said: Any) -> None:
    # "jamie" is typed, but nothing asks to change the roster.
    for text in ("reply to jamie about the invoice", "any new mail from jamie?",
                 "did jamie update the deck?"):
        said(text)
        for withholds in BOTH:
            assert withholds("upsert_person", _add(email="billing@evil.example")), text


def test_a_new_contacts_address_is_a_sender_or_typed(said: Any) -> None:
    said("add jamie as a contact")
    # The address that sent the mail read this turn: fine.
    assert not lockdown.mail_touched_withholds("upsert_person", _add())
    # One the mail's text only mentions: refused.
    assert lockdown.mail_touched_withholds("upsert_person", _add(email="jamie@evil.example"))
    # Another sender's address, under neither the contact's name nor theirs: refused.
    assert lockdown.mail_touched_withholds("upsert_person", _add(email="billing@evil.example"))
    # No address at all, or one they typed: fine.
    assert not lockdown.mail_touched_withholds("upsert_person", _add(email=None))
    said("add jamie as a contact, jamie@newfirm.example")
    assert not lockdown.mail_touched_withholds("upsert_person", _add(email="Jamie@newfirm.example"))


def test_reading_mail_notes_who_sent_it() -> None:
    from types import SimpleNamespace

    from openexecutive.orchestrator.mail_read_tools import _note_senders

    writer = SimpleNamespace(pinned=TurnDelegation())
    _note_senders(writer, ["Jamie Rivera <Jamie@Firm.example>", "ops@co.example", "", "no address"])
    assert writer.pinned.mail_senders == {"jamie@firm.example": "Jamie Rivera", "ops@co.example": ""}


def test_a_senders_address_matches_by_name_or_local_part() -> None:
    senders = {"jamie@firm.example": "", "j.rivera@firm.example": "", "ops@co.example": "Jamie Rivera"}
    assert lockdown._sent_by("jamie@firm.example", senders, "Jamie Rivera")
    assert lockdown._sent_by("j.rivera@firm.example", senders, "Jamie Rivera")
    assert lockdown._sent_by("ops@co.example", senders, "Jamie Rivera")
    assert not lockdown._sent_by("jamie@firm.example", senders, "Morgan Blake")
    assert not lockdown._sent_by("other@firm.example", senders, "Jamie Rivera")
