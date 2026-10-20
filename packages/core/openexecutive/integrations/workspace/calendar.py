"""Calendar backend contract for the typed booking tools.

`orchestrator.calendar_tools` owns the governance (roster, caps, business
hours, decision ledger, post-meeting follow-up); a `CalendarProvider` owns only
the three backend calls. Invariants:

1. `create_event` returns ``{"event_id", "raw"}`` plus ``"meet_link"`` when a
   video link was minted, or ``{"error": str}`` — never raises.
2. `delete_event` returns a dict; ``"error"`` present means it failed.
3. `has_conflicts` is advisory: ``True`` / ``False`` when the backend answered,
   ``None`` when it could not be determined. Callers treat ``None`` as
   "no conflict" and only log.
4. The video-link request honours `wants_video_link(payload, default)`, which
   also understands the legacy ``add_google_meet`` key persisted in decision
   ledger rows written before the provider switch existed.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


def wants_video_link(payload: dict[str, Any], default: bool = True) -> bool:
    """Whether a booking payload asks for a video-meeting link.

    ``add_video_link`` is the current key; ``add_google_meet`` is honoured for
    ledger rows and callers from before the rename. Absent both → ``default``
    (the `CALENDAR_MEET_LINKS_ENABLED` setting).
    """
    for key in ("add_video_link", "add_google_meet"):
        value = payload.get(key)
        if value is not None:  # an explicit null is "unspecified", not False
            return bool(value)
    return default


@runtime_checkable
class CalendarProvider(Protocol):
    """Protocol every calendar backend satisfies; see the module docstring."""

    name: str
    server_name: str
    video_link_label: str

    async def create_event(self, gateway: Any, payload: dict[str, Any]) -> dict[str, Any]:
        """Create the event described by a booking payload (``title``, ISO
        ``start``/``end``, ``attendee_emails``, optional ``description`` and
        the video-link flag)."""
        ...

    async def delete_event(self, gateway: Any, external_event_id: str) -> dict[str, Any]:
        """Delete/cancel a previously created event, notifying attendees."""
        ...

    async def has_conflicts(
        self, gateway: Any, start_iso: str, end_iso: str, attendee_emails: list[str],
    ) -> bool | None:
        """Advisory busy check for the slot; ``None`` when undeterminable."""
        ...
