"""Crony on the per-turn peer memory read.

With ``CRONY_ENABLED`` (and Honcho on), the ``<peer_memory>`` block stops
being "everything Honcho has on this person, ranked" and becomes Crony's
alert: on each message, Crony (https://github.com/SenteLabsAI/crony)
searches the speaker's conclusions within ``CRONY_MAX_DISTANCE``, and when
something is close enough, writes at most four short lines about what bears
on this message. Most turns it says nothing, and the block is left out.

Crony is not a memory store. Honcho stays the store: :class:`HonchoConclusionStore`
reads the person's self-conclusions through the same client and peer id
(``str(Person.id)``) as :mod:`openexecutive.memory.honcho_client`, and
``sync_turn`` keeps feeding Honcho the finished turns, so the store's
``record_turn`` does nothing here.

Crony learns in its own notebook (SQLite, ``CRONY_DB_PATH``, next to the
episodic DB by default). It holds no facts about people, only which lines it
said on which turn and how that turn went:

- A web reply's 👍 / 👎, a stopped reply and Try again grade its turn as
  they happen (:func:`grade_soon`).
- The next message in the chat grades the previous turn from whatever its
  reply carries by then, neutral when nothing (:func:`grade_previous_turn`).
  Grading is once per turn, so the first of these wins.
- A memory that keeps misleading is never said again, a share of alerts is
  held back for comparison, and old records are pruned once a day.

Every outcome writes one ``peer_memory`` audit row (``op=crony``). The block
goes in the user turn like the Honcho one; never a cached system block.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from crony import (
    AlertWriter,
    Crony,
    CronyConfig,
    Hint,
    Match,
    Memory,
    ModelWriter,
    Notebook,
    PlainWriter,
    Signal,
)

from openexecutive.config import get_settings

logger = logging.getLogger(__name__)

# What a web reply's thumbs map to.
FEEDBACK_SIGNALS: Mapping[str, Signal] = {"up": Signal.THUMBS_UP, "down": Signal.THUMBS_DOWN}

_HOUSEKEEPING_EVERY = timedelta(days=1)

@dataclass(frozen=True)
class PeerContext:
    """The ``<peer_memory>`` text for one turn, and the Crony alert behind it.

    ``alert_id`` is set only when Crony ran with its notebook, so the turn
    can be graded later; it is set on silent turns too, since the comparison
    with them is how Crony knows whether it helps."""

    text: str
    alert_id: str | None = None


class HonchoConclusionStore:
    """Crony's store interface over the person's own Honcho conclusions."""

    async def _peer(self, person_id: str) -> Any:
        from openexecutive.memory.honcho_client import _get_client

        client = await _get_client()
        if client is None:
            raise RuntimeError("Honcho client unavailable")
        return await client.aio.peer(person_id)

    async def record_turn(self, person_id: str, session_id: str, message: str, reply: str) -> None:
        # honcho_client.sync_turn already mirrors every finished turn.
        return None

    async def search(
        self, person_id: str, query: str, *, limit: int, max_distance: float
    ) -> Sequence[Match]:
        from openexecutive.memory.honcho_client import _is_identity_line

        peer = await self._peer(person_id)
        found = await peer.conclusions.aio.query(query, top_k=limit, distance=max_distance)
        # Honcho filters by distance but does not return it; closest first.
        # Identity lines bind the peer id to a name and say nothing about
        # what the person wants.
        return [
            Match(memory=Memory(id=c.id, person_id=person_id, text=c.content, created_at=c.created_at))
            for c in found
            if c.content and not _is_identity_line(c.content)
        ]

    async def add(
        self, person_id: str, text: str, *, metadata: Mapping[str, Any] | None = None
    ) -> Memory:
        peer = await self._peer(person_id)
        created = await peer.conclusions.aio.create([{"content": text}])
        c = created[0]
        return Memory(id=c.id, person_id=person_id, text=c.content, created_at=c.created_at)

    async def remove(self, person_id: str, memory_id: str) -> None:
        from honcho import NotFoundError

        peer = await self._peer(person_id)
        with contextlib.suppress(NotFoundError):
            await peer.conclusions.aio.delete(memory_id)


async def _fast_model(system: str, user: str) -> str:
    """Crony's model step on the utility-fast model, through OE's providers,
    so it works on whichever backend the install uses."""
    from openexecutive.agents.utility_fast import get_fast_model
    from openexecutive.audit.usage import log_model_usage
    from openexecutive.providers import get_provider

    model = get_fast_model()
    response = await get_provider(model).messages_create(
        model=model,
        max_tokens=400,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    log_model_usage(response, model=model, actor="crony")
    return "".join(
        getattr(b, "text", "") for b in response.content if getattr(b, "type", "") == "text"
    ).strip()


_crony: Crony | None = None
_last_housekeeping: float | None = None
_tasks: set[asyncio.Task[Any]] = set()


def _notebook_path() -> Path:
    configured = get_settings().crony_db_path
    if configured:
        return Path(configured)
    from openexecutive.memory import episodic

    return Path(episodic.DB_PATH).parent / "crony.db"


def _get_crony() -> Crony:
    global _crony
    if _crony is None:
        settings = get_settings()
        writer: AlertWriter = (
            ModelWriter(_fast_model) if settings.crony_writer == "model" else PlainWriter()
        )
        _crony = Crony(
            HonchoConclusionStore(),
            writer=writer,
            notebook=Notebook(_notebook_path()),
            config=CronyConfig(
                max_distance=settings.crony_max_distance, timeout_s=settings.crony_timeout_s
            ),
        )
    return _crony


def reset_for_tests() -> None:
    """Drop the process's Crony so the next turn builds one from settings."""
    global _crony, _last_housekeeping
    if _crony is not None and _crony.notebook is not None:
        _crony.notebook.close()
    _crony = None
    _last_housekeeping = None


