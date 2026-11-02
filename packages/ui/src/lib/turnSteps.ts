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

// The plan a long turn shows up front (`plan` events from the backend's
// `plan_steps`, orchestrator/turn_plan.py): every step, with the done ones
// ticked, the current one running and the rest still to do. A turn that
// sends no plan keeps the steps above, added as they happen.

export interface TurnPlan {
  steps: string[];
  current: number;
  // The steps the turn first showed: a step not among them was added later.
  first: string[];
}

export type PlanStepState = "done" | "current" | "todo";

export interface PlanRow {
  label: string;
  state: PlanStepState;
  added: boolean;
}

// Answer text this long since the last tool round means the turn is writing
// its reply, not a line before its next tools: from the step before the last,
// the plan moves to its last step ("Write the summary") without waiting for a
// call that never comes.
export const WRITING_FROM_CHARS = 280;

export function applyPlan(
  prev: TurnPlan | null,
  steps: unknown,
  current: unknown,
): TurnPlan | null {
  const clean = Array.isArray(steps)
    ? steps.filter((s): s is string => typeof s === "string" && s.trim() !== "").map((s) => s.trim())
    : [];
  if (clean.length === 0) return prev;
  const at = typeof current === "number" && Number.isFinite(current) ? Math.trunc(current) : 0;
  return {
    steps: clean,
    current: Math.max(0, Math.min(at, clean.length - 1)),
    first: prev ? prev.first : clean,
  };
}

export function planRows(
  plan: TurnPlan,
  { writing = false, handingOver = false }: { writing?: boolean; handingOver?: boolean } = {},
): PlanRow[] {
  const last = plan.steps.length - 1;
  // Only from the step before the last: a long line of text before an early
  // step's tool calls must not tick the whole plan off.
  const current = writing && plan.current >= last - 1 ? last : plan.current;
  return plan.steps.map((label, i) => ({
    label,
    state: i < current ? "done" : i === current && !handingOver ? "current" : "todo",
    added: !plan.first.includes(label),
  }));
}
