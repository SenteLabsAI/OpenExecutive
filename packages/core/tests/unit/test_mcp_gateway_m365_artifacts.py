"""Artifact attachments on Microsoft 365 mail tools.

The Outlook twin of the Gmail cases in test_artifact_delivery.py: an
`{"artifact_id": …}` entry — at the top level of the arguments (where the
artifact tool text tells the model to put it) or already inside the Graph
message — is rendered into a `#microsoft.graph.fileAttachment` on the message
at the location Graph expects, only after the recipient gate passes, with the
same caps, refusals and audit rows as Gmail.
"""
from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import openexecutive.orchestrator.mcp_gateway as gw_module
from openexecutive.alerts import store as alerts_store
from openexecutive.orchestrator.mcp_gateway import MCPGateway
from openexecutive.people import store as people_store
from openexecutive.workflows import persistence as wf_persistence

EXEC_ADDR = "exec@contoso.com"
ALICE = "alice@contoso.com"


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "episodic.db"
    for module in (alerts_store, people_store, wf_persistence):
        monkeypatch.setattr(module, "DB_PATH", db_path)
    alerts_store.initialize_db(db_path)
    people_store.initialize_db()
    wf_persistence.initialize_runs_db(db_path)
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **k: None)
    return db_path


@pytest.fixture()
def audit(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "openexecutive.audit.log_event",
        lambda event, summary, **kw: events.append((event, kw.get("details", {}))),
    )
    return events


def _artifact(title: str = "Q3 plan") -> str:
    aid = alerts_store.insert_alert(
        source="artifact", external_id=f"markdown-{title}", severity="medium",
        headline=title, body="# Q3 plan\n\nHire two engineers.", topic_tags=["artifact"],
        artifact_format="markdown",
    )
    assert aid is not None
    return f"alert:{aid}"


def _gateway(result_text: str = '{"ok": true}') -> tuple[MCPGateway, AsyncMock]:
    gateway = MCPGateway()
    session = MagicMock()
    fake_result = MagicMock()
    fake_result.content = [MagicMock(text=result_text)]
    session.call_tool = AsyncMock(return_value=fake_result)
    gateway._session = session
    return gateway, session.call_tool


def _message(to: str = ALICE, **extra: Any) -> dict[str, Any]:
    return {
        "subject": "Plan",
        "body": {"contentType": "Text", "content": "Attached."},
        "toRecipients": [{"emailAddress": {"address": to}}],
        **extra,
    }


def _send(arguments: dict[str, Any], tool: str = "microsoft_365__send-mail",
          result_text: str = '{"ok": true}') -> tuple[str, AsyncMock]:
    people_store.upsert_person(full_name="Alice", email=ALICE)
    gateway, session_call = _gateway(result_text)
    settings = SimpleNamespace(exec_email_address=EXEC_ADDR, email_poll_interval_seconds=60)
    with patch.object(gw_module, "get_settings", return_value=settings):
        result = asyncio.run(gateway.call_tool({"name": tool, "arguments": arguments}))
    return result, session_call


def _forwarded(session_call: AsyncMock) -> dict[str, Any]:
    assert session_call.await_count == 1
    return session_call.await_args.args[1]["arguments"]


def _assert_graph_file(att: dict[str, Any]) -> None:
    assert att["@odata.type"] == "#microsoft.graph.fileAttachment"
    assert att["name"] == "q3-plan.md"
    assert att["contentType"].startswith("text/markdown")
    assert base64.b64decode(att["contentBytes"]).decode() == "# Q3 plan\n\nHire two engineers."
    assert set(att) == {"@odata.type", "name", "contentType", "contentBytes"}


def test_top_level_artifact_entry_moves_into_the_graph_message() -> None:
    cid = _artifact()
    result, session_call = _send({
        "body": {"Message": _message(), "SaveToSentItems": True},
        "attachments": [{"artifact_id": cid}],
    })
    assert result == '{"ok": true}'
    forwarded = _forwarded(session_call)
    assert "attachments" not in forwarded
    (att,) = forwarded["body"]["Message"]["attachments"]
    _assert_graph_file(att)
    # Everything else on the message survives, casing included.
    assert forwarded["body"]["SaveToSentItems"] is True
    assert forwarded["body"]["Message"]["toRecipients"] == _message()["toRecipients"]


