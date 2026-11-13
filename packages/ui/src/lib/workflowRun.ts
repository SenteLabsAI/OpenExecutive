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

/** The query flag a list's Run sets so the workflow page starts the run on arrival. */
export const RUN_PARAM = "run";

/** Where a list's Run goes: the workflow's page, starting the run at once. */
export function runHref(name: string): string {
  return `/jobs/${encodeURIComponent(name)}?${RUN_PARAM}=1`;
}

/**
 * Whether the workflow page starts the run on arrival: only when Run asked
 * for it and nothing required is left to fill in. Otherwise the page shows
 * its form, and Run it now there starts it.
 */
export function startsOnArrival(runParam: string | null | undefined, allRequiredFilled: boolean): boolean {
  return runParam === "1" && allRequiredFilled;
}
