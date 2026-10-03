"use client";

import Link from "next/link";

import type { Hub } from "@/components/shell/navConfig";
import { isNavActive } from "@/components/shell/navConfig";

// The tab row at the top of every page in a hub (Work, Company / You), so
// the hub's pages read as one place. The shell renders it; pages don't.
export default function HubTabs({ hub, pathname }: { hub: Hub; pathname: string }) {
  return (
    <nav
      aria-label={hub.label}
      className="flex-shrink-0 border-b border-line px-4 sm:px-6 overflow-x-auto"
    >
      <div className="flex gap-1 py-2">
        {hub.tabs.map((tab) => {
          const active = isNavActive(tab.href, pathname);
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
