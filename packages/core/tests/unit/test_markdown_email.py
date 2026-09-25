"""The briefs the scheduler emails: formatted, and safe with text from outside."""
from __future__ import annotations

from openexecutive.utils.markdown_email import markdown_to_email_html


def test_the_brief_is_formatted() -> None:
    html = markdown_to_email_html("# Today\n\n**Cash** is fine.\n\n- one\n- two")
    assert "<h1>Today</h1>" in html
    assert "<strong>Cash</strong> is fine." in html
    assert "<li>one</li>" in html and "<li>two</li>" in html
    assert html.startswith("<div style=")


def test_markup_from_outside_shows_as_text() -> None:
    html = markdown_to_email_html(
        'Quoted: <script>alert(1)</script> <img src="x" onerror="alert(2)">\n\n<div>block</div>'
    )
    assert "<script>" not in html and "<img" not in html and "<div>block" not in html
    assert "&lt;script&gt;" in html and "&lt;div&gt;block" in html


def test_only_safe_links_become_links() -> None:
    html = markdown_to_email_html(
        "[ok](https://bank.example/rates?a=1&b=2) [bad](javascript:alert(1)) "
        "[data](data:text/html,hi) <https://auto.example>"
    )
    assert '<a href="https://bank.example/rates?a=1&amp;b=2">ok</a>' in html
    assert '<a href="https://auto.example">' in html
    assert 'href="javascript' not in html and 'href="data' not in html


def test_images_do_not_load() -> None:
    html = markdown_to_email_html("![pixel](https://tracker.example/p.gif)")
    assert "<img" not in html
    # The address stays visible, as a link the reader chooses to open.
    assert 'href="https://tracker.example/p.gif"' in html
