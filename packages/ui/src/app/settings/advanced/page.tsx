"use client";

import Link from "next/link";

import Icon from "@/components/Icon";
import SettingsSubpage from "@/components/settings/SettingsSubpage";
import { advancedItemsByGroup } from "@/components/shell/navConfig";

// Settings → Advanced: the admin and power-user pages (ADVANCED_ITEMS),
// grouped by what you'd use them for. Each opens its own screen.
export default function AdvancedSettingsPage() {
  return (
    <SettingsSubpage
      title="Advanced"
      description="Diagnostics, configuration and reference pages. Each opens its own screen."
    >
      {advancedItemsByGroup().map((group) => (
        <section key={group.key} aria-labelledby={`tools-${group.key}`}>
          <h2
            id={`tools-${group.key}`}
            className="px-1 text-sm font-semibold uppercase tracking-wider text-fg-subtle"
          >
            {group.label}
          </h2>
          <ul className="mt-2 overflow-hidden rounded-2xl border border-line bg-surface-elevated divide-y divide-line">
            {group.items.map((item) => (
              <li key={item.href}>
                <Link
                  href={item.href}
                  className="group flex items-center gap-4 px-5 py-4 transition-colors hover:bg-surface-overlay/60"
                >
                  <span className="flex w-10 h-10 flex-shrink-0 items-center justify-center rounded-xl bg-accent/10 text-accent">
                    <Icon name={item.icon} size="w-5 h-5" />
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block text-base font-semibold text-fg">{item.label}</span>
                    <span className="block text-sm text-fg-muted leading-relaxed">
                      {item.description}
                    </span>
                  </span>
                  <Icon
                    name="chevron-right"
                    size="w-5 h-5"
                    className="text-fg-subtle group-hover:text-fg transition-colors"
                  />
                </Link>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </SettingsSubpage>
  );
}
