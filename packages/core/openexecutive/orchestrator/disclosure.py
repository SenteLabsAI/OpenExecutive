"""AI disclosure: a line on what the Executive sends saying it is an AI.

People the Executive writes to should be able to tell they are hearing from
an AI assistant. Plain code adds the line, never the prompt, so a reply the
model writes can't leave it out:

- **Mail** from the Executive's own mailbox gets ``mail_line()`` below the
  signature: the MCP gateway adds it to every Gmail / Outlook send and draft
  after the outbound gates pass (``with_mail_line``), which covers replies
  the model writes and the fixed mails code sends (``send_from_executive``).
  Mail only to the principal goes without it: they know.
- **Chat** (Slack, Telegram, Discord, Google Chat) gets the shorter
  ``chat_line()`` on the first message in a conversation, then again after
  ``CHAT_REPEAT`` of quiet (``with_chat_line``), so a busy channel isn't
  marked on every reply. What a conversation last saw is kept in memory, so
  a restart shows it once more.

Sends *as a person* (Act as me) are ``delegation.disclosure``'s.

``AI_DISCLOSURE`` turns it all off. ``AI_DISCLOSURE_MAIL_TEXT`` /
``AI_DISCLOSURE_CHAT_TEXT`` reword the lines; a text that doesn't say "AI"
is ignored for the built-in one. Everything here fails open: a line that
can't be built is left off, never a send refused.
"""
from __future__ import annotations

import html
import logging
import re
import threading
import time

logger = logging.getLogger(__name__)

# Shown again in a conversation after this long without one.
CHAT_REPEAT = 24 * 60 * 60.0
# The most conversations remembered; the oldest go first.
_MAX_CONVERSATIONS = 5000

_SAYS_AI = re.compile(r"\bAI\b|artificial intelligence", re.IGNORECASE)
_MAIL_RULE = "---"

_lock = threading.Lock()
_last_shown: dict[str, float] = {}


def enabled() -> bool:
    from openexecutive.config import get_settings

    try:
        return getattr(get_settings(), "ai_disclosure", True) is True
    except Exception:
        return True


def says_ai(text: str) -> bool:
    """Whether ``text`` tells the reader they are dealing with an AI."""
    return bool(_SAYS_AI.search(text or ""))


def _custom(text: str, setting: str) -> str | None:
    text = " ".join((text or "").split())
    if not text:
        return None
    if says_ai(text):
        return text
    logger.warning("disclosure: %s doesn't say it's an AI, so the built-in line is used", setting)
    return None


def _company_name() -> str:
    try:
        from openexecutive.onboarding.profile_builder import load_or_create_profile

        return " ".join(load_or_create_profile().name.split())
    except Exception:
        return ""


def _principal_email() -> str:
    try:
        from openexecutive.people.registry import get_principal

        principal = get_principal()
        return (principal.email or "").strip() if principal is not None else ""
    except Exception:
        return ""


def mail_line() -> str:
    """The line under the Executive's mail."""
    from openexecutive.config import get_settings

    settings = get_settings()
    custom = _custom(settings.ai_disclosure_mail_text, "AI_DISCLOSURE_MAIL_TEXT")
    if custom:
        return custom
    name = settings.exec_display_name.strip() or "This assistant"
    company = _company_name()
    line = f"{name} is an AI assistant working for {company}." if company else f"{name} is an AI assistant."
    contact = _principal_email()
    if contact:
        line += f" To reach a person, reply and ask, or write to {contact}."
    return line


def chat_line() -> str:
    """The shorter line on the Executive's chat messages."""
    from openexecutive.config import get_settings

    settings = get_settings()
    custom = _custom(settings.ai_disclosure_chat_text, "AI_DISCLOSURE_CHAT_TEXT")
    if custom:
        return custom
    return f"{settings.exec_display_name.strip() or 'Open Executive'} · AI assistant"


def add_mail_line(body: str, *, as_html: bool = False) -> str:
    """``body`` with the mail line below it, once."""
    try:
        line = mail_line()
    except Exception:
        logger.warning("disclosure: couldn't build the mail line", exc_info=True)
        return body
    if line in body or html.escape(line) in body:
        return body
    if as_html:
        return f'{body}<hr><p style="color:#666;font-size:12px">{html.escape(line)}</p>'
    return f"{body.rstrip()}\n\n{_MAIL_RULE}\n{line}"


def with_mail_line(body: str, *, recipients: list[str] | None = None, as_html: bool = False) -> str:
    """The Executive's mail body as it should go: with the line, unless
    disclosure is off or it only goes to the principal."""
    if not enabled() or not isinstance(body, str):
        return body
    if recipients:
        principal = _principal_email().lower()
        if principal and all(r.strip().lower() == principal for r in recipients):
            return body
    return add_mail_line(body, as_html=as_html)


def with_chat_line(text: str, conversation: str, *, now: float | None = None) -> str:
    """``text`` as it should go to ``conversation`` (any stable key for one
    chat, DM or thread): with the chat line when that conversation hasn't
    had one for ``CHAT_REPEAT``."""
    if not enabled() or not isinstance(text, str) or not text.strip():
        return text
    try:
        line = chat_line()
    except Exception:
        logger.warning("disclosure: couldn't build the chat line", exc_info=True)
        return text
    if line in text:
        return text
    at = time.time() if now is None else now
    with _lock:
        last = _last_shown.get(conversation)
        if last is not None and at - last < CHAT_REPEAT:
            return text
        _last_shown.pop(conversation, None)
        _last_shown[conversation] = at
        while len(_last_shown) > _MAX_CONVERSATIONS:
            _last_shown.pop(next(iter(_last_shown)))
    return f"{text.rstrip()}\n\n_{line}_"


def reset() -> None:
    """Forget which conversations have seen the chat line (tests)."""
    with _lock:
        _last_shown.clear()
