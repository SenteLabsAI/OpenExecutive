"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

import {
  AddGoalForm,
  GoalRow,
  GoalStatusPill,
  goalStatusLabel,
  type DepartmentOption,
  type OwnerOption,
} from "@/components/goals/GoalEditor";
import Button from "@/components/ui/Button";
import { useWorkspace } from "@/components/workspace/WorkspaceContext";
import {
  getPeopleViewer,
  listDepartments,
  listGoals,
  listPeople,
  type GoalView,
} from "@/lib/api";
import { goalCounts, groupGoals, type GoalGrouping } from "@/lib/goalGroups";

// Every goal in one place (GET /goals). A goal has an owner and, optionally,
// a department ("area" in solo), so the page groups by owner, department or
// status. Goals with no department are checked by the weekly goal review, so
// nothing here needs a department to be tracked.

const GROUPING_LABEL: Record<GoalGrouping, string> = {
  owner: "Owner",
  department: "Department",
  status: "Status",
};

interface Loaded {
  goals: GoalView[];
  departments: DepartmentOption[];
}

async function load(): Promise<Loaded> {
  const [goals, ds] = await Promise.all([listGoals(), listDepartments()]);
  const departments = ds
    .map((d) => ({ slug: d.config.slug, title: d.config.title }))
    .sort((a, b) => a.title.localeCompare(b.title));
  return { goals, departments };
}

