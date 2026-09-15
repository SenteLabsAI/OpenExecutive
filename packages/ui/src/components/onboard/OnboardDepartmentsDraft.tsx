"use client";

import { type OnboardDepartmentDraft, type OnboardPersonDraft } from "@/lib/api";

const AUTHORITY_LEVELS = [
  { value: "propose_only", label: "Proposes, you approve" },
  { value: "escalate", label: "Escalates to you" },
  { value: "auto_execute", label: "Acts on its own" },
];

interface Props {
  departments: OnboardDepartmentDraft[];
  people: OnboardPersonDraft[];
  /** Titles of departments that already exist, so each row can say whether it
   * updates one or creates a new one. Nothing is ever deleted. */
  existingTitles: string[];
  onChange: (departments: OnboardDepartmentDraft[]) => void;
}

export default function OnboardDepartmentsDraft({
  departments,
  people,
  existingTitles,
  onChange,
}: Props) {
  const existing = new Set(existingTitles.map((t) => t.trim().toLowerCase()));

  function update(i: number, patch: Partial<OnboardDepartmentDraft>) {
    onChange(departments.map((d, j) => (j === i ? { ...d, ...patch } : d)));
  }

  return (
    <div className="bg-surface-elevated border border-line rounded-xl p-5">
      <div className="flex items-baseline justify-between mb-1">
        <h2 className="text-sm font-semibold text-fg">Departments</h2>
        <button
          onClick={() =>
            onChange([
              ...departments,
              {
                title: "",
                mission: "",
                head_person_name: "",
                authority_level: "propose_only",
              },
            ])
          }
          className="text-xs text-indigo-400 hover:text-indigo-300 transition-colors"
        >
          Add department
        </button>
      </div>
      <p className="text-xs text-fg-muted mb-4">
        Saving updates the departments listed here and adds any that are new.
        Departments you don&rsquo;t list are left exactly as they are.
      </p>

      {departments.length === 0 && (
        <p className="text-sm text-fg-subtle italic">No departments drafted.</p>
      )}

      <div className="flex flex-col gap-3">
        {departments.map((d, i) => {
          const isExisting = existing.has(d.title.trim().toLowerCase());
          return (
            <div key={i} className="flex flex-col gap-2 pb-3 border-b border-line last:border-0 last:pb-0">
              <div className="flex items-center gap-2">
                <input
                  value={d.title}
                  onChange={(e) => update(i, { title: e.target.value })}
                  placeholder="Department"
                  className="flex-1 rounded-lg border border-line-strong bg-surface-overlay px-3 py-2 text-sm text-fg placeholder-fg-subtle focus:outline-none focus:ring-2 focus:ring-indigo-500/50 transition-colors"
                />
                <span
                  className={`text-[10px] uppercase tracking-wide px-2 py-1 rounded-md whitespace-nowrap ${
                    isExisting
                      ? "bg-surface-overlay text-fg-muted"
                      : "bg-indigo-500/10 text-indigo-400"
                  }`}
                >
                  {d.title.trim() ? (isExisting ? "Updates existing" : "New") : "—"}
                </span>
                <button
                  onClick={() => onChange(departments.filter((_, j) => j !== i))}
                  aria-label={`Remove ${d.title || "department"}`}
                  className="text-xs text-fg-subtle hover:text-red-400 px-1 transition-colors"
                >
                  ✕
                </button>
              </div>
              <input
                value={d.mission}
                onChange={(e) => update(i, { mission: e.target.value })}
                placeholder="What this function owns"
                className="w-full rounded-lg border border-line-strong bg-surface-overlay px-3 py-2 text-sm text-fg placeholder-fg-subtle focus:outline-none focus:ring-2 focus:ring-indigo-500/50 transition-colors"
              />
              <div className="flex items-center gap-2">
                <select
                  value={d.head_person_name}
                  onChange={(e) => update(i, { head_person_name: e.target.value })}
                  className="flex-1 rounded-lg border border-line-strong bg-surface-overlay px-3 py-2 text-sm text-fg focus:outline-none focus:ring-2 focus:ring-indigo-500/50 transition-colors"
                >
                  <option value="">No head assigned</option>
                  {people
                    .filter((p) => p.full_name.trim())
                    .map((p) => (
                      <option key={p.full_name} value={p.full_name}>
                        {p.full_name}
                      </option>
                    ))}
                </select>
                <select
                  value={d.authority_level}
                  onChange={(e) => update(i, { authority_level: e.target.value })}
                  className="flex-1 rounded-lg border border-line-strong bg-surface-overlay px-3 py-2 text-sm text-fg focus:outline-none focus:ring-2 focus:ring-indigo-500/50 transition-colors"
                >
                  {AUTHORITY_LEVELS.map((a) => (
                    <option key={a.value} value={a.value}>
                      {a.label}
                    </option>
                  ))}
                </select>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
