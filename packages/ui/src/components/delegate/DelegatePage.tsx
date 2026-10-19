"use client";

import { useCallback, useState, type ReactNode } from "react";

import FeatureName, { type Feature } from "@/components/FeatureName";
import { WhatItDoesAside, WhatItDoesSheet, useWhatItDoesOpen } from "@/components/delegate/WhatItDoesPanel";

// The frame of each Delegate tab: the feature's brand label as the title, a
// line under it, and the page's cards, with What it does for you beside them
// (to the right on a wide screen, a sheet on a narrow one). The hub's tab row
// (rendered by the shell) is the way between them, so there's no back link.
export default function DelegatePage({
  feature,
  description,
  children,
}: {
  feature: Feature;
  description: ReactNode;
  children: ReactNode;
}) {
  const [open, setOpen] = useWhatItDoesOpen();
  const [sheet, setSheet] = useState(false);
  const closeSheet = useCallback(() => setSheet(false), []);
  const button =
    "flex-none rounded-lg border border-line px-3 py-1.5 text-sm font-medium text-fg hover:bg-surface-overlay";
  return (
    <div className="flex flex-1 min-h-0">
      <main className="flex-1 min-w-0 min-h-0 overflow-y-auto">
        <div className="max-w-3xl mx-auto px-4 sm:px-6 pt-6 sm:pt-8 pb-16">
          <div className="flex items-start justify-between gap-3">
            <h1 className="text-2xl sm:text-3xl tracking-tight">
              <FeatureName feature={feature} />
            </h1>
            <button type="button" onClick={() => setSheet(true)} className={`${button} mt-1 lg:hidden`}>
              What it does
            </button>
            {!open && (
              <button type="button" onClick={() => setOpen(true)} className={`${button} mt-1 hidden lg:block`}>
                What it does
              </button>
            )}
          </div>
          <p className="mt-3 text-[15px] text-fg-muted leading-relaxed">{description}</p>
          <div className="mt-6 space-y-5">{children}</div>
        </div>
      </main>
      {open && <WhatItDoesAside onClose={() => setOpen(false)} />}
      {sheet && <WhatItDoesSheet onClose={closeSheet} />}
    </div>
  );
}
