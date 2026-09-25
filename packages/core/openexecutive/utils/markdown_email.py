"""Markdown messages as email HTML — the briefs and digests the scheduler emails.

The brief is written in Markdown, and the chat channels show it as such; an
email client would show the asterisks and hashes instead. So the email body
is rendered to HTML with markdown-it-py, set up for text the Executive did
not fully write (a brief quotes alerts, which quote inbound email and web
pages):

- raw HTML off, so markup in that text shows as text rather than rendering;
- images off, so opening the email cannot load a remote picture (a read
  receipt for whoever planted the link) — the link stays, as a link;
- link targets through markdown-it's own check, which drops ``javascript:``,
  ``vbscript:``, ``file:`` and ``data:`` URLs.
"""
from __future__ import annotations

from markdown_it import MarkdownIt

_RENDERER = MarkdownIt("commonmark", {"html": False}).disable("image")

# Inline styles only: most email clients drop <style> blocks.
_WRAPPER = (
    '<div style="font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', '
    'Roboto, Helvetica, Arial, sans-serif; font-size: 15px; line-height: 1.5; '
    'color: #1f2328; max-width: 640px;">{}</div>'
)


def markdown_to_email_html(text: str) -> str:
    """``text`` (Markdown) as the HTML body of an email."""
    return _WRAPPER.format(_RENDERER.render(text))
