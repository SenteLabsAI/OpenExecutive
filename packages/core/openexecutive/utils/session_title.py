"""Shared helper for generating concise, topic-specific session titles.

Used by both the Discord integration (for bot-owned threads) and the web
chat route (for first-turn session renames). Backed by the
Council-configurable utility-fast model so admins can swap the underlying
Haiku model from one place.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_TITLE_SYSTEM = (
    "You generate concise 4-6 word titles for chat threads. "
    "Reply with the title only — no quotes, no trailing punctuation, "
    "no leading labels like 'Title:'."
)


async def generate_session_title(
    user_msg: str,
    assistant_msg: str,
    *,
    max_len: int = 60,
) -> str | None:
    """Generate a short, topic-specific session title via the utility-fast model.

    Returns the cleaned, length-capped title or ``None`` on any failure
    (caller is expected to keep whatever placeholder title it already has).
    Caps each side of the conversation at 2000 chars so a single huge turn
    doesn't blow up token cost.

    The cleaner strips surrounding quotes, trailing punctuation, collapses
    internal whitespace runs (incl. newlines) to single spaces, and drops
    non-printable control chars — Discord rejects embedded newlines in
    thread names and we don't want garbage leaking into the web-UI session
    sidebar either.
    """
    try:
        from openexecutive.agents.utility_fast import get_fast_model
        from openexecutive.audit.usage import log_model_usage
        from openexecutive.providers import get_provider

        model = get_fast_model()
        response = await get_provider(model).messages_create(
            model=model,
            max_tokens=32,
            system=_TITLE_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"USER MESSAGE:\n{user_msg[:2000]}\n\n"
                        f"EXECUTIVE RESPONSE:\n{assistant_msg[:2000]}\n\n"
                        "Title:"
                    ),
                }
            ],
        )
        log_model_usage(response, model=model, actor="session_title")
        raw = "".join(
            getattr(b, "text", "") for b in response.content
            if getattr(b, "type", "") == "text"
        ).strip()
        if not raw:
            return None
        cleaned = raw.strip().strip('"').strip("'").rstrip(".!?,:;").strip()
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        cleaned = "".join(c for c in cleaned if c.isprintable())
        if not cleaned:
            return None
        return cleaned[:max_len]
    except Exception:
        logger.exception("generate_session_title: failed")
        return None
