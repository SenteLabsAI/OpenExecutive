"""Telegram replies show the Markdown's formatting, not its asterisks.

The reply goes out as Telegram's HTML subset (utils/markdown_telegram.py);
if Telegram refuses that, the same chunk goes again as plain text."""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from openexecutive.integrations import telegram_bot
from openexecutive.utils.markdown_telegram import markdown_to_telegram_html as render


def test_inline_formatting_maps_to_telegram_tags() -> None:
    assert render("Finance is **off track** on the *statements*, see `make check`.") == (
        "Finance is <b>off track</b> on the <i>statements</i>, see <code>make check</code>."
    )
    assert render("~~gone~~") == "<s>gone</s>"


def test_headings_become_bold_lines_and_lists_get_markers() -> None:
    out = render("## Quarterly close\n\n- First\n- Second\n\n1. one\n2. two")
    assert out == "<b>Quarterly close</b>\n\n• First\n• Second\n\n1. one\n2. two"


def test_nested_lists_are_indented() -> None:
    assert render("- outer\n  1. inner\n  2. next") == "• outer\n  1. inner\n  2. next"


def test_code_blocks_and_quotes() -> None:
    assert render("```python\nx = 1 < 2\n```") == "<pre>x = 1 &lt; 2</pre>"
    assert render("> quoted\n> more") == "<blockquote>quoted\nmore</blockquote>"


def test_blank_lines_inside_code_are_kept() -> None:
    assert render("```\ndef a():\n    pass\n\n\ndef b():\n    pass\n```") == (
        "<pre>def a():\n    pass\n\n\ndef b():\n    pass</pre>"
    )


def test_text_and_raw_html_are_escaped() -> None:
    out = render("Tom & Jerry <b>not bold</b> <script>x</script>")
    assert "<b>" not in out and "<script>" not in out
    assert "Tom &amp; Jerry &lt;b&gt;not bold&lt;/b&gt;" in out


def test_a_link_shows_its_target_and_is_never_a_tag() -> None:
    # Quoted text must not hide where a link goes, nor make a tg:// or
    # ton:// address tappable: the target is shown as plain text.
    assert render("[Your bank](https://evil.example/phish)") == (
        "Your bank (https://evil.example/phish)"
    )
    assert render("[Approve](tg://proxy?server=1.2.3.4&port=443)") == (
        "Approve (tg://proxy?server=1.2.3.4&amp;port=443)"
    )
    assert render("<https://x.example/a>") == "https://x.example/a"
    assert render("<ann@x.example>") == "ann@x.example"
    for text in ("[a](https://x.example)", "[b](ton://transfer/EQ?amount=1)", "[c](javascript:alert(1))"):
        assert "<a" not in render(text)


class _Resp:
    def __init__(self, ok: bool, status: int = 400) -> None:
        self.is_error = not ok
        self.status_code = 200 if ok else status
        self.text = "" if ok else "Bad Request: can't parse entities"

    def json(self) -> dict[str, Any]:
        return {"result": {"message_id": 7}}


def _send(
    monkeypatch: pytest.MonkeyPatch, text: str, html_ok: bool = True, status: int = 400
) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    class Client:
        async def post(self, url: str, json: dict[str, Any]) -> _Resp:
            sent.append(json)
            return _Resp(html_ok or "parse_mode" not in json, status)

    monkeypatch.setattr(telegram_bot, "_get_http_client", lambda: Client())
    asyncio.run(telegram_bot.send_message("t", 1, text))
    return sent


def test_a_reply_is_sent_as_telegram_html(monkeypatch: pytest.MonkeyPatch) -> None:
    [payload] = _send(monkeypatch, "**Done.** Moved 3 files.")
    assert payload["parse_mode"] == "HTML"
    assert payload["text"] == "<b>Done.</b> Moved 3 files."
    assert payload["link_preview_options"] == {"is_disabled": True}


def test_a_refused_html_message_goes_again_as_plain_text(monkeypatch: pytest.MonkeyPatch) -> None:
    html_try, plain_try = _send(monkeypatch, "**Done.**", html_ok=False)
    assert html_try["parse_mode"] == "HTML"
    assert "parse_mode" not in plain_try and plain_try["text"] == "**Done.**"
    assert plain_try["link_preview_options"] == {"is_disabled": True}


@pytest.mark.parametrize("status", [429, 500, 403])
def test_only_a_refusal_is_retried_as_plain_text(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    # Rate limits, server errors and a blocked bot would fail again, and a
    # 5xx that was delivered anyway would send the reply twice.
    [only_try] = _send(monkeypatch, "**Done.**", html_ok=False, status=status)
    assert only_try["parse_mode"] == "HTML"
