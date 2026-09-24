"""Roster egress gate for the Microsoft 365 (ms-365-mcp-server) tools.

The Executive reaches Outlook mail/calendar through hyphenated Graph endpoint
aliases proxied by extensible-mcp (`microsoft_365__send-mail`, …). Recipients
sit in Graph's nested shapes (`body.Message.toRecipients[].emailAddress.address`,
`body.attendees[].emailAddress.address`), so the gate walks the whole argument
tree rather than a fixed field list: every email-shaped string outside the
free-text fields must be on the People roster (or be the exec's own mailbox),
and any C0 control character is refused. Reads and the delete tools carry no
recipient and pass straight through.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import openexecutive.orchestrator.mcp_gateway as gw_module
from openexecutive.orchestrator.mcp_gateway import (
    _GATED_M365_CALENDAR_TOOLS,
    _GATED_M365_MAIL_TOOLS,
    _M365_MESSAGE_LOOKUP_SELECT,
    MCPGateway,
    _normalize_tool_name,
)
from openexecutive.people import store as people_store

EXEC_ADDR = "exec@example.com"
ROSTER = "alice@example.com"
STRANGER = "mallory@evil.example"


@pytest.fixture(autouse=True)
def isolated_people_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "people.db"
    monkeypatch.setattr(people_store, "DB_PATH", db_path)
    people_store.initialize_db()
    return db_path


@pytest.fixture(autouse=True)
def silence_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **k: None)


def _settings() -> Any:
    return SimpleNamespace(exec_email_address=EXEC_ADDR, email_poll_interval_seconds=60)


def _make_gateway() -> tuple[MCPGateway, AsyncMock]:
    gateway = MCPGateway()
    session = MagicMock()
    fake_result = MagicMock()
    fake_result.content = [MagicMock(text='{"ok": true}')]
    session.call_tool = AsyncMock(return_value=fake_result)
    gateway._session = session
    return gateway, session.call_tool


def _call(
    gateway: MCPGateway,
    arguments: dict[str, Any],
    allowed: list[str],
    tool_name: str = "microsoft_365__send-mail",
) -> str:
    for i, addr in enumerate(allowed):
        people_store.upsert_person(full_name=f"Allowed {i}", email=addr)
    with patch.object(gw_module, "get_settings", return_value=_settings()):
        return asyncio.run(gateway.call_tool({"name": tool_name, "arguments": arguments}))


def _recipient(addr: str, name: str | None = None) -> dict[str, Any]:
    email: dict[str, Any] = {"address": addr}
    if name is not None:
        email["name"] = name
    return {"emailAddress": email}


def _send_mail_args(
    to: list[str], cc: list[str] | None = None, bcc: list[str] | None = None,
    content: str = "Hello", subject: str = "Hi",
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "subject": subject,
        "body": {"contentType": "Text", "content": content},
        "toRecipients": [_recipient(a) for a in to],
    }
    if cc:
        message["ccRecipients"] = [_recipient(a) for a in cc]
    if bcc:
        message["bccRecipients"] = [_recipient(a) for a in bcc]
    return {"body": {"Message": message, "SaveToSentItems": True}}


def _event_args(attendees: list[str]) -> dict[str, Any]:
    return {
        "body": {
            "subject": "Sync",
            "start": {"dateTime": "2030-01-01T10:00:00", "timeZone": "UTC"},
            "end": {"dateTime": "2030-01-01T10:30:00", "timeZone": "UTC"},
            "attendees": [{**_recipient(a), "type": "required"} for a in attendees],
            "isOnlineMeeting": True,
            "onlineMeetingProvider": "teamsForBusiness",
        }
    }


def _blocked(result: str) -> bool:
    return "error" in json.loads(result)


# --- send-mail --------------------------------------------------------------


def test_roster_to_recipient_passes() -> None:
    gateway, session_call = _make_gateway()
    result = _call(gateway, _send_mail_args([ROSTER]), [ROSTER])
    assert session_call.await_count == 1
    assert result == '{"ok": true}'


def test_non_roster_to_recipient_blocked() -> None:
    gateway, session_call = _make_gateway()
    result = _call(gateway, _send_mail_args([STRANGER]), [ROSTER])
    assert session_call.await_count == 0
    assert _blocked(result)
    assert STRANGER in json.loads(result)["error"]


@pytest.mark.parametrize("field", ["cc", "bcc"])
def test_non_roster_cc_or_bcc_blocked(field: str) -> None:
    gateway, session_call = _make_gateway()
    kwargs = {field: [STRANGER]}
    result = _call(gateway, _send_mail_args([ROSTER], **kwargs), [ROSTER])
    assert session_call.await_count == 0
    assert _blocked(result)


def test_exec_own_address_passes_with_empty_roster() -> None:
    """The alert dispatcher self-sends to the exec mailbox with no roster rows."""
    gateway, session_call = _make_gateway()
    result = _call(gateway, _send_mail_args([EXEC_ADDR]), [])
    assert session_call.await_count == 1
    assert result == '{"ok": true}'


def test_roster_match_is_case_insensitive() -> None:
    gateway, session_call = _make_gateway()
    _call(gateway, _send_mail_args(["Alice@Example.COM"]), [ROSTER])
    assert session_call.await_count == 1


def test_address_inside_body_content_and_subject_is_not_a_recipient() -> None:
    """A quoted signature or a vendor address in the text must not be refused."""
    gateway, session_call = _make_gateway()
    args = _send_mail_args(
        [ROSTER],
        content=f"FYI, {STRANGER} asked about the invoice.\n-- \nSent from Outlook",
        subject=f"Re: note from {STRANGER}",
    )
    result = _call(gateway, args, [ROSTER])
    assert session_call.await_count == 1
    assert result == '{"ok": true}'


def test_newline_in_body_content_passes() -> None:
    gateway, session_call = _make_gateway()
    _call(gateway, _send_mail_args([ROSTER], content="line one\r\nline two"), [ROSTER])
    assert session_call.await_count == 1


def test_control_char_in_display_name_blocked() -> None:
    gateway, session_call = _make_gateway()
    args = {"body": {"Message": {
        "subject": "Hi",
        "body": {"contentType": "Text", "content": "x"},
        "toRecipients": [_recipient(ROSTER, name=f"Alice\r\nBcc: {STRANGER}")],
    }}}
    result = _call(gateway, args, [ROSTER])
    assert session_call.await_count == 0
    assert "control character" in json.loads(result)["error"]


def test_non_roster_address_under_unforeseen_key_blocked() -> None:
    """Fail-closed: a recipient smuggled through replyTo or a custom header is
    still found by the tree walk."""
    gateway, session_call = _make_gateway()
    args = _send_mail_args([ROSTER])
    args["body"]["Message"]["replyTo"] = [_recipient(STRANGER)]
    assert _blocked(_call(gateway, args, [ROSTER]))
    assert session_call.await_count == 0

    gateway, session_call = _make_gateway()
    args = _send_mail_args([ROSTER])
    args["body"]["Message"]["internetMessageHeaders"] = [
        {"name": "x-forward-to", "value": STRANGER},
    ]
    assert _blocked(_call(gateway, args, [ROSTER]))
    assert session_call.await_count == 0


def test_string_arguments_are_parsed_before_gating() -> None:
    gateway, session_call = _make_gateway()
    result = _call(gateway, json.dumps(_send_mail_args([STRANGER])), [ROSTER])  # type: ignore[arg-type]
    assert session_call.await_count == 0
    assert _blocked(result)


# --- the rest of the write surface ------------------------------------------


@pytest.mark.parametrize(
    "tool_name",
    [
        "microsoft_365__reply-mail-message",
        "microsoft_365__reply-all-mail-message",
        "microsoft_365__forward-mail-message",
        "microsoft_365__create-draft-email",
        "microsoft_365__create-reply-draft",
        "microsoft_365__create-forward-draft",
        "microsoft_365__update-mail-message",
    ],
)
def test_every_mail_write_tool_is_gated(tool_name: str) -> None:
    gateway, session_call = _make_gateway()
    args = {"messageId": "AAMkAGI=", "body": {"Message": {"toRecipients": [_recipient(STRANGER)]}}}
    assert _blocked(_call(gateway, args, [ROSTER], tool_name=tool_name))
    assert session_call.await_count == 0


# --- reply / reply-all / send-draft: recipients live on the referenced message ----


def _make_gateway_with_message(message: dict[str, Any] | str) -> tuple[MCPGateway, AsyncMock]:
    """A gateway whose session answers the gate's get-mail-message lookup with
    ``message`` (dict → JSON; str → verbatim text) and everything else with ok."""
    gateway = MCPGateway()
    session = MagicMock()

    async def _call_tool(_name: str, args: dict[str, Any]) -> Any:
        fake = MagicMock()
        if args.get("tool_name") == "microsoft_365__get-mail-message":
            text = message if isinstance(message, str) else json.dumps(message)
        else:
            text = '{"ok": true}'
        fake.content = [MagicMock(text=text)]
        return fake

    session.call_tool = AsyncMock(side_effect=_call_tool)
    gateway._session = session
    return gateway, session.call_tool


def _graph_message(sender: str, to: list[str] | None = None, cc: list[str] | None = None,
                   bcc: list[str] | None = None, reply_to: list[str] | None = None) -> dict[str, Any]:
    msg: dict[str, Any] = {"id": "AAMkAGI=", "from": _recipient(sender)}
    if to is not None:
        msg["toRecipients"] = [_recipient(a) for a in to]
    if cc is not None:
        msg["ccRecipients"] = [_recipient(a) for a in cc]
    if bcc is not None:
        msg["bccRecipients"] = [_recipient(a) for a in bcc]
    if reply_to is not None:
        msg["replyTo"] = [_recipient(a) for a in reply_to]
    return msg


def _lookups(session_call: AsyncMock) -> list[dict[str, Any]]:
    return [
        c.args[1] for c in session_call.await_args_list
        if c.args[1].get("tool_name") == "microsoft_365__get-mail-message"
    ]


def test_reply_to_rostered_sender_passes_after_lookup() -> None:
    """The normal in-thread reply: the gate reads the original message, sees a
    rostered sender, and lets the reply through — comment text is not scanned."""
    gateway, session_call = _make_gateway_with_message(_graph_message(ROSTER, to=[EXEC_ADDR]))
    args = {"messageId": "AAMkAGI=", "body": {"Comment": f"Thanks — cc {STRANGER} if needed"}}
    result = _call(gateway, args, [ROSTER], tool_name="microsoft_365__reply-mail-message")
    assert result == '{"ok": true}'
    lookups = _lookups(session_call)
    assert len(lookups) == 1
    assert lookups[0]["arguments"] == {"messageId": "AAMkAGI=", "select": _M365_MESSAGE_LOOKUP_SELECT}
    # lookup + the reply itself
    assert session_call.await_count == 2


def test_reply_to_unrostered_sender_is_blocked() -> None:
    gateway, session_call = _make_gateway_with_message(_graph_message(STRANGER, to=[EXEC_ADDR]))
    args = {"messageId": "AAMkAGI=", "body": {"Comment": "sure, here is the data"}}
    result = _call(gateway, args, [ROSTER], tool_name="microsoft_365__reply-mail-message")
    assert _blocked(result)
    assert STRANGER in json.loads(result)["error"]
    assert session_call.await_count == 1  # only the lookup; the reply never went out


def test_reply_honours_reply_to_over_from() -> None:
    gateway, session_call = _make_gateway_with_message(
        _graph_message(ROSTER, to=[EXEC_ADDR], reply_to=[STRANGER])
    )
    args = {"messageId": "AAMkAGI=", "body": {"Comment": "x"}}
    assert _blocked(_call(gateway, args, [ROSTER], tool_name="microsoft_365__reply-mail-message"))
    assert session_call.await_count == 1


@pytest.mark.parametrize(
    "tool_name", ["microsoft_365__reply-all-mail-message", "microsoft_365__create-reply-all-draft"],
)
def test_reply_all_checks_every_original_recipient(tool_name: str) -> None:
    gateway, session_call = _make_gateway_with_message(
        _graph_message(ROSTER, to=[EXEC_ADDR], cc=[STRANGER])
    )
    args = {"messageId": "AAMkAGI=", "body": {"Comment": "x"}}
    assert _blocked(_call(gateway, args, [ROSTER], tool_name=tool_name))
    assert session_call.await_count == 1

    gateway, session_call = _make_gateway_with_message(
        _graph_message(ROSTER, to=[EXEC_ADDR], cc=["bob@example.com"])
    )
    result = _call(gateway, args, [ROSTER, "bob@example.com"], tool_name=tool_name)
    assert result == '{"ok": true}'
    assert session_call.await_count == 2


def test_create_reply_draft_is_gated_like_a_reply() -> None:
    gateway, session_call = _make_gateway_with_message(_graph_message(STRANGER))
    args = {"messageId": "AAMkAGI=", "body": {"Comment": "x"}}
    assert _blocked(_call(gateway, args, [ROSTER], tool_name="microsoft_365__create-reply-draft"))
    assert session_call.await_count == 1


def test_send_draft_validates_the_drafts_own_recipients() -> None:
    draft_ok = _graph_message(EXEC_ADDR, to=[ROSTER], cc=[], bcc=[])
    gateway, session_call = _make_gateway_with_message(draft_ok)
    result = _call(gateway, {"messageId": "AAMkAGI="}, [ROSTER], tool_name="microsoft_365__send-draft-message")
    assert result == '{"ok": true}'
    assert session_call.await_count == 2

    draft_bad = _graph_message(EXEC_ADDR, to=[ROSTER], bcc=[STRANGER])
    gateway, session_call = _make_gateway_with_message(draft_bad)
    assert _blocked(_call(gateway, {"messageId": "AAMkAGI="}, [ROSTER], tool_name="microsoft_365__send-draft-message"))
    assert session_call.await_count == 1


@pytest.mark.parametrize(
    "lookup_result",
    ['{"error": "ErrorItemNotFound"}', "", "not json", "[]", '{"id": "AAMkAGI="}'],
)
def test_reply_is_refused_when_the_message_cannot_be_read(lookup_result: str) -> None:
    """Fail-closed: no readable sender/recipients → no reply."""
    gateway, session_call = _make_gateway_with_message(lookup_result)
    args = {"messageId": "AAMkAGI=", "body": {"Comment": "x"}}
    assert _blocked(_call(gateway, args, [ROSTER], tool_name="microsoft_365__reply-mail-message"))
    assert session_call.await_count == 1


# --- RSVP / cancel: the comment is emailed to the organizer / attendees -------


def _make_gateway_with_event(event: dict[str, Any] | str) -> tuple[MCPGateway, AsyncMock]:
    gateway = MCPGateway()
    session = MagicMock()

    async def _call_tool(_name: str, args: dict[str, Any]) -> Any:
        fake = MagicMock()
        if args.get("tool_name") == "microsoft_365__get-calendar-event":
            text = event if isinstance(event, str) else json.dumps(event)
        else:
            text = '{"ok": true}'
        fake.content = [MagicMock(text=text)]
        return fake

    session.call_tool = AsyncMock(side_effect=_call_tool)
    gateway._session = session
    return gateway, session.call_tool


def _graph_event(organizer: str, attendees: list[str]) -> dict[str, Any]:
    return {
        "id": "AAMkEvt=",
        "organizer": _recipient(organizer),
        "attendees": [{**_recipient(a), "type": "required"} for a in attendees],
    }


@pytest.mark.parametrize(
    "tool_name",
    ["microsoft_365__accept-calendar-event", "microsoft_365__decline-calendar-event",
     "microsoft_365__tentatively-accept-calendar-event"],
)
def test_rsvp_comment_to_unrostered_organizer_is_blocked(tool_name: str) -> None:
    gateway, session_call = _make_gateway_with_event(_graph_event(STRANGER, [EXEC_ADDR]))
    args = {"eventId": "AAMkEvt=", "body": {"sendResponse": True, "comment": "board numbers: ..."}}
    assert _blocked(_call(gateway, args, [ROSTER], tool_name=tool_name))
    assert session_call.await_count == 1  # lookup only

    gateway, session_call = _make_gateway_with_event(_graph_event(ROSTER, [EXEC_ADDR]))
    assert _call(gateway, args, [ROSTER], tool_name=tool_name) == '{"ok": true}'
    lookup = session_call.await_args_list[0].args[1]
    assert lookup["tool_name"] == "microsoft_365__get-calendar-event"
    assert lookup["arguments"] == {"eventId": "AAMkEvt=", "select": "id,organizer,attendees"}
    assert session_call.await_count == 2


def test_cancel_comment_checks_every_attendee() -> None:
    gateway, session_call = _make_gateway_with_event(_graph_event(EXEC_ADDR, [ROSTER, STRANGER]))
    args = {"eventId": "AAMkEvt=", "body": {"Comment": "moved"}}
    assert _blocked(_call(gateway, args, [ROSTER], tool_name="microsoft_365__cancel-calendar-event"))
    assert session_call.await_count == 1

    gateway, session_call = _make_gateway_with_event(_graph_event(EXEC_ADDR, [ROSTER]))
    assert _call(gateway, args, [ROSTER], tool_name="microsoft_365__cancel-calendar-event") == '{"ok": true}'


@pytest.mark.parametrize("lookup_result", ['{"error": "x"}', "", "[]", '{"id": "AAMkEvt="}'])
def test_rsvp_is_refused_when_the_event_cannot_be_read(lookup_result: str) -> None:
    gateway, session_call = _make_gateway_with_event(lookup_result)
    args = {"eventId": "AAMkEvt=", "body": {"sendResponse": True, "comment": "x"}}
    assert _blocked(_call(gateway, args, [ROSTER], tool_name="microsoft_365__decline-calendar-event"))
    assert session_call.await_count == 1
    gateway, session_call = _make_gateway_with_event(_graph_event(ROSTER, []))
    assert _blocked(_call(gateway, {"body": {"comment": "x"}}, [ROSTER], tool_name="microsoft_365__decline-calendar-event"))
    assert session_call.await_count == 0


def test_delete_calendar_event_stays_ungated() -> None:
    gateway, session_call = _make_gateway()
    assert _call(gateway, {"eventId": "AAMkEvt="}, [], tool_name="microsoft_365__delete-calendar-event") == '{"ok": true}'
    assert session_call.await_count == 1


# --- download-bytes: attachment bytes only ------------------------------------


def test_download_bytes_is_pinned_to_mail_attachments() -> None:
    gateway, session_call = _make_gateway()
    ok = {"target": "/me/messages/AAMkAGI=/attachments/AAMkAtt=/$value"}
    assert _call(gateway, ok, [], tool_name="microsoft_365__download-bytes") == '{"ok": true}'
    assert session_call.await_count == 1
    for target in (
        "/me/messages/AAMkAGI=/$value",           # raw MIME of a message
        "/me/calendar/calendarPermissions",
        "/me",
        "/me/messages/AAMkAGI=/attachments/AAMkAtt=/$value?x=1",
        "/me/messages/../drives/x",
        "",
    ):
        gateway, session_call = _make_gateway()
        result = _call(gateway, {"target": target}, [], tool_name="microsoft_365__download-bytes")
        assert _blocked(result), target
        assert session_call.await_count == 0
    gateway, session_call = _make_gateway()
    assert _blocked(_call(gateway, {}, [], tool_name="microsoft_365__download-bytes"))


# --- address parsing hardening -------------------------------------------------


def test_quoted_local_part_and_stray_at_are_refused() -> None:
    for addr in (f'"{ROSTER}"@evil.example', f"{ROSTER} @", f"{ROSTER}@"):
        gateway, session_call = _make_gateway()
        result = _call(gateway, _send_mail_args([addr]), [ROSTER])
        assert _blocked(result), addr
        assert session_call.await_count == 0


def test_reply_without_message_id_is_refused_without_a_lookup() -> None:
    gateway, session_call = _make_gateway_with_message(_graph_message(ROSTER))
    assert _blocked(_call(gateway, {"body": {"Comment": "x"}}, [ROSTER], tool_name="microsoft_365__reply-mail-message"))
    assert session_call.await_count == 0


def test_reply_lookup_exception_is_refused() -> None:
    gateway = MCPGateway()
    session = MagicMock()
    session.call_tool = AsyncMock(side_effect=RuntimeError("transport down"))
    gateway._session = session
    args = {"messageId": "AAMkAGI=", "body": {"Comment": "x"}}
    assert _blocked(_call(gateway, args, [ROSTER], tool_name="microsoft_365__reply-mail-message"))


def test_update_mail_message_without_recipient_fields_passes() -> None:
    """update-mail-message IS gated (it can PATCH recipients onto a draft);
    the poller's mark-read payload passes because it carries no address."""
    gateway, session_call = _make_gateway()
    args = {"messageId": "AAMkAGI=", "body": {"isRead": True}}
    result = _call(gateway, args, [], tool_name="microsoft_365__update-mail-message")
    assert session_call.await_count == 1
    assert result == '{"ok": true}'


