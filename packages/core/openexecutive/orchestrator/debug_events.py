from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class DebugEvent:
    kind: str
    data: dict[str, Any]
    ts: float = field(default_factory=time.monotonic)
    turn_id: str | None = None


class DebugCollector:
    """Accumulates debug events during a single request. Single async task only."""

    def __init__(self, t0: float | None = None, turn_id: str | None = None) -> None:
        self._t0 = t0 if t0 is not None else time.monotonic()
        self._events: list[DebugEvent] = []
        self.turn_id = turn_id

    def emit(self, kind: str, data: dict[str, Any]) -> DebugEvent:
        event = DebugEvent(
            kind=kind,
            data=data,
            ts=round(time.monotonic() - self._t0, 3),
            turn_id=self.turn_id,
        )
        self._events.append(event)
        # Mirror to terminal so `make dev` shows orchestrator progress live.
        logger.info("debug_event kind=%s turn_id=%s data=%s", kind, self.turn_id, data)
        return event

    @property
    def events(self) -> list[DebugEvent]:
        """The events emitted so far, oldest first (a copy)."""
        return list(self._events)

    def to_sse_dict(self, event: DebugEvent) -> dict[str, Any]:
        # `turn_id` is always present (possibly null) so client-side types are stable.
        return {
            "type": "debug_event",
            "kind": event.kind,
            "ts": event.ts,
            "data": event.data,
            "turn_id": event.turn_id,
        }
