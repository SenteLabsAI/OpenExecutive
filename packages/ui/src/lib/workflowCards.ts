/**
 * What a card under "Your workflows" on /jobs shows: a plain line for its
 * schedule, and which one button leads. Pure, so it is tested on its own
 * (scripts/workflowCards.test.mjs).
 */

const DAYS: Record<string, string> = {
  mon: "Monday",
  tue: "Tuesday",
  wed: "Wednesday",
  thu: "Thursday",
  fri: "Friday",
  sat: "Saturday",
  sun: "Sunday",
};

const TIME = /^\d{1,2}:\d{2}$/;

/**
 * A saved cadence (`daily@HH:MM`, `weekly@DOW@HH:MM`, `quarterly@DD-HH:MM`,
 * all UTC) in words. Anything else comes back as written, so a format this
 * doesn't know yet still shows rather than vanishing.
 */
export function cadenceLabel(cadence: string | null | undefined): string {
  const raw = (cadence ?? "").trim();
  if (!raw) return "";
  const parts = raw.split("@");
  if (parts[0] === "daily" && parts.length === 2 && TIME.test(parts[1])) {
    return `Every day at ${parts[1]} UTC`;
  }
  if (parts[0] === "weekly" && parts.length === 3 && TIME.test(parts[2])) {
    const day = DAYS[parts[1].toLowerCase()];
    if (day) return `Every ${day} at ${parts[2]} UTC`;
  }
  if (parts[0] === "quarterly" && parts.length === 2) {
    const m = /^(\d{1,2})-(\d{1,2}:\d{2})$/.exec(parts[1]);
    if (m) return `Quarterly, day ${Number(m[1])} at ${m[2]} UTC`;
  }
  return raw;
}

interface RunLike {
  run_id: string;
  workflow_name: string;
  status: string;
  updated_at: string;
}

export type CardAction =
  /** Switched off: its review card turns it on. */
  | { kind: "approve" }
  /** A run is waiting on a sign-off: open that run. */
  | { kind: "signoff"; runId: string }
  | { kind: "run" };

/**
 * The card's primary button. A workflow that is off can only be reviewed;
 * one with a run waiting for sign-off leads with that run (the newest, if
 * several wait); otherwise Run.
 */
export function cardAction(name: string, active: boolean, runs: RunLike[]): CardAction {
  if (!active) return { kind: "approve" };
  const waiting = runs
    .filter((r) => r.workflow_name === name && r.status === "awaiting_human")
    .sort((a, b) => b.updated_at.localeCompare(a.updated_at));
  if (waiting.length > 0) return { kind: "signoff", runId: waiting[0].run_id };
  return { kind: "run" };
}

/** When this workflow last ran (its newest run's update time), or null. */
export function lastRunAt(name: string, runs: RunLike[]): string | null {
  let latest: string | null = null;
  for (const r of runs) {
    if (r.workflow_name !== name) continue;
    if (latest === null || r.updated_at > latest) latest = r.updated_at;
  }
  return latest;
}
