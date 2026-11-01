// The steps a long chat turn has taken so far, from its `activity` events.
// Each round names what it is doing; a repeat of the step already showing
// (the same read run again, or two rounds of "Working…") is one step.

export const MAX_STEPS = 12;

// Shown as a step list, with Stop and I'll finish it, once the turn has done
// this many different things. A quick answer keeps the one-line status.
export const STEPS_SHOWN_FROM = 2;

export function addStep(steps: string[], label: string | null | undefined): string[] {
  const clean = (label ?? "").trim();
  if (!clean || steps[steps.length - 1] === clean) return steps;
  const next = [...steps, clean];
  // Keep the newest: the list is about where the work is now.
  return next.length > MAX_STEPS ? next.slice(next.length - MAX_STEPS) : next;
}

export function showSteps(steps: string[]): boolean {
  return steps.length >= STEPS_SHOWN_FROM;
}
