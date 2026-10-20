"""Settings → Notifications: the caller's own quiet hours, digest or instant,
and whether urgent things get through (``people.notifications``).

Every route is about the CALLER'S OWN settings: each person sets theirs, and
nobody sets anyone else's (the principal included). The caller is resolved
as for the notes (``api.routes.history._caller``'s rule): a signed-in person
on the People list; a request with no caller only under local login, as the
principal. Anyone else gets 403.

Routes:
  GET  /notifications/settings — the caller's settings, the zone they are
                                 read in, how many messages are waiting for
                                 them, and whether they get the briefs
  PUT  /notifications/settings — replace them: {quiet_hours: {enabled, start,
                                 end, days}, delivery, urgent_through,
                                 timezone}
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from openexecutive.api import caller as api_caller
from openexecutive.people.models import Person
from openexecutive.people.notifications import (
    Delivery,
    NotificationSettings,
    QuietHours,
    get_notification_settings,
    save_notification_settings,
    zone_for,
)

router = APIRouter()
logger = logging.getLogger(__name__)


class NotificationsOut(BaseModel):
    quiet_hours: QuietHours
    delivery: Delivery
    urgent_through: bool
    # The zone they chose ("" for the workspace's), and the one in effect.
    timezone: str
    effective_timezone: str
    # Messages waiting for their quiet hours to end or their next brief.
    held_count: int
    # Whether the morning and end-of-day briefs go to them (the principal);
    # anyone else's held messages go out on their own when the briefs do.
    gets_briefs: bool
    updated_at: str


class NotificationsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quiet_hours: QuietHours
    delivery: Delivery
    urgent_through: bool
    timezone: str = Field(default="", max_length=64)


def _refuse(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _caller(request: Request) -> Person:
    """The caller on the People list; else 403."""
    from openexecutive.delegation.settings import local_login
    from openexecutive.people.store import find_person_by_email, find_principal_person

    who = api_caller.caller(request)
    try:
        if who.email:
            person = find_person_by_email(who.email)
        elif who.defaults_to_principal and local_login():
            person = find_principal_person()
        else:
            raise _refuse(403, "sign_in_required", "Sign in to change your notifications.")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("notifications: caller lookup failed")
        raise _refuse(503, "roster_unavailable", "Couldn't read the People list.") from exc
    if person is None or person.id is None or person.archived:
        raise _refuse(
            403, "not_on_roster", "Notification settings are for people on the People list."
        )
    return person


def _out(person: Person, settings: NotificationSettings) -> NotificationsOut:
    from openexecutive.people.held_messages import held_count

    assert person.id is not None
    return NotificationsOut(
        quiet_hours=settings.quiet_hours,
        delivery=settings.delivery,
        urgent_through=settings.urgent_through,
        timezone=settings.timezone,
        effective_timezone=str(zone_for(settings)),
        held_count=held_count(person.id),
        gets_briefs=person.is_principal,
        updated_at=settings.updated_at,
    )


@router.get("/notifications/settings", response_model=NotificationsOut)
def get_notifications(request: Request) -> NotificationsOut:
    person = _caller(request)
    return _out(person, get_notification_settings(person.id))


@router.put("/notifications/settings", response_model=NotificationsOut)
def update_notifications(request: Request, body: NotificationsUpdate) -> NotificationsOut:
    person = _caller(request)
    assert person.id is not None
    try:
        settings = NotificationSettings(
            quiet_hours=body.quiet_hours,
            delivery=body.delivery,
            urgent_through=body.urgent_through,
            timezone=body.timezone,
        )
    except ValueError as exc:
        raise _refuse(422, "invalid_settings", "That time zone isn't one we know.") from exc
    before = get_notification_settings(person.id)
    try:
        saved = save_notification_settings(person.id, settings)
    except Exception as exc:
        logger.exception("notifications: couldn't save person %s's settings", person.id)
        raise _refuse(503, "save_failed", "Couldn't save your notification settings.") from exc
    from datetime import UTC, datetime

    from openexecutive.people.held_messages import release_digest_now, release_quiet_now

    now = datetime.now(UTC)
    try:
        if before.delivery == "digest" and saved.delivery == "instant":
            # Back to "As they happen": what waited for their brief goes now.
            release_digest_now(person.id, now=now)
        if before.quiet_hours != saved.quiet_hours or before.timezone != saved.timezone:
            # Quiet hours (or their zone) changed: what waited for the old end time is due now,
            # and the release pass holds it again if the new ones are quiet.
            release_quiet_now(person.id, now=now)
    except Exception:
        logger.warning("notifications: couldn't release person %s's held messages", person.id, exc_info=True)
    from openexecutive.audit import log_event

    log_event(
        "notification_settings_changed",
        f"Person {person.id} changed their notification settings",
        actor=api_caller.actor(request),
        details={
            "person_id": person.id,
            "quiet_hours": saved.quiet_hours.enabled,
            "delivery": saved.delivery,
            "urgent_through": saved.urgent_through,
        },
    )
    return _out(person, saved)
