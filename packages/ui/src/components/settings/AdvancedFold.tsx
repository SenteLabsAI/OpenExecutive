"use client";

import { useState, type ReactNode } from "react";

import Icon from "@/components/Icon";

// The fold at the foot of a Settings page that holds its rarer settings.
// Closed by default; `summary` says what is inside without opening it.
export default function AdvancedFold({
  id,
  summary,
  children,
}: {
  id: string;
  summary: string;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-controls={id}
        className="flex w-full min-h-[3rem] items-center gap-3 rounded-2xl border border-line bg-surface-elevated px-5 py-3 text-left transition-colors hover:bg-surface-overlay cursor-pointer"
      >
        <span className="min-w-0 flex-1">
          <span className="block text-base font-semibold text-fg">Advanced</span>
          <span className="block text-sm text-fg-muted truncate">{summary}</span>
        </span>
        <Icon
          name="chevron-right"
          size="w-5 h-5"
          className={`text-fg-muted transition-transform ${open ? "rotate-90" : ""}`}
        />
      </button>
      {open && (
        <div id={id} className="mt-4 space-y-5">
          {children}
        </div>
      )}
    </div>
  );
}
