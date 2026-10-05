"use client";

import { useEffect, useState } from "react";
import { CouncilMark } from "./BrandMark";
import CommitteePhaseIndicator from "./CommitteePhaseIndicator";
import type { CommitteePhase } from "@/lib/api";
import type { TurnStatus } from "@/lib/turnStatus";

// Re-renders once a second while a turn runs and tracks when it started and
// when its last stream event arrived, for `turnStatus`. Call `start` in the
// same handler that sets the turn loading, so the first render of the turn
// already reads fresh times, and `markEvent` for every streamed item.
export function useTurnClock(isLoading: boolean) {
  // State, not refs: the derived ms values are computed during render, and a
  // ref read in render is what react-hooks/refs forbids. `markEvent` fires in
  // the same stream loop that already appends chunk text to state, so React
  // batches the two updates and this costs no extra render per event.
  const [startedAt, setStartedAt] = useState(0);
  const [lastEventAt, setLastEventAt] = useState(0);
  const [now, setNow] = useState(0);

  useEffect(() => {
    if (!isLoading) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [isLoading]);

  return {
    msSinceTurnStart: now - startedAt,
    msSinceLastEvent: now - lastEventAt,
    start: () => {
      const t = Date.now();
      setStartedAt(t);
      setLastEventAt(t);
      setNow(t);
    },
    markEvent: () => {
      setLastEventAt(Date.now());
    },
  };
}

// `showMark` is off where the Executive's avatar beside the row already plays
// the Consult animation, so the turn doesn't show it twice.
export default function TurnStatusRow({
  status,
  committeePhase,
  showMark = true,
}: {
  status: TurnStatus;
  committeePhase: CommitteePhase | null;
  showMark?: boolean;
}) {
  return (
    <div className="flex items-center gap-2 flex-wrap" aria-label="Thinking">
      {showMark && <CouncilMark consulting className="w-4 h-4 text-accent" />}
      {committeePhase ? (
        <CommitteePhaseIndicator phase={committeePhase} />
      ) : status.label ? (
        <span className="text-xs text-fg-muted italic" aria-live="polite">
          {status.label}
        </span>
      ) : null}
      {status.elapsed && (
        <span className="text-xs text-fg-muted/70 tabular-nums">· {status.elapsed}</span>
      )}
    </div>
  );
}
