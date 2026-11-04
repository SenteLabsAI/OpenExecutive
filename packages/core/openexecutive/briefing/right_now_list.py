"""Home's Right now as a plain list: one row per thing waiting on the
principal that Needs you doesn't already show, built from the records
themselves. No model writes it, so a row never changes its words, never
repeats, and is current on every page load.

The rows:

- ``mail``: an email from someone they deal with that the inbox watcher
  could not handle (it failed, or hit its daily limit) and that they have not
  answered since. Mail that needs nothing (thanks, FYI), mail the Executive
  answered, and any thread with a reply card (Needs you shows the card, and
  its Send warns when the thread moved on) are left out, so nothing is in
  both. One row per thread, its newest.
- ``draft``: a draft the Executive saved in their own mailbox from a chat
  that is still unsent (one addressed only to themselves is left out).
- ``chat``: a team chat message asking them something, unanswered.

A dismissed row (``dismiss``) stays gone until something newer lands on it:
a later email in the thread, or a later ask. Dismissals don't expire at
midnight. Private to the principal; every read here never raises.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from openexecutive.alerts.lifecycle import parse_aware
from openexecutive.briefing import narrative_cache

logger = logging.getLogger(__name__)

# How far back the rows look: mail and drafts older than this have either
# been dealt with or belong to the morning brief.
WINDOW = timedelta(days=3)
# A watcher failure younger than this may pass on its next scan.
_FAILED_SETTLE = timedelta(minutes=30)
ROWS_MAX = 8
_TEXT_MAX = 160
# Dismissals older than this go (a thread quiet that long is done).
_DISMISS_KEEP = timedelta(days=30)

TABLE = "right_now_dismissed"


@dataclass(frozen=True)
class Row:
    """One thing waiting on the reader. ``key`` names what it is about
    (``mail:<thread id>``, ``draft:<row id>``, ``chat:<ask id>``) and ``at``
    is the newest activity on it (ISO-8601 UTC): a dismissal holds until
    something newer than that."""

    key: str
    kind: str
    who: str
    subject: str
    at: str
    note: str = ""
    source: dict[str, Any] = field(default_factory=dict)


def _one_line(value: Any, limit: int = _TEXT_MAX) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def initialize_db() -> None:
    with narrative_cache._get_conn() as conn:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE} (
                item_key TEXT PRIMARY KEY,
                at TEXT NOT NULL,
                dismissed_at TEXT NOT NULL
            )
            """
        )


def _dismissed() -> dict[str, datetime]:
    """``key`` → the newest activity the reader had seen when they dismissed it."""
    if not narrative_cache._resolve_db_path(None).exists():
        return {}  # a read must not create the database
    try:
        with narrative_cache._get_conn() as conn:
            rows = conn.execute(f"SELECT item_key, at FROM {TABLE}").fetchall()  # noqa: S608 — constant table name
    except sqlite3.OperationalError:
        return {}  # nothing dismissed yet: the table comes with the first dismissal
    except Exception:
        logger.exception("right_now_list: reading dismissals failed")
        return {}
    out: dict[str, datetime] = {}
    for r in rows:
        at = parse_aware(r["at"])
        if at is not None:
            out[r["item_key"]] = at
    return out


def dismiss(key: str, now: datetime | None = None) -> bool:
    """The reader dismissed the row ``key``: kept out until something newer
    lands on it. False when no current row has that key, or it could not be
    stored."""
    now = now or datetime.now(UTC)
    row = next((r for r in _all_rows(now) if r.key == key), None)
    if row is None:
        return False
    try:
        initialize_db()
        with narrative_cache._get_conn() as conn:
            conn.execute(
                f"DELETE FROM {TABLE} WHERE dismissed_at < ?",  # noqa: S608 — constant table name
                ((now - _DISMISS_KEEP).isoformat(),),
            )
            conn.execute(
                f"INSERT OR REPLACE INTO {TABLE} (item_key, at, dismissed_at) VALUES (?, ?, ?)",  # noqa: S608
                (row.key, row.at, now.isoformat()),
            )
    except Exception:
        logger.exception("right_now_list: dismissing %s failed", key)
        return False
    return True


def rows(now: datetime | None = None) -> list[Row]:
    """What Right now lists for the principal, newest first, without the
    rows they dismissed (until something newer lands on one)."""
    now = now or datetime.now(UTC)
    gone = _dismissed()
    out: list[Row] = []
    for r in _all_rows(now):
        seen = gone.get(r.key)
        at = parse_aware(r.at)
        if seen is not None and (at is None or at <= seen):
            continue
        out.append(r)
    return out[:ROWS_MAX]


def _all_rows(now: datetime) -> list[Row]:
    from openexecutive.briefing.live_signals import _principal

    principal = _principal()
    if principal is None or principal.id is None:
        return []
    since = now - WINDOW
    found: list[Row] = []
    for read in (_mail_rows, _draft_rows, _chat_rows):
        try:
            found.extend(read(principal, since, now))
        except Exception:
            logger.exception("right_now_list: %s failed", read.__name__)
    found.sort(key=lambda r: parse_aware(r.at) or since, reverse=True)
    return found


