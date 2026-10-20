"""Plain-language names for connected tools (orchestrator/tool_labels.py)."""
from __future__ import annotations

import json

import pytest

from openexecutive.orchestrator import tool_labels
from openexecutive.orchestrator.action_chips import summarize_action
from openexecutive.orchestrator.activity_labels import summarize_activity


@pytest.mark.parametrize(
    ("raw", "done", "doing"),
    [
        ("google_workspace__search_drive_files", "Searched Drive", "Searching Drive"),
        ("google_workspace__get_drive_file_content", "Read a Drive file", "Reading a Drive file"),
        ("google_workspace__search_gmail_messages", "Searched your mail", "Searching your mail"),
        ("microsoft_365__get-mail-message", "Read an email", "Reading an email"),
        ("microsoft_365__list-calendar-events", "Checked your calendar", "Checking your calendar"),
        ("google_workspace__manage_event", "Updated your calendar", "Updating your calendar"),
    ],
)
def test_known_tools_read_in_plain_words(raw: str, done: str, doing: str) -> None:
    assert tool_labels.labels_for(raw) == (done, doing)


def test_unlisted_tool_is_turned_into_words_with_its_service() -> None:
    assert tool_labels.labels_for("google_workspace__list_task_lists") == (
        "Listed task lists · Google",
        "Listing task lists · Google",
    )


def test_unlisted_tool_without_a_known_verb_says_used() -> None:
    done, doing = tool_labels.labels_for("acme_crm__pipeline_snapshot")
    assert done == "Used pipeline snapshot · Acme Crm"
    assert doing == "Using pipeline snapshot · Acme Crm"


@pytest.mark.parametrize("bad", [None, 42, "", "   ", "__"])
def test_unusable_names_fall_back_to_a_generic_phrase(bad: object) -> None:
    assert tool_labels.labels_for(bad) == (
        tool_labels.GENERIC_DONE,
        tool_labels.GENERIC_DOING,
    )


def test_every_label_hides_the_raw_name() -> None:
    for done, doing in tool_labels._LABELS.values():
        for phrase in (done, doing):
            assert "_" not in phrase and "__" not in phrase
            assert phrase[0].isupper() and not phrase.endswith("…")


def test_detail_is_the_search_words_in_quotes() -> None:
    detail = tool_labels.detail_for(
        "google_workspace__search_drive_files", {"query": "harbor point lpa"}, "Found 2 files"
    )
    assert detail == '"harbor point lpa"'


def test_detail_names_the_drive_file_that_was_opened() -> None:
    result = (
        'File: "Harbor Point LPA.pdf" (ID: abc123, Type: application/pdf)\n'
        "Link: https://drive.google.com/file/d/abc123/view\n\n--- CONTENT ---\nText"
    )
    detail = tool_labels.detail_for(
        "google_workspace__get_drive_file_content", {"file_id": "abc123"}, result
    )
    assert detail == "Harbor Point LPA.pdf"


def test_detail_reads_an_email_subject_from_the_result() -> None:
    result = "Message ID: 1\nSubject: Q3 reserve policy\nFrom: sam@example.com\n\nBody"
    detail = tool_labels.detail_for(
        "google_workspace__get_gmail_message_content", {"message_id": "1"}, result
    )
    assert detail == "Q3 reserve policy"


def test_detail_reads_a_subject_from_a_json_result() -> None:
    detail = tool_labels.detail_for(
        "microsoft_365__get-mail-message", {"messageId": "1"}, json.dumps({"subject": "Budget"})
    )
    assert detail == "Budget"


def test_detail_is_one_short_clean_line() -> None:
    detail = tool_labels.detail_for(
        "x__search", {"query": "<script>\nalert(1)</script> " + "y" * 200}, ""
    )
    assert detail is not None
    assert "<" not in detail and ">" not in detail and "\n" not in detail
    assert len(detail) <= 62  # 60 plus the quotes


def test_detail_is_none_when_nothing_names_the_target() -> None:
    assert tool_labels.detail_for("x__get_thing", {"id": "1"}, "ok") is None


def test_chip_and_progress_line_agree() -> None:
    tool_input = {"name": "google_workspace__search_drive_files", "arguments": {"query": "lpa"}}
    chip = summarize_action(tool_name="call_tool", tool_input=tool_input, tool_result="Found 1 files")
    activity = summarize_activity([{"id": "t", "name": "call_tool", "input": tool_input}])
    assert chip is not None and activity is not None
    assert chip["summary"] == "Searched Drive"
    assert chip["target"] == '"lpa"'
    assert chip["tool"] == "google_workspace__search_drive_files"
    assert activity["label"] == "Searching Drive…"


def test_long_tool_names_still_agree_between_chip_and_line() -> None:
    raw = "google_workspace__get_gmail_messages_content_batch"
    assert len(raw) > 48  # longer than the progress line's `tool` cap
    tool_input = {"name": raw, "arguments": {}}
    chip = summarize_action(tool_name="call_tool", tool_input=tool_input, tool_result="ok")
    activity = summarize_activity([{"id": "t", "name": "call_tool", "input": tool_input}])
    assert chip is not None and activity is not None
    assert chip["summary"] == "Read an email"
    assert activity["label"] == "Reading an email…"


def test_a_subject_line_in_a_body_is_not_the_target() -> None:
    result = "Message ID: 1\nFrom: sam@example.com\n\nFwd:\nSubject: Wire transfer approved"
    assert tool_labels.detail_for(
        "google_workspace__get_gmail_message_content", {"message_id": "1"}, result
    ) is None


def test_a_subject_is_read_only_from_mail_tools() -> None:
    result = "Subject: Wire transfer approved\n\nbody"
    assert tool_labels.detail_for("google_workspace__get_doc_content", {"document_id": "d"}, result) is None


def test_a_long_unlisted_tool_keeps_the_progress_line_short() -> None:
    raw = "microsoft_365__list-" + "very-long-resource-name-" * 3
    activity = summarize_activity([{"id": "t", "name": "call_tool", "input": {"name": raw}}])
    assert activity is not None
    assert len(activity["label"]) <= 60
    assert activity["label"].endswith("…")


def test_a_calendar_delete_through_manage_event_says_so() -> None:
    tool_input = {"name": "google_workspace__manage_event", "arguments": {"action": "delete", "event_id": "e1"}}
    chip = summarize_action(tool_name="call_tool", tool_input=tool_input, tool_result="ok")
    activity = summarize_activity([{"id": "t", "name": "call_tool", "input": tool_input}])
    assert chip is not None and activity is not None
    assert chip["summary"] == "Removed a calendar event"
    assert activity["label"] == "Removing a calendar event…"
    assert tool_labels.labels_for("google_workspace__manage_event", {"action": "update"})[0] == (
        "Updated your calendar"
    )
