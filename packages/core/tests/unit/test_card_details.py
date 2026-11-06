"""An Approve card shows the step in plain words, never a file's bytes."""
from __future__ import annotations

import base64
import json

from openexecutive.integrations import channel_cards
from openexecutive.memory.decision_ledger import DecisionInstance
from openexecutive.orchestrator import card_details

_PDF = base64.b64encode(b"%PDF-1.7 " + b"x" * 300_000).decode()
_DRIVE_CALL = {
    "user_google_email": "ai@example.com",
    "file_name": "Harbor Lane Q3 Report.pdf",
    "base64_content": _PDF,
    "content_mime_type": "application/pdf",
    "base64_sha256": "ab" * 32,
    "folder_id": "1UX0G1Z8qUk8cmaeXp0CCG3eeyeF-HlnL",
}


def test_a_drive_upload_shows_name_kind_size_and_folder() -> None:
    shown = card_details.plain("google_workspace__create_drive_file", _DRIVE_CALL)
    assert shown == [
        {"field": "File name", "value": "Harbor Lane Q3 Report.pdf"},
        {"field": "File", "value": "PDF file, 293 KB"},
        {"field": "Folder", "value": "https://drive.google.com/drive/folders/1UX0G1Z8qUk8cmaeXp0CCG3eeyeF-HlnL"},
    ]
    text = json.dumps(shown)
    assert _PDF[:50] not in text and "user_google_email" not in text and "ab" * 32 not in text


def test_bytes_are_caught_by_their_look_too() -> None:
    shown = card_details.plain("some_app__upload", {"name": "scan.png", "payload": _PDF})
    assert shown[1] == {"field": "File", "value": "Image file, 293 KB"}


def test_words_under_a_bytes_like_key_are_still_shown() -> None:
    words = "Wire $50,000 to account 1234"
    assert card_details.plain("x__send", {"data": words, "base64_note": words}) == [
        {"field": "Data", "value": words}, {"field": "Base64 note", "value": words},
    ]


def test_plumbing_is_hidden_but_a_non_default_calendar_is_shown() -> None:
    call = {"user_google_email": "ai@example.com", "thread_id": "t1", "calendar_id": "primary", "title": "Sync"}
    assert card_details.plain("google_workspace__create_event", call) == [{"field": "Title", "value": "Sync"}]
    call["calendar_id"] = "team@group.calendar.google.com"
    assert {"field": "Calendar id", "value": "team@group.calendar.google.com"} in card_details.plain(
        "google_workspace__create_event", call,
    )


def test_the_account_is_shown_for_an_app_that_isnt_google() -> None:
    assert card_details.plain("microsoft_365__send-mail", {"user_email": "ceo@other.example"}) == [
        {"field": "User email", "value": "ceo@other.example"},
    ]


def test_a_call_that_only_names_its_target_still_shows_it() -> None:
    call = {"user_google_email": "ai@example.com", "draft_id": "r123"}
    assert card_details.plain("google_workspace__send_gmail_draft", call) == [{"field": "Draft id", "value": "r123"}]


def test_words_are_shown_whole() -> None:
    long = "word " * 1000
    [shown] = card_details.plain("send_email", {"body": long})
    assert shown == {"field": "Message", "value": long}
    [short] = card_details.plain("send_email", {"subject": "Final offers"})
    assert short == {"field": "Subject", "value": "Final offers"}


def test_lists_read_as_a_list() -> None:
    assert card_details.plain("send_email", {"to": ["a@example.com", "b@example.com"]}) == [
        {"field": "To", "value": "a@example.com, b@example.com"},
    ]


def test_served_payload_drops_the_bytes_and_rewrites_old_details() -> None:
    payload = {
        "tool": "google_workspace__create_drive_file", "input": dict(_DRIVE_CALL), "summary": "Create it",
        "details": [{"field": "base64_content", "value": _PDF}],
    }
    out = card_details.served(payload)
    assert out["input"]["base64_content"] == "" and out["input"]["file_name"] == _DRIVE_CALL["file_name"]
    assert out["details"][1] == {"field": "File", "value": "PDF file, 293 KB"}
    # The stored copy, which Approve reads, is untouched.
    assert payload["input"]["base64_content"] == _PDF


def test_chat_app_card_shows_plain_details(monkeypatch) -> None:
    payload = {
        "tool": "google_workspace__create_drive_file", "input": dict(_DRIVE_CALL),
        "summary": "Create Harbor Lane Q3 Report.pdf (Google Drive)",
        "reason": "the folder it goes in is shared outside the company", "held": True,
    }
    instance = DecisionInstance(
        id=5, decision_class="department_action", created_at="2026-11-04T00:00:00+00:00", department="executive",
        originating_session_id=None, proposed_payload_json=json.dumps(payload), idempotency_key=None,
        gate_mode="propose", approver_person_id=1, confidence=None, status="proposed", resolved_at=None,
        resolver_person_id=None, final_payload_json=None, external_event_id=None, reversal_reason=None, severity="",
    )
    from openexecutive.api.routes import decisions
    from openexecutive.memory import decision_ledger

    monkeypatch.setattr(decision_ledger, "get_decision_instance", lambda _id: instance)
    monkeypatch.setattr(decisions, "shown_in_chat", lambda _i: True)
    text = channel_cards.card_text(5)
    assert text is not None
    assert "File name: Harbor Lane Q3 Report.pdf" in text and "File: PDF file, 293 KB" in text
    assert _PDF[:50] not in text and "user_google_email" not in text


def test_the_decisions_route_serves_the_plain_copy() -> None:
    from openexecutive.api.routes import decisions

    payload = {"tool": "google_workspace__create_drive_file", "input": dict(_DRIVE_CALL), "summary": "Create it"}
    instance = DecisionInstance(
        id=6, decision_class="department_action", created_at="2026-11-04T00:00:00+00:00", department="executive",
        originating_session_id=None, proposed_payload_json=json.dumps(payload), idempotency_key=None,
        gate_mode="propose", approver_person_id=1, confidence=None, status="proposed", resolved_at=None,
        resolver_person_id=None, final_payload_json=None, external_event_id=None, reversal_reason=None, severity="",
    )
    shown = decisions._served(instance)
    assert _PDF[:50] not in shown.proposed_payload_json
    assert json.loads(shown.proposed_payload_json)["details"][0] == {"field": "File name", "value": "Harbor Lane Q3 Report.pdf"}
    other = instance.model_copy(update={"decision_class": "meeting_scheduling"})
    assert decisions._served(other) is other


def test_a_carried_out_step_serves_no_bytes_either() -> None:
    # final_payload_json copies the input with the result once Approve ran.
    from openexecutive.api.routes import decisions

    payload = {"tool": "google_workspace__create_drive_file", "input": dict(_DRIVE_CALL), "summary": "Create it"}
    instance = DecisionInstance(
        id=7, decision_class="department_action", created_at="2026-11-04T00:00:00+00:00", department="executive",
        originating_session_id=None, proposed_payload_json=json.dumps(payload), idempotency_key=None,
        gate_mode="propose", approver_person_id=1, confidence=None, status="approved",
        resolved_at="2026-11-04T01:00:00+00:00", resolver_person_id=1,
        final_payload_json=json.dumps({**payload, "result": "created"}), external_event_id=None,
        reversal_reason=None, severity="",
    )
    shown = decisions._served(instance)
    assert _PDF[:50] not in shown.proposed_payload_json
    assert shown.final_payload_json is not None and _PDF[:50] not in shown.final_payload_json
    assert json.loads(shown.final_payload_json)["result"] == "created"