def _mail_rows(principal: Any, since: datetime, now: datetime) -> list[Row]:
    from openexecutive.audit import logger as audit_logger
    from openexecutive.briefing import waiting_on
    from openexecutive.briefing.live_signals import (
        _INBOX_KNOWN,
        _INBOX_NOISE,
        _POOL,
        _mail_ref,
        _named,
        _wrote_back,
    )
    from openexecutive.delegation import inbox as watcher

    if audit_logger._default_logger is None and not audit_logger.DB_PATH.exists():
        return []  # a read must not create the database
    events = audit_logger.get_audit_logger().query(
        event_type=watcher.MAIL_SEEN_EVENT, since=since.isoformat(), limit=_POOL,
        include_private=True, owned_by=principal.id,
    )
    newest: dict[str, dict[str, Any]] = {}
    for ev in events:
        d = ev.details if isinstance(ev.details, dict) else {}
        message_id = str(d.get("message_id") or "")
        if d.get("person_id") != principal.id or not message_id:
            continue
        arrived = parse_aware(str(d.get("received_at") or "")) or parse_aware(ev.ts)
        if arrived is None or arrived < since:
            continue
        thread = str(d.get("thread_id") or "") or message_id
        held = newest.get(thread)
        if held is None or arrived > held["_at"]:
            newest[thread] = d | {"_at": arrived, "_thread": thread}
    if not newest:
        return []
    outcomes = watcher.outcomes(principal.id, [str(d["message_id"]) for d in newest.values()])
    answered = waiting_on.replied()
    wrote = waiting_on.wrote_to()
    out: list[Row] = []
    for d in newest.values():
        arrived = d["_at"]
        replied = answered.get(str(d.get("thread_id") or ""))
        if replied is not None and replied > arrived:
            continue
        address = str(d.get("from_email") or "")
        outcome = outcomes.get(str(d["message_id"]), "")
        if _wrote_back(d, outcome == watcher.NOT_NEEDED, wrote.get(address.strip().lower(), [])):
            continue
        if str(d.get("relation") or "") not in _INBOX_KNOWN or str(d.get("reason") or "") in _INBOX_NOISE:
            continue
        if d.get("card_id") is not None or not (
            outcome in (watcher.FAILED, watcher.RETRY) and now - arrived >= _FAILED_SETTLE
        ):
            # On a card in Needs you, needs nothing, answered for them, or
            # not looked at yet.
            continue
        note = "I couldn't draft a reply to this one"
        name = _one_line(d.get("from_name"), 60)
        out.append(Row(
            key=f"mail:{d['_thread']}",
            kind="mail",
            who=name or _named(address, True),
            subject=_one_line(d.get("subject"), 120) or "(no subject)",
            at=arrived.isoformat(),
            note=note,
            source=_mail_ref(d),
        ))
    return out


def _draft_rows(principal: Any, since: datetime, now: datetime) -> list[Row]:
    from openexecutive.briefing.live_signals import _named, _only_to
    from openexecutive.delegation import drafts
    from openexecutive.memory import episodic

    if not episodic._resolve_db_path(None).exists():
        return []
    own = {a.casefold() for a in [principal.email, *principal.email_aliases] if a}
    out: list[Row] = []
    for d in drafts.waiting_drafts(principal.id, since):
        # Not read by a settle pass yet (nothing to name), or kept for themselves.
        if not (d.to or d.subject) or _only_to(d.to, own) or not d.row_id:
            continue
        at = parse_aware(d.created_at)
        if at is None:
            continue  # no stable time, so a dismissal could never hold
        out.append(Row(
            key=f"draft:{d.row_id}",
            kind="draft",
            who=_one_line(", ".join(_named(a, True) for a in d.to), 80) or "No one yet",
            subject=_one_line(d.subject, 120) or "(no subject)",
            at=at.isoformat(),
            note="In your Drafts, not sent",
            source={"kind": "mail", "thread_id": d.thread_id} if d.thread_id else {},
        ))
    return out


def _chat_rows(principal: Any, since: datetime, now: datetime) -> list[Row]:
    from openexecutive.integrations import chat_asks

    out: list[Row] = []
    for ask in chat_asks.waiting(principal.id, now):
        at = parse_aware(ask.asked_at)
        if at is None:
            continue
        app = chat_asks.APP_NAMES.get(ask.channel, ask.channel)
        out.append(Row(
            key=f"chat:{ask.id}",
            kind="chat",
            who=_one_line(ask.from_name, 60),
            subject=_one_line(ask.text, 160),
            at=at.isoformat(),
            note=f"Asked you in {chat_asks.where(ask)}",
            source={"kind": "link", "url": ask.link, "app": app} if ask.link else {},
        ))
    return out


__all__ = ["ROWS_MAX", "TABLE", "WINDOW", "Row", "dismiss", "initialize_db", "rows"]
