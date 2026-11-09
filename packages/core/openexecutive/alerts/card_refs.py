"""Cards named by what they are, never by their number.

The Executive sees each card on the board as ``[220] headline`` (its tools
answer, snooze or clear a card by that id), and the morning reflection reads
them as ``alert_id=220``. Its prompts say to leave the number out, but a
reply or brief that says "Review card 220" still got through, and the reader
has no way to know which card that is.

``name_cards`` is the plain code that catches it before a message goes out:
"card 220" (or "alert #220", "cards 220 and 221", "alert_id=220") becomes the
card's own headline, ``the “Pine Street lease renewal” card``, linked to that
card on Home where the message can carry a link. A number that is no card on
record is left as written ("card 4242" may well be a payment card), and a
card the reader may not see (a teammate's drafted document, a card private
to the principal, any card for someone off the roster) becomes "that card"
with no headline and no link.

``link_format`` picks how a link is written: Markdown for the web, Telegram,
Discord and email, ``<url|text>`` for Slack and Google Chat, and none where
the text is read by a model rather than a person (``"plain"``).
Never raises: on any error the text comes back as it was.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

LinkFormat = Literal["markdown", "slack", "plain"]

# "card 220", "Card #220", "the card 220", "alert no. 220", "cards 220 and 221",
# "alert_id=220". The word must come right before the number, so "/cards/220"
# (a link) and "card ending 4242" are left alone, and so is a link's own
# "?card=220" (``card_url``, which the briefs now write themselves).
_MENTION = re.compile(
    r"(?P<the>\b(?:the|that)\s+)?"
    r"(?P<word>(?<![?&/])\b(?:card|alert)(?P<plural>s)?(?:\s*_?id)?)"
    r"(?:\s*[=:]\s*|\s*#\s*|\s+no\.\s*|\s+number\s+|\s+)"
    r"(?P<ids>#?\d{1,9}(?:(?:\s*,\s*|\s+and\s+|\s+or\s+|\s*&\s*)#?\d{1,9})*)\b"
    # "an alert 30 minutes before", "card 2 of 5": a count, not a card.
    r"(?!\s*(?:%|percent\b|of\b|(?:sec(?:ond)?|min(?:ute)?|h(?:ou)?r|day|week|month|year)s?\b))",
    re.IGNORECASE,
)
_ID = re.compile(r"\d{1,9}")
# Characters that would break the link or the formatting around a headline,
# which comes from inbound mail and chat and so is not ours to trust.
_LABEL_STRIP = re.compile(r"[\[\]()<>|*_`\\]")
_LABEL_MAX = 80
_LABEL_LINK = re.compile(r"(?:[a-z][a-z0-9+.-]*://|www\.)\S*", re.IGNORECASE)
_LOOPBACK = frozenset({"localhost", "::1", "0.0.0.0"})


@dataclass(frozen=True)
class _Viewer:
    person_id: int | None
    is_principal: bool


def link_format(channel: str | None) -> LinkFormat:
    """How a link is written in a message sent on ``channel`` (a session's
    ``origin_channel`` or a delivery channel such as ``slack_dm``)."""
    name = (channel or "").lower()
    if name.startswith("slack") or name.startswith("google_chat"):
        return "slack"
    return "markdown"


def _base() -> str:
    try:
        from openexecutive.config import get_settings

        base = str(getattr(get_settings(), "ui_base_url", "") or "").rstrip("/")
    except Exception:
        logger.warning("card_refs: no ui_base_url", exc_info=True)
        return ""
    if not base.startswith(("https://", "http://")):
        return ""
    # The default (http://localhost:3000) is no address a reader can open.
    host = (urlsplit(base).hostname or "").lower()
    if host in _LOOPBACK or host.startswith("127."):
        return ""
    return base


def card_url(alert_id: int) -> str:
    """The address that opens Home on this card, or "" when the app has no
    web address set."""
    base = _base()
    return f"{base}/?card={alert_id}" if base else ""


def home_url() -> str:
    """Home's address, or "" when the app has no web address set."""
    base = _base()
    return f"{base}/" if base else ""


