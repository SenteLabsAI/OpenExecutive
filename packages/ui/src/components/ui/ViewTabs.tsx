"use client";

import { useRef } from "react";

// A row of tabs that switches views inside one page (not between pages; the
// shell's HubTabs does that). Pill style, 40px tall, with an optional count
// badge per tab. Too many tabs for a phone scroll sideways inside the row,
// never the page. Left/Right arrows move between tabs.

export interface ViewTab<T extends string> {
  id: T;
  label: string;
  /** A count shown after the label; null or undefined shows none. */
  badge?: number | null;
  /** "attention" draws the badge in amber, for things waiting on you. */
  badgeTone?: "muted" | "attention";
}

export default function ViewTabs<T extends string>({
  tabs,
  value,
  onChange,
  label,
  className = "",
}: {
  tabs: ViewTab<T>[];
  value: T;
  onChange: (id: T) => void;
  /** Accessible name of the tab list. */
  label: string;
  className?: string;
}) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);

  const onKeyDown = (e: React.KeyboardEvent, i: number) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    e.preventDefault();
    const next = (i + (e.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    refs.current[next]?.focus();
    onChange(tabs[next].id);
  };

  return (
    <div className={`max-w-full overflow-x-auto ${className}`}>
      <div
        role="tablist"
        aria-label={label}
        className="inline-flex gap-1 rounded-2xl border border-line bg-surface-overlay/70 p-1"
      >
        {tabs.map((tab, i) => {
          const active = tab.id === value;
          return (
            <button
              key={tab.id}
              ref={(el) => {
                refs.current[i] = el;
              }}
              type="button"
              role="tab"
              aria-selected={active}
              tabIndex={active ? 0 : -1}
              onClick={() => onChange(tab.id)}
              onKeyDown={(e) => onKeyDown(e, i)}
              className={`inline-flex h-10 flex-shrink-0 items-center gap-2 whitespace-nowrap rounded-xl px-4 text-[15px] font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60 ${
                active
                  ? "bg-surface-elevated text-fg shadow-sm"
                  : "text-fg-muted hover:text-fg hover:bg-surface-hover"
              }`}
            >
              {tab.label}
              {tab.badge != null && (
                <span
                  className={`rounded-full px-2 py-0.5 text-xs font-semibold tabular-nums ${
                    tab.badgeTone === "attention" && tab.badge > 0
                      ? "bg-amber-500/15 text-amber-500"
                      : "bg-surface-input text-fg-muted"
                  }`}
                >
                  {tab.badge}
                </span>
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}
