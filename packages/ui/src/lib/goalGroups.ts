// /goals lists every goal (GET /goals) grouped by owner, department or
// status. Goals stand on their own: a department is optional, so a goal with
// none groups under "No department" ("No area" in solo). A goal marked done
// leaves its owner's or department's group for one "Done" group at the end.
//
// Type-only imports, so `npm test` can exercise this under
// `node --experimental-strip-types` (see scripts/goalGroups.test.mjs).

import type { Goal, GoalView } from "@/lib/api";

export type GoalGrouping = "owner" | "department" | "status";

export interface GoalGroup {
  key: string;
  title: string;
  goals: GoalView[];
}

export interface GroupOpts {
  /** The viewer's person id: their group comes first, titled "<name> (you)". */
  viewerId?: number | null;
  /** What a department is called: "department" (team) or "area" (solo). */
  unit?: "department" | "area";
}

// What a goal's pill says. A goal starts "on track" by default, so one the
// weekly check (or a chat update) has never looked at would claim a verdict
// nobody gave: it reads "not checked yet" until its first review.
export type ShownStatus = Goal["status"] | "not_checked";

export function shownStatus(goal: Pick<Goal, "status" | "last_reviewed_at">): ShownStatus {
  return goal.status === "on_track" && !goal.last_reviewed_at ? "not_checked" : goal.status;
}

/** Worst first; a goal nobody has checked sits above the ones known on track. */
export const STATUS_ORDER: ShownStatus[] = ["off_track", "at_risk", "not_checked", "on_track", "done"];
const STATUS_TITLE: Record<ShownStatus, string> = {
  off_track: "Off track",
  at_risk: "At risk",
  not_checked: "Not checked yet",
  on_track: "On track",
  done: "Done",
};

const DONE_KEY = "status:done";

const byId = (a: GoalView, b: GoalView) => (a.id ?? 0) - (b.id ?? 0);

/** Goals per shown status. */
export function goalCounts(goals: Pick<Goal, "status" | "last_reviewed_at">[]): Record<ShownStatus, number> {
  const counts: Record<ShownStatus, number> = { on_track: 0, at_risk: 0, off_track: 0, not_checked: 0, done: 0 };
  for (const g of goals) {
    const s = shownStatus(g);
    if (s in counts) counts[s] += 1;
  }
  return counts;
}

/** `goals` grouped `by`, each group's goals oldest first. Owners: the viewer
 * first, then by name, nobody last. Departments: by title, none last.
 * Status: worst first. Done goals come last, in a group of their own, however
 * the rest is grouped. Empty groups are left out. */
export function groupGoals(goals: GoalView[], by: GoalGrouping, opts: GroupOpts = {}): GoalGroup[] {
  const buckets = new Map<string, GoalGroup>();
  const add = (key: string, title: string, goal: GoalView) => {
    const group = buckets.get(key) ?? { key, title, goals: [] };
    group.goals.push(goal);
    buckets.set(key, group);
  };
  for (const g of goals) {
    if (g.status === "done") {
      add(DONE_KEY, STATUS_TITLE.done, g);
    } else if (by === "owner") {
      const you = g.owner_id != null && g.owner_id === opts.viewerId;
      const name = g.owner_name || (g.owner_id == null ? "No owner yet" : "Someone no longer on the team");
      add(g.owner_id == null ? "owner:none" : `owner:${g.owner_id}`, you ? `${name} (you)` : name, g);
    } else if (by === "department") {
      const none = `No ${opts.unit ?? "department"}`;
      add(g.department_slug ? `dept:${g.department_slug}` : "dept:none", g.department_title || none, g);
    } else {
      const s = shownStatus(g);
      add(`status:${s}`, STATUS_TITLE[s] ?? s, g);
    }
  }
  const groups = [...buckets.values()];
  for (const group of groups) group.goals.sort(byId);
  const rank = (group: GoalGroup): [number, string] => {
    if (group.key === DONE_KEY) return [STATUS_ORDER.length, ""];
    if (by === "status") return [STATUS_ORDER.indexOf(shownStatus(group.goals[0])), ""];
    if (group.key.endsWith(":none")) return [2, ""];
    if (by === "owner" && group.goals[0].owner_id === opts.viewerId && opts.viewerId != null) return [0, ""];
    return [1, group.title.toLowerCase()];
  };
  return groups.sort((a, b) => {
    const [ra, ta] = rank(a);
    const [rb, tb] = rank(b);
    return ra - rb || ta.localeCompare(tb);
  });
}
