// What the chat shows while a turn is running, so that a long turn never
// looks like a frozen cursor. The stream only sends an event when a tool round
// starts (`activity` + `thinking`) and while text is coming in. Between those,
// a specialist fan-out, a workflow or the model thinking can run for minutes
// with no bytes at all. This works out the status line from what has arrived
// so far and how long ago it arrived.

// Text that stops coming in for this long is treated as the Executive working
// on something, not a pause between tokens.
export const STALL_MS = 4000;
// The elapsed-time counter appears only once a turn has taken this long, so a
// quick reply isn't cluttered with "· 1s".
export const SHOW_ELAPSED_AFTER_MS = 5000;
// Before the first event, the dots show alone for this long, then get the
// fallback label.
export const LABEL_WAIT_AFTER_MS = 3000;

export interface TurnStatusInput {
  isLoading: boolean;
  // Whether any reply text has streamed yet.
  hasText: boolean;
  // A tool round has started (`thinking`) and no text has come in since.
  isConsulting: boolean;
  activityLabel: string | null;
  // Committee review is running; its own phase indicator takes over.
  inCommittee: boolean;
  msSinceTurnStart: number;
  msSinceLastEvent: number;
  // The label to show when work is going on but nothing has named it.
  fallbackLabel: string;
}

export interface TurnStatus {
  show: boolean;
  // null means the dots show without a label.
  label: string | null;
  // e.g. "42s". null until the turn has run SHOW_ELAPSED_AFTER_MS.
  elapsed: string | null;
}

const HIDDEN: TurnStatus = { show: false, label: null, elapsed: null };

export function turnStatus(input: TurnStatusInput): TurnStatus {
  if (!input.isLoading) return HIDDEN;
  const elapsed =
    input.msSinceTurnStart >= SHOW_ELAPSED_AFTER_MS
      ? formatElapsed(input.msSinceTurnStart)
      : null;

  if (input.inCommittee) return { show: true, label: null, elapsed };
  if (input.isConsulting) {
    return { show: true, label: input.activityLabel ?? input.fallbackLabel, elapsed };
  }
  if (!input.hasText) {
    const label =
      input.msSinceTurnStart >= LABEL_WAIT_AFTER_MS ? input.fallbackLabel : null;
    return { show: true, label, elapsed };
  }
  // Text is on screen: stay out of the way while it flows, speak up once it
  // stalls.
  if (input.msSinceLastEvent >= STALL_MS) {
    return { show: true, label: input.fallbackLabel, elapsed };
  }
  return HIDDEN;
}

export function formatElapsed(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  if (total < 60) return `${total}s`;
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}m ${String(s).padStart(2, "0")}s`;
}
