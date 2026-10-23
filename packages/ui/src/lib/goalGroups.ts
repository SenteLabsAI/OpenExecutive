// /goals lists every goal (GET /goals) grouped by owner, department or
// status. Goals stand on their own: a department is optional, so a goal with
// none groups under "No department" ("No area" in solo).
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

const STATUS_ORDER: Goal["status"][] = ["off_track", "at_risk", "on_track"];
const STATUS_TITLE: Record<Goal["status"], string> = {
  off_track: "Off track",
  at_risk: "At risk",
  on_track: "On track",
};

const byId = (a: GoalView, b: GoalView) => (a.id ?? 0) - (b.id ?? 0);

/** Goals per status. */
export function goalCounts(goals: GoalView[]): Record<Goal["status"], number> {
  const counts: Record<Goal["status"], number> = { on_track: 0, at_risk: 0, off_track: 0 };
  for (const g of goals) if (g.status in counts) counts[g.status] += 1;
  return counts;
}

/** `goals` grouped `by`, each group's goals oldest first. Owners: the viewer
 * first, then by name, nobody last. Departments: by title, none last.
 * Status: worst first. Empty groups are left out. */
export function groupGoals(goals: GoalView[], by: GoalGrouping, opts: GroupOpts = {}): GoalGroup[] {
  const buckets = new Map<string, GoalGroup>();
  const add = (key: string, title: string, goal: GoalView) => {
    const group = buckets.get(key) ?? { key, title, goals: [] };
    group.goals.push(goal);
    buckets.set(key, group);
  };
  for (const g of goals) {
    if (by === "owner") {
      const you = g.owner_id != null && g.owner_id === opts.viewerId;
      const name = g.owner_name || (g.owner_id == null ? "No owner yet" : "Someone no longer on the team");
      add(g.owner_id == null ? "owner:none" : `owner:${g.owner_id}`, you ? `${name} (you)` : name, g);
    } else if (by === "department") {
      const none = `No ${opts.unit ?? "department"}`;
      add(g.department_slug ? `dept:${g.department_slug}` : "dept:none", g.department_title || none, g);
    } else {
      add(`status:${g.status}`, STATUS_TITLE[g.status] ?? g.status, g);
    }
  }
  const groups = [...buckets.values()];
  for (const group of groups) group.goals.sort(byId);
  const rank = (group: GoalGroup): [number, string] => {
    if (by === "status") return [STATUS_ORDER.indexOf(group.goals[0].status), ""];
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
