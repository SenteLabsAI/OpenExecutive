"""Act as me drafts with a table: a real HTML table next to the plain text."""
from __future__ import annotations

import asyncio
import base64
import json
from email import message_from_bytes, policy
from typing import Any

import httpx

from openexecutive.delegation import email_html
from openexecutive.delegation.gmail import DelegateGmail, DraftSpec, ForwardOf, build_raw

TABLE_BODY = (
    "Hi Dana,\n"
    "Here are the bills:\n"
    "| Property | Bill # | Amount |\n"
    "|---|---|--:|\n"
    "| Harbor View LP <b>x</b> | 3161 | $6,130.88 |\n"
    "| Elm Street Lofts | 3162 | $5,000.00 |\n"
    "\n"
    "Thanks,\nSam\nhttps://example.com/x"
)


def _msg(raw: str) -> Any:
    return message_from_bytes(base64.urlsafe_b64decode(raw), policy=policy.default)


def test_plain_text_stays_plain() -> None:
    assert email_html.body_html("Hi Dana,\n\n- one\n- two | three\n\nThanks") is None
    assert not email_html.has_table("a | b\nc | d")
    msg = _msg(build_raw("me@x.example", DraftSpec(to=["dana@x.example"], subject="Hi", body="Hi | there")))
    assert not msg.is_multipart() and msg.get_content_type() == "text/plain"


def test_a_table_renders_as_a_styled_table_and_escapes_markup() -> None:
    markup = email_html.body_html(TABLE_BODY)
    assert markup is not None
    assert "<table style=\"border-collapse:collapse" in markup
    assert "<th style=" in markup and "text-align:right" in markup
    assert "<td style=" in markup and "$6,130.88" in markup
    # Raw HTML stays text; a URL stays text (no anchor); line breaks are kept.
    assert "<b>" not in markup and "&lt;b&gt;x&lt;/b&gt;" in markup
    assert "<a " not in markup and "https://example.com/x" in markup
    assert "Thanks,<br />" in markup


def test_build_raw_sends_text_and_html_with_files() -> None:
    raw = build_raw("me@x.example", DraftSpec(
        to=["dana@x.example"], subject="Bills", body=TABLE_BODY,
        attachments=[("bill.pdf", "application/pdf", b"%PDF-1.4")],
    ))
    msg = _msg(raw)
    assert msg.get_content_type() == "multipart/mixed"
    body = msg.get_body(preferencelist=("plain",))
    assert body is not None and "| Harbor View LP" in body.get_content()
    html_part = msg.get_body(preferencelist=("html",))
    assert html_part is not None and "<table" in html_part.get_content()
    assert [a.get_filename() for a in msg.iter_attachments()] == ["bill.pdf"]


def test_a_forward_quotes_the_original_as_text() -> None:
    raw = build_raw("me@x.example", DraftSpec(
        to=["dana@x.example"], subject="Fwd: Bills", body=TABLE_BODY,
        forward=ForwardOf(message_id="m1", header="---------- Forwarded message ---------",
                          text="| not | a table? |\n|---|---|\n<script>x</script>"),
    ))
    html_part = _msg(raw).get_body(preferencelist=("html",))
    assert html_part is not None
    content = html_part.get_content()
    assert content.count("<table") == 1 and "&lt;script&gt;" in content


def test_editing_a_table_draft_from_its_card_keeps_both_parts() -> None:
    from tests.unit.test_delegation_gmail import EMAIL, FakeGoogle, _cred

    raw = build_raw(EMAIL, DraftSpec(to=["dana@x.example"], subject="Bills", body=TABLE_BODY, thread_id="t1"))
    google = FakeGoogle()
    puts: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/drafts/d1") and request.method == "GET":
            return httpx.Response(200, json={"id": "d1", "message": {"id": "m9", "threadId": "t1", "raw": raw}})
        if request.url.path.endswith("/drafts/d1") and request.method == "PUT":
            puts.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "d1", "message": {"id": "m10", "threadId": "t1"}})
        return FakeGoogle.handler(google, request)

    client = DelegateGmail(EMAIL, credential=_cred(), transport=httpx.MockTransport(handler))
    edited = TABLE_BODY.replace("$5,000.00", "$5,100.00")
    assert asyncio.run(client.update_draft_text("d1", "m9", edited)) == "m10"
    msg = _msg(puts[0]["message"]["raw"])
    assert msg.get_content_type() == "multipart/alternative"
    html_part = msg.get_body(preferencelist=("html",))
    assert html_part is not None and "$5,100.00" in html_part.get_content()
    # Edited down to plain words: plain text again.
    asyncio.run(client.update_draft_text("d1", "m9", "Thanks, Dana."))
    msg = _msg(puts[1]["message"]["raw"])
    assert not msg.is_multipart() and msg.get_content().strip() == "Thanks, Dana."


def test_outlook_gets_an_html_body_only_for_a_table() -> None:
    from openexecutive.delegation import outlook

    assert outlook._body("Hi")["contentType"] == "text"
    body = outlook._body(TABLE_BODY)
    assert body["contentType"] == "html" and "<table" in body["content"]
