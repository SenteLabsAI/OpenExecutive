"use client";

import Link from "next/link";
import { useRef } from "react";

import FeatureName from "@/components/FeatureName";

import type { Hub } from "@/components/shell/navConfig";
import { isNavActive } from "@/components/shell/navConfig";
import { useScrollFade } from "@/components/ui/useScrollFade";

// The tab row at the top of every page in a hub (Work, Company / You), so
// the hub's pages read as one place. The shell renders it; pages don't.
// A tab for a named feature (Delegate's) shows its brand label: in full on
// the open tab, muted on the others. On a phone the row scrolls sideways
// and fades at an edge with more tabs past it.
export default function HubTabs({ hub, pathname }: { hub: Hub; pathname: string }) {
  const rowRef = useRef<HTMLDivElement>(null);
  const fade = useScrollFade(rowRef);
  return (
    <nav aria-label={hub.label} className="flex-shrink-0 border-b border-line">
      <div
        ref={rowRef}
        onScroll={fade.onScroll}
        style={fade.style}
        className="flex gap-1 overflow-x-auto px-4 py-2 sm:px-6"
      >
        {hub.tabs.map((tab) => {
          const active = isNavActive(tab.href, pathname);
          if (tab.feature) {
            return (
              <Link
                key={tab.href}
                href={tab.href}
                title={tab.description}
                aria-current={active ? "page" : undefined}
                className={`flex flex-shrink-0 items-center rounded-xl px-1 sm:px-2 py-1.5 text-[13px] sm:text-[15px] transition-opacity ${
                  active ? "" : "opacity-60 grayscale hover:opacity-100 hover:grayscale-0"
                }`}
              >
                <FeatureName feature={tab.feature} className={active ? "" : "!bg-transparent"} />
              </Link>
            );
          }
          return (
            <Link
              key={tab.href}
              href={tab.href}
              title={tab.description}
              aria-current={active ? "page" : undefined}
              className={`flex-shrink-0 rounded-xl px-4 py-2 text-[15px] font-medium transition-colors ${
                active
                  ? "bg-accent/10 text-accent"
                  : "text-fg-muted hover:text-fg hover:bg-surface-overlay"
              }`}
            >
              {tab.label}
            </Link>
          );
        })}
      </div>
    </nav>
  );
}
