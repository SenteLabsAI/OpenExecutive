// /goals groups every goal by its area. An area is a department row under the
// hood — solo mode calls it an area — so this reads GET /departments, whose
// rows carry their goals.
//
// Type-only imports, so `npm test` can exercise this under
// `node --experimental-strip-types` (see scripts/goalAreas.test.mjs).

import type { DepartmentState, Goal } from "@/lib/api";

export interface AreaRef {
  slug: string;
  title: string;
}

export interface GoalArea extends AreaRef {
  goals: Goal[];
}

export interface GoalAreas {
  /** Areas that have goals, in the order the API returned them. */
  withGoals: GoalArea[];
  /** Areas with no goals yet, by title — offered as places to add one. */
  empty: AreaRef[];
  /** Every area, by title — the add-goal form's picker. */
  all: AreaRef[];
  /** Goals per status across every area. */
  counts: Record<Goal["status"], number>;
}

const byTitle = (a: AreaRef, b: AreaRef) => a.title.localeCompare(b.title);

export function groupGoalsByArea(departments: DepartmentState[]): GoalAreas {
  const counts: GoalAreas["counts"] = { on_track: 0, at_risk: 0, off_track: 0 };
  const withGoals: GoalArea[] = [];
  const empty: AreaRef[] = [];
  for (const d of departments) {
    const ref = { slug: d.config.slug, title: d.config.title };
    if (d.goals.length === 0) {
      empty.push(ref);
      continue;
    }
    withGoals.push({ ...ref, goals: d.goals });
    for (const g of d.goals) {
      if (g.status in counts) counts[g.status] += 1;
    }
  }
  const all = departments.map((d) => ({ slug: d.config.slug, title: d.config.title })).sort(byTitle);
  return { withGoals, empty: empty.sort(byTitle), all, counts };
}

/** `departments` with `goal` added to, replaced in, or (by id) removed from its area. */
export function applyGoalChange(
  departments: DepartmentState[],
  change: { saved: Goal } | { deleted: { slug: string; id: number } },
): DepartmentState[] {
  return departments.map((d) => {
    if ("saved" in change) {
      const goal = change.saved;
      if (d.config.slug !== goal.department_slug) return d;
      const exists = d.goals.some((g) => g.id === goal.id);
      return {
        ...d,
        goals: exists ? d.goals.map((g) => (g.id === goal.id ? goal : g)) : [...d.goals, goal],
      };
    }
    const { slug, id } = change.deleted;
    if (d.config.slug !== slug) return d;
    return { ...d, goals: d.goals.filter((g) => g.id !== id) };
  });
}
