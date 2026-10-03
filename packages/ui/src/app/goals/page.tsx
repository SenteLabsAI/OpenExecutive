"use client";

import { useEffect, useMemo, useState } from "react";

import { AddGoalForm, GoalRow, GoalStatusPill, goalStatusLabel } from "@/components/goals/GoalEditor";
import Button from "@/components/ui/Button";
import { useWorkspace } from "@/components/workspace/WorkspaceContext";
import { listDepartments, type DepartmentState } from "@/lib/api";
import { applyGoalChange, groupGoalsByArea } from "@/lib/goalAreas";

// Every goal in one place, grouped by area. An area is a department row under
// the hood (solo mode calls it an area and shows this page instead of
// Departments); goals are added, edited and deleted through the same
// slug-scoped goal routes a department's own page uses. Team mode links it
// too, and the copy says "department" there.

export default function GoalsPage() {
  const [departments, setDepartments] = useState<DepartmentState[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  // The area the add form opens on, or null while it is closed.
  const [addingTo, setAddingTo] = useState<string | null>(null);
  const { mode } = useWorkspace();
  const unit = mode === "solo" ? "area" : "department";

  useEffect(() => {
    let cancelled = false;
    listDepartments()
      .then((ds) => {
        if (!cancelled) setDepartments(ds);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : "Failed to load goals");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const grouped = useMemo(() => groupGoalsByArea(departments ?? []), [departments]);
  const total = grouped.withGoals.reduce((n, a) => n + a.goals.length, 0);
  const summary = (["off_track", "at_risk", "on_track"] as const)
    .filter((s) => grouped.counts[s] > 0)
    .map((s) => `${grouped.counts[s]} ${goalStatusLabel(s)}`)
    .join(" · ");

  // Open the form on the first area that already has goals, else the first area.
  const defaultArea = grouped.withGoals[0]?.slug ?? grouped.all[0]?.slug ?? null;

  return (
    <div className="flex flex-col h-full bg-surface">
      <main className="flex-1 overflow-y-auto">
        <div className="max-w-3xl mx-auto px-4 sm:px-6 py-8">
          <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between mb-8">
            <div className="min-w-0">
              <h1 className="text-2xl sm:text-3xl font-bold tracking-tight text-fg">Goals</h1>
              <p className="text-[15px] text-fg-muted mt-2">
                What you&apos;re working towards, grouped by {unit}. Tell the Executive how a goal is
                going in chat and it updates the goal for you.
              </p>
            </div>
            {departments && grouped.all.length > 0 && addingTo === null && (
              <Button variant="primary" onClick={() => setAddingTo(defaultArea)} className="flex-shrink-0 self-start">
                Add goal
              </Button>
            )}
          </div>

          {!departments && !error && <p className="text-fg-muted text-[15px]">Loading…</p>}
          {error && (
            <div className="p-4 rounded-xl bg-rose-500/10 border border-rose-500/30 text-rose-500 text-[15px] mb-4">
              {error}
            </div>
          )}

          {departments && (
            <div className="space-y-8">
              {addingTo !== null && (
                <div className="rounded-2xl border border-line bg-surface-elevated px-5">
                  <AddGoalForm
                    key={addingTo}
                    slug={addingTo}
                    areas={grouped.all}
                    areaLabel={unit === "area" ? "Area" : "Department"}
                    onCreated={(goal) => {
                      setDepartments((ds) => applyGoalChange(ds ?? [], { saved: goal }));
                      setAddingTo(null);
                    }}
                    onCancel={() => setAddingTo(null)}
                  />
                </div>
              )}

              {grouped.all.length === 0 ? (
                <p className="text-[15px] text-fg-muted">
                  {mode === "solo"
                    ? "There are no areas to put goals in yet. Areas come with your profile — finish setup first."
                    : "There are no departments to put goals in yet. Add one on the Departments page first."}
                </p>
              ) : total === 0 && addingTo === null ? (
                <div className="rounded-2xl border border-line bg-surface-elevated p-8 text-center">
                  <p className="text-base font-medium text-fg">No goals yet.</p>
                  <p className="text-[15px] text-fg-muted mt-1">
                    Add the first thing you&apos;re working towards — just type it; the
                    timeframe defaults to this quarter.
                  </p>
                  <Button variant="primary" onClick={() => setAddingTo(defaultArea)} className="mt-5">
                    Add a goal
                  </Button>
                </div>
              ) : (
                total > 0 && (
                  <p className="text-[15px] text-fg-muted">
                    {total} goal{total === 1 ? "" : "s"}
                    {summary && <> · {summary}</>}
                  </p>
                )
              )}

              {grouped.withGoals.map((area) => (
                <section key={area.slug}>
                  <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2 mb-3">
                    <h2 className="text-lg font-semibold text-fg">
                      {area.title}{" "}
                      <span className="font-normal text-fg-subtle">{area.goals.length}</span>
                    </h2>
                    <StatusDots goals={area.goals} />
                  </div>
                  <div className="rounded-2xl border border-line bg-surface-elevated px-5">
                    {area.goals.map((goal) => (
                      <GoalRow
                        key={goal.id}
                        slug={area.slug}
                        goal={goal}
                        onSaved={(updated) =>
                          setDepartments((ds) => applyGoalChange(ds ?? [], { saved: updated }))
                        }
                        onDeleted={(id) =>
                          setDepartments((ds) =>
                            applyGoalChange(ds ?? [], { deleted: { slug: area.slug, id } }),
                          )
                        }
                      />
                    ))}
                  </div>
                </section>
              ))}

              {grouped.empty.length > 0 && total > 0 && (
                <section>
                  <h2 className="text-lg font-semibold text-fg mb-1">Other {unit}s</h2>
                  <p className="text-[15px] text-fg-muted mb-3">No goals yet. Pick one to add its first goal.</p>
                  <div className="flex flex-wrap gap-2">
                    {grouped.empty.map((a) => (
                      <button
                        key={a.slug}
                        type="button"
                        onClick={() => setAddingTo(a.slug)}
                        title={`Add a goal to ${a.title}`}
                        className="h-10 px-4 rounded-full border border-line bg-surface-elevated text-[15px] text-fg-muted hover:text-fg hover:border-line-strong transition-colors cursor-pointer"
                      >
                        + {a.title}
                      </button>
                    ))}
                  </div>
                </section>
              )}
            </div>
          )}
        </div>
      </main>
    </div>
  );
}

// One pill per status present in the area, worst first.
function StatusDots({ goals }: { goals: DepartmentState["goals"] }) {
  const present = (["off_track", "at_risk", "on_track"] as const).filter((s) =>
    goals.some((g) => g.status === s),
  );
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {present.map((s) => (
        <GoalStatusPill key={s} status={s} count={goals.filter((g) => g.status === s).length} />
      ))}
    </div>
  );
}