@pytest.mark.parametrize(
    "tool_name",
    ["microsoft_365__create-calendar-event", "microsoft_365__update-calendar-event"],
)
def test_non_roster_attendee_blocked(tool_name: str) -> None:
    gateway, session_call = _make_gateway()
    args = _event_args([ROSTER, STRANGER])
    if "update" in tool_name:
        args["eventId"] = "AAMkAGI="
    assert _blocked(_call(gateway, args, [ROSTER], tool_name=tool_name))
    assert session_call.await_count == 0


@pytest.mark.parametrize(
    ("tool_name", "args"),
    [
        ("microsoft_365__forward-calendar-event",
         {"eventId": "AAMkAGI=", "body": {"ToRecipients": [_recipient(STRANGER)], "Comment": "fyi"}}),
        ("microsoft_365__create-specific-calendar-event",
         {"calendarId": "AAMkCal=", **_event_args([STRANGER])}),
        ("microsoft_365__update-specific-calendar-event",
         {"calendarId": "AAMkCal=", "eventId": "AAMkAGI=", **_event_args([STRANGER])}),
        ("microsoft_365__create-my-calendar-permission",
         {"body": {"emailAddress": {"address": STRANGER}, "role": "read"}}),
        ("microsoft_365__create-mail-rule",
         {"mailFolderId": "inbox", "body": {"displayName": "fwd", "actions": {"forwardTo": [_recipient(STRANGER)]}}}),
        ("microsoft_365__update-mailbox-settings",
         {"body": {"automaticRepliesSetting": {"externalAudience": "all",
                                               "externalReplyMessage": "x", "replyTo": STRANGER}}}),
    ],
)
def test_forward_share_rule_and_settings_tools_are_gated(tool_name: str, args: dict[str, Any]) -> None:
    """Outside the launcher's default allow-list, but gated anyway for an
    operator who widens it: each can address someone the walk must see."""
    gateway, session_call = _make_gateway()
    assert _blocked(_call(gateway, args, [ROSTER], tool_name=tool_name))
    assert session_call.await_count == 0


