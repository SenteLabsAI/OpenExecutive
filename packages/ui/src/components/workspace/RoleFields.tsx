"use client";

import { ROLE_KIND_OPTIONS, ROLE_TEXT_MAX, type RoleForm } from "@/lib/principalRole";

// The principal's role as form fields — the onboarding role step and
// Settings → Workspace share it. Controlled: the caller owns the RoleForm and
// saves it (roleUpdate → PUT /workspace).

const INPUT =
  "w-full rounded-lg border border-line-strong bg-surface-overlay px-3 py-2 text-sm text-fg placeholder-fg-subtle focus:outline-none focus:ring-2 focus:ring-indigo-500/50 transition-colors disabled:opacity-60";

export default function RoleFields({
  value,
  onChange,
  disabled = false,
  idPrefix = "role",
}: {
  value: RoleForm;
  onChange: (next: RoleForm) => void;
  disabled?: boolean;
  idPrefix?: string;
}) {
  const set = <K extends keyof RoleForm>(key: K, v: RoleForm[K]) => onChange({ ...value, [key]: v });

  return (
    <div className="space-y-4">
      <fieldset>
        <legend className="text-xs font-medium text-fg mb-1.5">Which describes you best?</legend>
        <div role="radiogroup" className="grid gap-2 sm:grid-cols-2">
          {ROLE_KIND_OPTIONS.map((o) => {
            const selected = value.role_kind === o.kind;
            return (
              <button
                key={o.kind}
                type="button"
                role="radio"
                aria-checked={selected}
                disabled={disabled}
                // Clicking the selected kind again clears it.
                onClick={() => set("role_kind", selected ? null : o.kind)}
                className={`text-left rounded-lg border px-3 py-2 transition-colors cursor-pointer disabled:cursor-not-allowed disabled:opacity-60 focus:outline-none focus:ring-2 focus:ring-indigo-500/50 ${
                  selected
                    ? "border-indigo-500/70 bg-indigo-500/10"
                    : "border-line bg-surface hover:border-line-strong"
                }`}
              >
                <span className="block text-xs font-medium text-fg">{o.label}</span>
                <span className="block text-[11px] text-fg-muted mt-0.5 leading-snug">{o.hint}</span>
              </button>
            );
          })}
        </div>
      </fieldset>

      <div className="grid gap-3 sm:grid-cols-2">
        <div>
          <label htmlFor={`${idPrefix}-title`} className="block text-xs font-medium text-fg mb-1">
            Your title
          </label>
          <input
            id={`${idPrefix}-title`}
            value={value.role_title}
            onChange={(e) => set("role_title", e.target.value)}
            maxLength={ROLE_TEXT_MAX.role_title}
            placeholder="e.g. Director of Operations"
            disabled={disabled}
            className={INPUT}
          />
        </div>
        <div>
          <label htmlFor={`${idPrefix}-reports-to`} className="block text-xs font-medium text-fg mb-1">
            Who you report to <span className="text-fg-subtle font-normal">(optional)</span>
          </label>
          <input
            id={`${idPrefix}-reports-to`}
            value={value.reports_to}
            onChange={(e) => set("reports_to", e.target.value)}
            maxLength={ROLE_TEXT_MAX.reports_to}
            placeholder="e.g. VP of Operations, Dana Ruiz"
            disabled={disabled}
            className={INPUT}
          />
        </div>
      </div>

      <div>
        <label htmlFor={`${idPrefix}-remit`} className="block text-xs font-medium text-fg mb-1">
          What you&apos;re responsible for
        </label>
        <textarea
          id={`${idPrefix}-remit`}
          value={value.remit}
          onChange={(e) => set("remit", e.target.value)}
          maxLength={ROLE_TEXT_MAX.remit}
          rows={2}
          placeholder="e.g. Our three warehouses, carrier contracts and last-mile delivery"
          disabled={disabled}
          className={`${INPUT} resize-y`}
        />
      </div>

      <div>
        <label htmlFor={`${idPrefix}-measured-on`} className="block text-xs font-medium text-fg mb-1">
          What you&apos;re measured on <span className="text-fg-subtle font-normal">(optional)</span>
        </label>
        <input
          id={`${idPrefix}-measured-on`}
          value={value.measured_on}
          onChange={(e) => set("measured_on", e.target.value)}
          maxLength={ROLE_TEXT_MAX.measured_on}
          placeholder="e.g. On-time delivery rate and cost per shipment"
          disabled={disabled}
          className={INPUT}
        />
      </div>
    </div>
  );
}
