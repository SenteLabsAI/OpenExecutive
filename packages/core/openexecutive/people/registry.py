"""Cached read-through registry for People.

Pattern mirrors `departments.registry`: 60s TTL, thread-safe, invalidated
by the route layer after any mutation.

The cache holds the whole active roster, but every read is team-only unless
it passes ``include_contacts=True`` — the same deny-by-default rule as
``people.store``.
"""
from __future__ import annotations

import threading
import time

from openexecutive.people import store
from openexecutive.people.models import Person

_TTL_SECONDS = 60.0
_lock = threading.Lock()
_cache: list[Person] | None = None
_cache_expires_at: float = 0.0


def _refresh() -> list[Person]:
    global _cache, _cache_expires_at
    people = store.list_people(include_contacts=True)
    _cache = people
    _cache_expires_at = time.monotonic() + _TTL_SECONDS
    return people


def list_people(
    *, force_refresh: bool = False, include_contacts: bool = False
) -> list[Person]:
    """Return non-archived team members (and contacts, when asked), served
    from the 60s cache."""
    with _lock:
        if force_refresh or _cache is None or time.monotonic() >= _cache_expires_at:
            people = _refresh()
        else:
            people = list(_cache)
    if include_contacts:
        return list(people)
    return [p for p in people if p.kind == "team"]


def get_person(person_id: int, *, include_contacts: bool = False) -> Person | None:
    for p in list_people(include_contacts=include_contacts):
        if p.id == person_id:
            return p
    return None


def get_principal() -> Person | None:
    for p in list_people():
        if p.is_principal:
            return p
    return None


def invalidate() -> None:
    """Drop the cache so the next read goes back to the store."""
    global _cache, _cache_expires_at
    with _lock:
        _cache = None
        _cache_expires_at = 0.0