def test_non_ascii_address_is_refused_even_when_the_regex_misses_it() -> None:
    gateway, session_call = _make_gateway()
    for addr in ("mallory@ev\u00efl.example", "x@evil.\u0441\u0440\u0431"):
        result = _call(gateway, _send_mail_args([addr]), [ROSTER])
        assert _blocked(result), addr
        assert "non-ASCII" in json.loads(result)["error"]
    assert session_call.await_count == 0
    # Non-ASCII in free text (a name in the body) is still fine.
    gateway, session_call = _make_gateway()
    _call(gateway, _send_mail_args([ROSTER], content="Hola Jos\u00e9 \u2014 see jose@x.example"), [ROSTER])
    assert session_call.await_count == 1


def test_roster_attendees_pass() -> None:
    gateway, session_call = _make_gateway()
    result = _call(
        gateway, _event_args([ROSTER, EXEC_ADDR]), [ROSTER],
        tool_name="microsoft_365__create-calendar-event",
    )
    assert session_call.await_count == 1
    assert result == '{"ok": true}'


@pytest.mark.parametrize(
    ("tool_name", "args"),
    [
        ("microsoft_365__delete-calendar-event", {"eventId": "AAMkAGI="}),
        ("microsoft_365__list-mail-messages", {"filter": "isRead eq false", "top": 10}),
        ("microsoft_365__get-mail-message", {"messageId": "AAMkAGI="}),
        (
            "microsoft_365__find-meeting-times",
            {"body": {"attendees": [{"emailAddress": {"address": STRANGER}}]}},
        ),
        ("microsoft_365__get-calendar-view", {"startDateTime": "2030-01-01T00:00:00Z",
                                              "endDateTime": "2030-01-02T00:00:00Z"}),
    ],
)
def test_reads_and_delete_pass_through_ungated(tool_name: str, args: dict[str, Any]) -> None:
    gateway, session_call = _make_gateway()
    result = _call(gateway, args, [], tool_name=tool_name)
    assert session_call.await_count == 1
    assert result == '{"ok": true}'


