"""Act as me: the daily limit on drafts written as one person, shared by chat
(``ghostwrite_email``) and the inbox watcher.

``DELEGATION_MAX_DRAFTS_PER_DAY`` a UTC day, counted from ``delegation_drafts``
(every draft saved, from either place) with a floor of what this process saved
today, so a row that failed to write never lifts the limit. A slot is taken
synchronously, before the first await: a chat round's calls run concurrently,
so otherwise each would pass the check before any of them counted.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

# Why a slot was refused.
UNCOUNTABLE = "uncountable"  # the drafts couldn't be counted: refuse, never guess
REACHED = "reached"

# Drafts this process saved today, per (person_id, UTC day). Only today's are kept.
_SAVED_TODAY: dict[tuple[int, str], int] = {}
# Drafts being written right now, per person.
_IN_FLIGHT: dict[int, int] = {}


def _utc_day() -> str:
    return datetime.now(UTC).date().isoformat()


def drafts_today(person_id: int) -> int | None:
    """Drafts saved as ``person_id`` since UTC midnight, from chat and the
    inbox; None when they can't be counted. Never raises."""
    from openexecutive.delegation import drafts

    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        counted = drafts.count_since(person_id, start)
    except Exception:
        logger.warning("delegation.caps: can't count today's drafts — refusing", exc_info=True)
        return None
    return max(counted, _SAVED_TODAY.get((person_id, _utc_day()), 0))


def reserve(person_id: int, daily_cap: int) -> str | None:
    """Take one of today's draft slots for ``person_id``, or say why not
    (``UNCOUNTABLE`` / ``REACHED``). Synchronous: nothing runs between the
    check and the take."""
    counted = drafts_today(person_id)
    if counted is None:
        return UNCOUNTABLE
    if counted + _IN_FLIGHT.get(person_id, 0) >= daily_cap:
        return REACHED
    _IN_FLIGHT[person_id] = _IN_FLIGHT.get(person_id, 0) + 1
    return None


def release(person_id: int, *, saved: bool) -> None:
    """Hand a slot back: counted as saved today, or freed."""
    left = _IN_FLIGHT.get(person_id, 0) - 1
    if left > 0:
        _IN_FLIGHT[person_id] = left
    else:
        _IN_FLIGHT.pop(person_id, None)
    if not saved:
        return
    day = _utc_day()
    for stale in [key for key in _SAVED_TODAY if key[1] != day]:
        del _SAVED_TODAY[stale]
    _SAVED_TODAY[(person_id, day)] = _SAVED_TODAY.get((person_id, day), 0) + 1
