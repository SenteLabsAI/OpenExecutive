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
export function goalTotals(departments: DepartmentBriefItem[]): { goals: number; atRisk: number; unchecked: number } {
  let goals = 0;
  let atRisk = 0;
  let unchecked = 0;
  for (const d of departments) {
    goals += d.goal_count;
    atRisk += d.at_risk_count + d.off_track_count;
    unchecked += d.unchecked_count ?? 0;
  }
  return { goals, atRisk, unchecked };
}

// What Act as me sent or accepted for you in the last seven days.
export function handledThisWeek(replies: HandledReply[], now: Date): number {
  return replies.filter((r) => withinWeek(r.sent_at, now)).length;
}

// The workflows of your own that run on a schedule.
export function scheduledNames(defs: DynamicWorkflowDef[]): string[] {
  return defs.filter((d) => d.is_active !== false && Boolean(d.cadence)).map((d) => d.name);
}

// Workflows of your own that run on a schedule, and how many times they ran
// this week (runs of anything else, like the system's own, don't count).
export function scheduledWorkflows(
  defs: DynamicWorkflowDef[],
  runs: WorkflowRunSummary[],
  now: Date,
): { scheduled: number; runsThisWeek: number } {
  const names = new Set(scheduledNames(defs));
  const runsThisWeek = runs.filter((r) => names.has(r.workflow_name) && withinWeek(r.created_at, now)).length;
  return { scheduled: names.size, runsThisWeek };
}

// What's on the watchlist, and how many of those spoke up this week.
export function watching(items: WatchlistItem[], now: Date): { watched: number; firedThisWeek: number } {
  const on = items.filter((i) => i.enabled);
  return { watched: on.length, firedThisWeek: on.filter((i) => withinWeek(i.last_fired_at, now)).length };
}

// The words on Home's bottom line. Each item reads on its own ("5 goals,
// 1 at risk"), since the line has no headings.
export function counted(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

// Your tasks: those due this week, or else how many are due later.
export function tasksText(dueThisWeek: number, overdue: number, later: number): string {
  if (dueThisWeek > 0) {
    const due = `${counted(dueThisWeek, "task", "tasks")} due this week`;
    return overdue > 0 ? `${due}, ${overdue} overdue` : due;
  }
  return `${counted(later, "task", "tasks")} due later`;
}

// A goal nothing has checked yet is not called on track.
export function goalsText(goals: number, atRisk: number, unchecked = 0): string {
  const g = counted(goals, "goal", "goals");
  if (atRisk > 0) return `${g}, ${atRisk} at risk`;
  if (unchecked > 0) return `${g}, ${unchecked} not checked yet`;
  return `${g} on track`;
}

// Departments: the ones that need you, or else all of them on track. Those
// with goals nothing has checked yet (`unchecked`) aren't called on track.
export function departmentsText(active: number, needAttention: number, atRisk: boolean, unchecked = 0): string {
  if (needAttention > 0)
    return `${counted(needAttention, "department", "departments")} ${
      atRisk ? "at risk" : needAttention === 1 ? "needs attention" : "need attention"
    }`;
  const d = counted(active, "department", "departments");
  return unchecked > 0 ? `${d}, ${unchecked} not checked yet` : `${d} on track`;
}

// People: those to follow up with, or else everyone, all clear.
export function peopleText(total: number, toFollowUp: number): string {
  if (toFollowUp > 0) return `${counted(toFollowUp, "person", "people")} to follow up`;
  return `${counted(total, "person", "people")}, all clear`;
}

// The watchlist, with the new signals when there are any.
export function watchingText(watched: number, signals: number): string {
  const sig = counted(signals, "new signal", "new signals");
  if (watched > 0) {
    const w = `Watching ${counted(watched, "thing", "things")}`;
    return signals > 0 ? `${w}, ${sig}` : w;
  }
  return sig;
}