# --- name normalization -----------------------------------------------------


def test_underscore_spelling_is_gated_identically() -> None:
    gateway, session_call = _make_gateway()
    result = _call(gateway, _send_mail_args([STRANGER]), [ROSTER], tool_name="microsoft_365__send_mail")
    assert session_call.await_count == 0
    assert _blocked(result)


def test_normalize_tool_name() -> None:
    assert _normalize_tool_name("microsoft_365__send-mail") == "microsoft_365__send_mail"
    assert _normalize_tool_name("  Microsoft_365__Send-Mail ") == "microsoft_365__send_mail"
    assert _normalize_tool_name("google_workspace__send_gmail_message") == (
        "google_workspace__send_gmail_message"
    )


def test_gated_sets_are_stored_normalized_and_nested() -> None:
    from openexecutive.orchestrator.mcp_gateway import (
        _M365_DOWNLOAD_TOOL,
        _M365_EVENT_BY_ID_TOOLS,
        _M365_REPLY_ALL_TOOLS,
        _M365_REPLY_BY_ID_TOOLS,
        _M365_SEND_DRAFT_TOOL,
    )

    every = (
        _GATED_M365_MAIL_TOOLS | _GATED_M365_CALENDAR_TOOLS
        | _M365_REPLY_BY_ID_TOOLS | _M365_REPLY_ALL_TOOLS | {_M365_SEND_DRAFT_TOOL}
        | _M365_EVENT_BY_ID_TOOLS | {_M365_DOWNLOAD_TOOL}
    )
    for name in every:
        assert name == _normalize_tool_name(name), name
        assert name.startswith("microsoft_365__")
    # A by-id tool is a mail write; reply-all is a by-id tool; send-draft is by-id.
    assert _M365_REPLY_BY_ID_TOOLS <= _GATED_M365_MAIL_TOOLS
    assert _M365_REPLY_ALL_TOOLS <= _M365_REPLY_BY_ID_TOOLS
    assert _M365_SEND_DRAFT_TOOL in _M365_REPLY_BY_ID_TOOLS


