"""Peer memory records only the sender's own words for an inbound email.

The Executive's turn carries the whole message — the "You have an inbound
email" framing, every header (the Executive's own address in To:), the
quoted chain (often the Executive's own earlier email) and the attachment
listing. Recorded under the sender's peer, Honcho concluded the sender *was*
the Executive: "<sender> is associated with <exec address>", "<sender>
received an email from <sender>", "<sender> had requested the report from
<sender>". ``_email_memory_text`` keeps the subject, the new text and the
attachment filenames, and ``_run_executive`` passes it as ``memory_text``.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import openexecutive.integrations.email_poller as poller
from openexecutive.integrations.email_poller import _email_memory_text

EXEC = "ai@example.com"

# Shaped like the Gmail MCP's get_gmail_message_content output for the
# reply that surfaced the bug: a short reply, Gmail's two-line wrapped
# attribution, the quoted request, and one attachment whose name has
# parentheses of its own.
REPLY = f"""Message ID: 18c0ffee00000001
Subject: Q3 appraisal
From: Sam Lee <sam@example.com>
Date: Wed, 23 Sep 2026 11:21:18 -0400
Message-ID: <abc@mail.gmail.com>
To: {EXEC}

--- BODY ---
Here you go.

Sam

On Wed, Sep 23, 2026 at 11:00 AM Open Executive <
{EXEC}> wrote:
> Sam, can you send me the appraisal?
> Thanks

--- ATTACHMENTS ---
1. Harbor Point appraisal (1).pdf (application/pdf, 3996.2 KB)
   Attachment ID: ANGjdJ8
   Use get_gmail_attachment_content(message_id='18c0ffee00000001', attachment_id='ANGjdJ8') to download