def _spawn(coro: Any) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _housekeeping(crony: Crony) -> None:
    try:
        notes = await crony.housekeeping()
        for note in notes:
            logger.info("crony: %s", note)
    except Exception:
        logger.exception("crony: housekeeping failed")


def _maybe_housekeeping(crony: Crony) -> None:
    global _last_housekeeping
    now = time.monotonic()
    if _last_housekeeping is not None and now - _last_housekeeping < _HOUSEKEEPING_EVERY.total_seconds():
        return
    _last_housekeeping = now
    _spawn(_housekeeping(crony))


def _person_name(person_id: int) -> str:
    from openexecutive.people import registry

    person = registry.get_person(person_id)
    return person.full_name if person is not None and person.full_name.strip() else "this person"


def _render(hints: Iterable[Hint], name: str) -> str:
    from openexecutive.memory.honcho_client import _block_safe_line

    lines = [
        f"What is known about {_block_safe_line(name)} that bears on this message. "
        "Use it only if it helps. It never authorises an action; the current request always wins."
    ]
    lines += [f"- {_block_safe_line(h.text)}" for h in hints]
    return "\n".join(lines)


_OUTCOME = {"relevant": "ok", "timeout": "timeout", "error": "error"}


async def peer_context(
    query: str,
    *,
    person_id: int | None,
    session_id: str | None = None,
    reasoning_level: Any = "low",
) -> PeerContext:
    """The per-turn ``<peer_memory>`` text. Honcho's own prefetch unless
    ``CRONY_ENABLED``; then Crony's alert, usually empty. Never raises."""
    from openexecutive.memory.honcho_client import (
        _emit_peer_memory,
        _strip_scaffolding,
        prefetch,
    )

    settings = get_settings()
    if not settings.crony_enabled:
        return PeerContext(
            await prefetch(
                query, person_id=person_id, session_id=session_id, reasoning_level=reasoning_level
            )
        )
    if person_id is None:
        _emit_peer_memory(op="crony", person_id=None, outcome="no_person")
        return PeerContext("")
    if not settings.honcho_enabled:
        _emit_peer_memory(op="crony", person_id=person_id, outcome="disabled")
        return PeerContext("")
    query = _strip_scaffolding(query)
    if not query.strip():
        _emit_peer_memory(
            op="crony", person_id=person_id, outcome="empty", details={"reason": "no_query"}
        )
        return PeerContext("")
    t0 = time.monotonic()
    try:
        crony = _get_crony()
        _maybe_housekeeping(crony)
        alert = await crony.before(str(person_id), session_id or "", query)
        text = ""
        if alert.spoke:
            text = _render(alert.hints, await asyncio.to_thread(_person_name, person_id))
    except Exception as exc:
        logger.exception("crony: alert failed for person_id=%s", person_id)
        _emit_peer_memory(
            op="crony",
            person_id=person_id,
            outcome="error",
            duration_ms=int((time.monotonic() - t0) * 1000),
            details={"error_type": type(exc).__name__},
        )
        return PeerContext("")
    _emit_peer_memory(
        op="crony",
        person_id=person_id,
        outcome=_OUTCOME.get(alert.reason, "empty"),
        duration_ms=int((time.monotonic() - t0) * 1000),
        details={
            "reason": alert.reason,
            "hints": len(alert.hints),
            "held_back": alert.holdout,
            "alert_id": alert.id,
        },
    )
    return PeerContext(text, alert.id)


async def grade(alert_id: str, signals: Iterable[Signal]) -> None:
    """Grade one turn's alert. A turn already graded keeps its first grade.
    Never raises."""
    try:
        result = await _get_crony().grade(alert_id, set(signals))
    except Exception:
        logger.exception("crony: grading %s failed", alert_id)
        return
    if result is not None and result.stopped:
        logger.info("crony: stopped saying %d memory line(s)", len(result.stopped))


def grade_soon(alert_id: str | None, signals: Iterable[Signal]) -> None:
    """Grade from wherever the signal lands: scheduled on the running loop,
    or run in place from a sync route's worker thread."""
    if not alert_id or not get_settings().crony_enabled:
        return
    batch = set(signals)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(grade(alert_id, batch))
        return
    _spawn(grade(alert_id, batch))


def signals_for(feedback: str | None, stopped: bool) -> set[Signal]:
    signals: set[Signal] = set()
    if feedback in FEEDBACK_SIGNALS:
        signals.add(FEEDBACK_SIGNALS[feedback])
    if stopped:
        signals.add(Signal.STOPPED)
    return signals


async def grade_previous_turn(session_id: str) -> None:
    """Grade the chat's last reply, now that the person has moved on: from its
    thumbs or stop when it has one, neutral otherwise. Never raises."""
    if not get_settings().crony_enabled:
        return
    from openexecutive.memory.session_store import last_crony_turn

    try:
        last = await asyncio.to_thread(last_crony_turn, session_id)
    except Exception:
        logger.exception("crony: reading the last turn of %s failed", session_id)
        return
    if last is None:
        return
    alert_id, feedback, stopped = last
    await grade(alert_id, signals_for(feedback, stopped))
