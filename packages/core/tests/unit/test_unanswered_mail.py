"""An email to the Executive that got no reply becomes a card in Needs you.

Observed: a teammate emailed the Executive three times in one thread and a
forward from the principal went unanswered twice. Every turn ended without
the mailbox's send tool succeeding, and none called ``create_alert``, so the
brief (which reads the inbound log) said they were waiting while Home's
Needs you (alerts only) said "Nothing waiting on you".

These cover the three pieces: the gateway marking who a send reached, the
poller's check after the turn, and the card itself.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openexecutive.alerts import store as alert_store
from openexecutive.alerts.models import is_private_alert
from openexecutive.integrations import email_poller, unanswered_mail
from openexecutive.orchestrator import mcp_gateway
from openexecutive.orchestrator.schedule_tools import current_session
from openexecutive.orchestrator.session import Session
from openexecutive.people import store as people_store

RAW = "Subject: Re: Ocean Breeze LP\nFrom: ann@example.com\n\nWas it bought?"


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "alerts.db"
    alert_store.initialize_db(db_path)
    people_store.initialize_db(db_path)
    monkeypatch.setattr(alert_store, "DB_PATH", db_path)
    monkeypatch.setattr(people_store, "DB_PATH", db_path)
    return db_path


@pytest.fixture(autouse=True)
def audit_rows(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict[str, Any]]]:
    """The reply rows ``record_reply`` logs, kept off the shared audit DB."""
    from openexecutive import audit

    rows: list[tuple[str, str, dict[str, Any]]] = []

    def _log(event_type: str, summary: str, **kw: Any) -> None:
        rows.append((event_type, summary, kw))

    monkeypatch.setattr(audit, "log_event", _log)
    return rows


@pytest.fixture()
def people(db: Path) -> SimpleNamespace:
    principal_id = people_store.upsert_person(
        full_name="John Principal", email="john@example.com", is_principal=True
    )
    teammate_id = people_store.upsert_person(full_name="Ann Teammate", email="ann@example.com")
    return SimpleNamespace(
        principal_id=principal_id,
        principal=people_store.get_person(principal_id),
        teammate=people_store.get_person(teammate_id),
    )


def _cards(status: str = "unread") -> list[Any]:
    return [a for a in alert_store.list_alerts(status=status) if a.source == unanswered_mail.SOURCE]


def _note(session: Session, person: Any, *, message_id: str = "m1", answer: str = "My answer") -> None:
    email_poller._note_reply_outcome(
        session, person, RAW, message_id, "t1", person.email, answer,
    )


def _proven(**kw: Any) -> Session:
    s = Session(**kw)
    s.email_sender_authenticated = True
    return s


class TestTheCard:
    def test_a_teammate_with_no_reply_gets_a_private_card_for_the_principal(
        self, people: SimpleNamespace
    ) -> None:
        _note(_proven(), people.teammate)

        [card] = _cards()
        assert card.headline == "Ann Teammate is waiting on a reply"
        assert is_private_alert(card)
        assert card.routed_to_person_id == people.principal_id
        # Their conversation is theirs: neither what they asked nor what the
        # Executive wrote reaches the principal.
        assert "Ocean Breeze" not in card.body
        assert "My answer" not in card.body

    def test_a_repeat_in_the_thread_folds_into_one_card(self, people: SimpleNamespace) -> None:
        _note(_proven(), people.teammate, message_id="m1")
        _note(_proven(), people.teammate, message_id="m2")
        _note(_proven(), people.teammate, message_id="m3")

        [card] = _cards()
        assert card.occurrence_count == 3

    def test_the_principals_proven_mail_carries_what_the_executive_wrote(
        self, people: SimpleNamespace
    ) -> None:
        session = Session()
        session.email_authenticated = True
        _note(session, people.principal, answer="Brennan replied on Oct 9.")

        [card] = _cards()
        assert card.headline == "Your email got no reply: Re: Ocean Breeze LP"
        assert "Brennan replied on Oct 9." in card.body

    def test_mail_only_claiming_the_principal_never_reads_as_theirs(
        self, people: SimpleNamespace
    ) -> None:
        _note(Session(), people.principal, answer="Click https://evil.example now")

        [card] = _cards()
        assert card.headline == "An email from your address got no reply"
        assert "could not prove you sent it" in card.body
        assert card.suggested_action == "Check that you sent it."
        # Spoken to the principal, never about them by name.
        assert "John Principal" not in card.headline + card.body + card.suggested_action
        assert "evil" not in card.body
        assert "Ocean Breeze" not in card.headline + card.body

    def test_an_unproven_teammate_mail_says_so(self, people: SimpleNamespace) -> None:
        _note(Session(), people.teammate)

        [card] = _cards()
        assert card.headline == "An email from Ann Teammate's address got no reply"
        assert "could not prove" in card.body


class TestAReply:
    def test_a_reply_to_the_sender_files_nothing_and_logs_the_reply(
        self, people: SimpleNamespace, audit_rows: list[tuple[str, str, dict[str, Any]]],
    ) -> None:
        session = Session()
        session.mail_sent_to.add("ann@example.com")
        _note(session, people.teammate)

        assert _cards() == []
        # The brief's evidence that it was answered: private, keyed by thread.
        [(event_type, summary, kw)] = audit_rows
        assert event_type == "integration_outbound"
        assert summary == unanswered_mail.REPLIED_SUMMARY + "ann@example.com"
        assert kw["details"]["thread_key"] == "t1" and kw["private"] is True

    def test_no_reply_logs_no_reply_row(
        self, people: SimpleNamespace, audit_rows: list[tuple[str, str, dict[str, Any]]],
    ) -> None:
        _note(Session(), people.teammate)

        assert audit_rows == []

    def test_a_later_reply_resolves_the_card(self, people: SimpleNamespace) -> None:
        _note(_proven(), people.teammate, message_id="m1")
        answered = _proven()
        answered.mail_sent_to.add("ann@example.com")
        _note(answered, people.teammate, message_id="m2")

        assert _cards() == []
        assert len(_cards("resolved")) == 1

    def test_an_outlook_reply_by_message_id_counts(self, people: SimpleNamespace) -> None:
        session = Session()
        session.mail_replied_ids.add("m1")
        _note(session, people.teammate, message_id="m1")

        assert _cards() == []

    def test_a_sender_off_the_team_is_left_to_its_own_paths(self, people: SimpleNamespace) -> None:
        email_poller._note_reply_outcome(Session(), None, RAW, "m1", "t1", "stranger@x.com", "hi")

        assert _cards() == []


class TestTheGatewayMarksWhoASendReached:
    @pytest.fixture()
    def session(self) -> Any:
        s = Session()
        token = current_session.set(s)
        yield s
        current_session.reset(token)

    def test_gmail_to_and_cc(self, session: Session) -> None:
        mcp_gateway._note_mail_sent(
            "google_workspace__send_gmail_message",
            "google_workspace__send_gmail_message",
            {"to": "Ann <Ann@Example.com>", "cc": ["john@example.com"], "body": "x"},
        )
        assert session.mail_sent_to == {"ann@example.com", "john@example.com"}

    def test_outlook_send_and_reply(self, session: Session) -> None:
        mcp_gateway._note_mail_sent(
            "microsoft_365__send-mail",
            "microsoft_365__send_mail",
            {"body": {"Message": {"toRecipients": [{"emailAddress": {"address": "ann@example.com"}}]}}},
        )
        mcp_gateway._note_mail_sent(
            "microsoft_365__reply-mail-message",
            "microsoft_365__reply_mail_message",
            {"messageId": "AAMk1", "body": {"Comment": "hi"}},
        )
        assert session.mail_sent_to == {"ann@example.com"}
        assert session.mail_replied_ids == {"AAMk1"}

    def test_a_draft_marks_nothing(self, session: Session) -> None:
        mcp_gateway._note_mail_sent(
            "google_workspace__draft_gmail_message",
            "google_workspace__draft_gmail_message",
            {"to": "ann@example.com"},
        )
        assert session.mail_sent_to == set()

    def test_no_session_is_a_no_op(self) -> None:
        token = current_session.set(None)
        try:
            mcp_gateway._note_mail_sent(
                "google_workspace__send_gmail_message",
                "google_workspace__send_gmail_message",
                {"to": "ann@example.com"},
            )
        finally:
            current_session.reset(token)

