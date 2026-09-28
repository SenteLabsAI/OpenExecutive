"use client";

import { useEffect, useRef, useState } from "react";
import CommitteePhaseIndicator from "./CommitteePhaseIndicator";
import type { CommitteePhase } from "@/lib/api";
import type { TurnStatus } from "@/lib/turnStatus";

// Re-renders once a second while a turn runs and tracks when it started and
// when its last stream event arrived, for `turnStatus`. Call `start` in the
// same handler that sets the turn loading, so the first render of the turn
// already reads fresh times, and `markEvent` for every streamed item.
export function useTurnClock(isLoading: boolean) {
  const startedAt = useRef(0);
  const lastEventAt = useRef(0);
  const [now, setNow] = useState(0);

  useEffect(() => {
    if (!isLoading) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [isLoading]);

  return {
    msSinceTurnStart: now - startedAt.current,
    msSinceLastEvent: now - lastEventAt.current,
    start: () => {
      const t = Date.now();
      startedAt.current = t;
      lastEventAt.current = t;
      setNow(t);
    },
    markEvent: () => {
      lastEventAt.current = Date.now();
    },
  };
}

export default function TurnStatusRow({
  status,
  committeePhase,
}: {
  status: TurnStatus;
  committeePhase: CommitteePhase | null;
}) {
  return (
    <div className="flex items-center gap-2 flex-wrap">
      <div className="flex gap-1.5" aria-label="Thinking">
        {[0, 1, 2].map((i) => (
          <div
            key={i}
            className="w-1.5 h-1.5 bg-fg-muted rounded-full animate-bounce motion-reduce:animate-none"
            style={{ animationDelay: `${i * 0.15}s` }}
          />
        ))}
      </div>
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
