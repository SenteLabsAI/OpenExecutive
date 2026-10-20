"use client";

// A row of two to five buttons, one picked, read as a radio group: Off / In
// training / On on Act as me's jobs, and Who it handles under Handle it for
// me. `compact` drops to a dropdown on a phone when the labels won't fit.
export default function ModePicker<T extends string>({
  options,
  value,
  onPick,
  labelledBy,
  disabled = false,
  compact = false,
}: {
  options: { value: T; label: string; disabled?: boolean }[];
  value: T;
  onPick: (next: T) => void;
  labelledBy: string;
  disabled?: boolean;
  compact?: boolean;
}) {
  const cols = ["", "grid-cols-1", "grid-cols-2", "grid-cols-3", "grid-cols-4", "grid-cols-5"][options.length] ?? "";
  return (
    <>
      {compact && (
        <select
          aria-labelledby={labelledBy}
          value={value}
          disabled={disabled}
          onChange={(e) => onPick(e.target.value as T)}
          className="min-h-touch w-full rounded-xl border border-line bg-surface px-3 text-[15px] font-semibold text-fg sm:hidden disabled:opacity-60"
        >
          {options.map((o) => (
            <option key={o.value} value={o.value} disabled={o.disabled}>
              {o.label}
            </option>
          ))}
        </select>
      )}
      <div
        role="radiogroup"
        aria-labelledby={labelledBy}
        className={`${compact ? "hidden sm:grid" : "grid"} ${cols} overflow-hidden rounded-xl border border-line`}
      >
        {options.map((o, i) => {
          const picked = o.value === value;
          return (
            <button
              key={o.value}
              type="button"
              role="radio"
              aria-checked={picked}
              disabled={disabled || o.disabled}
              onClick={() => {
                if (!picked) onPick(o.value);
              }}
              className={`min-h-touch px-2 py-2.5 text-[15px] font-semibold transition-colors cursor-pointer disabled:cursor-not-allowed disabled:opacity-60 ${
                i > 0 ? "border-l border-line " : ""
              }${picked ? "bg-accent-strong text-white" : "text-fg hover:bg-surface-overlay/60"}`}
            >
              {o.label}
            </button>
          );
        })}
      </div>
    </>
  );
}
