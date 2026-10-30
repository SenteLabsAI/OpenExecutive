"""Who the principal is waiting on: their own sent mail nobody has answered.

Right now said "nothing new today" on a day the principal had emailed two
people and was waiting on both, because nothing it read looked at the mail
they send from their own mailbox. This reads it, in the background only
(the scheduler's tick and a view's settle pass, ``today.
refresh_principal_narrative``), never on the request: the principal's sent
mail from the last ``WINDOW`` that asked something (a question mark in their
own words, as ``follow_ups.unanswered`` asks), went to at most
``threads.MAX_RECIPIENTS`` people besides the Executive and is still the last
message in its thread.

- **Theirs alone.** Only while they have Act as me on and their own mailbox
  opens (the gate ``top_three._read_own_calendar`` uses), and only into the
  principal's own header (``live_signals.gather_live_signals`` with
  ``include_private``).
- **Where, never what.** An item keeps who it went to, its subject and when:
  no body, so a model sees the subject line and names, never the mail.
- **Cached in this process**, read at most once per ``MAX_AGE_SECONDS``;
  a read older than ``_HARD_MAX_AGE_SECONDS`` is not used. Never raises.

The same read notes which threads they last wrote in, and when
(``replied_at``): Right now drops an email they have answered since it
came, even one that never got a reply card (a card is settled against
their reply on its own, ``delegation.inbox.reconcile``). It also notes who
they last wrote to, and when (``wrote_to``): an email whose sender they
wrote to after it came counts as dealt with too, even from another thread
(signed papers sent on to someone else with the sender on copy).
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
# 5 minutes, not 15: the same read is how Right now learns they answered an
# email (``replied``), and a point they had dealt with stayed up a quarter
# of an hour. Each read is one sent-mail list plus at most ``_CANDIDATES``
# thread reads.
MAX_AGE_SECONDS = 5 * 60
_HARD_MAX_AGE_SECONDS = 3 * 60 * 60
_SENT_LOOKED_AT = 40
_CANDIDATES = 8
_FETCH_CONCURRENCY = 4
_TIMEOUT_SECONDS = 20.0
# After a failed read, calls in this window skip the mailbox and serve the
# last good read, so a slow mailbox doesn't hold every scheduler tick.
_RETRY_AFTER_SECONDS = 5 * 60
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


async def read_waiting(
    now: datetime | None = None,
    replied_out: dict[str, datetime] | None = None,
    wrote_out: dict[str, datetime] | None = None,
) -> list[Waiting] | None:
    """Their unanswered sent mail, newest first, or None when there is no
    mailbox of theirs to read (Act as me off, not connected). A mailbox that
    fails to answer raises, so ``refresh`` keeps the last good read.
    ``replied_out`` gets thread id → when they last sent in it, for every
    thread their recent sent mail is in; ``wrote_out`` gets address → when
    they last sent mail to it (to, cc or bcc)."""
    from openexecutive.config import get_settings
    from openexecutive.delegation.follow_ups import recipients
    from openexecutive.delegation.ghostwriter import one_line
    from openexecutive.delegation.gmail import GmailNotFound, normalize_email
    from openexecutive.delegation.threads import MAX_RECIPIENTS
    from openexecutive.integrations.email_poller import sender_new_text

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
            sent_at = _parse(sent.received_at)
            mine = sent.from_addr in own and "SENT" in sent.labels
            if wrote_out is not None and sent_at is not None and mine:
                for address in (*sent.to, *sent.cc, *(getattr(sent, "bcc", None) or [])):
                    key = normalize_email(address)
                    if key and key not in own and (key not in wrote_out or wrote_out[key] < sent_at):
                        wrote_out[key] = sent_at
            if sent.thread_id in seen:
                continue
            seen.add(sent.thread_id)
            if (
                replied_out is not None and sent_at is not None and sent.thread_id
                and sent.from_addr in own and "SENT" in sent.labels
            ):
                replied_out[sent.thread_id] = sent_at
            if sent_at is None or not (now - WINDOW <= sent_at <= now - MIN_AGE):
                continue
            if sent.from_addr not in own or "SENT" not in sent.labels or sent.calendar_invite:
                continue
            # A thank-you or an FYI waits on nobody. The body is read here only.
            if "?" not in sender_new_text(sent.text or ""):
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

        # A thread deleted since it was listed is skipped. Any other failure
        # fails the read, so ``refresh`` keeps the last good one rather than
        # storing a list with real items missing.
        threads = await asyncio.gather(*(fetch(s.thread_id) for s, _, _ in candidates), return_exceptions=True)
        for t in threads:
            if isinstance(t, BaseException) and not isinstance(t, GmailNotFound):
                raise t
    except Exception as exc:
        # The type only: a mailbox error can carry an address.
        logger.info("waiting_on: their sent mail couldn't be read (%s)", type(exc).__name__)
        raise
    waiting: list[Waiting] = []
    for (sent, to, sent_at), thread in zip(candidates, threads, strict=True):
        if isinstance(thread, BaseException):  # gone: any other failure raised above
            continue
        newest = [m for m in thread.messages if "DRAFT" not in m.labels]
        if not newest or newest[-1].id != sent.id:
            continue
        subject = one_line(sent.subject, _SUBJECT_MAX) or "(no subject)"
        waiting.append(Waiting(to=to, subject=subject, sent_at=sent_at))
    return waiting


# (monotonic read time, items or None when there is no mailbox to read).
_cache: tuple[float, list[Waiting] | None] | None = None
# Thread id → when they last sent in it, from the same read as ``_cache``.
_replied: dict[str, datetime] = {}
# Address → when they last sent mail to it, from the same read.
_wrote: dict[str, datetime] = {}
_lock: asyncio.Lock | None = None
# Bumped by ``reset_cache``: a read that was running when Act as me went off
# must not store what it read.
_generation = 0
_failed_at: float | None = None


async def refresh(now: datetime | None = None, *, max_age: float = MAX_AGE_SECONDS) -> list[Waiting] | None:
    """``read_waiting``, at most once per ``max_age`` seconds, cached for the
    /today hot path. A read that fails keeps the last good one (until it is
    ``_HARD_MAX_AGE_SECONDS`` old) and is tried again on the next call.
    Never raises."""
    global _cache, _lock, _failed_at, _replied, _wrote
    if _lock is None:
        _lock = asyncio.Lock()
    async with _lock:
        if _cache is not None and time.monotonic() - _cache[0] < max_age:
            return _cache[1]
        if _failed_at is not None and time.monotonic() - _failed_at < _RETRY_AFTER_SECONDS:
            return cached()
        started = _generation
        replied: dict[str, datetime] = {}
        wrote: dict[str, datetime] = {}
        try:
            items = await asyncio.wait_for(read_waiting(now, replied, wrote), timeout=_TIMEOUT_SECONDS)
        except Exception as exc:
            logger.info("waiting_on: refresh failed (%s)", type(exc).__name__)
            _failed_at = time.monotonic()
            return cached()
        if started != _generation:
            return None
        _cache, _failed_at, _replied, _wrote = (time.monotonic(), items), None, replied, wrote
        return items


def _usable() -> bool:
    """Whether the last read may be used: there is one, it isn't too old,
    and Act as me is still allowed and on for the principal (checked on
    every call, so a switch turned off by any path stops it at once)."""
    if _cache is None or time.monotonic() - _cache[0] > _HARD_MAX_AGE_SECONDS:
        return False
    try:
        from openexecutive.delegation import settings as delegation_settings
        from openexecutive.people.store import find_principal_person

        principal = find_principal_person()
        if principal is None or not (
            delegation_settings.can_delegate(principal) and delegation_settings.is_enabled(principal.id)
        ):
            return False
    except Exception:
        logger.exception("waiting_on: checking Act as me failed")
        return False
    return True


def cached() -> list[Waiting] | None:
    """The last read, or None (none yet, too old, or Act as me no longer
    allowed and on for the principal). Never calls out."""
    if not _usable() or _cache is None:
        return None
    return _cache[1]


def replied() -> dict[str, datetime]:
    """Thread id → when they last sent mail in it, from the last read; empty
    with no usable read. Checked once, so a caller looking up many threads
    pays for the Act as me check once. Never calls out."""
    return dict(_replied) if _usable() else {}


def wrote_to() -> dict[str, datetime]:
    """Address (lowercased) → when they last sent mail to it, from the last
    read; empty with no usable read. Never calls out."""
    return dict(_wrote) if _usable() else {}


def replied_at(thread_id: str) -> datetime | None:
    """When they last sent mail in ``thread_id`` (``replied``), or None."""
    return replied().get(thread_id) if thread_id else None


def reset_cache() -> None:
    """Forget the cached read, and any read still running (tests; Act as me
    turned off)."""
    global _cache, _generation, _failed_at, _replied, _wrote
    _cache = _failed_at = None
    _replied = {}
    _wrote = {}
    _generation += 1


__all__ = [
    "MAX_AGE_SECONDS", "WINDOW", "Waiting", "cached", "read_waiting", "refresh", "replied", "replied_at", "reset_cache",
    "wrote_to",
]
