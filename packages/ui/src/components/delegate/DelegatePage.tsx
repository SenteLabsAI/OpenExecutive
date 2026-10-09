import type { ReactNode } from "react";

import FeatureName, { type Feature } from "@/components/FeatureName";

// The frame of each Delegate tab: the feature's brand label as the title, a
// line under it, and the page's cards. The hub's tab row (rendered by the
// shell) is the way between them, so there's no back link.
export default function DelegatePage({
  feature,
  description,
  children,
}: {
  feature: Feature;
  description: ReactNode;
  children: ReactNode;
}) {
  return (
    <main className="flex-1 min-h-0 overflow-y-auto">
      <div className="max-w-3xl mx-auto px-4 sm:px-6 pt-6 sm:pt-8 pb-16">
        <h1 className="text-2xl sm:text-3xl tracking-tight">
          <FeatureName feature={feature} />
        </h1>
        <p className="mt-3 text-[15px] text-fg-muted leading-relaxed">{description}</p>
        <div className="mt-6 space-y-5">{children}</div>
      </div>
    </main>
  );
}