# Every form of link a brief could carry, matched in one pass so an address
# inside a link is not matched again on its own: an inline Markdown link
# (any address: scheme-relative, javascript:, mailto:), an <address>
# autolink, a reference definition ("[1]: address") and a bare address,
# which chat apps and mail clients link on their own.
_ANY_LINK = re.compile(
    r"(?P<md>\[(?P<md_text>[^\]\n]*)\]\(\s*<?(?P<md_url>(?:[^()\s<>]|\([^()\s]*\))*)>?[^)\n]*\))"
    r"|(?P<auto><(?P<auto_url>[a-z][a-z0-9+.-]*:[^>\s]*|//[^>\s]*)>)"
    r"|(?P<ref>^[ \t]*\[[^\]\n]+\]:[ \t]*<?(?P<ref_url>\S+?)>?(?:[ \t].*)?$)"
    r"|(?P<bare>(?:https?://|www\.)[^\s<>()\[\]]+)",
    re.IGNORECASE | re.MULTILINE,
)


def keep_app_links(text: str) -> str:
    """A brief with every link that doesn't point into this app taken out:
    an inline link becomes its text, any other form "(link removed)". A brief
    links only to the app's own pages (cards, write-ups), so a link anywhere
    else came from quoted mail or chat."""
    base = _base()

    def ours(url: str) -> bool:
        return bool(base) and (
            url == base or url.startswith(base + "/") or url.startswith(base + "?")
        )

    def keep(m: re.Match[str]) -> str:
        if m.group("md") is not None:
            if not ours(m.group("md_url")):
                return m.group("md_text")
            # An app link keeps its address; its text is cleaned too, so an
            # address shown as the text doesn't ride along.
            text = m.group("md_text")
            return f"[{_ANY_LINK.sub(keep, text)}]" + m.group(0)[len(text) + 2:]
        if m.group("auto") is not None:
            url = m.group("auto_url")
        elif m.group("ref") is not None:
            url = m.group("ref_url")
        else:
            url = m.group("bare").rstrip(".,;:!?'\"")
        return m.group(0) if ours(url) else "(link removed)"

    return _ANY_LINK.sub(keep, text or "")


def write_up_url(artifact_id: str) -> str:
    """The address of a run's write-up (``run_outcome.write_up_id``), or ""
    when the app has no web address set."""
    from urllib.parse import quote

    base = _base()
    if not base or not artifact_id:
        return ""
    return f"{base}/artifacts/{quote(artifact_id, safe='')}"


def _label(headline: str) -> str:
    # A headline comes from inbound mail and chat and is added after the
    # outbound checks ran: no link or @-mention of its own rides along.
    text = _LABEL_LINK.sub(" ", headline or "").replace("@", " ")
    text = " ".join(_LABEL_STRIP.sub(" ", text).split())
    if len(text) > _LABEL_MAX:
        text = text[: _LABEL_MAX - 1].rstrip() + "…"
    return text


def _linked(text: str, url: str, fmt: LinkFormat) -> str:
    if not url or fmt == "plain":
        return text
    if fmt == "slack":
        return f"<{url}|{text}>"
    return f"[{text}]({url})"


def _shown(alert: object, viewer: _Viewer, include_private: bool) -> bool:
    from openexecutive.alerts.models import is_private_alert, visible_alert

    if viewer.person_id is None:
        return False  # someone off the roster: the board isn't theirs to see
    if is_private_alert(alert) and not (include_private and viewer.is_principal):
        return False
    return visible_alert(alert, viewer)


def _cap(word: str, like: str) -> str:
    return word[:1].upper() + word[1:] if like[:1].isupper() else word


def _join(labels: list[str]) -> str:
    if len(labels) <= 2:
        return " and ".join(labels)
    return ", ".join(labels[:-1]) + " and " + labels[-1]