def test_nested_artifact_entry_expands_in_place_and_keeps_other_entries() -> None:
    cid = _artifact()
    passthrough = {"@odata.type": "#microsoft.graph.fileAttachment", "name": "a.txt",
                   "contentBytes": "aGk="}
    _, session_call = _send({
        "body": {"Message": _message(attachments=[passthrough, {"artifact_id": cid}])},
    })
    first, second = _forwarded(session_call)["body"]["Message"]["attachments"]
    assert first == passthrough
    _assert_graph_file(second)


def test_draft_email_takes_attachments_on_the_message_itself() -> None:
    cid = _artifact()
    _, session_call = _send(
        {"body": _message(), "attachments": [{"artifact_id": cid}]},
        tool="microsoft_365__create-draft-email",
    )
    forwarded = _forwarded(session_call)
    assert "attachments" not in forwarded
    (att,) = forwarded["body"]["attachments"]
    _assert_graph_file(att)


def test_stringified_top_level_and_nested_lists_are_combined() -> None:
    cid = _artifact()
    _, session_call = _send({
        "body": {"Message": _message(attachments=json.dumps([{"artifact_id": cid}]))},
        "attachments": json.dumps({"artifact_id": cid, "as": "docx"}),
    })
    md, docx = _forwarded(session_call)["body"]["Message"]["attachments"]
    assert md["name"] == "q3-plan.md"
    assert docx["name"] == "q3-plan.docx"


def test_tool_that_cannot_carry_attachments_refuses_an_artifact_entry() -> None:
    cid = _artifact()
    result, session_call = _send(
        {"messageId": "AAMk1", "body": {"isRead": True}, "attachments": [{"artifact_id": cid}]},
        tool="microsoft_365__update-mail-message",
    )
    assert "cannot carry attachments" in json.loads(result)["error"]
    assert session_call.await_count == 0


def test_blocked_recipient_never_renders_the_artifact(monkeypatch: pytest.MonkeyPatch) -> None:
    rendered: list[str] = []
    import openexecutive.orchestrator.artifact_records as records

    real = records.render_artifact_file

    def _spy(rec, as_=None):  # noqa: ANN001, ANN202
        rendered.append(rec.id)
        return real(rec, as_)

    monkeypatch.setattr(records, "render_artifact_file", _spy)
    cid = _artifact()
    result, session_call = _send({
        "body": {"Message": _message(to="attacker@evil.example")},
        "attachments": [{"artifact_id": cid}],
    })
    assert "error" in json.loads(result)
    assert session_call.await_count == 0
    assert rendered == []


@pytest.mark.parametrize("entry, needle", [
    ({"artifact_id": "alert:4242"}, "not found"),
    ({"artifact_id": "nonsense"}, "Malformed"),
    ({"artifact_id": "ALERT_PLACEHOLDER", "path": "/etc/passwd"}, "only 'artifact_id'"),
])
def test_artifact_errors_send_nothing(entry: dict, needle: str) -> None:
    cid = _artifact()
    entry = {k: (cid if v == "ALERT_PLACEHOLDER" else v) for k, v in entry.items()}
    result, session_call = _send({"body": {"Message": _message()}, "attachments": [entry]})
    assert needle in json.loads(result)["error"]
    assert session_call.await_count == 0


def test_without_artifact_entries_arguments_are_untouched() -> None:
    args = {"body": {"Message": _message(attachments=[{"name": "x", "contentBytes": "aGk="}])}}
    _, session_call = _send(json.loads(json.dumps(args)))
    assert _forwarded(session_call) == args


def test_audits_attachment_with_graph_recipients_after_a_successful_send(
    audit: list[tuple[str, dict]],
) -> None:
    cid = _artifact()
    _send({"body": {"Message": _message(ccRecipients=[{"emailAddress": {"address": EXEC_ADDR}}])},
           "attachments": [{"artifact_id": cid}]})
    attached = [d for e, d in audit if e == "artifact_attached"]
    assert len(attached) == 1
    assert attached[0]["artifact_ids"] == [cid]
    assert attached[0]["recipients"] == [ALICE, EXEC_ADDR]
    assert attached[0]["tool"] == "microsoft_365__send-mail"


def test_does_not_audit_attachment_when_graph_rejects_the_send(
    audit: list[tuple[str, dict]],
) -> None:
    cid = _artifact()
    _send({"body": {"Message": _message()}, "attachments": [{"artifact_id": cid}]},
          result_text=json.dumps({"error": "boom"}))
    assert [e for e, _ in audit if e == "artifact_attached"] == []


