"use client";

import { useEffect, useState } from "react";

import { getWhatIDid, type WhatIDidItem } from "@/lib/api";
import { formatAgo } from "@/lib/setupStatus";

// What I did on Today (GET /take-the-lead/done): everything done on its own
// this week, tagged As you (replies and follow-ups from your mailbox) or As
// the Executive (Take the lead, the owner's alone). Each shows why.
export function useWhatIDid(): WhatIDidItem[] {
  const [items, setItems] = useState<WhatIDidItem[]>([]);
  useEffect(() => {
    const controller = new AbortController();
    getWhatIDid(controller.signal)
      .then(setItems)
      .catch(() => setItems([]));
    return () => controller.abort();
  }, []);
  return items;
}

const STATUS_TEXT: Record<WhatIDidItem["status"], string> = {
  done: "",
  waiting: "Waiting for a yes",
  approved: "Done after a yes",
  declined: "Declined, not done",
  failed: "Didn't work",
};

type Filter = "all" | "you" | "executive";

export function WhatIDidList({ items }: { items: WhatIDidItem[] }) {
  const [filter, setFilter] = useState<Filter>("all");
  const [open, setOpen] = useState<number | null>(null);
  const shown = items.filter((i) => filter === "all" || i.actor === filter);
  const filters: [Filter, string][] = [
    ["all", "All"],
    ["you", "As you"],
    ["executive", "As the Executive"],
  ];

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap gap-2" role="group" aria-label="Show">
        {filters.map(([key, label]) => (
          <button
            key={key}
            type="button"
            aria-pressed={filter === key}
            onClick={() => setFilter(key)}
            className={`min-h-touch rounded-full border px-4 text-sm font-semibold ${
              filter === key ? "border-fg bg-fg text-surface" : "border-line text-fg hover:border-line-strong"
            }`}
          >
            {label}
          </button>
        ))}
      </div>
      {shown.length === 0 && <p className="text-[15px] text-fg-muted">Nothing here this week.</p>}
      <ul className="flex flex-col gap-3">
        {shown.map((item, index) => {
          const you = item.actor === "you";
          const expanded = open === index;
          return (
            <li key={`${item.at}-${index}`} className="rounded-2xl border border-line bg-surface-elevated p-4">
              <button
                type="button"
                aria-expanded={expanded}
                onClick={() => setOpen(expanded ? null : index)}
                className="flex w-full flex-col gap-1.5 text-left"
              >
                <span className="flex w-full items-center gap-2">
                  <span
                    className={`rounded-full px-2.5 py-0.5 text-xs font-semibold ${
                      you ? "bg-accent/10 text-accent" : "bg-surface-overlay text-fg"
                    }`}
                  >
                    {you ? "As you" : "As the Executive"}
                  </span>
                  {STATUS_TEXT[item.status] && (
                    <span className="text-xs font-medium text-fg-muted">{STATUS_TEXT[item.status]}</span>
                  )}
                  <span className="ml-auto text-xs text-fg-muted">{formatAgo(item.at)}</span>
                </span>
                <span className="text-[15px] font-semibold leading-snug text-fg">{item.title}</span>
                {item.detail && <span className="text-sm leading-snug text-fg-muted">{item.detail}</span>}
              </button>
              {expanded && (
                <div className="mt-2 rounded-lg bg-surface-overlay/60 px-3 py-2 text-sm leading-relaxed">
                  {item.why && (
                    <p>
                      <span className="font-semibold">Why: </span>
                      {item.why}
                    </p>
                  )}
                  {item.link && (
                    <a href={item.link} target="_blank" rel="noreferrer" className="mt-1 inline-block text-accent">
                      Open in your mailbox
                    </a>
                  )}
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
