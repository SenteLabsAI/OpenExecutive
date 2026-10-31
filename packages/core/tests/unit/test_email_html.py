"""Act as me drafts with a table: a real HTML table next to the plain text."""
from __future__ import annotations

import asyncio
import base64
import json
from email import message_from_bytes, policy
from email.message import EmailMessage
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
    assert "Thanks,<br>" in markup


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


def test_only_our_html_counts_as_ours() -> None:
    markup = email_html.body_html(TABLE_BODY)
    assert markup is not None and email_html.is_ours(markup)
    # Graph wraps and respaces it; still ours.
    assert email_html.is_ours("<html><body>" + markup.replace(";", "; ").upper() + "</body></html>")
    assert not email_html.is_ours("<div dir='ltr'><b>Hi</b> Dana</div>")


def test_a_forward_in_the_edited_text_stays_escaped() -> None:
    text = (
        "Here it is:\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n"
        "---------- Forwarded message ---------\n| x | y |\n|---|---|\n# not a heading"
    )
    markup = email_html.body_html(text)
    assert markup is not None and markup.count("<table") == 1 and "<h1" not in markup
    # A table only in the forwarded part: the email stays plain.
    assert email_html.body_html("FYI\n\n---------- Forwarded message ---------\n| x | y |\n|---|---|") is None


def _gmail_with_draft(raw: str) -> tuple[Any, list[dict[str, Any]]]:
    from tests.unit.test_delegation_gmail import EMAIL, FakeGoogle, _cred

    google = FakeGoogle()
    puts: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/drafts/d1") and request.method == "GET":
            return httpx.Response(200, json={"id": "d1", "message": {"id": "m9", "threadId": "t1", "raw": raw}})
        if request.url.path.endswith("/drafts/d1") and request.method == "PUT":
            puts.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "d1", "message": {"id": "m10", "threadId": "t1"}})
        return FakeGoogle.handler(google, request)

    return DelegateGmail(EMAIL, credential=_cred(), transport=httpx.MockTransport(handler)), puts


def test_a_gmail_draft_with_its_own_formatting_or_files_is_left_alone() -> None:
    import pytest

    from openexecutive.delegation.gmail import DraftChanged

    composed = EmailMessage()
    composed["To"] = "dana@x.example"
    composed.set_content("Hi Dana")
    composed.add_alternative("<div dir='ltr'><b>Hi</b> Dana</div>", subtype="html")
    with_files = build_raw("me@x.example", DraftSpec(
        to=["dana@x.example"], subject="Bills", body=TABLE_BODY,
        attachments=[("bill.pdf", "application/pdf", b"%PDF-1.4")],
    ))
    for raw in (base64.urlsafe_b64encode(composed.as_bytes()).decode(), with_files):
        client, puts = _gmail_with_draft(raw)
        with pytest.raises(DraftChanged) as err:
            asyncio.run(client.update_draft_text("d1", "m9", "Hi"))
        assert err.value.reason == "not_plain" and not puts


def test_an_outlook_table_draft_can_be_edited_and_a_rich_one_cannot() -> None:
    import pytest

    from openexecutive.delegation import outlook as ol
    from openexecutive.delegation.gmail import DraftChanged
    from tests.unit.test_delegation_outlook import FakeGraph
    from tests.unit.test_delegation_outlook import _msg as graph_msg

    graph = FakeGraph()
    graph.messages["d1"] = graph_msg("d1", isDraft=True, body=ol._body(TABLE_BODY))
    asyncio.run(graph.client().update_draft_text("d1", ol.draft_version("d1", "ck1"), "Thanks, Dana."))
    patch = next(r for r in graph.graph_calls() if r.method == "PATCH")
    assert json.loads(patch.content)["body"] == {"contentType": "text", "content": "Thanks, Dana."}

    graph = FakeGraph()
    graph.messages["d1"] = graph_msg("d1", isDraft=True, body={"contentType": "html", "content": "<b>Hi</b>"})
    with pytest.raises(DraftChanged) as err:
        asyncio.run(graph.client().update_draft_text("d1", ol.draft_version("d1", "ck1"), "Hi"))
    assert err.value.reason == "not_plain"


def test_only_the_table_is_markdown() -> None:
    body = (
        "Totals are 2*3*4 and a\\_b &amp; c.\n"
        "- not a list\n1. not a list\n    not code\n[1]: http://x.example\n\n"
        "| a | b |\n|---|---|\n| *x* | [y](http://y.example) |\n"
    )
    markup = email_html.body_html(body)
    assert markup is not None
    for kept in ("2*3*4", "a\\_b &amp;amp; c.", "- not a list", "1. not a list", "[1]: http://x.example",
                 "<td style=\"border:1px solid #d0d7de;padding:6px 10px\">*x*</td>", "[y](http://y.example)"):
        assert kept in markup, kept
    assert "<ul" not in markup and "<ol" not in markup and "<pre" not in markup
    assert "<em>" not in markup and "<a " not in markup
