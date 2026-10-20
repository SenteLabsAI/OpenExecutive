"use client";

// The "on its own next time" tick on a card in training (a reply, a
// suggested action, a Take the lead card): ticked, the card's Send or
// Approve also lets the same kind, with the same people, go on its own from
// then on. It replaced the separate "+ allow" buttons.
export default function NextTimeTick({
  checked,
  onChange,
  label,
  help,
  disabled = false,
  className = "",
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  label: string;
  help: string;
  disabled?: boolean;
  className?: string;
}) {
  return (
    <label
      className={`flex cursor-pointer items-start gap-3 rounded-xl border border-line bg-surface px-3 py-2.5 ${className}`}
    >
      <input
        type="checkbox"
        className="mt-1 h-4 w-4 shrink-0 accent-[rgb(var(--accent-strong))]"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span className="min-w-0">
        <span className="block text-[15px] font-medium text-fg">{label}</span>
        <span className="mt-0.5 block text-[13px] leading-snug text-fg-muted">{help}</span>
      </span>
    </label>
  );
}
