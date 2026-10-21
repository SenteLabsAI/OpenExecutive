"""Who the principal is waiting on: their own sent mail nobody has answered.

Right now said "nothing new today" on a day the principal had emailed two
people and was waiting on both, because nothing it read looked at the mail
they send from their own mailbox. This reads it, in the background only
(the scheduler's tick and a view's settle pass, ``today.
refresh_principal_narrative``), never on the request: the principal's sent
mail from the last ``WINDOW`` that went to at most ``threads.MAX_RECIPIENTS``
people besides the Executive and is still the last message in its thread.

- **Theirs alone.** Only while they have Act as me on and their own mailbox
  opens (the gate ``top_three._read_own_calendar`` uses), and only into the
  principal's own header (``live_signals.gather_live_signals`` with
  ``include_private``).
- **Where, never what.** An item keeps who it went to, its subject and when:
  no body, so a model sees the subject line and names, never the mail.
- **Cached in this process**, read at most once per ``MAX_AGE_SECONDS``;
  a read older than ``_HARD_MAX_AGE_SECONDS`` is not used. Never raises.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

WINDOW = timedelta(days=7)
# Mail sent in the last hour is not waiting on anyone yet.
MIN_AGE = timedelta(hours=1)
MAX_AGE_SECONDS = 15 * 60
_HARD_MAX_AGE_SECONDS = 3 * 60 * 60
_SENT_LOOKED_AT = 40
_CANDIDATES = 8
_FETCH_CONCURRENCY = 4
_TIMEOUT_SECONDS = 30.0
_SUBJECT_MAX = 120


@dataclass(frozen=True)
class Waiting:
    """One email of theirs with no answer in its thread yet."""

    to: tuple[str, ...]
    subject: str
    sent_at: datetime


def _parse(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


async def _open_mailbox() -> tuple[Any, set[str]] | None:
    """The principal's own mailbox and their addresses, or None unless they
    have Act as me on and it opens as theirs."""
    from openexecutive.delegation import settings as delegation_settings
    from openexecutive.delegation.gmail import gmail_for, gmail_status, normalize_email
    from openexecutive.people.store import find_principal_person

    principal = await asyncio.to_thread(find_principal_person)
    if principal is None or principal.id is None or not principal.email:
        return None
    if not (delegation_settings.can_delegate(principal) and delegation_settings.is_enabled(principal.id)):
        return None
    mailbox = gmail_for(principal.email)
    if await gmail_status(principal.email, gmail=mailbox) != "connected":
        return None
    own = {normalize_email(principal.email), *(normalize_email(a) for a in principal.email_aliases)}
    own |= {normalize_email(a) for a in await mailbox.send_as_addresses()}
    return mailbox, {a for a in own if a}


async def read_waiting(now: datetime | None = None) -> list[Waiting] | None:
    """Their unanswered sent mail, newest first, or None when their mailbox
    can't be read. Never raises."""
    from openexecutive.config import get_settings
    from openexecutive.delegation.follow_ups import recipients
    from openexecutive.delegation.ghostwriter import one_line
    from openexecutive.delegation.gmail import normalize_email
    from openexecutive.delegation.threads import MAX_RECIPIENTS

    now = now or datetime.now(UTC)
    try:
        opened = await _open_mailbox()
        if opened is None:
            return None
        mailbox, own = opened
        exec_address = normalize_email(get_settings().exec_email_address)
        candidates: list[tuple[Any, tuple[str, ...], datetime]] = []
        seen: set[str] = set()
        # Newest first, so the first sent message seen in a thread is their latest there.
        for sent in await mailbox.list_sent(limit=_SENT_LOOKED_AT):
            if sent.thread_id in seen:
                continue
            seen.add(sent.thread_id)
            sent_at = _parse(sent.received_at)
            if sent_at is None or not (now - WINDOW <= sent_at <= now - MIN_AGE):
                continue
            if sent.from_addr not in own or "SENT" not in sent.labels or sent.calendar_invite:
                continue
            going = recipients(sent, own, exec_address)
            if not going or len(going) > MAX_RECIPIENTS:
                continue
            candidates.append((sent, tuple(going), sent_at))
            if len(candidates) >= _CANDIDATES:
                break
        gate = asyncio.Semaphore(_FETCH_CONCURRENCY)

        async def fetch(thread_id: str) -> Any:
            async with gate:
                return await mailbox.get_thread(thread_id)

        threads = await asyncio.gather(*(fetch(s.thread_id) for s, _, _ in candidates))
    except Exception as exc:
        # The type only: a mailbox error can carry an address.
        logger.info("waiting_on: their sent mail couldn't be read (%s)", type(exc).__name__)
        return None
    waiting: list[Waiting] = []
    for (sent, to, sent_at), thread in zip(candidates, threads, strict=True):
        newest = [m for m in thread.messages if "DRAFT" not in m.labels]
        if not newest or newest[-1].id != sent.id:
            continue
        subject = one_line(sent.subject, _SUBJECT_MAX) or "(no subject)"
        waiting.append(Waiting(to=to, subject=subject, sent_at=sent_at))
    return waiting


# (monotonic read time, items or None when there is no mailbox to read).
_cache: tuple[float, list[Waiting] | None] | None = None
_lock: asyncio.Lock | None = None


async def refresh(now: datetime | None = None, *, max_age: float = MAX_AGE_SECONDS) -> list[Waiting] | None:
    """``read_waiting``, at most once per ``max_age`` seconds, cached for the
    /today hot path. Never raises."""
    global _cache, _lock
    if _lock is None:
        _lock = asyncio.Lock()
    async with _lock:
        if _cache is not None and time.monotonic() - _cache[0] < max_age:
            return _cache[1]
        try:
            items = await asyncio.wait_for(read_waiting(now), timeout=_TIMEOUT_SECONDS)
        except Exception as exc:
            logger.info("waiting_on: refresh failed (%s)", type(exc).__name__)
            items = None
        _cache = (time.monotonic(), items)
        return items


def cached() -> list[Waiting] | None:
    """The last read, or None (none yet, or too old). Never calls out."""
    if _cache is None or time.monotonic() - _cache[0] > _HARD_MAX_AGE_SECONDS:
        return None
    return _cache[1]


def reset_cache() -> None:
    """Forget the cached read (tests; Act as me turned off)."""
    global _cache
    _cache = None


__all__ = ["MAX_AGE_SECONDS", "WINDOW", "Waiting", "cached", "read_waiting", "refresh", "reset_cache"]