def name_cards(
    text: str,
    *,
    viewer_person_id: int | None = None,
    is_principal: bool = False,
    include_private: bool = False,
    fmt: LinkFormat = "markdown",
) -> str:
    """``text`` with every card number written as the card's headline.

    ``viewer_person_id`` / ``is_principal`` say who reads it: a card they may
    not see is called "that card" with no headline. ``include_private`` lets
    the principal's private cards be named (only where the message is for
    them alone). ``fmt`` is how the link to the card is written."""
    if not text or not _MENTION.search(text):
        return text
    try:
        from openexecutive.alerts.store import get_alert

        viewer = _Viewer(viewer_person_id, is_principal)
        cache: dict[int, object | None] = {}

        def lookup(alert_id: int) -> object | None:
            if alert_id not in cache:
                cache[alert_id] = get_alert(alert_id)
            return cache[alert_id]

        def replace(m: re.Match[str]) -> str:
            ids = [int(n) for n in _ID.findall(m.group("ids"))]
            found = [(i, a) for i in ids if (a := lookup(i)) is not None]
            if len(found) < len(ids):
                # A number that is no card is left as written, and so is the
                # list it sits in, so no number goes missing.
                return m.group(0)
            shown = [
                _linked(f"“{label}”", card_url(i), fmt)
                for i, a in found
                if _shown(a, viewer, include_private) and (label := _label(str(getattr(a, "headline", ""))))
            ]
            lead = m.group("the") or m.group("word")
            plural = len(found) > 1
            if not shown:
                phrase = "those cards" if plural else "that card"
                return _cap(phrase, lead)
            noun = "cards" if len(shown) > 1 else "card"
            the = (m.group("the") or "the ").strip()
            if the.lower() == "that" and len(shown) > 1:
                the = "those"
            # Cards the reader may not see still count, unnamed.
            hidden = len(found) - len(shown)
            more = f" and {hidden} other{'s' if hidden > 1 else ''}" if hidden else ""
            return f"{_cap(the, lead)} {_join(shown)} {noun}{more}"

        return _MENTION.sub(replace, text)
    except Exception:
        logger.warning("card_refs: couldn't name the cards; text left as written", exc_info=True)
        return text


_MD_LINK = re.compile(r"\[([^\[\]\n]{1,200})\]\((https?://[^\s()<>]{1,500})\)")


def slack_links(text: str) -> str:
    """Markdown links (``[text](https://…)``) written the way Slack and
    Google Chat show a link (``<https://…|text>``); they show Markdown's
    form as typed."""
    return _MD_LINK.sub(lambda m: f"<{m.group(2)}|{m.group(1).replace('|', ' ')}>", text)


def name_cards_for(
    text: str,
    person_id: int | None,
    *,
    principal: bool = False,
    include_private: bool = False,
    fmt: LinkFormat = "markdown",
) -> str:
    """``name_cards`` for a message read by ``person_id`` (None: someone not
    on the roster), or by the principal when ``principal`` is set, as the
    briefs are. Works out whether the reader is the principal; their private
    cards are named only with ``include_private``. Never raises."""
    if not text or not _MENTION.search(text):
        return text
    try:
        from openexecutive.people.store import find_principal_person

        owner = find_principal_person()
        principal_id = owner.id if owner is not None else None
    except Exception:
        logger.warning("card_refs: principal lookup failed", exc_info=True)
        principal_id = None
    reader = principal_id if principal else person_id
    is_principal = reader is not None and reader == principal_id
    return name_cards(
        text,
        viewer_person_id=reader,
        is_principal=is_principal,
        include_private=include_private and is_principal,
        fmt=fmt,
    )


def on_home_note() -> str:
    """Where carried-over cards are, for a brief's CARRIED OVER line: Home,
    with its address when the app has one, so the brief can link it rather
    than name a path ("see /today") the reader can't tap."""
    url = home_url()
    return f"on Home ({url})" if url else "on Home"


def name_cards_in_reply(
    text: str, session: object, person_id: int | None, co_present_person_ids: list[int] | None,
) -> str:
    """``name_cards_for`` for a chat-app or email reply (``Executive.chat``):
    links in the form ``session``'s channel shows, and the principal's
    private cards named only in a conversation nobody else reads."""
    if not text or not _MENTION.search(text):
        return text
    try:
        from openexecutive.delegation.settings import private_conversation

        alone = not co_present_person_ids and private_conversation(session)
    except Exception:
        logger.warning("card_refs: privacy check failed; private cards stay unnamed", exc_info=True)
        alone = False
    return name_cards_for(
        text, person_id, include_private=alone,
        fmt=link_format(str(getattr(session, "origin_channel", "") or "")),
    )
