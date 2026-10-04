"use client";

// An on/off switch. The caller labels it with `labelledBy`, the id of the
// text that says what it switches, so a screen reader reads that text.
// 44 by 24, with a taller invisible tap area (44px) and a solid gray track
// when off, so Off reads as a choice rather than a missing control.
export default function Switch({
  checked,
  onChange,
  disabled = false,
  labelledBy,
}: {
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
  labelledBy: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-labelledby={labelledBy}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={`relative inline-flex h-6 w-11 flex-shrink-0 items-center rounded-full transition-colors cursor-pointer before:absolute before:inset-x-0 before:-inset-y-2.5 before:content-[''] disabled:opacity-50 disabled:cursor-not-allowed ${
        checked ? "bg-indigo-500" : "bg-zinc-300 dark:bg-zinc-600"
      }`}
    >
      <span
        aria-hidden="true"
        className={`inline-block h-5 w-5 rounded-full bg-white shadow transition-transform ${
          checked ? "translate-x-[22px]" : "translate-x-0.5"
        }`}
      />
    </button>
  );
}
