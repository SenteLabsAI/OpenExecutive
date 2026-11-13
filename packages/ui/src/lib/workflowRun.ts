// Starting a workflow from a list in one tap, and counting its steps the same
// way on every screen.

/** Every run's setup step. Not something the workflow does, so never shown or counted. */
export const SETUP_STEP_ID = "context";

/** The steps a person sees: everything but the setup step. */
export function shownSteps<T extends { id: string }>(steps: readonly T[]): T[] {
  return steps.filter((s) => s.id !== SETUP_STEP_ID);
}

/** "1 step", "2 steps". */
export function stepCount(steps: readonly { id: string }[]): string {
  const n = shownSteps(steps).length;
  return `${n} ${n === 1 ? "step" : "steps"}`;
}

// A list's Run leaves a note in this tab's session storage and opens the
// workflow's page, which starts the run when it finds a fresh note. Only a
// click in the app writes the note, so a link someone sends can open a
// workflow but never start one.
const NOTE_PREFIX = "oe.runOnArrival:";
/** A note older than this is from an abandoned navigation, not this one. */
export const RUN_NOTE_MAX_AGE_MS = 30_000;

type NoteStore = Pick<Storage, "getItem" | "setItem" | "removeItem">;

function sessionStore(): NoteStore | null {
  try {
    return typeof window === "undefined" ? null : window.sessionStorage;
  } catch {
    return null;
  }
}

/** A click that opens the link in this tab: not a new tab or window, not another button. */
export function opensHere(e: {
  button: number;
  metaKey: boolean;
  ctrlKey: boolean;
  shiftKey: boolean;
  altKey: boolean;
}): boolean {
  return e.button === 0 && !e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey;
}

/** Called by a list's Run just before it opens the workflow's page in this tab. */
export function noteRunOnArrival(
  name: string,
  store: NoteStore | null = sessionStore(),
  now: number = Date.now(),
): void {
  try {
    store?.setItem(NOTE_PREFIX + name, String(now));
  } catch {
    // Storage full or blocked: the page opens without starting, one tap more.
  }
}

/** Whether Run asked for this workflow to start, and forgets the note either way. */
export function takeRunOnArrival(
  name: string,
  store: NoteStore | null = sessionStore(),
  now: number = Date.now(),
): boolean {
  try {
    const raw = store?.getItem(NOTE_PREFIX + name);
    if (raw == null) return false;
    store?.removeItem(NOTE_PREFIX + name);
    const at = Number(raw);
    return Number.isFinite(at) && now - at >= 0 && now - at <= RUN_NOTE_MAX_AGE_MS;
  } catch {
    return false;
  }
}

/**
 * Whether the workflow page starts the run on arrival: only when Run asked
 * for it and nothing required is left to fill in. Otherwise the page shows
 * its form, and Run it now there starts it.
 */
export function startsOnArrival(asked: boolean, allRequiredFilled: boolean): boolean {
  return asked && allRequiredFilled;
}