"""


def _email(body: str, *, subject: str = "Status", attachments: str = "") -> str:
    raw = f"Subject: {subject}\nFrom: sam@example.com\nTo: {EXEC}\n\n--- BODY ---\n{body}\n"
    if attachments:
        raw += f"\n--- ATTACHMENTS ---\n{attachments}\n"
    return raw


def test_reply_keeps_subject_new_text_and_attachment_only() -> None:
    assert _email_memory_text(REPLY) == (
        "Subject: Q3 appraisal\n\n"
        "Here you go.\n\nSam\n\n"
        "[Attached: Harbor Point appraisal (1).pdf]"
    )


def test_reply_drops_everything_that_made_the_sender_look_like_the_executive() -> None:
    text = _email_memory_text(REPLY)
    for leaked in (EXEC, "From:", "To:", "Message ID", "wrote:", "can you send me", "Attachment ID"):
        assert leaked not in text


def test_single_line_attribution_without_quote_marks_ends_the_new_text() -> None:
    """Some clients quote without ">": everything after the attribution is
    the older message."""
    raw = _email("Sounds good.\n\nOn Tue, Sep 22, 2026, Exec <ai@example.com> wrote:\nOld text")
    assert _email_memory_text(raw) == "Subject: Status\n\nSounds good."


def test_quoted_lines_without_attribution_are_dropped() -> None:
    raw = _email("> earlier line\nMy answer is yes.")
    assert _email_memory_text(raw) == "Subject: Status\n\nMy answer is yes."


def test_outlook_original_message_block_is_cut() -> None:
    raw = _email("Approved.\n\n-----Original Message-----\nFrom: Exec\nSent: Monday\nPlease approve")
    assert _email_memory_text(raw) == "Subject: Status\n\nApproved."


def test_outlook_from_sent_header_block_is_cut() -> None:
    raw = _email(
        "Will do.\n\n________________________________\n"
        "From: Exec <ai@example.com>\nSent: Monday\nTo: Sam\nSubject: Status\n\nOld"
    )
    assert _email_memory_text(raw) == "Subject: Status\n\nWill do."


def test_bare_from_sent_block_without_rule_is_cut() -> None:
    raw = _email("Will do.\n\nFrom: Exec <ai@example.com>\nSent: Monday\n\nOld request")
    assert _email_memory_text(raw) == "Subject: Status\n\nWill do."


def test_a_from_line_in_the_senders_own_text_is_kept() -> None:
    """Only a From: followed by Sent:/Date: is a quoted header block."""
    raw = _email("From: the lender's side, all clear.\nMore detail here.")
    assert "From: the lender's side, all clear." in _email_memory_text(raw)


def test_forwarded_message_is_replaced_by_a_note() -> None:
    raw = _email(
        "FYI see below.\n\n---------- Forwarded message ---------\nFrom: Bank\nDate: Mon\n\nRate is 5%",
        subject="Fwd: Rates",
    )
    assert _email_memory_text(raw) == (
        "Subject: Fwd: Rates\n\nFYI see below.\n\n[Forwarded an earlier message]"
    )


def test_multiple_attachments_are_listed_by_name() -> None:
    raw = _email(
        "Both attached.",
        attachments=(
            "1. a.pdf (application/pdf, 10.0 KB)\n   Attachment ID: x\n"
            "2. b (final).xlsx (application/vnd.ms-excel, 2.5 KB) [in attached message]\n"
            "   Attachment ID: y"
        ),
    )
    assert _email_memory_text(raw).endswith("[Attached: a.pdf, b (final).xlsx]")


def test_attachment_only_email_records_subject_and_names() -> None:
    raw = _email(
        "[No text/plain body found]\n\nOn Mon, Exec <ai@example.com> wrote:\n> send it",
        subject="Appraisal",
        attachments="1. appraisal.pdf (application/pdf, 1.0 KB)",
    )
    # The MCP's placeholder is not the sender's words.
    assert _email_memory_text(raw) == "Subject: Appraisal\n\n[Attached: appraisal.pdf]"


def test_no_subject_placeholder_is_not_recorded() -> None:
    assert _email_memory_text(_email("Thanks!", subject="(no subject)")) == "Thanks!"


def test_bottom_posted_reply_keeps_text_below_the_quote() -> None:
    raw = _email(
        "On Mon, Sep 22, 2026, Exec <ai@example.com> wrote:\n"
        "> Can you send the appraisal?\n\nAttached — numbers updated."
    )
    assert _email_memory_text(raw) == "Subject: Status\n\nAttached — numbers updated."


def test_interleaved_reply_keeps_every_answer() -> None:
    raw = _email(
        "On Mon, Exec <ai@example.com> wrote:\n> Rate?\nFixed at 5%.\n> Term?\nTen years."
    )
    assert _email_memory_text(raw) == "Subject: Status\n\nFixed at 5%.\nTen years."


def test_attribution_wrapped_over_four_lines_does_not_leak_the_exec_address() -> None:
    raw = _email(
        "Here you go.\n\n"
        "On Wednesday, September 23, 2026 at 11:00 AM Open Executive Chief of\n"
        "Staff <\nai@example.com>\nwrote:\n> send it"
    )
    text = _email_memory_text(raw)
    assert text == "Subject: Status\n\nHere you go."
    assert EXEC not in text


def test_senders_own_on_line_before_a_wrote_line_is_kept() -> None:
    """Not an attribution: no quoted lines follow the "wrote:" line."""
    raw = _email("On Monday I'll send the deck.\nHere's what the lender wrote:\nRate lock expires Friday.")
    assert _email_memory_text(raw) == (
        "Subject: Status\n\n"
        "On Monday I'll send the deck.\nHere's what the lender wrote:\nRate lock expires Friday."
    )


def test_reply_starting_with_on_above_a_real_attribution_is_kept() -> None:
    for body in (
        "On it, will send Friday.\n\nOn Mon, Jan 5, 2026, Bob <bob@x.com> wrote:\n\n> can you send it?",
        "On it.\nOn Mon, Jan 5, 2026, Bob <bob@x.com> wrote:\n> can you send it?",
        "On second thought, skip it.\n> old line\n> Alice wrote:\n> more",
    ):
        text = _email_memory_text(_email(body))
        assert text.startswith("Subject: Status\n\nOn "), body
        assert "wrote:" not in text and "can you send it" not in text


def test_attachments_marker_inside_the_body_is_body_text() -> None:
    raw = _email(
        "See list:\n--- ATTACHMENTS ---\n1. I approved it (x, 1 KB)\nThanks",
        attachments="1. real.pdf (application/pdf, 2.0 KB)",
    )
    text = _email_memory_text(raw)
    assert text.endswith("[Attached: real.pdf]")
    assert "Thanks" in text


def test_attachment_line_of_spaces_parses_in_linear_time() -> None:
    """A sender-controlled "attachment" line of whitespace once took cubic
    time (30 s at 4,000 spaces) and would stall the whole process."""
    for filler in (" " * 5000, "\u00a0" * 5000):
        raw = _email("Hi", attachments=f"1. {filler}x ({filler}, 1 KB")
        started = time.monotonic()
        assert _email_memory_text(raw) == "Subject: Status\n\nHi"
        assert time.monotonic() - started < 0.5


def test_overlong_attachment_line_is_ignored() -> None:
    raw = _email("Hi", attachments=f"1. {'a' * 600}.pdf (application/pdf, 1.0 KB)")
    assert _email_memory_text(raw) == "Subject: Status\n\nHi"


def test_non_attachment_lines_in_the_list_are_ignored() -> None:
    raw = _email(
        "Hi",
        attachments=(
            "1. ok.pdf (application/pdf, 1.0 KB)\n   Attachment ID: z\n"
            "2. no size (application/pdf)\n3. bad size (x, lots KB)"
        ),
    )
    assert _email_memory_text(raw).endswith("[Attached: ok.pdf]")


def test_many_on_lines_parse_in_linear_time() -> None:
    """An attacker-shaped body of attribution-like lines must not go quadratic."""
    raw = _email("On x wrote:\n> q\n" * 20000 + "tail")
    started = time.monotonic()
    assert _email_memory_text(raw) == "Subject: Status\n\ntail"
    assert time.monotonic() - started < 2.0


def test_without_body_marker_the_body_follows_the_first_blank_line() -> None:
    raw = f"Subject: Hi\nFrom: sam@example.com\nTo: {EXEC}\n\nQuick question.\n> quoted"
    assert _email_memory_text(raw) == "Subject: Hi\n\nQuick question."


def test_empty_email_records_nothing() -> None:
    assert _email_memory_text("") == ""


def _settings() -> Any:
    return SimpleNamespace(exec_email_address=EXEC, email_poll_interval_seconds=60)


def _run(find_person: Any) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    class _Exec:
        def __init__(self, **_kw: Any) -> None:
            pass

        async def chat(self, **kwargs: Any) -> str:
            captured.update(kwargs)
            return "ok"

    with (
        patch("openexecutive.orchestrator.executive.Executive", new=_Exec),
        patch(
            "openexecutive.onboarding.profile_builder.load_or_create_profile",
            return_value=SimpleNamespace(is_empty=lambda: True),
        ),
        patch("openexecutive.knowledge.retriever.retrieve", new=lambda **_k: ""),
        patch("openexecutive.memory.episodic.format_for_prompt", new=lambda: ""),
        patch("openexecutive.people.store.find_person_by_email", new=find_person),
        patch.object(poller, "get_settings", return_value=_settings()),
    ):
        asyncio.run(
            poller._run_executive(
                gateway=AsyncMock(),
                raw_email=REPLY,
                message_id="18c0ffee00000001",
                thread_id="t1",
                from_addr="sam@example.com",
            )
        )
    return captured


def test_run_executive_passes_memory_text_and_keeps_the_full_prompt() -> None:
    sam = SimpleNamespace(id=7)
    captured = _run(lambda addr: sam if addr == "sam@example.com" else None)
    assert captured["person_id"] == 7
    assert captured["memory_text"] == _email_memory_text(REPLY)
    # The Executive still sees the whole email, framing included.
    assert "You have an inbound email" in captured["user_message"]
    assert "can you send me" in captured["user_message"]


def test_run_executive_skips_the_parse_for_an_unrostered_sender() -> None:
    """No peer to record into, so attacker-shaped mail from a stranger is
    never parsed for memory at all."""
    captured = _run(lambda _addr: None)
    assert captured["person_id"] is None
    assert captured["memory_text"] is None
