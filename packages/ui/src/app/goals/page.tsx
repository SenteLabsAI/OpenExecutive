"use client";

import { useEffect, useMemo, useState } from "react";

import { AddGoalForm, GOAL_STATUS_COLORS, GoalRow } from "@/components/goals/GoalEditor";
import { listDepartments, type DepartmentState } from "@/lib/api";
import { applyGoalChange, groupGoalsByArea } from "@/lib/goalAreas";

// Every goal in one place, grouped by area. An area is a department row under
// the hood (solo mode calls it an area and shows this page instead of
// Departments); goals are added, edited and deleted through the same
// slug-scoped goal routes a department's own page uses. Reachable in team mode
// too — it is simply not in the team nav.

const STATUS_LABEL: Record<string, string> = {
  on_track: "on track",
  at_risk: "at risk",
  off_track: "off track",
};

export default function GoalsPage() {
  const [departments, setDepartments] = useState<DepartmentState[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  // The area the add form opens on, or null while it is closed.
  const [addingTo, setAddingTo] = useState<string | null>(null);

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
    .map((s) => `${grouped.counts[s]} ${STATUS_LABEL[s]}`)
    .join(" · ");

  // Open the form on the first area that already has goals, else the first area.
  const defaultArea = grouped.withGoals[0]?.slug ?? grouped.all[0]?.slug ?? null;

  return (
    <div className="flex flex-col h-full bg-surface">
      <main className="flex-1 overflow-y-auto">
        <div className="max-w-3xl mx-auto px-4 sm:px-6 py-6">
          <div className="flex items-center justify-between gap-3 mb-1">
            <h1 className="text-xl font-semibold text-fg">Goals</h1>
            {departments && grouped.all.length > 0 && addingTo === null && (
              <button
                type="button"
                onClick={() => setAddingTo(defaultArea)}
                className="px-3 py-1.5 text-xs rounded-lg bg-indigo-600/20 hover:bg-indigo-600/30 text-indigo-300 border border-indigo-500/30 cursor-pointer"
              >
                + Add goal
              </button>
            )}
          </div>
          <p className="text-sm text-fg-muted mb-6">
            What you&apos;re working towards, grouped by area. Tell the Executive how a goal is
            going in chat and it updates the goal for you.
          </p>

          {!departments && !error && <p className="text-fg-muted text-sm">Loading…</p>}
          {error && (
            <div className="p-3 rounded-lg bg-rose-500/10 border border-rose-500/30 text-rose-300 text-sm mb-4">
              {error}
            </div>
          )}

          {departments && (
            <div className="space-y-6">
              {addingTo !== null && (
                <div className="rounded-xl border border-line bg-surface-elevated px-4">
                  <AddGoalForm
                    key={addingTo}
                    slug={addingTo}
                    areas={grouped.all}
                    onCreated={(goal) => {
                      setDepartments((ds) => applyGoalChange(ds ?? [], { saved: goal }));
                      setAddingTo(null);
                    }}
                    onCancel={() => setAddingTo(null)}
                  />
                </div>
              )}

              {grouped.all.length === 0 ? (
                <p className="text-sm text-fg-muted">
                  There are no areas to put goals in yet. Areas come with your business
                  profile — finish setup first.
                </p>
              ) : total === 0 && addingTo === null ? (
                <div className="rounded-xl border border-line bg-surface-elevated p-6 text-center">
                  <p className="text-sm text-fg">No goals yet.</p>
                  <p className="text-xs text-fg-muted mt-1">
                    Add the first thing you&apos;re working towards, and pick the area it
                    belongs to.
                  </p>
                  <button
                    type="button"
                    onClick={() => setAddingTo(defaultArea)}
                    className="mt-3 text-xs text-indigo-400 hover:text-indigo-300 cursor-pointer"
                  >
                    Add a goal →
                  </button>
                </div>
              ) : (
                total > 0 && (
                  <p className="text-xs text-fg-muted">
                    {total} goal{total === 1 ? "" : "s"}
                    {summary && <> · {summary}</>}
                  </p>
                )
              )}

              {grouped.withGoals.map((area) => (
                <section key={area.slug}>
                  <div className="flex items-center justify-between gap-3 mb-2">
                    <h2 className="text-xs font-semibold uppercase tracking-wide text-fg-muted">
                      {area.title}{" "}
                      <span className="font-normal normal-case tracking-normal">
                        ({area.goals.length})
                      </span>
                    </h2>
                    <StatusDots goals={area.goals} />
                  </div>
                  <div className="rounded-xl border border-line bg-surface-elevated px-4">
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
                  <h2 className="text-xs font-semibold uppercase tracking-wide text-fg-muted mb-2">
                    Other areas
                  </h2>
                  <div className="flex flex-wrap gap-2">
                    {grouped.empty.map((a) => (
                      <button
                        key={a.slug}
                        type="button"
                        onClick={() => setAddingTo(a.slug)}
                        title={`Add a goal to ${a.title}`}
                        className="px-2.5 py-1 rounded-full border border-line text-xs text-fg-muted hover:text-fg hover:border-line-strong transition-colors cursor-pointer"
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

// One small pill per status present in the area, worst first.
function StatusDots({ goals }: { goals: DepartmentState["goals"] }) {
  const present = (["off_track", "at_risk", "on_track"] as const).filter((s) =>
    goals.some((g) => g.status === s),
  );
  return (
    <div className="flex items-center gap-1.5 flex-shrink-0">
      {present.map((s) => {
        const n = goals.filter((g) => g.status === s).length;
        return (
          <span
            key={s}
            className={`inline-block px-1.5 py-0.5 rounded border text-[10px] font-medium ${GOAL_STATUS_COLORS[s]}`}
          >
            {n} {STATUS_LABEL[s]}
          </span>
        );
      })}
    </div>
  );
}
