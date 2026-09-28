"""The email poller's audit row carries the mail's text, not only its subject.

The Executive's reply is kept in full on its ``chat_turn`` row, so /audit
showed the answer but not the question: the ``integration_inbound`` row held
the sender, the subject and the ids and nothing else. It now keeps the text
in its full payload — ``message`` (the sender's new text, quoted replies and
a forwarded message cut off, as peer memory sees it) and ``attachments``
(the filenames) — with a 120-character ``preview`` and ``body_len`` in the
details. The whole body with its quoted chain (``body``) is kept only on a
row private to the principal: the chain is other people's mail, and every
signed-in user reads a public row. The summary is unchanged: the session
graph labels the node from it.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

import openexecutive.integrations.email_poller as poller
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store

EXEC = "exec@example.com"
OWNER = "olivia@example.com"
TEAMMATE = "alice@example.com"
CONTACT = "jordan@acme.example"

NEW_TEXT = (
    "Can we move the pilot to October 5?\n"
    "Pricing is attached — please confirm by Friday."
)
QUOTED_CHAIN = (
    "On Mon, 21 Sep 2026 at 09:12, Executive <exec@example.com> wrote:\n"
    "> Happy to start the pilot on September 28.\n"
    "> Let me know if the date works."
)
ATTACHMENTS = (
    "1. pricing.pdf (application/pdf, 42.0 KB)\n"
    "   Attachment ID: ANGjdJ8\n"
    "   Use get_gmail_attachment_content(message_id='m1', attachment_id='ANGjdJ8') to download"
)
FORWARDED_BODY = (
    "Can you deal with this?\n\n"
    "---------- Forwarded message ---------\n"
    "From: Dana Prospect <dana@prospect.example>\n"
    "Subject: Pilot\n\n"
    "We'd like to start the pilot on October 5.\n"
)


@pytest.fixture(autouse=True)
def roster(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(people_store, "DB_PATH", tmp_path / "people.db")
    people_store.initialize_db()
    people_store.upsert_person(full_name="Olivia Owner", is_principal=True, email=OWNER)
    people_store.upsert_person(full_name="Alice", email=TEAMMATE)
    people_store.upsert_person(full_name="Jordan Client", email=CONTACT, kind="contact")
    people_registry.invalidate()


def _raw(
    body: str, *, sender: str = TEAMMATE, subject: str | None = "Re: Pilot", attachments: str = ""
) -> str:
    raw = ""
    if subject is not None:
        raw += f"Subject: {subject}\n"
    raw += f"From: {sender}\nTo: {EXEC}\n\n--- BODY ---\n{body}\n"
    if attachments:
        raw += f"\n--- ATTACHMENTS ---\n{attachments}\n"
    return raw


def _audit_rows(raw: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    gateway = AsyncMock()
    gateway.call_tool = AsyncMock(return_value=raw)
    settings = SimpleNamespace(exec_email_address=EXEC, email_poll_interval_seconds=60)
    with (
        patch(
            "openexecutive.audit.log_event",
            side_effect=lambda event_type, summary, **kw: rows.append(
                {"event_type": event_type, "summary": summary, **kw}
            ),
        ),
        patch.object(poller, "get_settings", return_value=settings),
        patch.object(poller, "_run_executive", new=AsyncMock()),
        patch.object(poller, "_mark_read", new=AsyncMock()),
    ):
        asyncio.run(poller._handle_email(gateway, "m1", "t1", EXEC))
    return rows


def _inbound_row(raw: str) -> dict[str, Any]:
    rows = [
        r for r in _audit_rows(raw)
        if r["event_type"] == "integration_inbound" and r["summary"].startswith("Inbound email")
    ]
    assert len(rows) == 1
    return rows[0]


def test_row_keeps_the_senders_text_and_the_attachments() -> None:
    row = _inbound_row(_raw(f"{NEW_TEXT}\n\n{QUOTED_CHAIN}", attachments=ATTACHMENTS))

    assert row["full"]["message"] == NEW_TEXT
    assert row["full"]["attachments"] == ["pricing.pdf"]

    details = row["details"]
    assert details["preview"] == NEW_TEXT[:120]
    assert details["body_len"] == len(f"{NEW_TEXT}\n\n{QUOTED_CHAIN}")
    assert details["subject"] == "Re: Pilot"
    assert details["from"] == TEAMMATE
    assert details["message_id"] == "m1" and details["thread_id"] == "t1"


def test_a_public_row_leaves_out_the_quoted_chain() -> None:
    # A teammate's reply quotes the thread below their words — which may be a
    # contact's mail, or the principal's. Every signed-in user reads this
    # row, so only the sender's own words are kept, as web chat keeps them.
    row = _inbound_row(_raw(f"{NEW_TEXT}\n\n{QUOTED_CHAIN}"))
    assert row["private"] is False
    assert "body" not in row["full"]
    assert "September 28" not in str(row["full"])


def test_a_private_row_keeps_the_whole_body() -> None:
    # Mail from a contact is the principal's alone to read (private row), so
    # the whole body, quoted chain included, is kept for them.
    row = _inbound_row(_raw(f"{NEW_TEXT}\n\n{QUOTED_CHAIN}", sender=CONTACT))
    assert row["private"] is True
    assert row["full"]["message"] == NEW_TEXT
    assert row["full"]["body"].startswith(NEW_TEXT)
    assert "> Happy to start the pilot on September 28." in row["full"]["body"]


def test_a_forward_from_the_principal_keeps_the_forwarded_mail_privately() -> None:
    row = _inbound_row(_raw(FORWARDED_BODY, sender=OWNER, subject="Fwd: Pilot"))
    assert row["private"] is True
    assert row["full"]["message"] == "Can you deal with this?"
    assert "start the pilot on October 5" in row["full"]["body"]
    assert row["details"]["preview"] == "Can you deal with this?"


def test_summary_and_session_id_are_unchanged() -> None:
    row = _inbound_row(_raw(NEW_TEXT))
    assert row["summary"] == f"Inbound email from {TEAMMATE}: Re: Pilot"
    assert row["session_id"] == "email:t1"
    assert row["actor"] == "email"


def test_preview_is_cut_at_120_characters() -> None:
    long_text = "word " * 100
    row = _inbound_row(_raw(long_text))
    assert len(row["details"]["preview"]) == 120
    assert row["full"]["message"] == long_text.strip()


def test_details_stay_structured_for_a_subject_and_preview_of_non_ascii_text() -> None:
    # The logger keeps `details` only up to 4 KB of JSON, in which a non-ASCII
    # character is 12 escaped bytes: a subject and a preview of them must
    # still fit beside the ids, or the row loses its channel and sender.
    import json

    emoji = "\U0001f600" * 200
    row = _inbound_row(_raw(emoji, subject=emoji))
    assert len(json.dumps(row["details"])) <= 4000


def test_a_stranger_is_a_public_row_with_their_own_words_only() -> None:
    # A sender on neither the roster nor the contacts — cold inbound — is
    # everyone's to read, as their subject already was and as the
    # Executive's reply about them is: the audit row must read like a
    # contact's (only the visibility differs), so it keeps `message` and,
    # being public, leaves out the quoted chain.
    row = _inbound_row(_raw(f"{NEW_TEXT}\n\n{QUOTED_CHAIN}", sender="stranger@else.example"))
    assert row["private"] is False
    assert row["full"]["message"] == NEW_TEXT
    assert "body" not in row["full"]
    assert row["details"]["preview"] == NEW_TEXT[:120]


def _json_len(value: Any) -> int:
    import json

    return len(json.dumps(value, default=str))


def test_oversized_text_is_cut_not_dropped() -> None:
    huge = "x" * 30_000
    row = _inbound_row(_raw(huge, sender=CONTACT))
    assert _json_len(row["full"]["message"]) <= poller._AUDIT_TEXT_MAX_JSON
    assert _json_len(row["full"]["body"]) <= poller._AUDIT_TEXT_MAX_JSON
    assert len(row["full"]["message"]) > 19_000
    assert row["details"]["body_len"] == 30_000


@pytest.mark.parametrize("char", ["\u4e2d", "\u00e9", "\U0001f600"])
def test_a_long_mail_in_another_script_keeps_the_rows_structure(char: str) -> None:
    # The logger measures its 64 KB cap on the escaped JSON, where this
    # character is 6 or 12 characters, and over it the whole payload turns
    # into an unstructured preview. A normal-length mail in another script
    # must not get there, on a private row that carries message and body.
    text = char * 11_000
    row = _inbound_row(_raw(text, sender=CONTACT))
    assert set(row["full"]) == {"attachments", "message", "body"}
    assert _json_len(row["full"]) <= 64 * 1024
    assert row["full"]["message"].startswith(char * 100)
    assert _json_len(row["full"]["message"]) <= poller._AUDIT_TEXT_MAX_JSON


def test_a_mail_with_many_attachments_keeps_the_leading_names() -> None:
    names = [f"{i:03d}-" + "\u00e9" * 100 + ".pdf" for i in range(60)]
    listing = "\n".join(f"{i + 1}. {n} (application/pdf, 1.0 KB)" for i, n in enumerate(names))
    row = _inbound_row(_raw("See attached.", sender=CONTACT, attachments=listing))
    kept = row["full"]["attachments"]
    assert kept == names[: len(kept)] and 0 < len(kept) < 60
    assert _json_len(kept) <= poller._AUDIT_ATTACHMENTS_MAX_JSON
    assert _json_len(row["full"]) <= 64 * 1024


def test_an_empty_body_and_a_missing_subject_still_write_the_row() -> None:
    row = _inbound_row(_raw("", subject=None))
    assert row["summary"] == f"Inbound email from {TEAMMATE}"
    assert row["details"]["subject"] == ""
    assert row["details"]["preview"] == ""
    assert row["details"]["body_len"] == 0
    assert row["full"] == {"attachments": [], "message": ""}


def test_subject_comes_from_the_header_not_a_quoted_subject_line_in_the_body() -> None:
    row = _inbound_row(_raw("Subject: not this one\nReal text.", subject="Header subject"))
    assert row["details"]["subject"] == "Header subject"
    assert row["summary"].endswith(": Header subject")
