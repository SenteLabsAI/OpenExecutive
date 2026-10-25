// Pure counting behind Home's "At a glance" tiles (components/Briefing.tsx,
// components/briefing/Glance.tsx). No React here so `npm test` can exercise
// it directly (scripts/glance.test.mjs).
import type { DepartmentBriefItem, DynamicWorkflowDef, HandledReply, WatchlistItem, WorkflowRunSummary } from "./api";

const WEEK_MS = 7 * 24 * 60 * 60 * 1000;

function withinWeek(iso: string | null | undefined, now: Date): boolean {
  if (!iso) return false;
  const t = Date.parse(iso);
  return Number.isFinite(t) && t <= now.getTime() && now.getTime() - t < WEEK_MS;
}

// The briefing's real departments. The row with slug "" holds the goals that
// have no department ("Other goals"); it is not a department.
export function realDepartments(departments: DepartmentBriefItem[]): DepartmentBriefItem[] {
  return departments.filter((d) => d.slug !== "");
}

// Every goal on the briefing, with or without a department.
export function goalTotals(departments: DepartmentBriefItem[]): { goals: number; atRisk: number } {
  let goals = 0;
  let atRisk = 0;
  for (const d of departments) {
    goals += d.goal_count;
    atRisk += d.at_risk_count + d.off_track_count;
  }
  return { goals, atRisk };
}

// What Act as me sent or accepted for you in the last seven days.
export function handledThisWeek(replies: HandledReply[], now: Date): number {
  return replies.filter((r) => withinWeek(r.sent_at, now)).length;
}

// Workflows of your own that run on a schedule, and how many times they ran
// this week (runs of anything else, like the system's own, don't count).
export function scheduledWorkflows(
  defs: DynamicWorkflowDef[],
  runs: WorkflowRunSummary[],
  now: Date,
): { scheduled: number; runsThisWeek: number } {
  const names = new Set(defs.filter((d) => d.is_active !== false && Boolean(d.cadence)).map((d) => d.name));
  const runsThisWeek = runs.filter((r) => names.has(r.workflow_name) && withinWeek(r.created_at, now)).length;
  return { scheduled: names.size, runsThisWeek };
}

// What's on the watchlist, and how many of those spoke up this week.
export function watching(items: WatchlistItem[], now: Date): { watched: number; firedThisWeek: number } {
  const on = items.filter((i) => i.enabled);
  return { watched: on.length, firedThisWeek: on.filter((i) => withinWeek(i.last_fired_at, now)).length };
}
