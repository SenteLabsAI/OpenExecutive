"use client";

import { type OnboardGoalDraft, type OnboardPersonDraft } from "@/lib/api";

const FIELD =
  "rounded-lg border border-line-strong bg-surface-overlay px-3 py-2 text-sm text-fg placeholder-fg-subtle focus:outline-none focus:ring-2 focus:ring-indigo-500/50 transition-colors";

interface Props {
  goals: OnboardGoalDraft[];
  /** Who can own a goal. Empty in Personal: every goal is yours. */
  people: OnboardPersonDraft[];
  /** Drafted department titles. Empty: no department picker. */
  departments: string[];
  onChange: (goals: OnboardGoalDraft[]) => void;
}

// "This year" on the review screen: each goal with its owner (and its
// department, only when setup drafted departments). Saved as goals, which
// are checked every week, and as this year's priorities on the profile.
export default function OnboardGoalsDraft({ goals, people, departments, onChange }: Props) {
  const named = people.filter((p) => p.full_name.trim());

  function update(i: number, patch: Partial<OnboardGoalDraft>) {
    onChange(goals.map((g, j) => (j === i ? { ...g, ...patch } : g)));
  }

  return (
    <div className="bg-surface-elevated border border-line rounded-xl p-5">
      <div className="flex items-baseline justify-between mb-1">
        <h2 className="text-sm font-semibold text-fg">This year</h2>
        <button
          type="button"
          onClick={() => onChange([...goals, { title: "", target: "", owner_name: "", department_title: "" }])}
          className="text-xs text-indigo-400 hover:text-indigo-300 transition-colors"
        >
          Add a goal
        </button>
      </div>
      <p className="text-xs text-fg-muted mb-4">
        Your goals. Each one is checked weekly, and any at risk shows in your brief.
      </p>

      {goals.length === 0 && (
        <p className="text-sm text-fg-subtle italic">No goals yet. Add one, or add them later on the Goals page.</p>
      )}

      <div className="flex flex-col gap-3">
        {goals.map((g, i) => (
          <div key={i} className="flex flex-col gap-2 pb-3 border-b border-line last:border-0 last:pb-0">
            <div className="flex items-center gap-2">
              <input
                value={g.title}
                onChange={(e) => update(i, { title: e.target.value })}
                placeholder="e.g. Launch the hosted version"
                aria-label="Goal"
                className={`flex-1 min-w-0 ${FIELD}`}
              />
              <button
                type="button"
                onClick={() => onChange(goals.filter((_, j) => j !== i))}
                aria-label={`Remove ${g.title || "goal"}`}
                className="text-xs text-fg-subtle hover:text-red-400 px-1 transition-colors"
              >
                ✕
              </button>
            </div>
            {(named.length > 1 || departments.length > 0) && (
              <div className="flex flex-col sm:flex-row gap-2">
                {named.length > 1 && (
                  <select
                    value={g.owner_name}
                    onChange={(e) => update(i, { owner_name: e.target.value })}
                    aria-label="Owner"
                    className={`flex-1 ${FIELD}`}
                  >
                    {/* Blank means the person marked "This is me". */}
                    <option value="">Owner: you</option>
                    {named
                      .filter((p) => !p.is_principal)
                      .map((p) => (
                        <option key={p.full_name} value={p.full_name}>
                          Owner: {p.full_name}
                        </option>
                      ))}
                  </select>
                )}
                {departments.length > 0 && (
                  <select
                    value={g.department_title}
                    onChange={(e) => update(i, { department_title: e.target.value })}
                    aria-label="Department"
                    className={`flex-1 ${FIELD}`}
                  >
                    <option value="">No department</option>
                    {departments.map((d) => (
                      <option key={d} value={d}>
                        {d}
                      </option>
                    ))}
                  </select>
                )}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