def test_google_tools_are_unaffected() -> None:
    """The Gmail gate keeps its exact-name, fixed-key contract."""
    gateway, session_call = _make_gateway()
    result = _call(
        gateway, {"to": ROSTER, "subject": "hi", "body": "b", "user_google_email": EXEC_ADDR},
        [ROSTER], tool_name="google_workspace__send_gmail_message",
    )
    assert session_call.await_count == 1
    assert result == '{"ok": true}'


def test_odata_annotation_key_is_not_an_address_but_its_value_is_scanned() -> None:
    """A Graph `fileAttachment` carries `@odata.type`; the `@` in that KEY
    must not trip the malformed-address refusal, while an address hidden in
    the entry's value (or any other key) is still roster-checked."""
    gateway, session_call = _make_gateway()
    args = _send_mail_args(to=[ROSTER])
    args["body"]["Message"]["attachments"] = [{
        "@odata.type": "#microsoft.graph.fileAttachment", "name": "plan.pdf",
        "contentBytes": "aGk=",
    }]
    assert not _blocked(_call(gateway, args, allowed=[ROSTER]))
    assert session_call.await_count == 1

    gateway, session_call = _make_gateway()
    args["body"]["Message"]["attachments"][0]["@odata.type"] = f"x {STRANGER}"
    assert _blocked(_call(gateway, args, allowed=[ROSTER]))
    assert session_call.await_count == 0

    gateway, session_call = _make_gateway()
    args["body"]["Message"]["attachments"][0]["@odata.type"] = "#microsoft.graph.fileAttachment"
    args["body"]["Message"]["attachments"][0]["name"] = f"for {STRANGER}"
    assert _blocked(_call(gateway, args, allowed=[ROSTER]))
    assert session_call.await_count == 0
