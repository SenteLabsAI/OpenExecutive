"""Proactive-outcome ledger — which outreach lands, per person and source.

Nothing used to measure whether the Executive's proactive messages worked. A
nudge that was always ignored kept being sent, and one that always got an
answer was ranked no higher. This ledger records every proactive DM to a
rostered person and resolves it deterministically, with no model call:

- **replied** — the recipient's reply was matched to the DM
  (``memory.episodic.mark_outbound_context_consumed``);
- **acted** — the thing it was about got done: the open loop it chased was
  reported done by its owner, the approval it nudged was resolved, the
  initiative it checked on was updated, or the alert it was about was
  acknowledged or resolved;
- **void** — the alert it was about was dismissed or found stale: the DM
  neither landed nor was ignored, so it counts toward neither;
- **ignored** — nothing of the above within ``ATTUNEMENT_IGNORE_AFTER_HOURS``
  (swept at the start of each nudge scan and whenever rates are read). A
  later acted / void verdict overrides it.

Recording happens at the one place every proactive DM passes through —
``orchestrator.schedule_tools._record_outbound_context`` — but only while a
proactive source is tagged with :func:`tag_proactive`. Callers that start
proactive work (the scheduler's dispatch of nudges and follow-ups, the
reflection and research workflows, alert review) wrap their sends in it. A
user's own chat turn is never tagged, so replies in conversation are not
counted as outreach.

Rates feed back into the nudge engine (``muted_pairs``), the reflection
prompt (``render_for_reflection``) and the person page (``acceptance``).
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Sources, as stored. Kept small and stable: they key the nudge engine's
# per-person ranking and the reflection summary.
SOURCE_NUDGE_STALLED = "nudge_stalled"
SOURCE_NUDGE_COMMITMENT = "nudge_commitment"
SOURCE_NUDGE_INITIATIVE = "nudge_initiative"
SOURCE_OPEN_LOOP = "open_loop"
SOURCE_FOLLOWUP = "followup"
SOURCE_REFLECTION = "reflection"
SOURCE_RESEARCH = "research"
SOURCE_ALERT_REVIEW = "alert_review"

SOURCE_LABELS: dict[str, str] = {
    SOURCE_NUDGE_STALLED: "approval nudges",
    SOURCE_NUDGE_COMMITMENT: "commitment chases",
    SOURCE_NUDGE_INITIATIVE: "initiative check-ins",
    SOURCE_OPEN_LOOP: "open-loop chases",
    SOURCE_FOLLOWUP: "scheduled follow-ups",
    SOURCE_REFLECTION: "standup DMs",
    SOURCE_RESEARCH: "research DMs",
    SOURCE_ALERT_REVIEW: "alert-review DMs",
}

OUTCOME_REPLIED = "replied"
OUTCOME_ACTED = "acted"
OUTCOME_IGNORED = "ignored"
# The thing the DM was about turned out not to matter (its alert was
# dismissed): the DM neither landed nor was ignored, so it counts toward
# neither. Keeps a dismissed alert — which anyone with API access can do —
# from dragging down the rate of the person who was DM'd about it.
OUTCOME_VOID = "void"
POSITIVE_OUTCOMES = frozenset({OUTCOME_REPLIED, OUTCOME_ACTED})

# Window the rates are computed over.
_STATS_WINDOW_DAYS = 30
# A person needs this many resolved sends from one source before the reflection
# summary mentions it; below that the rate is noise.
_REFLECTION_MIN_RESOLVED = 3
_REFLECTION_MAX_PEOPLE = 10

# (source, ref) of the proactive work currently sending, if any.
_proactive: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar(
    "attunement_proactive_source", default=None
)


@contextlib.contextmanager
def tag_proactive(source: str, ref: str = "") -> Iterator[None]:
    """Mark every DM sent inside the block as proactive outreach from
    ``source``. ``ref`` names what it is about (a nudge scope key,
    ``alert:<id>``) so a later action on that thing resolves it.

    Wrap only non-yielding code: a ContextVar set inside an async generator
    would leak across its ``yield``."""
    token = _proactive.set((source, ref))
    try:
        yield
    finally:
        _proactive.reset(token)


def current_tag() -> tuple[str, str] | None:
    return _proactive.get()


def _db(db_path: Path | None) -> Path:
    from openexecutive.memory.episodic import _resolve_db_path

    return _resolve_db_path(db_path)


def record_send(
    *,
    person_id: int | None,
    channel: str,
    channel_ref: str,
    outbound_context_id: int | None,
    db_path: Path | None = None,
) -> int | None:
    """Record one proactive DM if a source is tagged and the recipient is a
    rostered person. Returns the row id, or None when nothing was recorded.
    Never raises: it runs right after a send that already succeeded."""
    tag = _proactive.get()
    if tag is None or person_id is None:
        return None
    source, ref = tag
    if source == SOURCE_FOLLOWUP and _is_principal(person_id):
        # A reminder the principal set for themselves is not outreach, and
        # would otherwise read as "the principal ignores follow-ups".
        return None
    try:
        from openexecutive.memory.episodic import _get_conn

        with _get_conn(_db(db_path)) as conn:
            cur = conn.execute(
                "INSERT INTO proactive_outcomes "
                "(created_at, person_id, source, ref, channel, channel_ref, outbound_context_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (datetime.now(UTC).isoformat(), person_id, source, ref or None,
                 channel, channel_ref, outbound_context_id),
            )
            return int(cur.lastrowid or 0) or None
    except Exception:
        logger.warning("outcomes: record_send failed (non-fatal)", exc_info=True)
        return None


def _is_principal(person_id: int) -> bool:
    try:
        from openexecutive.people.store import get_person

        person = get_person(person_id)
        return bool(person is not None and person.is_principal)
    except Exception:
        return False


def _resolve(
    where: str,
    params: tuple[Any, ...],
    outcome: str,
    db_path: Path | None,
    *,
    override_ignored: bool = False,
) -> int:
    """Set ``outcome`` on still-open rows matching ``where`` — and, with
    ``override_ignored``, on rows already swept to ``ignored``: the thing a
    DM chased can get done (or be dismissed) days after the 72h sweep, and
    that later verdict is the truer one. Never raises."""
    try:
        from openexecutive.memory.episodic import _get_conn

        resolved = _db(db_path)
        if not resolved.exists():
            return 0
        with _get_conn(resolved) as conn:
            open_clause = (
                "(outcome IS NULL OR outcome = 'ignored')" if override_ignored
                else "outcome IS NULL"
            )
            cur = conn.execute(
                "UPDATE proactive_outcomes SET outcome = ?, resolved_at = ? "
                f"WHERE {open_clause} AND {where}",
                (outcome, datetime.now(UTC).isoformat(), *params),
            )
            return int(cur.rowcount)
    except Exception:
        logger.warning("outcomes: resolve failed (non-fatal)", exc_info=True)
        return 0


def resolve_by_ref(
    ref: str,
    outcome: str,
    *,
    person_ids: set[int] | None = None,
    db_path: Path | None = None,
) -> int:
    """Resolve open outcomes about ``ref`` (a nudge scope key, an alert).

    ``person_ids`` limits it to DMs sent to those people — an alert can be
    DM'd to several people over successive review passes, and acting on it
    credits only the ones it now belongs to."""
    if not ref:
        return 0
    # A later "done" or "didn't matter" overrides an earlier timeout.
    override = outcome in (OUTCOME_ACTED, OUTCOME_VOID)
    if person_ids is None:
        return _resolve("ref = ?", (ref,), outcome, db_path, override_ignored=override)
    ids = sorted(person_ids)
    if not ids:
        return 0
    placeholders = ",".join("?" * len(ids))
    return _resolve(
        f"ref = ? AND person_id IN ({placeholders})", (ref, *ids), outcome, db_path,
        override_ignored=override,
    )


def resolve_by_outbound_context(
    context_id: int, outcome: str, *, db_path: Path | None = None
) -> int:
    """Resolve the outcome recorded for one outbound DM (a matched reply)."""
    return _resolve("outbound_context_id = ?", (context_id,), outcome, db_path)


def sweep_ignored(
    now: datetime | None = None,
    *,
    after_hours: int | None = None,
    db_path: Path | None = None,
) -> int:
    """Mark outcomes still open after ``after_hours`` as ignored."""
    if after_hours is not None:
        hours = after_hours
    else:
        try:
            from openexecutive.config import get_settings

            hours = get_settings().attunement_ignore_after_hours
        except Exception:
            # Runs on read paths too; a settings failure must not 500 a page.
            logger.warning("outcomes: sweep settings unavailable", exc_info=True)
            return 0
    if hours <= 0:
        return 0
    cutoff = ((now or datetime.now(UTC)) - timedelta(hours=hours)).isoformat()
    return _resolve("created_at < ?", (cutoff,), OUTCOME_IGNORED, db_path)


@dataclass
class SourceStats:
    sent: int = 0
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def resolved(self) -> int:
        return sum(self.counts.values())

    @property
    def positive(self) -> int:
        return sum(n for o, n in self.counts.items() if o in POSITIVE_OUTCOMES)

    @property
    def pending(self) -> int:
        return self.sent - self.resolved

    def rate(self) -> float:
        """Smoothed share of resolved sends that landed — a Laplace prior
        keeps one or two sends from swinging it to 0 or 1."""
        return (self.positive + 1) / (self.resolved + 2)


def acceptance(
    *,
    person_id: int | None = None,
    days: int = _STATS_WINDOW_DAYS,
    db_path: Path | None = None,
) -> dict[tuple[int, str], SourceStats]:
    """Per (person, source) counts over the last ``days``."""
    from openexecutive.memory.episodic import _get_conn

    resolved = _db(db_path)
    if not resolved.exists():
        return {}
    # Sweep on read as well as in the nudge scan: with the scan disabled,
    # unanswered DMs would otherwise stay pending forever and the rates would
    # only ever see the answers.
    sweep_ignored(db_path=db_path)
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    sql = (
        "SELECT person_id, source, outcome, COUNT(*) AS n FROM proactive_outcomes "
        "WHERE created_at >= ? AND (outcome IS NULL OR outcome != 'void')"
    )
    params: list[Any] = [since]
    if person_id is not None:
        sql += " AND person_id = ?"
        params.append(person_id)
    sql += " GROUP BY person_id, source, outcome"
    out: dict[tuple[int, str], SourceStats] = {}
    try:
        with _get_conn(resolved) as conn:
            rows = conn.execute(sql, params).fetchall()
    except Exception:
        logger.warning("outcomes: acceptance query failed", exc_info=True)
        return {}
    for r in rows:
        stats = out.setdefault((int(r["person_id"]), str(r["source"])), SourceStats())
        n = int(r["n"])
        stats.sent += n
        if r["outcome"]:
            stats.counts[str(r["outcome"])] = stats.counts.get(str(r["outcome"]), 0) + n
    return out


def muted_pairs(
    *,
    min_sends: int,
    days: int = _STATS_WINDOW_DAYS,
    db_path: Path | None = None,
) -> set[tuple[int, str]]:
    """``(person, source)`` pairs this person reliably ignores: their most
    recent ``min_sends`` resolved DMs from that source all went unanswered.

    Judged on the most recent sends, not the 30-day share, so a single answer
    lifts it at once — a demoted source gets fewer chances to be answered,
    and a share would keep it demoted long after the person came back."""
    from openexecutive.memory.episodic import _get_conn

    resolved = _db(db_path)
    if min_sends <= 0 or not resolved.exists():
        return set()
    sweep_ignored(db_path=db_path)
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    try:
        with _get_conn(resolved) as conn:
            rows = conn.execute(
                "SELECT person_id, source, outcome FROM proactive_outcomes "
                "WHERE created_at >= ? AND outcome IS NOT NULL AND outcome != 'void' "
                # By when the DM was SENT: an old send swept to "ignored"
                # after a newer one was answered must not bury that answer.
                "ORDER BY person_id, source, created_at DESC, id DESC",
                (since,),
            ).fetchall()
    except Exception:
        logger.warning("outcomes: muted_pairs query failed", exc_info=True)
        return set()
    recent: dict[tuple[int, str], list[str]] = {}
    for r in rows:
        seq = recent.setdefault((int(r["person_id"]), str(r["source"])), [])
        if len(seq) < min_sends:
            seq.append(str(r["outcome"]))
    return {
        key for key, seq in recent.items()
        if len(seq) >= min_sends and not any(o in POSITIVE_OUTCOMES for o in seq)
    }


def render_for_reflection(*, db_path: Path | None = None) -> list[str]:
    """One line per person with enough history: how each source lands."""
    from openexecutive.people.store import list_people

    stats = acceptance(db_path=db_path)
    if not stats:
        return []
    try:
        names = {p.id: p.full_name for p in list_people() if p.id is not None}
    except Exception:
        names = {}
    by_person: dict[int, list[str]] = {}
    for (pid, source), s in sorted(stats.items()):
        if s.resolved < _REFLECTION_MIN_RESOLVED or pid not in names:
            continue
        label = SOURCE_LABELS.get(source, source)
        by_person.setdefault(pid, []).append(f"{label} {s.positive}/{s.resolved} answered")
    return [
        f"- {names[pid]} (id={pid}): " + "; ".join(parts)
        for pid, parts in list(by_person.items())[:_REFLECTION_MAX_PEOPLE]
    ]
