"use client";

import { useRef } from "react";

// A row of tabs inside one page or panel (a person's page, a settings form
// split into short sections), as opposed to HubTabs, which links pages. The
// caller renders the active tab's content and spreads
// `sectionPanelProps(idBase, active)` on it, with the same `idBase` (from
// useId) it passed here. Arrow keys, Home and End move between tabs.

export interface SectionTab<T extends string> {
  id: T;
  label: string;
  /** Shown after the label, e.g. how many rows the tab lists. */
  count?: number;
}

export default function SectionTabs<T extends string>({
  tabs,
  active,
  onChange,
  label,
  idBase,
  disabled = false,
}: {
  tabs: SectionTab<T>[];
  active: T;
  onChange: (id: T) => void;
  /** Accessible name of the tab row. */
  label: string;
  /** Shared with the content's `sectionPanelProps`, so the two point at each other. */
  idBase: string;
  disabled?: boolean;
}) {
  const base = idBase;
  const refs = useRef<(HTMLButtonElement | null)[]>([]);

  const onKeyDown = (e: React.KeyboardEvent, i: number) => {
    const last = tabs.length - 1;
    const next =
      e.key === "ArrowRight" ? (i === last ? 0 : i + 1)
      : e.key === "ArrowLeft" ? (i === 0 ? last : i - 1)
      : e.key === "Home" ? 0
      : e.key === "End" ? last
      : -1;
    if (next < 0) return;
    e.preventDefault();
    refs.current[next]?.focus();
    onChange(tabs[next].id);
  };

  return (
    <div
      role="tablist"
      aria-label={label}
      className="inline-flex max-w-full gap-1 overflow-x-auto rounded-2xl bg-surface-overlay p-1"
    >
      {tabs.map((tab, i) => {
        const selected = tab.id === active;
        return (
          <button
            key={tab.id}
            ref={(el) => {
              refs.current[i] = el;
            }}
            type="button"
            role="tab"
            id={`${base}-tab-${tab.id}`}
            aria-selected={selected}
            aria-controls={`${base}-panel-${tab.id}`}
            tabIndex={selected ? 0 : -1}
            disabled={disabled}
            onClick={() => onChange(tab.id)}
            onKeyDown={(e) => onKeyDown(e, i)}
            className={`flex h-10 flex-shrink-0 items-center gap-2 rounded-xl px-4 text-[15px] font-medium whitespace-nowrap transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60 disabled:opacity-50 ${
              selected
                ? "bg-surface-elevated text-fg shadow-sm"
                : "text-fg-muted hover:text-fg"
            }`}
          >
            {tab.label}
            {tab.count !== undefined && (
              <span className={`text-sm ${selected ? "text-fg-muted" : "text-fg-subtle"}`}>{tab.count}</span>
            )}
          </button>
        );
      })}
    </div>
  );
}

/** The attributes for the active tab's content (its tab's `aria-controls`). */
export function sectionPanelProps(idBase: string, id: string) {
  return { role: "tabpanel", id: `${idBase}-panel-${id}`, "aria-labelledby": `${idBase}-tab-${id}` };
}
