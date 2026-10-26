"""Tell the principal when Right now changes: one short chat message.

Right now (the /today header, ``briefing.right_now``) is rewritten through the
day, but only someone looking at Home sees it change. When a rewrite adds a
point, this sends the principal one short message on their own chat app, the
way the briefs reach them (``scheduler.runner.deliver_to_person``, chat apps
only: Slack, Discord, Telegram, and any the install adds to
``delivery_order``; never email). Their reply lands in the same conversation
(the scheduler saves what it sends there), so "yes, set it up" has context.

What sends, by the principal's own Settings → Notifications choice
(``NotificationSettings.right_now``):

- ``important`` (the default): a new point that cites a line needing them
  (``header_context.NEEDS_YOU_PREFIXES``: mail from someone, something stuck,
  a decision raised today, their own overdue item, a reply overdue).
- ``everything``: any new point.
- ``off``: nothing.

Never for the first header of the day (the morning brief covers the night),
at most ``DAILY_MAX`` a day, one message per rewrite with every new point in
it, and nothing at all while their quiet hours or "In my briefs" would hold
it: by the time it went out the brief or Home would have said it, so a ping
that waits is noise and is dropped, not held.

The text is the header's own points, written by the model from quoted data,
so before it goes out every link and address is taken out and markup is
flattened (``plain_point``): a message from the Executive must never carry a
link someone else's email put there. Never raises.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime

from openexecutive.attunement.outcomes import SOURCE_RIGHT_NOW

logger = logging.getLogger(__name__)

EVENT = "right_now_ping"
LABEL = "Right now"
DAILY_MAX = 4
_POINT_MAX = 280
_POINTS_MAX = 3

_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_SLACK_LINK_RE = re.compile(r"<[^>|]*\|([^>]*)>")
# Anything a chat app could turn into a link: a token with a scheme
# ("https:", "tg:", "mailto:"), an email address, an IP address, or any
# dot-joined name ending in two or more letters ("evil.xyz/login",
# "bit.ly", "xn--pple-43d.shop"). Matched as whole tokens, so nothing is
# left half-stripped. Over-eager on purpose: "report.pdf" goes too.
_SCHEME_RE = re.compile(r"\S*\b[a-z][a-z0-9+.-]*:[\w/?%#~.-]\S*", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\S*@\S+")
_IP_RE = re.compile(r"\S*\b\d{1,3}(?:\.\d{1,3}){3}\S*")
_DOMAIN_RE = re.compile(r"\S*[\w-]\.[^\W\d_]{2,}\S*")


@dataclass(frozen=True)
class PingResult:
    """``sent`` (with the channel), or why not: ``off``, ``none_new``,
    ``first_today``, ``not_important``, ``nothing_left`` (only links),
    ``daily_limit``, ``quiet``,
    ``no_chat_app``, ``not_reached``."""

    status: str
    channel: str | None = None


def plain_point(text: str) -> str:
    """A point as plain words for a chat message: link text kept, every URL,
    email address and bare domain dropped, markup and angle brackets gone,
    one line, cut."""
    # Look-alikes first: a fullwidth dot or letter reads as the real one.
    out = unicodedata.normalize("NFKC", text)
    # Invisible format characters (zero-width space, soft hyphen) would
    # split "evil.com" past the patterns while a chat app still links it.
    out = "".join(c for c in out if unicodedata.category(c) != "Cf")
    out = _MD_LINK_RE.sub(r"\1", out)
    out = _SLACK_LINK_RE.sub(r"\1", out)
    out = out.replace("**", "").replace("__", "")
    for pattern in (_SCHEME_RE, _EMAIL_RE, _IP_RE, _DOMAIN_RE):
        out = pattern.sub("", out)
    out = out.replace("`", "'")
    table: dict[str, str | int | None] = {"<": None, ">": None, "*": None, "_": " ", "[": "(", "]": ")"}
    out = out.translate(str.maketrans(table))
    out = re.sub(r"\(\s*\)", "", out)
    out = " ".join(out.split())
    return out if len(out) <= _POINT_MAX else out[: _POINT_MAX - 1].rstrip() + "…"


def ping_text(points: list[str]) -> str:
    """The message: one point on its own, or a short list; empty when no
    point has words left once links are taken out."""
    lines = [p for p in (plain_point(p) for p in points[:_POINTS_MAX]) if p]
    if not lines:
        return ""
    if len(lines) == 1:
        return f"Right now: {lines[0]}"
    return "Right now:\n" + "\n".join(f"• {line}" for line in lines)


def _sent_today(now: datetime) -> int:
    from openexecutive.audit import logger as audit_logger
    from openexecutive.memory.workspace_settings import get_user_timezone

    midnight = now.astimezone(get_user_timezone()).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = audit_logger.get_audit_logger().query(
        event_type=EVENT, since=midnight.astimezone(UTC).isoformat(), limit=100,
    )
    return sum(1 for r in rows if isinstance(r.details, dict) and r.details.get("status") == "sent")


def _log(result: PingResult, count: int) -> None:
    from openexecutive.audit import log_event

    log_event(
        EVENT, f"Right now ping: {result.status}", actor="executive", private=True,
        details={"status": result.status, "channel": result.channel, "points": count},
    )


async def maybe_ping(
    new_points: list[str], important: set[str], *, earlier_today: bool, now: datetime | None = None,
) -> PingResult:
    """Send the principal one message about ``new_points`` (as written in
    the header) when their setting allows. ``important`` holds the
    ``right_now.point_key`` of each new point that needs them."""
    try:
        result, sent = await _maybe_ping(new_points, important, earlier_today=earlier_today, now=now)
    except Exception:
        logger.exception("right_now_ping: failed")
        return PingResult("not_reached")
    if sent:
        try:
            _log(result, sent)
        except Exception:
            logger.warning("right_now_ping: audit failed", exc_info=True)
    return result


async def _maybe_ping(
    new_points: list[str], important: set[str], *, earlier_today: bool, now: datetime | None,
) -> tuple[PingResult, int]:
    from openexecutive.briefing.right_now import point_key
    from openexecutive.people.notifications import get_notification_settings, hold_for
    from openexecutive.people.store import find_principal_person

    now = now or datetime.now(UTC)
    if not new_points:
        return PingResult("none_new"), 0
    principal = find_principal_person()
    if principal is None or principal.id is None:
        return PingResult("no_chat_app"), 0
    choice = get_notification_settings(principal.id).right_now
    if choice == "off":
        return PingResult("off"), 0
    if not earlier_today:
        return PingResult("first_today"), 0
    points = new_points if choice == "everything" else [p for p in new_points if point_key(p) in important]
    if not points:
        return PingResult("not_important"), 0
    text = ping_text(points)
    if not text:
        return PingResult("nothing_left"), len(points)
    if _sent_today(now) >= DAILY_MAX:
        return PingResult("daily_limit"), len(points)
    if hold_for(principal.id, source=SOURCE_RIGHT_NOW, now=now) is not None:
        return PingResult("quiet"), len(points)
    from openexecutive.attunement.outcomes import tag_proactive
    from openexecutive.scheduler.runner import deliver_to_person

    with tag_proactive(SOURCE_RIGHT_NOW):
        delivery = await deliver_to_person(principal, text, label=LABEL, chat_only=True)
    if not delivery.ok:
        status = "no_chat_app" if delivery.reason == "no_channel" else "not_reached"
        return PingResult(status), len(points)
    return PingResult("sent", delivery.channel), len(points)


__all__ = ["DAILY_MAX", "EVENT", "LABEL", "PingResult", "maybe_ping", "ping_text", "plain_point"]