def test_refusal_is_audited_with_graph_recipients(audit: list[tuple[str, dict]]) -> None:
    _send({"body": {"Message": _message()}, "attachments": [{"artifact_id": "alert:4242"}]})
    refused = [d for e, d in audit if e == "artifact_attachment_refused"]
    assert len(refused) == 1
    assert refused[0]["recipients"] == [ALICE]


def test_update_mail_message_refuses_an_artifact_entry_anywhere() -> None:
    cid = _artifact()
    result, session_call = _send(
        {"messageId": "m", "body": {"attachments": [{"artifact_id": cid}]}},
        tool="microsoft_365__update-mail-message",
    )
    assert "cannot carry attachments" in json.loads(result)["error"]
    assert session_call.await_count == 0


def test_send_draft_message_refuses_an_artifact_entry_in_the_expander() -> None:
    """In `call_tool` the referenced-message gate refuses send-draft first;
    the expander itself must still refuse rather than pass the entry on."""
    cid = _artifact()
    with patch("openexecutive.audit.log_event", lambda *a, **k: None):
        result = asyncio.run(gw_module._expand_m365_artifact_attachments(
            "microsoft_365__send-draft-message", "microsoft_365__send_draft_message",
            {"messageId": "m", "body": {"attachments": [{"artifact_id": cid}]}},
        ))
    assert isinstance(result, str)
    assert "cannot carry attachments" in json.loads(result)["error"]


@pytest.mark.parametrize("tool, arguments", [
    # The other tool's location.
    ("microsoft_365__create-draft-email",
     {"body": {"Message": {"subject": "s", "attachments": [{"artifact_id": "ALERT"}]}}}),
    ("microsoft_365__send-mail", {"body": {"attachments": [{"artifact_id": "ALERT"}]}}),
    # Somewhere unrelated, and stringified.
    ("microsoft_365__send-mail",
     {"body": {"Message": {"subject": "s", "extra": {"artifact_id": "ALERT"}}}}),
    ("microsoft_365__send-mail",
     {"body": {"Message": {"subject": "s", "note": '[{"artifact_id": "ALERT"}]'}}}),
])
def test_artifact_entry_outside_the_expanded_locations_is_refused(
    tool: str, arguments: dict,
) -> None:
    cid = _artifact()
    arguments = json.loads(json.dumps(arguments).replace("ALERT", cid))
    result, session_call = _send(arguments, tool=tool)
    assert "must be in the top-level 'attachments'" in json.loads(result)["error"]
    assert session_call.await_count == 0


def test_capitalised_top_level_attachments_key_is_moved_too() -> None:
    cid = _artifact()
    _, session_call = _send({"body": {"Message": _message()}, "Attachments": [{"artifact_id": cid}]})
    forwarded = _forwarded(session_call)
    assert "Attachments" not in forwarded and "attachments" not in forwarded
    (att,) = forwarded["body"]["Message"]["attachments"]
    _assert_graph_file(att)


@pytest.mark.parametrize("body", ['{"subject": "Q3"}', ["x"], 7])
def test_stringified_or_non_object_body_is_refused_not_wiped(body: Any) -> None:
    cid = _artifact()
    result, session_call = _send(
        {"body": body, "attachments": [{"artifact_id": cid}]},
        tool="microsoft_365__create-draft-email",
    )
    assert "'body' must be a JSON object" in json.loads(result)["error"]
    assert session_call.await_count == 0


def test_single_object_graph_attachment_survives_the_combine() -> None:
    cid = _artifact()
    existing = {"@odata.type": "#microsoft.graph.fileAttachment", "name": "a.pdf",
                "contentBytes": "AA=="}
    _, session_call = _send({
        "body": {"Message": _message(attachments=existing)},
        "attachments": {"artifact_id": cid},
    })
    first, second = _forwarded(session_call)["body"]["Message"]["attachments"]
    assert first == existing
    _assert_graph_file(second)


def test_non_json_nested_attachments_string_is_refused_when_an_artifact_is_present() -> None:
    cid = _artifact()
    result, session_call = _send({
        "body": {"Message": _message(attachments="not json")},
        "attachments": [{"artifact_id": cid}],
    })
    assert "not valid JSON" in json.loads(result)["error"]
    assert session_call.await_count == 0


def test_caller_arguments_are_not_mutated() -> None:
    cid = _artifact()
    args = {"body": {"Message": _message()}, "attachments": [{"artifact_id": cid}]}
    snapshot = json.loads(json.dumps(args))
    _send(args)
    assert args == snapshot
