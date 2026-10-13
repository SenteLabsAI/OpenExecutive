"use client";

import { useState } from "react";

import type { HeldBackItem } from "@/lib/api";

// "Left out of your brief": the lines the grounding check held back from the
// morning brief or end-of-day digest because they named people or figures it
// couldn't match to your data. The brief's own note sends people here. Hide
// is remembered in this browser only; the block also ages out after a day.

const HIDDEN_KEY = "oe.heldBack.hidden";

function readHidden(): number[] {
  try {
    const raw = window.localStorage.getItem(HIDDEN_KEY);
    const ids = raw ? (JSON.parse(raw) as unknown) : [];
    return Array.isArray(ids) ? ids.filter((n): n is number => typeof n === "number") : [];
  } catch {
    return [];
  }
}

function writeHidden(ids: number[]): void {
  try {
    window.localStorage.setItem(HIDDEN_KEY, JSON.stringify(ids.slice(-20)));
  } catch {
    /* storage unavailable: Hide lasts until the page reloads */
  }
}

function whenLabel(item: HeldBackItem): string {
  const day = new Date(item.at).toLocaleDateString(undefined, { weekday: "long" });
  return item.surface === "end-of-day digest" ? `${day}'s end-of-day digest` : `${day}'s morning brief`;
}

export default function HeldBack({ items }: { items: HeldBackItem[] }) {
  const [hidden, setHidden] = useState<number[]>(() => (typeof window === "undefined" ? [] : readHidden()));
  const shown = items.filter((i) => !hidden.includes(i.event_id));
  if (shown.length === 0) return null;
  const hide = () => {
    const next = [...hidden, ...shown.map((i) => i.event_id)];
    setHidden(next);
    writeHidden(next);
  };
  return (
    <section aria-labelledby="held-back-title" className="mb-8 rounded-2xl border border-line bg-surface-overlay/40 p-4">
      <div className="mb-2 flex items-center gap-2">
        <h2 id="held-back-title" className="text-base font-semibold text-fg">
          Left out of your brief
        </h2>
        <button
          type="button"
          onClick={hide}
          className="ml-auto rounded-lg px-2 py-1 text-sm text-fg-muted hover:bg-surface-hover hover:text-fg"
        >
          Hide
        </button>
      </div>
      <p className="mb-3 text-sm text-fg-muted">
        These lines named people or figures I couldn&apos;t match to your data, so I didn&apos;t send them.
      </p>
      <div className="space-y-3">
        {shown.map((item) => (
          <div key={item.event_id}>
            <div className="mb-1 text-xs font-medium uppercase tracking-wide text-fg-subtle">{whenLabel(item)}</div>
            <ul className="space-y-1 text-sm text-fg">
              {item.lines.map((line, i) => (
                <li key={i} className="whitespace-pre-wrap break-words">
                  {line.replace(/^\s*[-*•]\s+/, "")}
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </section>
  );
}