export default function GoalsPage() {
  const [goals, setGoals] = useState<GoalView[] | null>(null);
  const [departments, setDepartments] = useState<DepartmentOption[]>([]);
  const [owners, setOwners] = useState<OwnerOption[]>([]);
  const [viewerId, setViewerId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [grouping, setGrouping] = useState<GoalGrouping>("owner");
  const { mode } = useWorkspace();
  const solo = mode === "solo";
  const unit = solo ? "area" : "department";

  const apply = useCallback((loaded: Loaded) => {
    setGoals(loaded.goals);
    setDepartments(loaded.departments);
    setError(null);
  }, []);
  const fail = useCallback((e: unknown) => {
    setError(e instanceof Error ? e.message : "Failed to load goals");
  }, []);
  const reload = useCallback(() => {
    load().then(apply).catch(fail);
  }, [apply, fail]);

  useEffect(() => {
    load()
      .then((loaded) => {
        apply(loaded);
        // `?add=1` (Home's first-week card) opens the add form once loaded.
        if (new URLSearchParams(window.location.search).get("add") === "1") setAdding(true);
      })
      .catch(fail);
    // The owner pickers and "(you)": best effort, the page works without them.
    listPeople()
      .then((ps) =>
        setOwners(ps.filter((p) => p.id != null).map((p) => ({ id: p.id!, name: p.full_name }))),
      )
      .catch(() => {});
    getPeopleViewer()
      .then((v) => setViewerId(v.person_id))
      .catch(() => {});
  }, [apply, fail]);

  // Grouping by department only means something once one exists.
  const groupings: GoalGrouping[] = departments.length > 0 ? ["owner", "department", "status"] : ["owner", "status"];
  const by = groupings.includes(grouping) ? grouping : "owner";
  const groups = useMemo(() => groupGoals(goals ?? [], by, { viewerId, unit }), [goals, by, viewerId, unit]);
  const ownerOptions = useMemo(
    () => owners.map((o) => (o.id === viewerId ? { ...o, name: `${o.name} (you)` } : o)),
    [owners, viewerId],
  );
  const total = goals?.length ?? 0;
  const counts = goalCounts(goals ?? []);
  const summary = (["off_track", "at_risk", "on_track"] as const)
    .filter((s) => counts[s] > 0)
    .map((s) => `${counts[s]} ${goalStatusLabel(s)}`)
    .join(" · ");

  // One line under each goal: what the grouping doesn't already say.
  const metaFor = (g: GoalView): string => {
    const parts: string[] = [];
    if (by !== "owner" && g.owner_name) parts.push(g.owner_id === viewerId ? `${g.owner_name} (you)` : g.owner_name);
    if (by !== "department" && g.department_title) parts.push(g.department_title);
    return parts.join(" · ");
  };

  return (
    <div className="flex flex-col h-full bg-surface">
      <main className="flex-1 overflow-y-auto">
        <div className="max-w-3xl mx-auto px-4 sm:px-6 py-8">
          <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between mb-6">
            <div className="min-w-0">
              <h1 className="text-2xl sm:text-3xl font-bold tracking-tight text-fg">Goals</h1>
              <p className="text-[15px] text-fg-muted mt-2">
                What you&apos;re working towards. Each goal is checked weekly. Tell the Executive how one is
                going in chat and it updates the goal for you.
              </p>
            </div>
            {goals && total > 0 && !adding && (
              <Button variant="primary" onClick={() => setAdding(true)} className="flex-shrink-0 self-start">
                Add goal
              </Button>
            )}
          </div>

          {!goals && !error && <p className="text-fg-muted text-[15px]">Loading…</p>}
          {error && (
            <div className="p-4 rounded-xl bg-rose-500/10 border border-rose-500/30 text-rose-500 text-[15px] mb-4">
              {error}
            </div>
          )}

          {goals && (
            <div className="space-y-8">
              {(adding || total === 0) && (
                <div className="rounded-2xl border border-line bg-surface-elevated px-5">
                  <AddGoalForm
                    slug={null}
                    first={total === 0}
                    departments={departments}
                    owners={ownerOptions}
                    defaultOwnerId={viewerId}
                    areaLabel={solo ? "Area" : "Department"}
                    onCreated={() => {
                      setAdding(false);
                      reload();
                    }}
                    onCancel={total > 0 ? () => setAdding(false) : undefined}
                  />
                </div>
              )}

              {total > 0 && (
                <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                  <p className="text-[15px] text-fg-muted">
                    {total} goal{total === 1 ? "" : "s"}
                    {summary && <> · {summary}</>}
                  </p>
                  <div role="group" aria-label="Group goals by" className="flex flex-wrap items-center gap-1.5">
                    <span className="text-sm text-fg-subtle mr-1">Group by</span>
                    {groupings.map((g) => (
                      <button
                        key={g}
                        type="button"
                        aria-pressed={by === g}
                        onClick={() => setGrouping(g)}
                        className={`min-h-9 px-3.5 rounded-full border text-sm transition-colors cursor-pointer ${
                          by === g
                            ? "border-accent bg-accent/10 text-fg"
                            : "border-line text-fg-muted hover:text-fg hover:border-line-strong"
                        }`}
                      >
                        {g === "department" && solo ? "Area" : GROUPING_LABEL[g]}
                      </button>
                    ))}
                  </div>
                </div>
              )}

              {groups.map((group) => (
                <section key={group.key}>
                  <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2 mb-3">
                    <h2 className="text-lg font-semibold text-fg">
                      {group.title} <span className="font-normal text-fg-subtle">{group.goals.length}</span>
                    </h2>
                    {by !== "status" && <StatusDots goals={group.goals} />}
                  </div>
                  <div className="rounded-2xl border border-line bg-surface-elevated px-5">
                    {group.goals.map((goal) => (
                      <GoalRow
                        // Re-mount on a move so the edit form starts from the saved owner.
                        key={`${goal.id}:${goal.owner_id}:${goal.department_slug}`}
                        slug={null}
                        goal={goal}
                        meta={metaFor(goal)}
                        owners={ownerOptions}
                        ownerId={goal.owner_id}
                        departments={departments}
                        departmentLabel={solo ? "Area" : "Department"}
                        onSaved={() => reload()}
                        onDeleted={(id) => setGoals((gs) => (gs ?? []).filter((g) => g.id !== id))}
                      />
                    ))}
                  </div>
                </section>
              ))}

              {!solo && departments.length === 0 && total > 0 && (
                <div className="rounded-2xl border border-dashed border-line px-5 py-4 text-[15px] text-fg-muted leading-relaxed">
                  No departments yet, and that&apos;s fine: every goal is checked weekly and shows in your brief.{" "}
                  <Link href="/departments" className="text-accent hover:underline">
                    Add a department
                  </Link>{" "}
                  when you want a lead, its own authority and its own check-in for part of the company. The
                  Departments tab appears once you have one.
                </div>
              )}
            </div>
          )}
        </div>
      </main>
    </div>
  );
}

// One pill per status present in the group, worst first.
function StatusDots({ goals }: { goals: GoalView[] }) {
  const present = (["off_track", "at_risk", "on_track"] as const).filter((s) => goals.some((g) => g.status === s));
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {present.map((s) => (
        <GoalStatusPill key={s} status={s} count={goals.filter((g) => g.status === s).length} />
      ))}
    </div>
  );
}
