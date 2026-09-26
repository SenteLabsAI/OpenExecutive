"use client";

import type { PeriodType } from "@/lib/api";

/**
 * Pair of (period_type dropdown + period_value text input) for editing a Goal's
 * timeframe. When the dropdown changes, the parent receives both new values via
 * `onChange`; the helper `suggestPeriodValue(type, today)` lets callers populate
 * a sensible default for the new type.
 *
 * For period_type="ongoing" the value input is hidden and the value is forced
 * to the literal string "Ongoing" so the stored row is well-formed.
 */

const PERIOD_TYPES: { value: PeriodType; label: string; placeholder: string }[] = [
  { value: "week", label: "Week", placeholder: "Week of May 18" },
  { value: "month", label: "Month", placeholder: "May 2026" },
  { value: "quarter", label: "Quarter", placeholder: "Q2 2026" },
  { value: "year", label: "Year", placeholder: "2026" },
  { value: "ongoing", label: "Ongoing", placeholder: "Ongoing" },
];

const MONTH_NAMES = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

const MONTH_SHORT = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

/**
 * Suggest a sensible period_value string for a given period_type, anchored on
 * `today`. Used by the form when the user changes the dropdown so they're not
 * left with a stale value from a different period scale.
 *
 * Week: "Week of {Mon DD}" — anchored on the most recent Monday.
 * Month: "{MonthName YYYY}"
 * Quarter: "Q{1-4} YYYY"
 * Year: "{YYYY}"
 * Ongoing: "Ongoing"
 */
export function suggestPeriodValue(periodType: PeriodType, today: Date = new Date()): string {
  switch (periodType) {
    case "week": {
      const d = new Date(today);
      // ISO weekday: Mon=1..Sun=7. Date.getDay(): Sun=0..Sat=6.
      const dayOfWeek = d.getDay() === 0 ? 7 : d.getDay();
      d.setDate(d.getDate() - (dayOfWeek - 1));
      return `Week of ${MONTH_SHORT[d.getMonth()]} ${d.getDate()}`;
    }
    case "month":
      return `${MONTH_NAMES[today.getMonth()]} ${today.getFullYear()}`;
    case "quarter": {
      const q = Math.floor(today.getMonth() / 3) + 1;
      return `Q${q} ${today.getFullYear()}`;
    }
    case "year":
      return String(today.getFullYear());
    case "ongoing":
      return "Ongoing";
  }
}

const CHIP_LABELS: Record<PeriodType, string> = {
  week: "This week",
  month: "This month",
  quarter: "This quarter",
  year: "This year",
  ongoing: "Ongoing",
};

/**
 * One-click timeframe for a new goal: each chip picks a period type and the
 * current period's label (`suggestPeriodValue`), so there is nothing to type.
 * Editing an existing goal uses the full picker below, where the label is
 * free text ("H2 FY27").
 */
export function TimeframeChips({
  periodType,
  onChange,
}: {
  periodType: PeriodType;
  onChange: (periodType: PeriodType, periodValue: string) => void;
}) {
  return (
    <div className="flex flex-col gap-1">
      <span className="text-xs text-fg-muted" id="timeframe-chips-label">
        Timeframe
      </span>
      <div role="radiogroup" aria-labelledby="timeframe-chips-label" className="flex flex-wrap gap-1.5">
        {PERIOD_TYPES.map((p) => {
          const selected = p.value === periodType;
          return (
            <button
              key={p.value}
              type="button"
              role="radio"
              aria-checked={selected}
              onClick={() => onChange(p.value, suggestPeriodValue(p.value))}
              title={p.value === "ongoing" ? undefined : suggestPeriodValue(p.value)}
              className={
                "px-2.5 py-1 rounded-full border text-xs transition-colors cursor-pointer focus:outline-none focus:ring-2 focus:ring-indigo-500/50 " +
                (selected
                  ? "border-indigo-500/70 bg-indigo-500/10 text-fg font-medium"
                  : "border-line text-fg-muted hover:text-fg hover:border-line-strong")
              }
            >
              {CHIP_LABELS[p.value]}
            </button>
          );
        })}
      </div>
    </div>
  );
}

interface TimeframePickerProps {
  periodType: PeriodType;
  periodValue: string;
  onChange: (periodType: PeriodType, periodValue: string) => void;
  /** Visual size: compact inputs match the in-list edit row; default matches the new-row form. */
  size?: "compact" | "default";
}

export default function TimeframePicker({
  periodType,
  periodValue,
  onChange,
  size = "default",
}: TimeframePickerProps) {
  const placeholder =
    PERIOD_TYPES.find((p) => p.value === periodType)?.placeholder ?? "";
  const inputCls =
    size === "compact"
      ? "px-2 py-1.5 rounded-lg bg-surface-input border border-line text-sm focus:outline-none focus:border-indigo-500"
      : "px-2 py-1.5 rounded-lg bg-surface-input border border-line text-sm focus:outline-none focus:border-indigo-500";

  return (
    <div className="grid grid-cols-2 gap-2">
      <label className="text-xs text-fg-muted flex flex-col gap-1">
        Timeframe
        <select
          value={periodType}
          onChange={(e) => {
            const next = e.target.value as PeriodType;
            // Auto-fill a sensible default for the new type unless the existing
            // value still makes sense (rare; the type usually implies a format).
            onChange(next, suggestPeriodValue(next));
          }}
          className={inputCls}
        >
          {PERIOD_TYPES.map((p) => (
            <option key={p.value} value={p.value}>{p.label}</option>
          ))}
        </select>
      </label>
      {periodType !== "ongoing" && (
        <label className="text-xs text-fg-muted flex flex-col gap-1">
          Period
          <input
            value={periodValue}
            onChange={(e) => onChange(periodType, e.target.value)}
            className={inputCls}
            placeholder={placeholder}
          />
        </label>
      )}
    </div>
  );
}
